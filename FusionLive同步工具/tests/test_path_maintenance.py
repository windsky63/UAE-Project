"""Unified maintenance uses real files/SQLite and never contacts external services."""
import hashlib
from pathlib import Path
import json
import subprocess
import sys
import unittest
from unittest.mock import patch

import file_layout as layout
import history_repair as history
import path_maintenance as maintenance
from tests import test_history_repair as fixture


class MaintenanceTests(unittest.TestCase):
    setUp = fixture.HistoryRepairTests.setUp
    add = fixture.HistoryRepairTests.add

    def test_existing_batch_and_new_auto_archive_reserve_distinct_shared_names(self):
        source=self.add(data=b'historical')
        new=self.base/'downloads/Project/Drawings/drawing__rev_A__id.pdf'
        new.write_bytes(b'untracked')
        result=maintenance.apply(self.ledger)
        archives=[m for m in result['moves'] if m['target'].startswith('history/')]
        self.assertEqual(len(archives),2)
        self.assertEqual(len({m['target'].casefold() for m in archives}),2)
        self.assertTrue(all(m['target'].startswith('history/Project/Drawings/') for m in archives))
        self.assertEqual({(self.base/m['target']).read_bytes() for m in archives},{b'historical',b'untracked'})
        self.assertFalse((self.base/source).exists())
        self.assertFalse(new.exists())
        self.assertEqual(maintenance.plan(self.ledger)['moves'],[])

    def pending(self):
        self.current.unlink()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path=NULL,status='pending',bytes=0,sha256=NULL")

    def test_preview_is_read_only_and_restores_only_newest_available_old_version(self):
        older = self.add('version-1', data=b'old')
        latest = self.add('version-2', data=b'last')
        self.pending()
        before = '\n'.join(self.ledger.db.iterdump())
        proposal = maintenance.plan(self.ledger)
        self.assertEqual(proposal['restored'], 1)
        self.assertEqual('\n'.join(self.ledger.db.iterdump()), before)
        self.assertTrue((self.base/latest).is_file())
        self.assertFalse(self.current.exists())
        maintenance.apply(self.ledger)
        self.assertEqual(self.current.read_bytes(), b'last')
        current = self.ledger.one('SELECT * FROM files')
        self.assertEqual(current['status'], 'pending')
        self.assertIsNone(current['path'])
        self.assertIsNone(current['sha256'])
        self.assertEqual(self.ledger.one('SELECT path FROM file_history ORDER BY id DESC')['path'],
                         self.current.relative_to(self.base).as_posix())
        old = self.ledger.one('SELECT path FROM file_history ORDER BY id LIMIT 1')['path']
        self.assertTrue(old.startswith('history/Project/Drawings/'))
        self.assertEqual((self.base/old).read_bytes(), b'old')
        self.assertFalse((self.base/older).exists())
        self.assertEqual(maintenance.plan(self.ledger)['moves'], [])
        self.assertTrue(list((self.base/'runtime/backups').glob('before-unified-paths-*.sqlite3')))
        # The restored bytes are still old content and will be retained until download succeeds.
        self.assertEqual(layout.plan(self.ledger)['moves'], [])
        self.assertIn(('p', 'f'), layout.local_update_keys(self.ledger))

    def test_latest_already_downloaded_only_history_is_organized(self):
        self.add()
        before = dict(self.ledger.one('SELECT * FROM files'))
        result = maintenance.apply(self.ledger)
        self.assertEqual(result['restored'], 0)
        self.assertEqual(dict(self.ledger.one('SELECT * FROM files')), before | {'target_path': self.current.relative_to(self.base).as_posix()})
        self.assertEqual(self.current.read_bytes(), b'current')
        self.assertTrue(self.ledger.one('SELECT path FROM file_history')['path'].startswith('history/Project/'))

    def test_existing_local_old_version_prevents_another_restoration(self):
        self.add('version-1')
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO file_history(project,file_id,path,bytes,sha256,archived) VALUES('p','f',?,7,?,'2026-10-30')",
                                   (self.current.relative_to(self.base).as_posix(), hashlib.sha256(b'current').hexdigest()))
            self.ledger.db.execute("UPDATE files SET path=NULL,status='pending',bytes=0,sha256=NULL")
        proposal = maintenance.apply(self.ledger)
        self.assertEqual(proposal['restored'], 0)
        self.assertEqual(self.current.read_bytes(), b'current')

    def test_changed_file_id_restores_inactive_predecessor(self):
        source = self.base/'history/inactive-one/Project/Drawings/current.pdf'
        source.parent.mkdir(parents=True)
        self.current.rename(source)
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,first_id,active) VALUES('p','d','42','current.pdf','{}','first',0)")
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,first_id) VALUES('p','d2','42','current.pdf','{}','first')")
            self.ledger.db.execute('UPDATE files SET active=0,path=?', (source.relative_to(self.base).as_posix(),))
            self.ledger.db.execute("INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,active) VALUES('p','f2','d2','42','current.pdf','{}','pending',1)")
        self.assertEqual(maintenance.apply(self.ledger)['restored'], 1)
        self.assertEqual(self.current.read_bytes(), b'current')
        row = self.ledger.one("SELECT * FROM files WHERE id='f2'")
        self.assertEqual(row['status'], 'pending')
        self.assertIsNone(row['path'])
        self.assertEqual(layout.plan(self.ledger)['moves'], [])
        self.assertIn(('p', 'f2'), layout.local_update_keys(self.ledger))

    def test_newly_archived_untracked_content_is_organized_in_same_plan(self):
        source = self.base/'downloads/Project/Drawings/unknown.pdf'
        source.write_bytes(b'unknown')
        proposal = maintenance.apply(self.ledger)
        self.assertEqual(proposal['history_files'], 1)
        self.assertEqual((self.base/'history/Project/Drawings/unknown.pdf').read_bytes(), b'unknown')
        self.assertFalse(any(p.name.startswith('untracked-') for p in (self.base/'history').iterdir()))
        self.assertEqual(maintenance.plan(self.ledger)['moves'], [])

    def test_main_document_predecessor_is_not_restored_as_same_named_attachment(self):
        source = self.base/'history/inactive-one/Project/Drawings/current.pdf'
        source.parent.mkdir(parents=True)
        self.current.rename(source)
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,first_id,active) VALUES('p','d','42','current.pdf','{}','first',0)")
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,first_id) VALUES('p','d2','42','current.pdf','{}','first')")
            self.ledger.db.execute("UPDATE files SET active=0,path=?,relation='Document'", (source.relative_to(self.base).as_posix(),))
            for identifier, relation in [('f2', 'Document'), ('attachment', 'Attachment')]:
                self.ledger.db.execute("INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,active,relation) VALUES('p',?,'d2','42','current.pdf','{}','pending',1,?)", (identifier, relation))
        proposal = maintenance.plan(self.ledger)
        restored = [m for m in proposal['moves'] if m.get('current_file_id')]
        self.assertEqual([m['current_file_id'] for m in restored], ['f2'])
        self.assertEqual(proposal['warnings'], [])

    def test_ambiguous_predecessor_is_not_assigned_to_arbitrary_current_file(self):
        source = self.base/'history/inactive-one/Project/Drawings/current.pdf'
        source.parent.mkdir(parents=True)
        self.current.rename(source)
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,first_id,active) VALUES('p','d','42','current.pdf','{}','first',0)")
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,first_id) VALUES('p','d2','42','current.pdf','{}','first')")
            self.ledger.db.execute('UPDATE files SET active=0,path=?', (source.relative_to(self.base).as_posix(),))
            for identifier in ('f2', 'f3'):
                self.ledger.db.execute("INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,active) VALUES('p',?,'d2','42','current.pdf','{}','pending',1)", (identifier,))
        proposal = maintenance.plan(self.ledger)
        self.assertEqual(proposal['restored'], 0)
        self.assertEqual(len(proposal['warnings']), 1)
        self.assertTrue(source.exists())

    def test_hash_mismatch_does_not_publish_wrong_content(self):
        source = self.add()
        self.pending()
        (self.base/source).write_bytes(b'bad')
        with self.assertRaises(layout.LayoutError):
            maintenance.apply(self.ledger)
        self.assertTrue((self.base/source).exists())
        self.assertFalse(self.current.exists())

    def test_interruption_after_publication_resumes_both_versions(self):
        self.add()
        self.add('version-2', data=b'last')
        self.pending()
        publish = layout.publish
        def interrupt(*args):
            publish(*args)
            raise OSError('interrupted after publish')
        with patch('file_layout.publish', side_effect=interrupt):
            with self.assertRaises(OSError):
                maintenance.apply(self.ledger)
        self.assertTrue(maintenance.plan(self.ledger)['recovery'])
        maintenance.apply(self.ledger)
        self.assertEqual(self.current.read_bytes(), b'last')
        rows = self.ledger.all('SELECT path FROM file_history')
        self.assertEqual({(self.base/r['path']).read_bytes() for r in rows}, {b'old', b'last'})

    def test_legacy_history_repair_is_resumed_before_restoration(self):
        self.add()
        self.pending()
        publish = history.publish
        def interrupt(*args):
            publish(*args)
            raise OSError('legacy interruption')
        with patch('history_repair.publish', side_effect=interrupt):
            with self.assertRaises(OSError):
                history.apply(self.ledger)
        self.assertTrue(maintenance.plan(self.ledger)['recovery'])
        maintenance.apply(self.ledger)
        self.assertEqual(self.current.read_bytes(), b'old')

    def test_copied_ledger_without_actual_data_is_not_modified(self):
        self.current.unlink()
        for parent in (self.current.parent, self.current.parent.parent, self.base/'downloads'):
            parent.rmdir()
        before = '\n'.join(self.ledger.db.iterdump())
        with self.assertRaisesRegex(layout.LayoutError, '服务器真实数据目录'):
            maintenance.apply(self.ledger)
        self.assertEqual('\n'.join(self.ledger.db.iterdump()), before)

    def test_root_command_and_legacy_aliases_use_same_read_only_preview(self):
        self.add()
        self.pending()
        before = '\n'.join(self.ledger.db.iterdump())
        launcher = Path(maintenance.__file__).resolve().parents[1]/'main.py'
        for command in ('path_maintenance', 'file_layout', 'history_repair'):
            result = subprocess.run([sys.executable, str(launcher), command, '--base-dir', str(self.base)],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            proposal = json.loads((self.base/'runtime/path_maintenance_preview.json').read_text(encoding='utf-8'))
            self.assertEqual(proposal['restored'], 1)
        self.assertEqual('\n'.join(self.ledger.db.iterdump()), before)
        self.assertFalse(self.current.exists())
