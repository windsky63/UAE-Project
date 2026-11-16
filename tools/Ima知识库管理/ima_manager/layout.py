"""Recoverable local layout migration, completed before collection copies bytes."""
import hashlib
from collections import defaultdict
import json
import os
from pathlib import Path

from .api import ImaError
from .config import atomic_json
from . import collection_rules as rules


def safe(root, path, *, purpose='目录迁移'):
    root, path = Path(root).absolute(), Path(path).absolute()
    if not path.is_relative_to(root) or not path.resolve().is_relative_to(root.resolve()):
        raise ImaError(f'{purpose}路径越界：允许目录={root}；实际路径={path}')
    for node in (path, *path.parents):
        if node == root.parent:
            break
        if node.is_symlink() or getattr(node, 'is_junction', lambda: False)():
            raise ImaError(f'{purpose}路径含链接：{node}')
    return path


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def destination(root, path):
    """Classify existing copies by their own number, never by an obsolete folder."""
    path = Path(path)
    result = rules.sync_module('business_type').classify(filename=path.name)
    row = {'文件名': path.name, '业务类型': result['business_type'],
           '业务专业': result['discipline']}
    if not rules.selected(row):
        return None
    category = rules.classify(row)
    if category == '99_待核对':
        return None
    return Path(root) / rules.relative_destination(row, category, path.name)


def manifest_path(source):
    return safe(source, Path(source) / '.ima-state/layout-migrations.json')


def read_moves(source):
    path = manifest_path(source)
    try:
        value = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
        if not isinstance(value, list):
            raise ValueError()
        for entry in value:
            safe(source, entry['old'])
            safe(source, entry['new'])
        return value
    except (OSError, ValueError, KeyError, TypeError):
        raise ImaError('目录迁移记录损坏，请核对 .ima-state/layout-migrations.json') from None


def migrate_local(source, runtime, states, *, dry_run=False, stop=None, notify=lambda message: None):
    source, runtime = Path(source), Path(runtime)
    first_run = not manifest_path(source).exists()
    moves = read_moves(source)
    # Earlier releases already moved local copies to recoverable backups without
    # moving cloud identities. Carry those exact old/new mappings forward.
    legacy = runtime / 'drawing-layout-backups'
    if first_run and legacy.is_dir():
        safe(runtime, legacy)
        for record in sorted(legacy.glob('*/origin.json')):
            safe(runtime, record)
            try:
                item = json.loads(record.read_text(encoding='utf-8'))
                old, new = Path(item['original_path']), Path(item['destination'])
                if not old.is_relative_to(source) or not new.is_relative_to(source):
                    continue
                safe(source, old)
                safe(source, new)
                if old != new and not old.exists() and new.is_file():
                    moves.append({'old': str(old), 'new': str(new), 'sha256': item['sha256'], 'status': 'done'})
            except (ValueError, KeyError, TypeError):
                raise ImaError('旧目录迁移记录损坏：' + str(record)) from None
        if not dry_run:
            atomic_json(manifest_path(source), moves)
    changes = []
    roots = set(rules.COLLECTION_ROOTS) | {'其他图纸'}
    paths = []
    if source.is_dir():
        for current, directories, names in os.walk(source, followlinks=False):
            current = Path(current)
            directories[:] = sorted(d for d in directories if
                (current != source or d in roots) and not d.startswith('.'))
            for name in directories:
                safe(source, current / name)
            if current != source:
                paths.extend(current / name for name in sorted(names) if not name.startswith(('.', '~$')))
    # Recover a move interrupted between its durable intent and the rename.
    pending = [entry for entry in moves if entry.get('status') != 'done']
    pending_old = {entry['old'] for entry in pending}
    reserved = {e['new']: e['sha256'] for e in pending}
    hashes = {}
    def checked_digest(path):
        stat = path.stat()
        stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = hashes.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        value = digest(path)
        after = path.stat()
        if stamp != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ImaError('迁移文件在校验期间变化，尚未复制新文件')
        hashes[str(path)] = (stamp, value)
        return value
    for path in paths:
        safe(source, path)
        target = destination(source, path)
        if target is None or target == path or str(path) in pending_old:
            continue
        sha = checked_digest(path)
        base, number = target, 0
        while ((target.exists() and checked_digest(safe(source, target)) != sha)
               or (str(target) in reserved and reserved[str(target)] != sha)):
            number += 1
            target = base.with_name(f'{base.stem}-{number}{base.suffix}')
        entry = {'old': str(path), 'new': str(target), 'sha256': sha, 'status': 'pending'}
        pending.append(entry)
        pending_old.add(str(path))
        reserved[str(target)] = sha
        if not dry_run:
            moves.append(entry)
    if pending and not dry_run:
        # Commit every intent before the first rename. An interrupted pass can
        # verify and finish already moved entries without rewriting JSON per file.
        atomic_json(manifest_path(source), moves)
    for entry in pending:
        if stop is not None and stop.is_set():
            raise ImaError('已安全停止目录迁移，尚未复制新文件')
        old, new = safe(source, entry['old']), safe(source, entry['new'])
        sha = entry['sha256']
        if dry_run:
            changes.append(dict(entry, status='would_migrate'))
            continue
        if old.exists():
            if checked_digest(old) != sha:
                raise ImaError('迁移源文件已变化，尚未复制新文件：' + str(old))
            new.parent.mkdir(parents=True, exist_ok=True)
            safe(source, new)
            if new.exists():
                if checked_digest(new) != sha:
                    raise ImaError('迁移目标已被其他内容占用：' + str(new))
                # Keep redundant bytes outside the sync tree, with the durable origin record.
                backup = runtime / 'layout-duplicates' / hashlib.sha256(str(old).encode()).hexdigest() / old.name
                safe(runtime, backup)
                backup.parent.mkdir(parents=True, exist_ok=True)
                if backup.exists():
                    raise ImaError('迁移备份已存在，未覆盖：' + str(backup))
                old.rename(backup)
                entry['backup'] = str(backup)
            else:
                if os.name == 'nt':
                    old.rename(new)
                else:
                    os.link(old, new)
                    old.unlink()
        if not new.is_file() or checked_digest(new) != sha:
            raise ImaError('目录迁移结果未确认，尚未复制新文件：' + str(new))
        entry['status'] = 'done'
        changes.append(dict(entry))
        notify('本地目录迁移：' + str(old.relative_to(source)) + ' → ' + str(new.relative_to(source)))
    if pending and not dry_run:
        atomic_json(manifest_path(source), moves)
    if not dry_run:
        # Apply completed mappings even after a crash before collection-state checkpoint.
        owners = defaultdict(list)
        for state in states.values():
            owners[(state.get('destination'), state.get('sha256'))].append(state)
        for entry in moves:
            if entry.get('status') != 'done':
                continue
            for state in owners.pop((entry['old'], entry['sha256']), []):
                original_name = Path(state.get('base_destination') or entry['old']).name
                base = destination(source, Path(original_name)) or Path(entry['new'])
                state.update(destination=entry['new'], base_destination=str(base))
                actual = Path(entry['new'])
                tag = actual.stem[len(base.stem):] if actual.stem.startswith(base.stem + '-') else ''
                state['identity_tag'] = tag
                if Path(entry['new']).is_file():
                    stat = Path(entry['new']).stat()
                    state['destination_stamp'] = [stat.st_size, stat.st_mtime_ns]
                owners[(entry['new'], entry['sha256'])].append(state)
        # Only empty managed directories are removed; no recursive deletion.
        for current, directories, names in (os.walk(source, topdown=False, followlinks=False) if source.is_dir() else []):
            path = Path(current)
            if path != source and path.relative_to(source).parts[0] in roots:
                safe(source, path)
                if not any(path.iterdir()):
                    path.rmdir()
    return changes
