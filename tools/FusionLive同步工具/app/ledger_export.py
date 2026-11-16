"""Manually regenerate the Excel ledger without starting a platform sync."""
from contextlib import closing
from pathlib import Path
import sqlite3

from paths import HOME
from sync import export_ledger


class ExportLedger:
    def __init__(self, base, db):
        self.base_dir, self.db = Path(base), db

    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()


def export(base=HOME):
    base = Path(base).resolve()
    database = base / 'runtime/ledger.sqlite3'
    if not database.is_file():
        raise ValueError('未找到文件台账数据库，无法导出。')
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro', uri=True, timeout=15)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        return export_ledger(ExportLedger(base, db), supplemental=False)


if __name__ == '__main__':
    export()
