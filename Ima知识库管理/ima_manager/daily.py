"""Compact, append-only daily file journal in the sync source root."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
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


def record(source, operation, filename, result, *, version='', old_version='', target='', detail='', at=None):
    root = Path(source).resolve()
    path = ensure_journal(root)
    folder = root / '.ima-state'
    if folder.is_symlink() or not folder.resolve().is_relative_to(root):
        raise OSError('每日记录索引位置异常')
    folder.mkdir(exist_ok=True)
    at = (at or datetime.now(BEIJING)).astimezone(BEIJING)
    clean = lambda value: str(value).replace('\r', ' ').replace('\n', ' ').replace('|', '／')
    payload = dict(operation=operation, file=filename, result=result, version=version,
                   old_version=old_version, target=target, detail=detail)
    identity = hashlib.sha256(json.dumps([at.date().isoformat(), operation, filename, target], ensure_ascii=False).encode()).hexdigest()
    signature = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    db = sqlite3.connect(folder / 'journal.sqlite3', timeout=30)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS last_events (id TEXT PRIMARY KEY, signature TEXT)')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT signature FROM last_events WHERE id=?', (identity,)).fetchone()
        if previous and previous[0] == signature:
            return False
        parts = [at.strftime('%Y-%m-%d %H:%M:%S'), clean(operation), clean(filename), clean(result)]
        if version:
            parts.append('版本=' + ((old_version[:12] + '→') if old_version else '') + version[:12])
        if target:
            parts.append('目标=' + clean(target))
        if detail:
            parts.append(clean(detail))
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(' | '.join(parts) + '\n')
            stream.flush()
        db.execute('INSERT OR REPLACE INTO last_events VALUES (?,?)', (identity, signature))
        db.commit()
        return True
    finally:
        db.close()


def record_collection(source, report):
    if report.get('dry_run'):
        return
    ensure_journal(source)
    labels = {'copied': ('提取新增', '成功'), 'updated': ('提取更新', '成功'),
              'error': ('提取', '失败'), 'conflict': ('提取', '冲突')}
    for item in report.get('files', []):
        if item.get('status') not in labels:
            continue
        operation, result = labels[item['status']]
        destination = item.get('destination', '')
        try:
            filename = Path(destination).relative_to(Path(source).resolve()).as_posix() if destination else item['name']
        except ValueError:
            filename = item['name']
        record(source, operation, filename, result, version=item.get('sha256', ''),
               old_version=item.get('old_sha256', ''), detail=item.get('reason', ''))
