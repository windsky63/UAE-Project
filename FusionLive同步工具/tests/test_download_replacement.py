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
    def test_startup_checks_presence_once_and_repair_reuses_same_scan(self):
        import reporting
        with patch('reporting.local_presence', wraps=reporting.local_presence) as presence, \
             patch('reporting.download_directory_stats', wraps=reporting.download_directory_stats) as directory:
            workflow = Workflow(self.ledger, self.client)
            self.assertEqual(presence.call_count, 0)
            self.assertEqual(directory.call_count, 0)
            with patch('workflow.resolve_path', side_effect=AssertionError('repeated file probe')):
                self.assertEqual(workflow.repair_missing_downloads(), 0)
            self.assertEqual(presence.call_count, 1)
            self.assertEqual(directory.call_count, 0)
            self.old.unlink()
            self.assertEqual(workflow.repair_missing_downloads(), 1)
            self.assertEqual(self.ledger.one('SELECT status FROM files')[0], 'pending')

    def test_busy_publication_returns_with_verified_bytes_and_stays_unfinished(self):
        from analysis_worker import worker_lock
        self.updated()
        guard = None
        original = self.receive
        def receive(item, target, **kwargs):
            nonlocal guard
            result = original(item, target, **kwargs)
            guard = worker_lock(self.base/'runtime/file_analysis')
            self.assertTrue(guard.__enter__())
            return result
        self.client.download.side_effect = receive
        # A bounded stop probe catches an accidental busy loop without weakening business timeouts.
        calls = 0
        def check():
            nonlocal calls
            calls += 1
            if calls > 30:
                raise AssertionError('publication waited instead of deferring')
        try:
            with patch('sync.check_window', side_effect=check):
                self.assertEqual(download(self.client, self.ledger), 1)
            self.assertEqual(self.old.read_bytes(), b'old')
            self.assertTrue(self.ledger.one('SELECT path FROM files')[0].startswith('runtime/download_ready/'))
            workflow = Workflow(self.ledger, self.client)
            self.assertFalse(workflow.finish_download())
            self.assertEqual(workflow.state['phase'], 'download')
        finally:
            if guard:
                guard.__exit__(None, None, None)
        with patch.object(self.client, 'download', side_effect=AssertionError('redownload')):
            download(self.client, self.ledger)
        self.assertEqual(self.old.read_bytes(), b'new')

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
        self.assertEqual(self.client.download.call_count, 1)
        self.assertEqual(self.old.read_bytes(), b'old')
        download(self.client, self.ledger)
        self.assertEqual(self.client.download.call_count, 2)
        self.assertEqual(self.old.read_bytes(), b'new')

    def test_busy_parser_defers_update_and_downloads_next_file_first(self):
        from analysis_worker import worker_lock
        self.scan([doc(revision='1'), doc('later', firstVersionId='later-first', fileName='later.pdf')],
                  [content(revision='1'), content('later', filename='later.pdf')])
        guard = None

        def receive(item, target, **kwargs):
            nonlocal guard
            if item['id'] == 'later':
                self.assertEqual(self.old.read_bytes(), b'old')
                saved = self.ledger.one("SELECT * FROM files WHERE id='v1'")
                self.assertEqual(saved['status'], 'done')
                self.assertTrue(saved['path'].startswith('runtime/download_ready/'))
                self.assertEqual((self.base/saved['path']).read_bytes(), b'new')
                self.assertFalse((self.base/'runtime/layout.pending').exists())
                self.assertTrue((self.base/'runtime/file_analysis/stop.request').exists())
                guard.__exit__(None, None, None)
                guard = None
            result = self.receive(item, target, **kwargs)
            if item['id'] == 'v1':
                guard = worker_lock(self.base/'runtime/file_analysis')
                self.assertTrue(guard.__enter__())
            return result

        self.client.download.side_effect = receive
        try:
            with patch('sync.priority_download_keys', return_value=[('p', 'v1')]):
                download(self.client, self.ledger)
        finally:
            if guard is not None:
                guard.__exit__(None, None, None)
        self.assertEqual(self.calls, ['v1', 'later'])
        self.assertEqual(self.old.read_bytes(), b'new')
        self.assertFalse((self.base/'runtime/file_analysis/stop.request').exists())
        self.assertFalse((self.base/'runtime/layout.analysis_pause.json').exists())

    def test_deferred_update_survives_stop_and_reopen_without_redownload(self):
        from analysis_worker import worker_lock
        self.scan([doc(revision='1'), doc('later', firstVersionId='later-first', fileName='later.pdf')],
                  [content(revision='1'), content('later', filename='later.pdf')])
        guard = None

        def receive(item, target, **kwargs):
            nonlocal guard
            if item['id'] == 'later':
                raise WindowClosed('fixture safe stop')
            result = self.receive(item, target, **kwargs)
            guard = worker_lock(self.base/'runtime/file_analysis')
            self.assertTrue(guard.__enter__())
            return result

        self.client.download.side_effect = receive
        try:
            with patch('sync.priority_download_keys', return_value=[('p', 'v1')]):
                with self.assertRaises(WindowClosed):
                    download(self.client, self.ledger)
            self.assertEqual(self.old.read_bytes(), b'old')
            saved = self.ledger.one("SELECT * FROM files WHERE id='v1'")
            self.assertEqual((self.base/saved['path']).read_bytes(), b'new')
        finally:
            if guard is not None:
                guard.__exit__(None, None, None)
        self.ledger.close()
        self.ledger = Ledger(self.base/'runtime/ledger.sqlite3', base_dir=self.base)
        self.addCleanup(self.ledger.close)
        def receive_after_restart(item, target, **kwargs):
            Path(target).write_bytes(b'new')
            return dict(bytes=3, sha256=hashlib.sha256(b'new').hexdigest(), path=str(target))

        self.client.download.side_effect = receive_after_restart
        self.client.download.reset_mock()
        download(self.client, self.ledger)
        self.assertEqual(self.client.download.call_count, 1)
        self.assertEqual(self.client.download.call_args.args[0]['id'], 'later')
        self.assertEqual(self.old.read_bytes(), b'new')
