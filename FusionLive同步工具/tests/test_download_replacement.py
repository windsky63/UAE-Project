"""Offline version replacement, ordering, and interruption recovery."""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from ledger import Ledger
from fusion_client import WindowClosed
from sync import download
from workflow import Workflow
from paths import relative_path
import file_layout as layout
from tests.test_inventory import doc, content


class ReplacementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base/'runtime/ledger.sqlite3', base_dir=self.base)
        self.addCleanup(self.ledger.close)
        self.client = Mock(project_id='p', cabinet_id='r', project_name='project')
        for target in ('sync.check_window', 'sync.notify_download_completed',
                       'analysis_worker.resume_background_analysis', 'sync.time.sleep'):
            guard = patch(target)
            guard.start()
            self.addCleanup(guard.stop)
        self.scan([doc()], [content()])
        row = self.ledger.one('SELECT * FROM files')
        self.old = layout.target_for(self.ledger, row)
        self.old.parent.mkdir(parents=True, exist_ok=True)
        self.old.write_bytes(b'old')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='done',path=?,bytes=3,sha256=?",
                                   (relative_path(self.old, self.base), hashlib.sha256(b'old').hexdigest()))
        self.calls = []

    def scan(self, documents, items):
        self.ledger.root(self.client, rescan=True)
        root = self.ledger.one("SELECT * FROM folders WHERE id='r'")
        self.ledger.save_children('p', root, [{'id': 'c', 'text': 'Folder'}])
        folder = self.ledger.one("SELECT * FROM folders WHERE id='c'")
        self.ledger.save_children('p', folder, [])
        self.ledger.save_page('p', 'c', len(documents), documents, len(documents), True)
        self.assertTrue(self.ledger.finish_catalogue('p'))
        for document, item in zip(documents, items):
            row = self.ledger.one('SELECT * FROM documents WHERE id=?', (document['id'],))
            self.ledger.save_related(row, [item])
        self.assertTrue(self.ledger.finish_refresh('p'))

    def updated(self):
        self.scan([doc(revision='1')], [content(revision='1')])

    def receive(self, item, target, progress=None, verified=None):
        self.assertEqual(self.old.read_bytes(), b'old')
        self.calls.append(item['id'])
        target = Path(target)
        target.write_bytes(b'new')
        return dict(bytes=3, sha256=hashlib.sha256(b'new').hexdigest(), path=str(target))

    def test_failed_update_keeps_old_file_in_downloads(self):
        self.updated()
        self.client.download.side_effect = WindowClosed('stop')
        with self.assertRaises(WindowClosed):
            download(self.client, self.ledger)
        self.assertEqual(self.old.read_bytes(), b'old')
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')['path'], relative_path(self.old, self.base))

    def test_new_files_precede_updates_and_success_archives_old(self):
        self.scan([doc(revision='1'), doc('new', firstVersionId='new-first', fileName='new.pdf')],
                  [content(revision='1'), content('new', filename='new.pdf')])
        self.client.download.side_effect = self.receive
        download(self.client, self.ledger)
        self.assertEqual(self.calls, ['new', 'v1'])
        self.assertEqual(self.old.read_bytes(), b'new')
        history = self.ledger.one('SELECT path FROM file_history')['path']
        self.assertTrue(history.startswith('history/'))
        self.assertEqual((self.base/history).read_bytes(), b'old')

    def test_changed_platform_id_preserves_old_until_success(self):
        self.scan([doc('v2', revision='1')], [content('v2', revision='1')])
        layout.prepare_downloads(self.ledger)
        self.assertEqual(self.old.read_bytes(), b'old')
        self.assertIn(('p', 'v2'), layout.local_update_keys(self.ledger))
        self.client.download.side_effect = self.receive
        download(self.client, self.ledger)
        self.assertEqual(self.old.read_bytes(), b'new')
        history = self.ledger.one("SELECT path FROM files WHERE id='v1'")['path']
        self.assertTrue(history.startswith('history/'))
        self.assertEqual((self.base/history).read_bytes(), b'old')

    def test_interrupted_publishing_recovers_without_redownload(self):
        self.updated()
        self.client.download.side_effect = self.receive
        with patch('file_layout.publish', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                download(self.client, self.ledger)
        Workflow(self.ledger, self.client).repair_missing_downloads()
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v1'")['status'], 'done')
        download(self.client, self.ledger)
        self.assertEqual(self.calls, ['v1'])
        self.assertEqual(self.old.read_bytes(), b'new')
        history = self.ledger.one('SELECT path FROM file_history')['path']
        self.assertEqual((self.base/history).read_bytes(), b'old')

    def test_permanent_download_error_is_not_retried(self):
        self.updated()
        response = requests.Response()
        response.status_code = 404
        self.client.download.side_effect = requests.HTTPError('not found', response=response)
        download(self.client, self.ledger)
        download(self.client, self.ledger)
        self.assertEqual(self.client.download.call_count, 1)
        self.assertEqual(self.old.read_bytes(), b'old')
        workflow = Workflow(self.ledger, self.client)
        self.assertEqual(workflow.unfinished(), 0)

    def test_restored_historical_file_stays_until_new_download_finishes(self):
        import path_maintenance
        self.updated()
        archived = self.base/'history/version-1/Project/old.pdf'
        archived.parent.mkdir(parents=True)
        self.old.rename(archived)
        with self.ledger.db:
            self.ledger.db.execute('UPDATE file_history SET path=?', (relative_path(archived, self.base),))
        self.assertEqual(path_maintenance.apply(self.ledger)['restored'], 1)
        self.assertEqual(self.old.read_bytes(), b'old')
        self.client.download.side_effect = self.receive
        download(self.client, self.ledger)
        self.assertEqual(self.old.read_bytes(), b'new')
        history = self.ledger.one('SELECT path FROM file_history')['path']
        self.assertTrue(history.startswith('history/'))
        self.assertEqual((self.base/history).read_bytes(), b'old')

    def test_missing_predecessor_is_new_download_and_does_not_reference_new_bytes(self):
        self.updated()
        self.old.unlink()
        def receive(item, target, progress=None, verified=None):
            self.assertEqual(Path(target), self.old)
            Path(target).write_bytes(b'new')
            return dict(bytes=3, sha256=hashlib.sha256(b'new').hexdigest(), path=str(target))
        self.client.download.side_effect = receive
        download(self.client, self.ledger)
        self.assertIsNone(self.ledger.one('SELECT path FROM file_history')['path'])
        self.assertEqual(layout.plan(self.ledger)['moves'], [])

    def test_transient_download_error_still_retries(self):
        self.updated()
        def receive(item, target, progress=None, verified=None):
            if self.client.download.call_count == 1:
                raise OSError('temporary')
            return self.receive(item, target, progress)
        self.client.download.side_effect = receive
        download(self.client, self.ledger)
        self.assertEqual(self.client.download.call_count, 2)
        self.assertEqual(self.old.read_bytes(), b'new')
