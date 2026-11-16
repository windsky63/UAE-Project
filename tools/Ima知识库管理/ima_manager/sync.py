"""Preview-first, additive sync with durable receipts and uncertain-write blocking."""
from collections import defaultdict, Counter
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import sqlite3
import threading
import time

from .api import ImaError, ImaQuotaError, ImaRateLimitError, target
from .daily import JOURNAL_NAME, record as journal_record

DELETE_DIRECTORY = '删除'


def ensure_delete_directory(source):
    root = Path(source).resolve()
    if not root.is_dir():
        raise ImaError('本地同步目录不存在。')
    deleted = root / DELETE_DIRECTORY
    if deleted.is_symlink() or not deleted.resolve().is_relative_to(root):
        raise ImaError('删除目录必须位于同步目录内，不能是链接。')
    deleted.mkdir(exist_ok=True)
    return deleted


def move_local_to_deleted(source, path):
    root, path = Path(source).resolve(), Path(path).absolute()
    if path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file():
        raise ImaError('只能移动同步目录内的普通文件。')
    relative = path.relative_to(root)
    if len(relative.parts) == 1 and path.name.casefold() == JOURNAL_NAME.casefold():
        raise ImaError('每日更新记录由工具维护，不能移入删除目录。')
    if relative.parts[0] == DELETE_DIRECTORY:
        raise ImaError('该文件已经在删除目录中。')
    destination = ensure_delete_directory(root) / relative
    if not destination.parent.resolve().is_relative_to(root / DELETE_DIRECTORY) or destination.exists():
        raise ImaError('删除目录中已有同路径文件或目录位置异常；不会覆盖。')
    destination.parent.mkdir(parents=True, exist_ok=True)
    path.rename(destination)
    journal_record(root, '本地逻辑删除', relative.as_posix(), '成功', detail='移入删除/' + relative.as_posix())
    return destination

TYPES = {
    'pdf': (1, 'application/pdf'), 'doc': (3, 'application/msword'),
    'docx': (3, 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'),
    'ppt': (4, 'application/vnd.ms-powerpoint'),
    'pptx': (4, 'application/vnd.openxmlformats-officedocument.presentationml.presentation'),
    'xls': (5, 'application/vnd.ms-excel'),
    'xlsx': (5, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
    'csv': (5, 'text/csv'), 'md': (7, 'text/markdown'), 'markdown': (7, 'text/markdown'),
    'png': (9, 'image/png'), 'jpg': (9, 'image/jpeg'), 'jpeg': (9, 'image/jpeg'),
    'webp': (9, 'image/webp'), 'txt': (13, 'text/plain'),
    'xmind': (14, 'application/x-xmind'), 'html': (20, 'text/html'),
    'epub': (21, 'application/epub+zip'),
}
LIMITS = {5: 10, 7: 10, 9: 30, 13: 10, 14: 10, 20: 10, 21: 50}


def fingerprint(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
        digest.update(chunk)
    return digest.hexdigest()


def inspect_file(path, root):
    path, root = Path(path), Path(root).resolve()
    result = {'path': str(path.absolute()), 'relative': path.relative_to(root).as_posix(),
              'name': path.name, 'size': 0, 'status': 'blocked', 'reason': ''}
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        result['reason'] = '跳过链接或源目录外的文件'
        return result
    extension = path.suffix.lower().lstrip('.')
    if extension not in TYPES:
        result['reason'] = '此类型未启用上传（压缩包、视频和音频请先在客户端处理）'
        return result
    media_type, content_type = TYPES[extension]
    size = path.stat().st_size
    result.update(size=size, media_type=media_type, content_type=content_type, file_ext=extension)
    if not size or size > LIMITS.get(media_type, 200) * 1024 * 1024 or len(path.name) > 1024:
        result['reason'] = '文件为空、名称过长或超过官方大小限制'
        return result
    with path.open('rb') as stream:
        before = os.fstat(stream.fileno())
        head = stream.read(12)
        if media_type == 9:
            actual = ('image/png' if head.startswith(b'\x89PNG\r\n\x1a\n') else
                      'image/jpeg' if head.startswith(b'\xff\xd8\xff') else
                      'image/webp' if head[:4] == b'RIFF' and head[8:12] == b'WEBP' else '')
            if actual != content_type:
                result['reason'] = '图片内容与扩展名不匹配'
                return result
        stream.seek(0)
        digest = fingerprint(stream)
        after = os.fstat(stream.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        result['reason'] = '文件扫描期间发生变化，请稍后重试'
        return result
    from .plans import stamp
    # Windows CRT fstat can report a different creation time than Path.stat.
    # Compare identity/size/mtime to the opened handle, then persist path stamps
    # consistently so an unchanged file does not invalidate its own preview.
    path_stat = path.stat()
    if stamp(path_stat)[:4] != stamp(after)[:4]:
        result['reason'] = '文件扫描期间被替换，请稍后重试'
        return result
    result.update(status='ready', sha256=digest, mtime_ns=after.st_mtime_ns, file_stamp=stamp(path_stat))
    return result


def scan(source, recursive=True, *, notify=None):
    root = Path(source).resolve()
    if not root.is_dir():
        raise ImaError('本地同步目录不存在。')
    if root.parent.name == '':
        raise ImaError('请选择具体资料目录。')
    paths = []
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith('.') and
                                d not in {'runtime', '__pycache__', 'node_modules'} and
                                not (Path(current) == root and d == DELETE_DIRECTORY) and
                                not (Path(current) / d).is_symlink())
        for name in sorted(filenames):
            if not name.startswith(('.', '~$')) and not (Path(current) == root and name.casefold() == JOURNAL_NAME.casefold()):
                paths.append(Path(current) / name)
        if not recursive:
            break
    rows = []
    last_progress = float('-inf')
    for path_index, path in enumerate(paths, 1):
        if notify is not None and time.monotonic()-last_progress >= 15:
            notify(f'本地扫描进度：已检查={path_index-1}/{len(paths)}；当前文件={path.relative_to(root)}')
            last_progress = time.monotonic()
        try:
            rows.append(inspect_file(path, root))
        except OSError:
            rows.append({'path': str(path), 'relative': path.relative_to(root).as_posix(),
                         'name': path.name, 'size': 0, 'status': 'blocked', 'reason': '文件不可读'})
    deleted = root / DELETE_DIRECTORY
    if deleted.exists():
        if not deleted.is_dir() or deleted.is_symlink() or not deleted.resolve().is_relative_to(root):
            raise ImaError('删除目录位置异常。')
        for current, directories, filenames in os.walk(deleted, followlinks=False):
            directories[:] = sorted(d for d in directories if not d.startswith('.') and
                                    not (Path(current) / d).is_symlink())
            for name in sorted(filenames):
                if name.startswith(('.', '~$')):
                    continue
                path = Path(current) / name
                relative = path.relative_to(deleted)
                try:
                    row = inspect_file(path, deleted)
                except OSError:
                    row = {'path': str(path), 'name': name, 'size': 0, 'reason': '文件不可读'}
                row.update(action='delete', original_path=str(root / relative),
                           relative=DELETE_DIRECTORY + '/' + relative.as_posix(),
                           status='deleted_local', reason='位于删除目录，不会上传')
                rows.append(row)
    return rows


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS receipts (
                account TEXT, kb TEXT, folder TEXT, path TEXT, sha256 TEXT,
                status TEXT, media_id TEXT, note TEXT, updated REAL,
                PRIMARY KEY(account,kb,folder,path,sha256))''')
            db.execute('''CREATE TABLE IF NOT EXISTS update_jobs (
                account TEXT,kb TEXT,folder TEXT,path TEXT,sha256 TEXT,payload TEXT,
                PRIMARY KEY(account,kb,folder,path,sha256))''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, account, kb, folder, row):
        with self.connect() as db:
            result = db.execute('SELECT status,media_id FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?',
                                (account, kb, folder, row['path'], row['sha256'])).fetchone()
        return result

    def archive_receipt(self, account, kb, folder, row):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            result = db.execute('SELECT status,media_id,note FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?',
                                (account, kb, folder, row['original_path'], row['sha256'])).fetchone()
            return dict(result) if result else None

    def previous_uploads(self, account, kb, folder, row):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute(
                "SELECT sha256,media_id FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND sha256<>? AND status='done' ORDER BY updated DESC",
                (account, kb, folder, row['path'], row['sha256']))]

    def update_job(self, account, kb, folder, row, value=None):
        import json
        key = (account, kb, folder, row['path'], row['sha256'])
        with self.connect() as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO update_jobs VALUES (?,?,?,?,?,?)',
                           (*key, json.dumps(value, ensure_ascii=False)))
                return value
            found = db.execute('SELECT payload FROM update_jobs WHERE account=? AND kb=? AND folder=? AND path=? AND sha256=?', key).fetchone()
            return json.loads(found[0]) if found else None

    def pending(self, account, kb, folder, row):
        with self.connect() as db:
            return db.execute("SELECT status FROM receipts WHERE account=? AND kb=? AND folder=? AND path=? AND status IN ('adding','uncertain')",
                              (account, kb, folder, row['path'])).fetchone()

    def put(self, account, kb, folder, row, status, media_id='', note=''):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO receipts VALUES (?,?,?,?,?,?,?,?,?)',
                       (account, kb, folder, row['path'], row['sha256'], status, media_id, note, time.time()))

    def history(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return [dict(r) for r in db.execute('SELECT kb,folder,path,status,media_id,note,updated FROM receipts ORDER BY updated DESC LIMIT 200')]

    def resolve(self, account, kb, folder, path, uploaded):
        with self.connect() as db:
            changed = db.execute("UPDATE receipts SET status=?,note=?,updated=? WHERE account=? AND kb=? AND folder=? AND path=? AND status IN ('adding','uncertain')",
                                 ('done' if uploaded else 'retry', '用户已人工核对', time.time(),
                                  account, kb, folder, str(Path(path).resolve()))).rowcount
        if not changed:
            raise ImaError('该文件在所选账号和目标中没有待核对记录。')


class Engine:
    def __init__(self, client, store):
        self.client, self.store = client, store
        self.account = hashlib.sha256(client.client_id.encode()).hexdigest()

    def plan(self, source, kb, folder='', recursive=True):
        self.directory_snapshot = {}
        with self._directory_session():
            rows = self._plan(source, kb, folder, recursive)
            self.directory_snapshot = self.client.export()
            return rows

    @contextmanager
    def _path_index(self):
        db = sqlite3.connect(self.store.path.with_name('cloud-paths.sqlite3'), timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def _directory_session(self):
        from .tree import DirectorySession
        previous = self.client
        self.client = DirectorySession(previous)
        with self._path_index() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS cloud_paths (
                account TEXT, source TEXT, kb TEXT, folder TEXT, path TEXT,
                PRIMARY KEY(account,source,kb,folder))''')
            self.client.paths = {(source, kb, folder): Path(path) for source, kb, folder, path
                in db.execute('SELECT source,kb,folder,path FROM cloud_paths WHERE account=?', (self.account,))
                if Path(path).is_dir() and Path(path).is_relative_to(Path(source))}
        try:
            yield
        finally:
            with self._path_index() as db:
                db.execute('DELETE FROM cloud_paths WHERE account=?', (self.account,))
                db.executemany('INSERT OR REPLACE INTO cloud_paths VALUES (?,?,?,?,?)',
                    [(self.account, *key, str(path)) for key, path in self.client.paths.items()])
            self.client = previous

    def _plan(self, source, kb, folder='', recursive=True, *, scanned_rows=None, check_migrations=True):
        notify = getattr(self, 'plan_notify', lambda _: None)
        started = time.monotonic()
        notify('预览计划：开始核对所选源目录，扫描本地文件并校验大小、内容摘要')
        target(kb, folder)
        from .scope import require_directory
        from .tree import CloudTree, LAYOUT, relative_target
        local_base = require_directory(self.client, source, kb, folder)
        folder = "" if folder == kb else folder
        tree = CloudTree(self.client, kb, folder)
        rows = scan(source, recursive, notify=notify) if scanned_rows is None else scanned_rows
        notify(f'预览计划：本地扫描完成，文件={len(rows)}；耗时={time.monotonic()-started:.1f}秒')
        from .cloud_layout import migrate_cloud
        migrations = defaultdict(list)
        for item in (migrate_cloud(self, source, kb, folder, threading.Event(), notify, preview=True)
                     if check_migrations else []):
            migrations[item['path']].append(item)
        notify(f'预览计划：迁移候选核对完成，涉及本地文件={len(migrations)}；开始核对新增文件目标目录')
        to_check = defaultdict(list)
        for row in rows:
            row['sync_root'] = str(Path(source).resolve())
            row['layout'] = LAYOUT
            row['target_base_folder_id'] = folder
            try:
                relative = relative_target(row, local_base)
                row['target_parts'] = list(relative.parent.parts)
                row['target_path'] = relative.as_posix()
                row['target_folder_id'] = None if migrations.get(row['path']) and row.get('action') != 'delete' else tree.parent(relative.parent.parts)
            except (ImaQuotaError, ImaRateLimitError):
                raise
            except ImaError as exc:
                row.update(status='blocked', reason=str(exc))
                continue
            destination = row['target_folder_id']
            candidates = migrations.get(row['path'], [])
            if candidates and row.get('action') != 'delete' and row['status'] == 'ready':
                problems = [item for item in candidates if item['status'] == 'blocked']
                selected = problems[0] if problems else candidates[0]
                row.update({key: value for key, value in selected.items() if key != 'path'})
                row['migration_count'] = len(candidates)
                if not problems:
                    row['reason'] = ('先迁移已有云端文件，内容未变不重复上传' if all(item['migration_sha256'] == row['sha256'] for item in candidates)
                                     else '先迁移云端旧版，再按内容变化执行版本更新')
                continue
            if destination is None:
                if row.get('action') != 'delete' and row['status'] == 'ready':
                    row['reason'] = '云端目录尚不存在；正式同步时创建对应目录后上传'
                continue
            if row.get('action') == 'delete':
                if 'sha256' not in row:
                    continue
                saved = self.store.archive_receipt(self.account, kb, destination, row)
                if Path(row['original_path']).exists():
                    row.update(status='blocked', reason='原位置仍有文件，请移动文件而不是复制到删除目录')
                elif saved and saved['status'] in ('done', 'archiving'):
                    row.update(status='ready_delete', media_id=saved['media_id'],
                               reason='将按上传回执把云端文件移入同步目标的删除目录')
                elif saved and saved['status'] == 'archived':
                    row.update(status='deleted', media_id=saved['media_id'], reason='已逻辑删除，等待在 ima 手动清理')
                else:
                    row['reason'] = '本地已逻辑删除；没有匹配内容的成功上传回执，不操作云端'
                continue
            if row['status'] != 'ready':
                continue
            if self.store.pending(self.account, kb, destination, row):
                row.update(status='uncertain', reason='上次入库结果待核对；请先在客户端核对并通过 resolve 命令处理')
                continue
            saved = self.store.get(self.account, kb, destination, row)
            if saved and saved[0] == 'done':
                row.update(status='unchanged', reason='此账号与目标已有相同内容的成功上传记录')
            elif (job := self.store.update_job(self.account, kb, destination, row)) and job.get('phase') != 'done':
                row.update(status='ready_update', action='update', reason='继续完成已记录的版本更新')
            elif saved and saved[0] in ('archived', 'archiving'):
                row.update(status='blocked', reason='云端文件已移入或正在移入删除目录；恢复需在 ima 手动处理')
            else:
                to_check[destination].append(row)
        for destination, group in to_check.items():
            notify(f'预览计划查重：目标目录={destination or "根目录"}；本批文件={len(group)}')
            from .collection_rules import COLLECTION_ROOTS
            managed_roots = set(COLLECTION_ROOTS) | {'其他图纸'}
            entries = self.client.cached_listing(kb, destination)
            managed = any(Path(row['relative']).parts[0] in managed_roots for row in group)
            if entries is not None or managed:
                # Managed directories are also used for old-cloud discovery. One
                # full listing serves both checks without a second name endpoint.
                if entries is None:
                    entries = tree.entries(destination)
                names = {(item.get('title') or item.get('name') or '').casefold() for item in entries}
                repeated = {row['name']: row['name'].casefold() in names for row in group}
            else:
                # A new unmanaged root can still use one cheap batched name check.
                repeated = self.client.repeated_many(kb, destination,
                    [{'name': row['name'], 'media_type': row['media_type']} for row in group])
                entries = tree.entries(destination) if any(repeated.values()) else []
            for row in group:
                named = [item for item in entries if (item.get('title') or item.get('name')) == row['name']]
                if not named and not repeated[row['name']]:
                    continue
                candidates = self.store.previous_uploads(self.account, kb, destination, row)
                matches = [old for old in candidates if any(item.get('media_id') == old['media_id']
                           for item in named)]
                if len(matches) == 1 and len(named) == 1:
                    row.update(status='ready_update', action='update', old_media_id=matches[0]['media_id'],
                               old_sha256=matches[0]['sha256'], reason='上传并核验新版后，将旧版移入删除目录')
                elif len(named) == 1:
                    from .api import folder_id
                    row.update(status='conflict' if folder_id(named[0]) or not named[0].get('media_id') else 'cloud_existing',
                               reason='此云端路径已有同名条目；正式同步时按当前本地内容认领并补建回执',
                               media_id=named[0].get('media_id', ''))
                else:
                    row.update(status='conflict', reason='云端同名条目不唯一或查重结果尚未匹配到列表，请核对后重新预览')
        if check_migrations:
            from .cloud_layout import discover_unreceipted
            discover_unreceipted(self, source, kb, folder, rows, notify)
        notify(f'预览计划完成：文件={len(rows)}；状态={dict(Counter(row["status"] for row in rows))}；耗时={time.monotonic()-started:.1f}秒')
        return rows

    def execute(self, rows, kb, folder='', stop=None, notify=lambda message: None):
        with self._directory_session():
            snapshot = getattr(self, 'directory_snapshot', {})
            self.directory_snapshot = {}
            self.client.restore(snapshot)
            return self._execute(rows, kb, folder, stop, notify)

    def _execute(self, rows, kb, folder='', stop=None, notify=lambda message: None):
        stop = stop or threading.Event()
        inspected=executed=0
        started=last_report=time.monotonic()
        local_bases, trees = {}, {}
        # A cross-process SQLite write lock prevents simultaneous uploads by GUI/watch/CLI.
        lock_path = self.store.path.with_name('upload-lock.sqlite3')
        lock = sqlite3.connect(lock_path, timeout=0)
        try:
            try:
                lock.execute('CREATE TABLE IF NOT EXISTS guard (id INTEGER)')
                lock.execute('BEGIN IMMEDIATE')
            except sqlite3.OperationalError:
                raise ImaError('另一个上传任务正在运行，请等待它完成。') from None
            from .cloud_layout import migrate_cloud, prepare_unreceipted
            scopes = sorted({row['sync_root'] for row in rows if row.get('sync_root')})
            migrated_scopes = []
            for scope in scopes:
                prepare_unreceipted(self, scope, kb, folder, rows, stop)
                selected = {row['path'] for row in rows if row.get('sync_root') == scope
                            and row.get('action') != 'delete' and row['status'] not in ('blocked', 'conflict', 'uncertain')}
                if migrate_cloud(self, scope, kb, folder, stop, notify, selected_paths=selected):
                    migrated_scopes.append(scope)
            if migrated_scopes:
                # Moves change cloud folders and upload receipts. Re-plan only the
                # selected paths, preserving the caller's selection and stop rules.
                refreshed = {}
                for scope in migrated_scopes:
                    selected = []
                    for row in rows:
                        if row.get('sync_root') != scope or row['status'] != 'ready_migrate':
                            continue
                        clean = {key: row[key] for key in ('path', 'relative', 'name', 'size', 'media_type',
                            'content_type', 'file_ext', 'sha256', 'mtime_ns', 'file_stamp') if key in row}
                        clean.update(status='ready', reason='')
                        selected.append(clean)
                    refreshed.update({r['path']: r for r in self._plan(scope, kb, folder, True,
                        scanned_rows=selected, check_migrations=False)})
                for row in rows:
                    if row.get('sync_root') in migrated_scopes and row['status'] == 'ready_migrate':
                        replacement = refreshed.get(row['path'])
                        if replacement is None:
                            raise ImaError('目录迁移后本地计划已变化，请重新预览')
                        row.clear()
                        row.update(replacement)
            from .empty_folders import report_empty_folders
            report_empty_folders(self, rows, kb, folder, stop, notify)
            self._archive_batches(rows, kb, stop, notify)
            for row in rows:
                if stop.is_set():
                    break
                inspected+=1
                if row['status'] not in ('ready', 'ready_delete', 'ready_update', 'cloud_existing'):
                    if row.get('sync_root') and row['status'] in ('conflict', 'blocked', 'uncertain'):
                        journal_record(row['sync_root'], '同步', row['relative'], row['status'],
                                       version=row.get('sha256', ''), target=kb + '/' + (folder or '根目录') + '/' + row.get('target_path', row['relative']), detail=row.get('reason', ''))
                    continue
                destination = '' if folder == kb else folder
                abort_error = None
                try:
                    if row.get('sync_root'):
                        from .scope import require_directory
                        from .tree import CloudTree, LAYOUT, relative_target
                        scope = str(Path(row['sync_root']).resolve())
                        if scope not in local_bases:
                            local_bases[scope] = require_directory(self.client, scope, kb, folder)
                        local_base = local_bases[scope]
                        if row.get('layout') != LAYOUT or row.get('target_base_folder_id') != destination:
                            raise ImaError('同步计划的目录规则已过期，请重新预览。')
                        relative = relative_target(row, local_base)
                        if relative.as_posix() != row.get('target_path'):
                            raise ImaError('目标路径已改变，请重新预览。')
                        tree_key = (scope, row.get('action') == 'delete')
                        if tree_key not in trees:
                            trees[tree_key] = CloudTree(self.client, kb, folder,
                                create=None if row.get('action') == 'delete' else self._directory)
                        tree = trees[tree_key]
                        destination = tree.parent(relative.parent.parts)
                        if destination is None:
                            raise ImaError('云端原目录已不存在，请重新预览。')
                        if row.get('target_folder_id') is not None and row['target_folder_id'] != destination:
                            raise ImaError('云端目录身份已改变，请重新预览。')
                        row['target_folder_id'] = destination
                    if row.get('action') == 'update':
                        self._update(row, kb, destination)
                    elif row.get('action') == 'delete':
                        self._archive(row, kb, destination)
                    else:
                        self._one(row, kb, destination)
                except (ImaError, OSError, ValueError, KeyError, sqlite3.Error) as exc:
                    if isinstance(exc, (ImaQuotaError, ImaRateLimitError)):
                        abort_error = exc
                    row.update(status='failed', reason=self.client.redact(str(exc)))
                    if self.store.pending(self.account, kb, destination, row):
                        row.update(status='uncertain', reason='入库结果待核对，自动重试已暂停')
                    elif row.get('action') == 'update':
                        job = self.store.update_job(self.account, kb, destination, row)
                        if job and self.store.pending(self.account, kb, job['stage'], row):
                            row.update(status='uncertain', reason='新版暂存入库结果待核对；旧版保留，可在“处理待核对”中处理')
                notify(f"{row['relative']}：{row['status']} {row.get('reason', '')}")
                executed+=1
                if time.monotonic()-last_report>=30:
                    notify(f'ima同步进度：已检查 {inspected}/{len(rows)}；本轮执行={executed}；当前路径={row["relative"]}；状态={row["status"]}；耗时={time.monotonic()-started:.1f}秒')
                    last_report=time.monotonic()
                if row.get('sync_root'):
                    old_version = row.get('old_sha256', '')
                    if row.get('action') == 'update' and not old_version:
                        job = self.store.update_job(self.account, kb, destination, row)
                        old_version = (job or {}).get('old_sha', '')
                    journal_record(row['sync_root'], {'update': '云端更新', 'delete': '云端逻辑删除'}.get(row.get('action'), '上传'),
                                   row['relative'], {'done': '成功', 'deleted': '成功', 'failed': '失败', 'uncertain': '待核对',
                                   'conflict': '冲突', 'blocked': '跳过'}.get(row['status'], row['status']),
                                   version=row.get('sha256', ''), old_version=old_version,
                                   target=kb + '/' + (folder or '根目录') + '/' + row.get('target_path', row['relative']), detail=row.get('reason', ''))
                if abort_error is not None:
                    notify(f'ima {abort_error.code} 持续拒绝请求，已中止本轮同步；已成功文件的回执保留，后续文件不再请求。')
                    raise abort_error
        finally:
            lock.close()
            notify(f'ima同步进度：已检查 {inspected}/{len(rows)}；本轮执行={executed}；未检查={len(rows)-inspected}；'
                   f'状态分布={dict(Counter(row["status"] for row in rows))}；停止请求={stop.is_set()}；耗时={time.monotonic()-started:.1f}秒')
        return rows

    def _directory(self, kb, parent, name):
        from .scope import create_folder
        from .api import folder_id
        entries = self.client.listing(kb, parent)
        matches = [r for r in entries if (r.get('title') or r.get('name')) == name]
        if matches:
            if len(matches) != 1 or not folder_id(matches[0]):
                raise ImaError('更新目录名称已被其他条目占用，请先核对。')
            return folder_id(matches[0])
        return create_folder(self.client, kb, parent, name)

    def resolve_upload(self, kb, folder, path, uploaded):
        path = Path(path).resolve()
        with path.open('rb') as stream:
            row = {'path': str(path), 'sha256': fingerprint(stream)}
        job = self.store.update_job(self.account, kb, folder, row)
        self.store.resolve(self.account, kb, job['stage'] if job else folder, path, uploaded)

    def _update(self, row, kb, folder):
        from .scope import move_file
        path = Path(row['path'])
        if path.is_symlink() or not path.is_file():
            raise ImaError('本地同步文件已不存在，不允许修改云端旧版。')
        with path.open('rb') as stream:
            if fingerprint(stream) != row['sha256']:
                raise ImaError('本地同步文件已变化，请重新预览更新计划。')
        job = self.store.update_job(self.account, kb, folder, row)
        if job and job.get('phase') == 'done':
            job = None  # The same contents can recur after later revisions; start a fresh update.
        if not job:
            # Persist the old identity before creating or uploading any replacement.
            candidates = self.store.previous_uploads(self.account, kb, folder, row)
            if not any(old['media_id'] == row['old_media_id'] and old['sha256'] == row['old_sha256'] for old in candidates):
                raise ImaError('旧版上传回执已变化，请重新预览。')
            stage_root = self._directory(kb, folder, '更新暂存')
            stage = self._directory(kb, stage_root, hashlib.sha256((row['path'] + row['sha256'] + row['old_media_id']).encode()).hexdigest()[:20])
            job = {'old_media': row['old_media_id'], 'old_sha': row['old_sha256'], 'stage': stage, 'phase': 'upload'}
            self.store.update_job(self.account, kb, folder, row, job)

        def contains(where, media):
            return any(r.get('media_id') == media for r in self.client.listing(kb, where))

        staged = self.store.get(self.account, kb, job['stage'], row)
        if staged and staged[0] in ('adding', 'uncertain'):
            if contains(job['stage'], staged[1]):
                self.store.put(self.account, kb, job['stage'], row, 'done', staged[1])
            else:
                raise ImaError('新版暂存入库结果待核对，尚未归档旧版。')
        staged = self.store.get(self.account, kb, job['stage'], row)
        if not staged or staged[0] != 'done':
            upload = dict(row, status='ready')
            self._one(upload, kb, job['stage'])
            if upload['status'] != 'done':
                raise ImaError('新版暂存目录存在冲突，尚未归档旧版。')
            staged = self.store.get(self.account, kb, job['stage'], row)
        new_media = staged[1]
        job['new_media'] = new_media
        self.store.update_job(self.account, kb, folder, row, job)
        if not contains(job['stage'], new_media) and not contains(folder, new_media):
            raise ImaError('尚未确认新版入库，旧版保持原位置。')
        if not job.get('archive'):
            deleted = self._directory(kb, folder, DELETE_DIRECTORY)
            old_root = self._directory(kb, deleted, '更新旧版')
            job['archive'] = self._directory(kb, old_root, hashlib.sha256(job['old_media'].encode()).hexdigest()[:20])
            job['phase'] = 'archive_old'
            self.store.update_job(self.account, kb, folder, row, job)
        if not contains(job['archive'], job['old_media']):
            move_file(self.client, kb, folder, job['old_media'], job['archive'], expected_title=row['name'])
        if contains(folder, job['old_media']) or not contains(job['archive'], job['old_media']):
            raise ImaError('旧版归档位置未确认，保留更新任务供下次继续。')
        job['phase'] = 'publish'
        self.store.update_job(self.account, kb, folder, row, job)
        if not contains(folder, new_media):
            move_file(self.client, kb, job['stage'], new_media, folder, expected_title=row['name'])
        if not contains(folder, new_media) or contains(job['stage'], new_media):
            raise ImaError('新版正常目录位置未确认，下次将继续核对。')
        self.store.put(self.account, kb, folder, dict(row, sha256=job['old_sha']), 'superseded', job['old_media'], job['archive'])
        self.store.put(self.account, kb, folder, row, 'done', new_media)
        job['phase'] = 'done'
        self.store.update_job(self.account, kb, folder, row, job)
        row.update(status='done', media_id=new_media, reason='新版已更新，旧版保留在删除/更新旧版中，待用户清理')

    def _archive(self, row, kb, folder):
        from .scope import move_file
        destination = self._archive_prepare(row, kb, folder)
        if destination is None:
            return
        if not any(r.get('media_id') == row['media_id'] for r in self.client.listing(kb, destination)):
            move_file(self.client, kb, folder, row['media_id'], destination, expected_title=row['name'])
        self._archive_finish(row, kb, folder, destination)

    def _archive_prepare(self, row, kb, folder):
        from .scope import create_folder
        path = Path(row['path'])
        if path.is_symlink() or Path(row['original_path']).exists():
            raise ImaError('删除标记已变化，请重新预览。')
        with path.open('rb') as stream:
            if fingerprint(stream) != row['sha256']:
                raise ImaError('删除目录中的文件已变化，请重新预览。')
        saved = self.store.archive_receipt(self.account, kb, folder, row)
        if not saved or saved['status'] not in ('done', 'archiving', 'archived') or saved['media_id'] != row['media_id']:
            raise ImaError('删除回执不匹配，停止云端移动。')
        if saved['status'] == 'archived':
            row.update(status='deleted', reason='已逻辑删除')
            return None

        def get_directory(parent, name):
            entries = self.client.listing(kb, parent)
            matches = [r for r in entries if (r.get('title') or r.get('name')) == name]
            from .api import folder_id
            if matches:
                if len(matches) != 1 or not folder_id(matches[0]):
                    raise ImaError('删除目录名称已被其他条目占用，请在 ima 中核对。')
                return folder_id(matches[0])
            return create_folder(self.client, kb, parent, name)

        destination = saved['note'] if saved['status'] == 'archiving' else ''
        if not destination:
            destination = get_directory(folder, DELETE_DIRECTORY)
            entries = self.client.listing(kb, destination)
            if any(r.get('media_id') != row['media_id'] and
                   r.get('title', '').casefold() == row['name'].casefold() for r in entries):
                # Preserve the original filename; isolate collisions by stable media identity.
                destination = get_directory(destination, hashlib.sha256(row['media_id'].encode()).hexdigest()[:16])
        original_row = dict(row, path=row['original_path'])
        self.store.put(self.account, kb, folder, original_row, 'archiving', row['media_id'], destination)
        return destination

    def _archive_finish(self, row, kb, folder, destination):
        original_row = dict(row, path=row['original_path'])
        self.store.put(self.account, kb, folder, original_row, 'archived', row['media_id'], destination)
        row.update(status='deleted', reason='已移入云端删除目录，等待在 ima 手动清理')

    def _archive_batches(self, rows, kb, stop, notify):
        from .scope import MOVE_BATCH_SIZE, move_files
        groups = defaultdict(list)
        for row in rows:
            if row['status'] == 'ready_delete' and row.get('target_folder_id') is not None:
                groups[row['target_folder_id']].append(row)
        for source, group in groups.items():
            if len(group) < 2:
                continue
            destinations = defaultdict(list)
            names = Counter(row['name'].casefold() for row in group)
            for row in group:
                if stop.is_set():
                    return
                if names[row['name'].casefold()] != 1:
                    continue  # Existing per-file handling isolates duplicate names.
                try:
                    destination = self._archive_prepare(row, kb, source)
                except (ImaQuotaError, ImaRateLimitError):
                    raise
                except (ImaError, OSError, ValueError, sqlite3.Error):
                    continue  # The ordinary row handler reports this file's error.
                if destination is not None:
                    destinations[destination].append(row)
            for destination, candidates in destinations.items():
                current = {item.get('media_id') for item in self.client.listing(kb, destination)}
                remaining = []
                for row in candidates:
                    if row['media_id'] in current:
                        self._archive_finish(row, kb, source, destination)
                    else:
                        remaining.append(row)
                for start in range(0, len(remaining), MOVE_BATCH_SIZE):
                    if stop.is_set():
                        return
                    batch = remaining[start:start + MOVE_BATCH_SIZE]
                    move_files(self.client, kb, source,
                               [{'media_id': row['media_id'], 'title': row['name']} for row in batch],
                               destination, stop=stop)
                    for row in batch:
                        self._archive_finish(row, kb, source, destination)
                        if row.get('sync_root'):
                            journal_record(row['sync_root'], '云端逻辑删除', row['relative'], '成功',
                                           version=row['sha256'], target=kb + '/' + destination)
                    notify(f'云端批量逻辑删除接口已确认：本批文件={len(batch)}；回执已保存')

    def _one(self, row, kb, folder):
        if self.store.pending(self.account, kb, folder, row):
            raise ImaError('此文件上次入库结果待核对。')
        saved = self.store.get(self.account, kb, folder, row)
        if saved and saved[0] == 'done':
            row.update(status='unchanged', reason='已上传')
            return
        path = Path(row['path'])
        if path.is_symlink():
            raise ImaError('文件变成链接，请重新扫描。')
        with path.open('rb') as stream:
            stat = os.fstat(stream.fileno())
            if stat.st_size != row['size'] or fingerprint(stream) != row['sha256']:
                raise ImaError('文件已变化，请重新生成上传计划。')
            if row['status'] == 'cloud_existing':
                self._adopt(row, kb, folder)
                return
            if self.client.repeated(kb, folder, row['name'], row['media_type']):
                self._adopt(row, kb, folder)
                return
            created = self.client.call('create_media', {
                'file_name': row['name'], 'file_size': row['size'], 'file_ext': row['file_ext'],
                'content_type': row['content_type'], 'knowledge_base_id': kb})
            media_id, credential = created['media_id'], created['cos_credential']
            self.store.put(self.account, kb, folder, row, 'uploading', media_id)
            stream.seek(0)
            self.client.upload_object(stream, row['size'], row['content_type'], credential)
            stream.seek(0)
            if fingerprint(stream) != row['sha256']:
                raise ImaError('上传期间文件发生变化，未执行知识库入库，请重新扫描。')
            self.store.put(self.account, kb, folder, row, 'adding', media_id)
            try:
                added = self.client.call('add_knowledge', dict(target(kb, folder),
                    media_id=media_id, media_type=row['media_type'], title=row['name'],
                    file_info={'cos_key': credential['cos_key'], 'file_name': row['name'],
                               'file_size': row['size'], 'last_modify_time': int(stat.st_mtime)}))
                if not added.get('media_id'):
                    raise ImaError('入库响应缺少 media_id。')
            except Exception as exc:
                rejected = isinstance(exc, ImaError) and exc.code in (200001, 110021, 220021)
                self.store.put(self.account, kb, folder, row, 'retry' if rejected else 'uncertain', media_id,
                               str(exc) if rejected else '请在 ima 核对该媒体是否已经入库')
                raise
            self.store.put(self.account, kb, folder, row, 'done', added['media_id'])
            row.update(status='done', media_id=added['media_id'], reason='已提交入库，解析状态请在 ima 查看')

    def _adopt(self, row, kb, folder):
        from .api import folder_id
        entries = self.client.listing(kb, folder)
        named = [item for item in entries if (item.get('title') or item.get('name')) == row['name']]
        if (len(named) != 1 or folder_id(named[0]) or not named[0].get('media_id') or
                (row.get('media_id') and row['media_id'] != named[0]['media_id'])):
            row.update(status='conflict', reason='云端同名文件不唯一、身份已变化或列表无法确认，请重新预览')
            return
        media = named[0]['media_id']
        self.store.put(self.account, kb, folder, row, 'done', media, '按同路径同名文件认领，以当前本地内容为基线')
        row.update(status='unchanged', media_id=media, reason='已认领云端同名文件并补建回执，未重复上传')
