"""Durable SQLite checkpoints, shared by the CLI and offline desktop ledger."""

from paths import HOME, CODE
import json
import re
import sqlite3
from pathlib import Path
from fusion_client import HOME, RUNTIME, now
from inventory import InventoryMixin, fingerprint, CONTENT_FIELDS

DB_PATH = RUNTIME / 'ledger.sqlite3'


def safe_name(value, limit=90):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .') or '_'
    if value.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
        value = '_' + value
    return value[:limit].rstrip(' .') or '_'


class Ledger(InventoryMixin):
    def __init__(self, path=DB_PATH, base_dir=None):
        self.base_dir = Path(base_dir or (HOME if Path(path).resolve() == DB_PATH.resolve() else Path(path).resolve().parent)).resolve()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA foreign_keys=ON')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS folders (
          project TEXT NOT NULL, id TEXT NOT NULL, parent TEXT, name TEXT NOT NULL,
          path TEXT NOT NULL, tree_state TEXT NOT NULL DEFAULT 'pending',
          list_state TEXT NOT NULL DEFAULT 'pending', next_start INTEGER NOT NULL DEFAULT 0,
          total INTEGER, error TEXT, PRIMARY KEY(project,id));
        CREATE TABLE IF NOT EXISTS documents (
          project TEXT NOT NULL, id TEXT NOT NULL, folder_id TEXT NOT NULL, first_id TEXT,
          reference TEXT, filename TEXT, revision TEXT, title TEXT, size INTEGER,
          metadata TEXT NOT NULL, related_state TEXT NOT NULL DEFAULT 'pending', error TEXT,
          PRIMARY KEY(project,id,folder_id));
        CREATE TABLE IF NOT EXISTS files (
          project TEXT NOT NULL, id TEXT NOT NULL, document_id TEXT NOT NULL, folder_id TEXT NOT NULL,
          filename TEXT NOT NULL, revision TEXT, relation TEXT, size INTEGER,
          metadata TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          bytes INTEGER NOT NULL DEFAULT 0, path TEXT, sha256 TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT, updated TEXT, PRIMARY KEY(project,id));
        CREATE TABLE IF NOT EXISTS runs (
          id INTEGER PRIMARY KEY, action TEXT NOT NULL, started TEXT NOT NULL, ended TEXT,
          status TEXT NOT NULL DEFAULT 'running', logout_confirmed INTEGER NOT NULL DEFAULT 0,
          error TEXT);
        CREATE TABLE IF NOT EXISTS scan_pages (
          project TEXT NOT NULL,folder_id TEXT NOT NULL,start INTEGER NOT NULL,ids TEXT NOT NULL,
          PRIMARY KEY(project,folder_id,start));
        CREATE INDEX IF NOT EXISTS documents_folder ON documents(project,folder_id);
        CREATE INDEX IF NOT EXISTS files_status ON files(project,status);
        CREATE TABLE IF NOT EXISTS workflows (
          project TEXT PRIMARY KEY,stage TEXT NOT NULL,phase TEXT NOT NULL,snapshot_id INTEGER,
          cycle INTEGER,last_inventory_day TEXT,initial_download_completed TEXT,updated TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS download_scopes (
          project TEXT PRIMARY KEY,definition TEXT NOT NULL,confirmed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS download_receipts (
          project TEXT NOT NULL,file_id TEXT NOT NULL,identity TEXT NOT NULL,
          path TEXT NOT NULL,target_path TEXT NOT NULL,bytes INTEGER NOT NULL,
          sha256 TEXT NOT NULL,completed TEXT NOT NULL,PRIMARY KEY(project,file_id));
        CREATE TABLE IF NOT EXISTS download_activity (
          id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,file_id TEXT NOT NULL,
          filename TEXT NOT NULL,started TEXT NOT NULL,completed TEXT,had_download_copy INTEGER,
          previous_path TEXT,bytes INTEGER,sha256 TEXT);
        CREATE INDEX IF NOT EXISTS activity_completion ON download_activity(completed,project,file_id);
        CREATE TABLE IF NOT EXISTS download_receipt_context (
          project TEXT NOT NULL,file_id TEXT NOT NULL,activity_id INTEGER NOT NULL,PRIMARY KEY(project,file_id));
        ''')
        self.upgrade_schema()
        self.upgrade_download_dates()
        columns = {r['name'] for r in self.all('PRAGMA table_info(download_activity)')}
        with self.db:
            for name in ('previous_sha256', 'source_identity', 'action'):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE download_activity ADD COLUMN {name} TEXT')

    def upgrade_download_dates(self):
        columns = {r['name'] for r in self.all('PRAGMA table_info(files)')}
        with self.db:
            for name in ('downloaded_at', 'download_date_source', 'target_path'):
                if name not in columns:
                    self.db.execute(f'ALTER TABLE files ADD COLUMN {name} TEXT')
            for item in self.all("SELECT * FROM files WHERE status='done' AND downloaded_at IS NULL AND path IS NOT NULL"):
                from paths import resolve_path
                from datetime import datetime
                from fusion_client import LOCAL
                path = resolve_path(item['path'], self.base_dir)
                if path.is_file():
                    self.db.execute('UPDATE files SET downloaded_at=?,download_date_source=? WHERE project=? AND id=?',
                        (datetime.fromtimestamp(path.stat().st_mtime, LOCAL).isoformat(), '本地文件修改时间推定', item['project'], item['id']))

    def clean_partial_downloads(self):
        from paths import resolve_path
        for temporary in (self.base_dir / 'runtime/download_parts').glob('*.part'):
            if temporary.is_file() and not temporary.is_symlink():
                state_path = temporary.with_suffix('.json')
                try:
                    state = json.loads(state_path.read_text(encoding='utf-8'))
                    item = self.one('SELECT active,status,metadata FROM files WHERE project=? AND id=?',
                                    (state['project'], state['file_id']))
                    # Hash the actual prefix only when resuming; avoid expensive cleanup reads.
                    if (item and item['active'] and item['status'] not in ('done', 'restricted')
                            and fingerprint(json.loads(item['metadata']), CONTENT_FIELDS) == state['identity']):
                        continue
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                temporary.unlink()
                state_path.unlink(missing_ok=True)
        reserved = {str(row[key]).replace('\\', '/').casefold()
                    for row in self.all('SELECT path,target_path FROM files WHERE active=1')
                    for key in ('path', 'target_path') if row[key]}
        for item in self.all('SELECT target_path FROM files WHERE active=1 AND target_path IS NOT NULL'):
            target = resolve_path(item['target_path'], self.base_dir)
            if not item['target_path'].replace('\\', '/').startswith('downloads/'):
                raise ValueError('临时文件路径必须位于 downloads 中')
            if (item['target_path'] + '.part').replace('\\', '/').casefold() not in reserved:
                target.with_name(target.name + '.part').unlink(missing_ok=True)

    def clean_partial_for(self, target):
        from paths import relative_path
        relative = relative_path(target, self.base_dir) + '.part'
        if not self.one('''SELECT 1 FROM files WHERE active=1 AND
                           (path=? COLLATE NOCASE OR target_path=? COLLATE NOCASE)''', (relative, relative)):
            target.with_name(target.name + '.part').unlink(missing_ok=True)

    def save_download_receipt(self, item, result, target):
        """Durably record verified bytes before the HTTP client publishes them."""
        from paths import relative_path
        item = dict(item)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO download_receipts VALUES(?,?,?,?,?,?,?,?)',
                (item['project'], item['id'], fingerprint(json.loads(item['metadata']), CONTENT_FIELDS),
                 relative_path(result['path'], self.base_dir), relative_path(target, self.base_dir),
                 result['bytes'], result['sha256'], now().isoformat()))
            if item.get('download_activity_id'):
                self.db.execute('INSERT OR REPLACE INTO download_receipt_context VALUES(?,?,?)',
                                (item['project'],item['id'],item['download_activity_id']))

    def begin_download_activity(self, item, previous_path, *, started=None, known=True):
        from inventory import source_file_identity
        from paths import resolve_path
        import hashlib
        previous_sha256 = None
        if previous_path:
            # Read the actual retained bytes before HTTP, not mutable remote metadata.
            with resolve_path(previous_path, self.base_dir).open('rb') as stream:
                previous_sha256 = hashlib.file_digest(stream, 'sha256').hexdigest()
        with self.db:
            return self.db.execute('''INSERT INTO download_activity
                (project,file_id,filename,started,had_download_copy,previous_path,previous_sha256,source_identity)
                VALUES(?,?,?,?,?,?,?,?)''',
                (item['project'],item['id'],item['filename'],started or now().isoformat(),
                 int(bool(previous_path)) if known else None,previous_path,previous_sha256,source_file_identity(item))).lastrowid

    def finish_download_activity(self, identifier, result, completed):
        # Caller commits this with the successful file/receipt state.
        row = self.one('SELECT * FROM download_activity WHERE id=?', (identifier,))
        action = ('new' if row['had_download_copy']==0 else
                  'unchanged' if row['previous_sha256'] and row['previous_sha256']==result['sha256'] else
                  'updated' if row['had_download_copy']==1 and row['previous_sha256'] else 'unclassified')
        self.db.execute('UPDATE download_activity SET action=? WHERE id=? AND completed IS NULL', (action,identifier))
        self.db.execute('UPDATE download_activity SET completed=?,bytes=?,sha256=? WHERE id=? AND completed IS NULL',
                        (completed,result['bytes'],result['sha256'],identifier))

    def recover_download_receipts(self, check=lambda: None):
        """Adopt only the same version's journaled, fully reverified published bytes."""
        from paths import resolve_path
        import hashlib
        recovered = 0
        for receipt in self.all('SELECT * FROM download_receipts'):
            check()
            item = self.one('SELECT * FROM files WHERE project=? AND id=?',
                            (receipt['project'], receipt['file_id']))
            if (not item or not item['active'] or
                    fingerprint(json.loads(item['metadata']), CONTENT_FIELDS) != receipt['identity']):
                print('下载恢复记录对应的版本已变化，保留文件及记录待核对：'+receipt['path'], flush=True)
                continue
            source = resolve_path(receipt['path'], self.base_dir)
            if not source.exists():
                # Interrupted before publication. Ordinary download may safely retry.
                with self.db:
                    self.db.execute('DELETE FROM download_receipts WHERE project=? AND file_id=?',
                                    (receipt['project'], receipt['file_id']))
                    self.db.execute('DELETE FROM download_receipt_context WHERE project=? AND file_id=?',
                                    (receipt['project'],receipt['file_id']))
                continue
            before = source.stat()
            digest = hashlib.sha256()
            if source.is_file() and before.st_size == receipt['bytes']:
                with source.open('rb') as stream:
                    for block in iter(lambda: stream.read(1024*1024), b''):
                        check()
                        digest.update(block)
            after = source.stat()
            if (digest.hexdigest() != receipt['sha256'] or
                    (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)):
                with self.db:
                    self.db.execute("UPDATE files SET status='failed',retryable=0,error=? WHERE project=? AND id=?",
                        ('已落盘下载文件与恢复记录不符，保留现场待核对：'+receipt['path'],
                         receipt['project'], receipt['file_id']))
                continue
            with self.db:
                context = self.one('SELECT activity_id FROM download_receipt_context WHERE project=? AND file_id=?',
                                   (receipt['project'],receipt['file_id']))
                activity_id = context['activity_id'] if context else self.db.execute('''INSERT INTO download_activity
                    (project,file_id,filename,started) VALUES(?,?,?,?)''',
                    (item['project'],item['id'],item['filename'],receipt['completed'])).lastrowid
                self.finish_download_activity(activity_id,receipt,receipt['completed'])
                self.db.execute("""UPDATE files SET status='done',bytes=?,path=?,target_path=?,sha256=?,
                    error=NULL,retryable=1,updated=?,downloaded_at=?,download_date_source='下载完成实录（中断恢复）'
                    WHERE project=? AND id=?""",
                    (receipt['bytes'], receipt['path'], receipt['target_path'], receipt['sha256'],
                     now().isoformat(), receipt['completed'], receipt['project'], receipt['file_id']))
                self.db.execute('DELETE FROM download_receipts WHERE project=? AND file_id=?',
                                (receipt['project'], receipt['file_id']))
                self.db.execute('DELETE FROM download_receipt_context WHERE project=? AND file_id=?',
                                (receipt['project'],receipt['file_id']))
            recovered += 1
            print('已核验并恢复下载成功记录：'+receipt['path'], flush=True)
        return recovered

    def reuse_verified_versions(self, check=lambda: None):
        """Reuse only exact historical version identities after verifying bytes."""
        from file_layout import confined, checksum
        recovered = 0
        def cancelled():
            check()
            return False
        self.db.execute('CREATE INDEX IF NOT EXISTS file_history_identity ON file_history(project,file_id,fingerprint)')
        self.db.commit()
        pending = self.all("SELECT * FROM files WHERE active=1 AND status IN ('pending','failed') AND retryable=1")
        for item in pending:
            check()
            versions = self.all('''SELECT bytes,sha256 FROM file_history
                WHERE project=? AND file_id=? AND fingerprint=? AND bytes=? AND sha256 IS NOT NULL''',
                (item['project'],item['id'],item['fingerprint'],item['size']))
            adopted = False
            for version in versions:
                candidates = self.all('''SELECT DISTINCT path FROM file_history
                    WHERE project=? AND file_id=? AND sha256=? AND bytes=? AND path IS NOT NULL''',
                    (item['project'],item['id'],version['sha256'],version['bytes']))
                for row in candidates:
                    path = row['path'].replace('\\','/')
                    # Ordinary reconciliation restores history to downloads first.
                    # Never mark a file stored only in history as the current copy.
                    if not path.startswith(('downloads/','runtime/download_ready/')):
                        continue
                    source = confined(self.base_dir,path)
                    try:
                        before = source.stat()
                        if not source.is_file() or before.st_size != version['bytes']:
                            continue
                        digest = checksum(source,cancelled=cancelled)
                        after = source.stat()
                    except FileNotFoundError:
                        continue
                    if (digest != version['sha256'] or
                            (before.st_size,before.st_mtime_ns,before.st_ino) !=
                            (after.st_size,after.st_mtime_ns,after.st_ino)):
                        continue
                    with self.db:
                        self.db.execute("""UPDATE files SET status='done',path=?,bytes=?,sha256=?,error=NULL,
                            updated=?,downloaded_at=NULL,download_date_source='已验证历史版本复用'
                            WHERE project=? AND id=? AND fingerprint=? AND status IN ('pending','failed')""",
                            (path,version['bytes'],digest,now().isoformat(),item['project'],item['id'],item['fingerprint']))
                    print('复用已验证的同一版本，无需传输：'+item['filename'],flush=True)
                    recovered += 1
                    adopted = True
                    break
                if adopted:
                    break
        if recovered:
            self._download_layout_token = None
            self._download_reconciled_token = None
            self._download_updates_token = None
        return recovered

    def close(self):
        self.db.close()

    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()

    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()

    def start_run(self, action):
        self.clean_partial_downloads()
        with self.db:
            self.db.execute("UPDATE runs SET status='interrupted',ended=? WHERE status='running'", (now().isoformat(),))
            self.db.execute("UPDATE files SET status='pending',attempts=0,error='上次下载中断，等待重新下载' WHERE status='downloading'")
            return self.db.execute('INSERT INTO runs(action,started) VALUES(?,?)', (action, now().isoformat())).lastrowid

    def end_run(self, run_id, status, logout, error=None):
        with self.db:
            self.db.execute('UPDATE runs SET ended=?,status=?,logout_confirmed=?,error=? WHERE id=?',
                            (now().isoformat(), status, int(logout), error, run_id))





    def summary(self):
        folders = dict(self.one("SELECT COUNT(*) total, SUM(tree_state='done') tree_done,SUM(list_state='done') list_done FROM folders WHERE active=1"))
        return {'folders': folders, 'documents': self.one('SELECT COUNT(*) n FROM documents WHERE active=1')['n'],
                'files': {r['status']: r['n'] for r in self.all('SELECT status,COUNT(*) n FROM files WHERE active=1 GROUP BY status')},
                'workflows': [dict(r) for r in self.all('SELECT * FROM workflows ORDER BY project')],
                'latest_snapshot': dict(self.one('SELECT * FROM snapshots ORDER BY id DESC LIMIT 1') or {})}
