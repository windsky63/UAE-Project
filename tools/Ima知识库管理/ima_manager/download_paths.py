"""Read the downloader's identity/path registry without importing or changing it."""
from pathlib import Path
import sqlite3

from .api import ImaError
from .document_rules import inside, relative_path


def existing_download_path(downloads, value):
    """Resolve an exact registered file inside downloads, never history or staging."""
    if not value:
        return None
    path = Path(str(value))
    if not path.is_absolute():
        relative = relative_path(value)
        if relative is None:
            return None
        path = downloads / relative
    if inside(downloads, path) and not path.is_symlink() and path.is_file():
        return path
    return None


class DownloadPaths:
    def __init__(self, ledger, downloads, *, database=None):
        candidates = [Path(downloads).parent / 'runtime/ledger.sqlite3']
        if Path(ledger).parent.name.casefold() == 'exports':
            candidates.append(Path(ledger).parent.parent / 'runtime/ledger.sqlite3')
        self.path = Path(database) if database is not None else next((path for path in candidates if path.is_file()), None)
        self.db = None
        self.tables = set()
        self.columns = {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        if self.db is not None:
            self.db.close()

    def lookup(self, project, file_id):
        """None means no registry is installed; an empty list means a known ID is missing."""
        if self.path is None:
            return None
        try:
            if self.db is None:
                self.db = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
                self.db.row_factory = sqlite3.Row
                self.db.execute('PRAGMA query_only=ON')
                self.tables = {row[0] for row in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                for table in ('files', 'local_paths'):
                    if table in self.tables:
                        self.columns[table] = {row[1] for row in self.db.execute(f'PRAGMA table_info({table})')}
            paths = []
            file_row = None
            if {'project', 'id', 'path'}.issubset(self.columns.get('files', set())):
                file_row = self.db.execute('SELECT * FROM files WHERE project=? AND id=?', (project, file_id)).fetchone()
                if file_row is None or ('active' in file_row.keys() and not file_row['active']):
                    return []  # Never revive another identity or an inactive platform record.
            if {'project', 'file_id', 'local_path'}.issubset(self.columns.get('local_paths', set())):
                row = self.db.execute('SELECT local_path FROM local_paths WHERE project=? AND file_id=?',
                                      (project, file_id)).fetchone()
                if row and row['local_path']:
                    paths.append(row['local_path'])
            if file_row is not None and file_row['path'] and file_row['path'] not in paths:
                paths.append(file_row['path'])
            return paths
        except sqlite3.Error as exc:
            raise ImaError(f'下载身份路径登记暂不可读：{exc}') from None
