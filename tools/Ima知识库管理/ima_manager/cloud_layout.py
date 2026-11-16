"""Move receipted cloud identities before allowing new uploads; resume lost responses."""
from pathlib import Path
from collections import defaultdict
import hashlib
import json
import sqlite3
import time

from .api import ImaError
from .layout import destination, read_moves, safe
from .collection_rules import COLLECTION_ROOTS
from .scope import MOVE_BATCH_SIZE, move_file, move_files, rename_file, require_directory
from .tree import CloudTree


def discover_unreceipted(engine, source, kb, folder, rows, notify):
    """Read old managed cloud trees once; never create receipts in a preview."""
    from .api import folder_id
    root = Path(source).resolve()
    roots = set(COLLECTION_ROOTS) | {'其他图纸'}
    local = defaultdict(list)
    for row in rows:
        if row.get('action') == 'delete':
            continue
        parts = Path(row['path']).relative_to(root).parts
        if parts and parts[0] in roots:
            local[row['name']].append(row)
    candidates = [row for group in local.values() for row in group if row['status'] == 'ready']
    if not candidates:
        return
    notify(f'旧云端目录核对：待查本地文件={len(candidates)}；扫描所选目标内的资料目录')
    wanted = {row['name'] for row in candidates}
    involved_roots = {Path(row['path']).relative_to(root).parts[0] for row in candidates}
    found = defaultdict(list)
    base = '' if folder == kb else folder
    pending, visited = [(base, ())], set()
    last = time.monotonic()
    while pending:
        parent, parts = pending.pop()
        if parent in visited:
            raise ImaError('旧云端目录存在重复身份或循环，请核对后重新预览')
        visited.add(parent)
        if len(visited) > 10000:
            raise ImaError('旧云端目录数量异常，停止预览')
        for item in engine.client.listing(kb, parent):
            name = item.get('title') or item.get('name') or (item.get('folder_info') or {}).get('name')
            fid = folder_id(item)
            if fid:
                if (not isinstance(name, str) or name.startswith('.') or
                        name in {'删除', '更新暂存'} or '/' in name or '\\' in name):
                    continue
                if not base and not parts and name not in involved_roots | {root.name}:
                    continue
                if parts == (root.name,) and name not in involved_roots:
                    continue
                pending.append((fid, (*parts, name)))
            elif name in wanted and item.get('media_id'):
                found[name].append((parent, item['media_id']))
        if time.monotonic() - last >= 15:
            notify(f'旧云端目录核对进度：已读目录={len(visited)}；剩余目录={len(pending)}')
            last = time.monotonic()
    with engine.store.connect() as db:
        busy = {r[0] for r in db.execute(
            "SELECT path FROM receipts WHERE account=? AND kb=? AND status IN ('adding','uncertain','archiving')",
            (engine.account, kb))}
        busy.update(r[0] for r in db.execute('SELECT path,payload FROM update_jobs WHERE account=? AND kb=?',
                    (engine.account, kb)) if json.loads(r[1]).get('phase') != 'done')
    count = 0
    for row in candidates:
        matches = found[row['name']]
        if not matches:
            continue
        if row['path'] in busy:
            row.update(status='uncertain', reason='此本地路径有待核对或未完成更新，不能按文件名认领迁移')
        elif len(local[row['name']]) != 1 or len(matches) != 1:
            row.update(status='conflict', reason='旧云端文件或本地同名文件不唯一，不能确定迁移对象；不会按新增上传')
        else:
            old_folder, media = matches[0]
            if old_folder == row.get('target_folder_id'):
                row.update(status='cloud_existing', media_id=media, reason='目标目录已有同名文件，正式同步时补建认领回执')
                continue
            row.update(status='ready_migrate', migration_unreceipted=True,
                       migration_old_path=row['path'], migration_old_folder=old_folder,
                       migration_media_id=media, migration_sha256=row['sha256'], migration_count=1,
                       reason='旧云端目录有唯一同名文件；按当前本地内容认领并迁移，不重复上传')
            count += 1
    notify(f'旧云端目录核对完成：目录={len(visited)}；无回执待迁移={count}')


def prepare_unreceipted(engine, source, kb, folder, rows, stop):
    """Validate reviewed identities, then persist intents for the normal mover."""
    from .scope import live_listing
    from .api import folder_id
    from .sync import fingerprint
    root = Path(source).resolve()
    base = require_directory(engine.client, root, kb, folder)
    selected = [row for row in rows if row.get('sync_root') == str(root)
                and row['status'] == 'ready_migrate' and row.get('migration_unreceipted')]
    if not selected:
        return
    listings = {}
    for row in selected:
        if stop.is_set():
            raise ImaError('已安全停止无回执云端迁移')
        path = safe(root, Path(row['path']))
        if not path.is_file():
            raise ImaError('认领迁移的本地文件已不存在，请重新预览')
        with path.open('rb') as stream:
            if fingerprint(stream) != row['sha256']:
                raise ImaError('认领迁移的本地内容已变化，请重新预览')
        parent = row['migration_old_folder']
        if parent not in listings:
            listings[parent] = live_listing(engine.client, kb, parent)
        named = [item for item in listings[parent] if (item.get('title') or item.get('name')) == row['name']]
        if (len(named) != 1 or folder_id(named[0]) or
                named[0].get('media_id') != row['migration_media_id']):
            raise ImaError('旧云端同名文件身份已变化，停止迁移及上传，请重新预览')
    tree = CloudTree(engine.client, kb, folder, create=engine._directory)
    with engine.store.connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS layout_moves (
            account TEXT,kb TEXT,old_folder TEXT,old_path TEXT,sha256 TEXT,media TEXT,
            new_folder TEXT,new_path TEXT,phase TEXT,
            PRIMARY KEY(account,kb,old_folder,old_path,sha256))''')
    for row in selected:
        if stop.is_set():
            raise ImaError('已安全停止无回执云端迁移')
        path = Path(row['path'])
        target = tree.parent(path.relative_to(base).parent.parts)
        if row.get('target_folder_id') is not None and row['target_folder_id'] != target:
            raise ImaError('认领迁移目标目录身份已变化，请重新预览')
        key = (engine.account, kb, row['migration_old_folder'], str(path), row['sha256'])
        media = row['migration_media_id']
        with engine.store.connect() as db:
            pending = db.execute("SELECT 1 FROM receipts WHERE account=? AND kb=? AND path=? AND status IN ('adding','uncertain','archiving')",
                                 (engine.account, kb, str(path))).fetchone()
            if pending:
                raise ImaError('认领迁移出现待核对回执，请重新预览')
            saved = db.execute('SELECT media_id FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?', key).fetchone()
            if saved and saved[0] != media:
                raise ImaError('认领迁移回执已被其他媒体占用，请重新预览')
            db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?)',
                       (*key, 'done', media, '旧云端目录同名认领，待迁移', time.time()))
            db.execute('INSERT OR IGNORE INTO layout_moves VALUES (?,?,?,?,?,?,?,?,?)',
                       (*key, media, target, str(path), 'pending'))


def migrate_cloud(engine, source, kb, folder, stop, notify, *, preview=False, selected_paths=None):
    source = Path(source).resolve()
    local_base = require_directory(engine.client, source, kb, folder)
    moves = read_moves(source)
    if any(entry.get('status') != 'done' for entry in moves):
        raise ImaError('本地目录迁移尚未完成，请先完成提取；尚未上传新文件')
    moves_by_old = defaultdict(list)
    for entry in moves:
        moves_by_old[entry['old']].append(entry)
    with engine.store.connect() as db:
        if not preview:
            db.execute('''CREATE TABLE IF NOT EXISTS layout_moves (
            account TEXT,kb TEXT,old_folder TEXT,old_path TEXT,sha256 TEXT,media TEXT,
            new_folder TEXT,new_path TEXT,phase TEXT,
            PRIMARY KEY(account,kb,old_folder,old_path,sha256))''')
        db.row_factory = sqlite3.Row
        receipts = [dict(r) for r in db.execute(
            "SELECT * FROM receipts WHERE account=? AND kb=? AND status='done' ORDER BY updated",
            (engine.account, kb))]
        has_jobs = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='layout_moves'").fetchone()
        jobs = {(r['old_folder'], r['old_path'], r['sha256']): dict(r) for r in db.execute(
            'SELECT * FROM layout_moves WHERE account=? AND kb=?', (engine.account, kb))} if has_jobs else {}
    tree = CloudTree(engine.client, kb, folder, create=None if preview else engine._directory)
    old_tree = CloudTree(engine.client, kb, folder)
    migrated = [] if preview else 0
    pending = []
    preview_media = {}
    def preview_contains(folder_id, media):
        if folder_id is None:
            return False
        if folder_id not in preview_media:
            preview_media[folder_id] = {item.get('media_id') for item in engine.client.listing(kb, folder_id)}
        return media in preview_media[folder_id]
    roots = set(COLLECTION_ROOTS) | {'其他图纸'}
    last_progress = time.monotonic()
    if preview:
        notify(f'迁移预览核对开始：成功上传回执={len(receipts)}；只读核对旧云端媒体')
    for receipt_index, receipt in enumerate(receipts, 1):
        if preview and time.monotonic() - last_progress >= 15:
            notify(f'迁移回执核对进度：已检查={receipt_index-1}/{len(receipts)}；当前旧路径={receipt["path"]}')
            last_progress = time.monotonic()
        if stop.is_set():
            raise ImaError('已安全停止云端目录迁移，尚未上传新文件')
        old = Path(receipt['path'])
        if not old.is_absolute() or not old.is_relative_to(source):
            continue
        relative = old.relative_to(source)
        if not relative.parts or relative.parts[0] not in roots:
            continue
        safe(source, old)
        if not old.is_relative_to(local_base):
            continue
        key = (engine.account, kb, receipt['folder'], str(old), receipt['sha256'])
        job = jobs.get((receipt['folder'], str(old), receipt['sha256']))
        if job:
            new = safe(source, Path(job['new_path']))
            if selected_paths is not None and str(new) not in selected_paths:
                continue
        else:
            new = old
            visited = set()
            while str(new) not in visited:
                visited.add(str(new))
                matches = moves_by_old.get(str(new), [])
                exact = [e for e in matches if e['sha256'] == receipt['sha256']]
                targets = {e['new'] for e in (exact or matches)}
                if not targets:
                    break
                if len(targets) != 1:
                    raise ImaError('旧路径对应多个迁移目标，尚未上传新文件：' + str(new))
                new = Path(targets.pop())
            else:
                raise ImaError('目录迁移记录包含循环，尚未上传新文件')
            if new == old:
                new = destination(source, old)
            if new is None or not new.is_relative_to(local_base):
                continue
            if selected_paths is not None and str(new) not in selected_paths:
                continue
            safe(source, new)
            if not new.is_file():
                # Never invent an upload source or relocate cloud-only/manual files.
                continue
            if (not (preview and local_base == source) and old_tree.parent(old.relative_to(local_base).parent.parts) != receipt['folder']
                    and receipt['folder'] != ('' if folder == kb else folder)):
                # A receipt for a different selected cloud root is not ours to move.
                continue
            # A local update may already have replaced these bytes. The old cloud
            # version still moves first, then the ordinary version-update flow runs.
            # For a known path change, preview only needs the receipted old
            # identity. New folders are resolved and verified during execution.
            target_folder = None if preview and new != old else tree.parent(new.relative_to(local_base).parent.parts)
            if new == old and target_folder == receipt['folder']:
                continue
            with engine.store.connect() as db:
                unresolved = db.execute("SELECT 1 FROM receipts WHERE account=? AND kb=? AND path=? AND status IN ('adding','uncertain','archiving')",
                                        (engine.account, kb, str(old))).fetchone()
                updates = db.execute('SELECT payload FROM update_jobs WHERE account=? AND kb=? AND path=?',
                                     (engine.account, kb, str(old))).fetchall()
            if unresolved or any(json.loads(r[0]).get('phase') != 'done' for r in updates):
                raise ImaError('旧路径有待核对或未完成的更新，暂停迁移及新增上传：' + str(old))
            job = dict(new_folder=target_folder, new_path=str(new), media=receipt['media_id'])
            if not preview:
                with engine.store.connect() as db:
                    db.execute('INSERT INTO layout_moves VALUES (?,?,?,?,?,?,?,?,?)',
                               (*key, job['media'], job['new_folder'], job['new_path'], 'pending'))
        if preview:
            # Preview uses the execution candidate rules without creating folders,
            # moving media, or changing receipts. Verify the old identity exists.
            media = job['media']
            # A rejected/interrupted batch usually leaves media in its old parent.
            # Read that shared parent first; avoid thousands of empty target reads.
            present = preview_contains(receipt['folder'], media) or preview_contains(job['new_folder'], media)
            status = 'ready_migrate' if present else 'blocked'
            reason = '先迁移已有云端文件，再核对内容是否需要更新' if present else '旧上传回执对应的云端媒体未找到，停止迁移；不按新增上传'
            migrated.append(dict(path=str(new), status=status, reason=reason, migration_old_path=str(old),
                                 migration_old_folder=receipt['folder'], migration_media_id=media,
                                 migration_sha256=receipt['sha256']))
            continue
        pending.append((receipt, old, new, job, key, relative))
    if preview:
        notify(f'迁移预览核对完成：回执={len(receipts)}；迁移候选={len(migrated)}')
        return migrated

    # Every intent is durable before dispatching a batch. Interrupted/partial
    # batches are reconciled from their media IDs on the next execution.
    _batch_moves(engine.client, kb, pending, stop, notify)
    for receipt, old, new, job, key, relative in pending:
        if stop.is_set():
            raise ImaError('已安全停止云端目录迁移，尚未上传新文件')
        if not new.is_file():
            raise ImaError('云端迁移对应的本地文件已不存在：' + str(new))
        target_folder, media = job['new_folder'], job['media']
        current = engine.client.listing(kb, target_folder)
        others = [r for r in current if r.get('media_id') != media and
                  (r.get('title') or r.get('name', '')).casefold() == new.name.casefold()]
        if others:
            with engine.store.connect() as db:
                known = db.execute("SELECT media_id FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=? AND status='done'",
                    (engine.account, kb, target_folder, str(new), receipt['sha256'])).fetchone()
            if len(others) != 1 or not known or known[0] != others[0].get('media_id'):
                raise ImaError('云端迁移目标已有其他同名内容，尚未上传新文件：' + str(new))
            # Two successful old receipts can refer to identical local bytes now
            # merged into one path. Keep one normal identity, archive the duplicate.
            deleted = engine._directory(kb, folder, '删除')
            duplicates = engine._directory(kb, deleted, '目录迁移重复')
            target_folder = engine._directory(kb, duplicates, hashlib.sha256(media.encode()).hexdigest()[:24])
            job.update(new_folder=target_folder, phase='duplicate_pending')
            with engine.store.connect() as db:
                db.execute("UPDATE layout_moves SET new_folder=?,phase='duplicate_pending' WHERE account=? AND kb=? AND old_folder=? AND old_path=? AND sha256=?",
                           (target_folder, *key))
            current = engine.client.listing(kb, target_folder)
        duplicate = job.get('phase') == 'duplicate_pending'
        desired_name = old.name if duplicate else new.name
        present = next((r for r in current if r.get('media_id') == media), None)
        if present is None:
            previous = next((r for r in engine.client.listing(kb, receipt['folder']) if r.get('media_id') == media), None)
            if previous is None or (previous.get('title') or previous.get('name')) not in {old.name, desired_name}:
                raise ImaError('云端迁移源身份或名称已变化，尚未上传新文件')
            previous_title = previous.get('title') or previous.get('name')
            # Rename in the source first: the target may already contain the
            # original basename whose different bytes required a numeric suffix.
            if previous_title != desired_name:
                rename_file(engine.client, kb, receipt['folder'], media, desired_name, expected_title=previous_title)
            move_file(engine.client, kb, receipt['folder'], media, target_folder, expected_title=desired_name)
            present = next((r for r in engine.client.listing(kb, target_folder) if r.get('media_id') == media), None)
        if present is None:
            raise ImaError('云端迁移目标未确认，尚未上传新文件')
        title = present.get('title') or present.get('name')
        if title != desired_name:
            if title != old.name:
                raise ImaError('云端迁移文件名称已变化，请核对')
            rename_file(engine.client, kb, target_folder, media, desired_name, expected_title=title)
        if receipt['folder'] != target_folder and any(r.get('media_id') == media for r in engine.client.listing(kb, receipt['folder'])):
            raise ImaError('云端迁移源位置仍存在，尚未上传新文件')
        # Path/folder and receipt change together after the actual media was verified.
        with engine.store.connect() as db:
            if duplicate:
                db.execute("UPDATE receipts SET status='migrated_duplicate',note=?,updated=? WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?",
                           (target_folder, time.time(), *key))
                db.execute("UPDATE layout_moves SET phase='done_duplicate' WHERE account=? AND kb=? AND old_folder=? AND old_path=? AND sha256=?", key)
                notify('云端重复副本已移入 删除/目录迁移重复：' + str(relative))
                migrated += 1
                continue
            existing = db.execute('SELECT media_id FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?',
                                  (engine.account, kb, target_folder, str(new), receipt['sha256'])).fetchone()
            if existing and existing[0] != media:
                raise ImaError('迁移目标回执已被其他媒体占用')
            db.execute("UPDATE receipts SET status='migrated',updated=? WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?",
                       (time.time(), *key))
            db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?)',
                       (engine.account, kb, target_folder, str(new), receipt['sha256'], 'done', media, '目录迁移', time.time()))
            db.execute("UPDATE layout_moves SET phase='done' WHERE account=? AND kb=? AND old_folder=? AND old_path=? AND sha256=?", key)
        notify('云端目录迁移：' + str(relative) + ' → ' + str(new.relative_to(source)))
        migrated += 1
    return migrated


def _batch_moves(client, kb, pending, stop, notify):
    groups = defaultdict(list)
    counts = defaultdict(int)
    for receipt, old, new, job, key, relative in pending:
        counts[(job['new_folder'] or kb, new.name.casefold())] += 1
        if (old.name == new.name and job.get('phase') != 'duplicate_pending'
                and (receipt['folder'] or kb) != (job['new_folder'] or kb)):
            groups[(receipt['folder'], job['new_folder'])].append((job['media'], new))
    for (source, destination), candidates in groups.items():
        if stop.is_set():
            raise ImaError('已安全停止云端目录迁移，尚未上传新文件')
        if len(candidates) < 2:
            continue
        # Already moved IDs are left for receipt reconciliation. Same-name
        # collisions/merged duplicates retain the existing archive workflow.
        current = client.listing(kb, destination)
        existing_media = {item.get('media_id') for item in current}
        existing_names = {(item.get('title') or item.get('name', '')).casefold() for item in current}
        ready = [(media, new) for media, new in candidates if media not in existing_media
                 and new.name.casefold() not in existing_names
                 and counts[(destination or kb, new.name.casefold())] == 1]
        for start in range(0, len(ready), MOVE_BATCH_SIZE):
            batch = ready[start:start + MOVE_BATCH_SIZE]
            if stop.is_set():
                raise ImaError('已安全停止云端目录迁移，尚未上传新文件')
            if len(batch) < 2:
                continue
            if any(not new.is_file() or new.is_symlink() for _, new in batch):
                raise ImaError('云端迁移对应的本地文件已不存在或变为链接，尚未移动本批文件')
            notify(f'云端批量迁移开始：源目录={source or "根目录"}；目标目录={destination or "根目录"}；本批文件={len(batch)}')
            move_files(client, kb, source, [{'media_id': media, 'title': new.name} for media, new in batch],
                       destination, stop=stop)
            notify(f'云端批量迁移接口已确认：本批文件={len(batch)}；更新本地迁移回执')
