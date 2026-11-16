"""Compact, append-only daily file journal in the sync source root."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from itertools import islice
import os
from pathlib import Path
import sqlite3

JOURNAL_NAME = '每日更新记录.txt'
BEIJING = timezone(timedelta(hours=8))


def ensure_journal(source):
    root = Path(source).resolve()
    root.mkdir(parents=True, exist_ok=True)
    path = root / JOURNAL_NAME
    if path.is_symlink():
        raise OSError('每日更新记录不能是链接')
    try:
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write('ima 每日文件更新记录（北京时间）\n格式：时间 | 操作 | 文件 | 结果与版本\n')
    except FileExistsError:
        pass
    return path


def _event(operation, filename, result, *, version='', old_version='', target='', detail='', at=None):
    at = (at or datetime.now(BEIJING)).astimezone(BEIJING)
    clean = lambda value: str(value).replace('\r', ' ').replace('\n', ' ').replace('|', '／')
    payload = dict(operation=operation, file=filename, result=result, version=version,
                   old_version=old_version, target=target, detail=detail)
    identity = hashlib.sha256(json.dumps([at.date().isoformat(), operation, filename, target], ensure_ascii=False).encode()).hexdigest()
    signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    parts = [at.strftime('%Y-%m-%d %H:%M:%S'), clean(operation), clean(filename), clean(result)]
    if version:
        parts.append('版本=' + ((old_version[:12] + '→') if old_version else '') + version[:12])
    if target:
        parts.append('目标=' + clean(target))
    if detail:
        parts.append(clean(detail))
    return identity, signature, (' | '.join(parts) + '\n').encode('utf-8')


def _flush_pending(db, stream):
    """Replay committed bytes at their recorded offset, including partial writes."""
    pending = db.execute('SELECT offset,payload FROM pending_journal WHERE id=1').fetchone()
    if pending is None:
        return
    offset, payload = pending
    stream.seek(0, os.SEEK_END)
    available = stream.tell() - offset
    if available < 0:
        raise OSError('每日记录已被截断，未覆盖现有文件')
    stream.seek(offset)
    existing = stream.read(min(available, len(payload)))
    if existing != payload[:len(existing)]:
        raise OSError('每日记录待恢复位置已被修改，未覆盖现有文件')
    if len(existing) < len(payload):
        stream.seek(offset + len(existing))
        stream.write(payload[len(existing):])
    stream.flush()
    os.fsync(stream.fileno())
    db.execute('DELETE FROM pending_journal WHERE id=1')


def record_batch(source, events, *, batch_size=128):
    """Reuse handles and commit bounded batches; a durable outbox protects TXT."""
    if batch_size < 1 or batch_size > 256:
        raise ValueError('每日记录批次必须为 1～256 条')
    root = Path(source).resolve()
    path = ensure_journal(root)
    folder = root / '.ima-state'
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise OSError('每日记录索引位置异常')
    folder.mkdir(exist_ok=True)
    db = sqlite3.connect(folder / 'journal.sqlite3', timeout=30)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS last_events (id TEXT PRIMARY KEY, signature TEXT)')
        db.execute('CREATE TABLE IF NOT EXISTS pending_journal (id INTEGER PRIMARY KEY CHECK(id=1), offset INTEGER, payload BLOB)')
        count, iterator = 0, iter(events)
        with path.open('r+b') as stream:
            with db:
                db.execute('BEGIN IMMEDIATE')
                _flush_pending(db, stream)
            while batch := list(islice(iterator, batch_size)):
                prepared = [_event(**event) for event in batch]
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    _flush_pending(db, stream)
                    ids = list(dict.fromkeys(item[0] for item in prepared))
                    placeholders = ','.join('?' for _ in ids)
                    signatures = dict(db.execute(f'SELECT id,signature FROM last_events WHERE id IN ({placeholders})', ids))
                    lines = []
                    for identity, signature, line in prepared:
                        if signatures.get(identity) != signature:
                            lines.append(line)
                            signatures[identity] = signature
                    if lines:
                        stream.seek(0, os.SEEK_END)
                        db.execute('INSERT INTO pending_journal VALUES (1,?,?)', (stream.tell(), b''.join(lines)))
                        db.executemany('INSERT OR REPLACE INTO last_events VALUES (?,?)', signatures.items())
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    _flush_pending(db, stream)
                count += len(lines)
        return count
    finally:
        db.close()


def record(source, operation, filename, result, **kwargs):
    return bool(record_batch(source, [dict(operation=operation, filename=filename, result=result, **kwargs)]))


def record_collection(source, report):
    if report.get('dry_run'):
        return
    root = Path(source).resolve()
    labels = {'copied': ('提取新增', '成功'), 'updated': ('提取更新', '成功'),
              'error': ('提取', '失败'), 'conflict': ('提取', '冲突')}
    def events():
        for item in report.get('files', []):
            if item.get('status') not in labels:
                continue
            operation, result = labels[item['status']]
            destination = item.get('destination', '')
            try:
                filename = Path(destination).relative_to(root).as_posix() if destination else item['name']
            except ValueError:
                filename = item['name']
            yield dict(operation=operation, filename=filename, result=result, version=item.get('sha256', ''),
                       old_version=item.get('old_sha256', ''), detail=item.get('reason', ''))
    record_batch(root, events())
