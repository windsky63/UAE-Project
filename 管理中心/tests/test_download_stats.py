"""Live physical counts are independent of duplicate or stale ledger rows."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from daily_publications import version_progress, _inventory
from unittest.mock import patch


class DownloadStatsTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path=Path(temporary.name)/'ledger.sqlite3'
        self.base=self.path.parent
        (self.base/'downloads').mkdir()
        (self.base/'downloads/one.pdf').write_bytes(b'old')
        self.sha=hashlib.sha256(b'old').hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executescript('''CREATE TABLE files(project,id,fingerprint,sha256,bytes,path,status,active);
              CREATE TABLE file_history(project,file_id,fingerprint,sha256,bytes,path);
              CREATE TABLE report_local_versions(project,id,version);''')
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                       ('p','one','old',self.sha,3,'downloads/one.pdf','done',1))

    def scan(self):
        with closing(sqlite3.connect(self.path)) as db:
            stats=_inventory.summary(_inventory.collect_inventory(self.base,db))
        folder=self.base/'logs/2026-11-04'
        folder.mkdir(parents=True,exist_ok=True)
        text='fixture snapshot'
        data=dict(snapshot_format='daily-file-snapshot-v1',date='2026-11-04',ready=True,
                  generated_at='2026-11-04T10:40:00+08:00',text=text,
                  body_sha256=hashlib.sha256(text.encode()).hexdigest(),file_inventory=stats)
        (folder/'日报快照.json').write_text(json.dumps(data),encoding='utf-8')
        return version_progress(self.path)

    def test_dashboard_reads_snapshot_without_scanning_or_opening_database(self):
        self.scan()
        (self.base/'downloads/one.pdf').unlink()
        with patch.object(_inventory,'collect_inventory',side_effect=AssertionError('directory scan')), \
             patch('sqlite3.connect',side_effect=AssertionError('database open')):
            self.assertEqual(version_progress(self.path)['downloads']['files'],1)
        self.assertEqual(self.scan()['downloads']['files'],0)

    def test_pending_revision_retains_cumulative_download_but_not_latest_completion(self):
        self.assertEqual(self.scan()['total'],dict(files=1,bytes=3))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO file_history SELECT project,id,fingerprint,sha256,bytes,path FROM files")
            db.execute("UPDATE files SET fingerprint='new',sha256=NULL,bytes=0,path=NULL,status='pending'")
        stats=self.scan()
        self.assertEqual(stats['downloads'],dict(files=1,bytes=3))
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE files SET sha256=?,bytes=3,path='runtime/download_ready/new',status='done'",
                       (hashlib.sha256(b'new').hexdigest(),))
        stats=self.scan()
        self.assertEqual(stats['total'],dict(files=1,bytes=3))
        self.assertEqual(stats['pending_publication']['files'],0)
        staged=self.base/'runtime/download_ready/new'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'new')
        stats=self.scan()
        self.assertEqual(stats['total'],dict(files=1,bytes=3))
        self.assertEqual(stats['pending_publication'],dict(files=1,bytes=3))

    def test_pending_publication_supports_windows_paths_and_excludes_inactive_records(self):
        staged=self.base/'runtime/download_ready/new/content'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'old')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE files SET path=?', (r'D:\project\runtime\download_ready\new\content',))
        before=self.path.read_bytes()
        self.assertEqual(self.scan()['pending_publication']['files'],1)
        self.assertEqual(self.path.read_bytes(),before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('UPDATE files SET active=0')
        self.assertEqual(self.scan()['pending_publication']['files'],0)

    def test_moving_into_history_and_duplicate_references_do_not_change_cumulative_total(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("INSERT INTO file_history SELECT project,id,fingerprint,sha256,bytes,path FROM files")
            db.execute('INSERT INTO report_local_versions VALUES(?,?,?)',('p','one',json.dumps(['old',self.sha])))
        before=self.path.read_bytes()
        self.assertEqual(self.scan()['total']['files'],1)
        self.assertEqual(self.path.read_bytes(),before)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute("UPDATE files SET active=0,path='history/one.pdf'")
            db.execute("UPDATE file_history SET path='history/one.pdf'")
        # The database update alone cannot move a real file.
        self.assertEqual(self.scan()['downloads']['files'],1)
        (self.base/'history').mkdir()
        (self.base/'downloads/one.pdf').rename(self.base/'history/one.pdf')
        stats=self.scan()
        self.assertEqual(stats['total']['files'],1)
        self.assertEqual(stats['downloads']['files'],0)
        self.assertEqual(stats['history']['files'],1)
        (self.base/'history/one.pdf').unlink()
        self.assertEqual(self.scan()['total']['files'],0)

    def test_files_without_ledger_rows_and_resizes_are_visible_on_next_read(self):
        extra=self.base/'downloads/new.pdf'
        extra.write_bytes(b'new')
        self.assertEqual(self.scan()['total'],dict(files=2,bytes=6))
        extra.write_bytes(b'longer')
        self.assertEqual(self.scan()['total'],dict(files=2,bytes=9))
        extra.unlink()
        self.assertEqual(self.scan()['total'],dict(files=1,bytes=3))

    def test_directory_symlink_is_not_counted_twice(self):
        alias=self.base/'downloads/alias'
        try:
            alias.symlink_to(self.base/'downloads',target_is_directory=True)
        except OSError:
            self.skipTest('Directory symlinks unavailable')
        self.assertEqual(self.scan()['total'],dict(files=1,bytes=3))

    def test_missing_or_invalid_ledger_does_not_crash_overview(self):
        self.assertIsNone(version_progress(self.path.parent/'missing.sqlite3'))
        self.path.write_bytes(b'invalid database')
        self.assertIsNone(version_progress(self.path))
