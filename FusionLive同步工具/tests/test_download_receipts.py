"""Real download publication with fake HTTP; all files live in temporary fixtures."""
import os
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from fusion_client import FusionClient
from ledger import Ledger
from sync import download
import file_layout
from tests import test_download_replacement as replacement


class DownloadReceiptTests(unittest.TestCase):
    def setUp(self):
        self.case = replacement.ReplacementTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.scan([replacement.doc(), replacement.doc('new', firstVersionId='new-first', fileName='new.pdf')],
                       [replacement.content(), replacement.content('new', filename='new.pdf')])
        self.client = FusionClient('offline-fixture', '')
        self.addCleanup(self.client.http.close)
        self.client.project_id = 'p'
        self.client.rendition_security = False
        response = Mock(headers={'Content-Length': '3'})
        response.iter_content.return_value = [b'new']
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        for guard in (patch.object(self.client, 'request', return_value=context),
                      patch.object(self.client, 'check_access_window'),
                      patch('fusion_client.RUNTIME', self.case.base/'runtime')):
            guard.start()
            self.addCleanup(guard.stop)
        self.target = file_layout.target_for(self.case.ledger, self.row())

    def row(self):
        return self.case.ledger.one("SELECT * FROM files WHERE id='new'")

    def interrupt_publication(self, after=True):
        replace = os.replace
        def interrupt(source, target):
            if Path(target) == self.target:
                self.assertEqual(self.case.ledger.one('SELECT count(*) FROM download_receipts')[0], 1)
                self.assertNotEqual(self.row()['status'], 'done')
                if after:
                    replace(source, target)
                raise KeyboardInterrupt()
            return replace(source, target)
        with patch('fusion_client.os.replace', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                download(self.client, self.case.ledger)

    def test_published_file_recovers_after_reopening_database_without_redownload(self):
        self.interrupt_publication()
        self.assertEqual(self.target.read_bytes(), b'new')
        timestamp = self.case.ledger.one('SELECT completed FROM download_receipts')[0]
        self.case.ledger.close()
        self.case.ledger = Ledger(self.case.base/'runtime/ledger.sqlite3', base_dir=self.case.base)
        self.addCleanup(self.case.ledger.close)
        self.case.ledger.start_run('download')
        with patch.object(self.client, 'download', side_effect=AssertionError('redownload forbidden')):
            download(self.client, self.case.ledger)
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.row()['downloaded_at'], timestamp)
        self.assertEqual(self.row()['download_date_source'], '下载完成实录（中断恢复）')
        self.assertEqual(self.case.ledger.one('SELECT count(*) FROM download_receipts')[0], 0)
        self.assertEqual(self.case.old.read_bytes(), b'old')

    def test_interruption_before_publication_retries_missing_file(self):
        self.interrupt_publication(after=False)
        self.assertFalse(self.target.exists())
        download(self.client, self.case.ledger)
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.target.read_bytes(), b'new')
        self.assertEqual(self.case.ledger.one('SELECT count(*) FROM download_receipts')[0], 0)

    def test_modified_published_file_is_preserved_as_conflict(self):
        self.interrupt_publication()
        self.target.write_bytes(b'bad')
        with patch.object(self.client, 'download', side_effect=AssertionError('must preserve conflict')):
            download(self.client, self.case.ledger)
        self.assertEqual(self.target.read_bytes(), b'bad')
        self.assertEqual(self.row()['status'], 'failed')
        self.assertEqual(self.row()['retryable'], 0)
        self.assertEqual(self.case.ledger.one('SELECT count(*) FROM download_receipts')[0], 1)

    def test_changed_platform_version_is_not_adopted(self):
        self.interrupt_publication()
        self.case.scan([replacement.doc(), replacement.doc('new', firstVersionId='new-first', revision='1')],
                       [replacement.content(), replacement.content('new', filename='new.pdf', revision='1')])
        self.assertEqual(self.case.ledger.recover_download_receipts(), 0)
        self.assertNotEqual(self.row()['status'], 'done')
        self.assertEqual(self.target.read_bytes(), b'new')
        self.assertEqual(self.case.ledger.one('SELECT count(*) FROM download_receipts')[0], 1)

    def test_normal_download_commits_and_removes_receipt(self):
        download(self.client, self.case.ledger)
        self.assertEqual(self.row()['status'], 'done')
        self.assertEqual(self.case.ledger.one('SELECT count(*) FROM download_receipts')[0], 0)

    def test_receipt_write_failure_never_publishes_untracked_file(self):
        with patch.object(self.case.ledger, 'save_download_receipt', side_effect=OSError('fixture database unavailable')):
            download(self.client, self.case.ledger)
        self.assertFalse(self.target.exists())
        self.assertNotEqual(self.row()['status'], 'done')
        self.assertEqual(list((self.case.base/'runtime/download_parts').glob('*.part')), [])

    def test_unknown_existing_file_without_receipt_is_never_adopted(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(b'new')
        with patch.object(self.client, 'download', side_effect=AssertionError('must not overwrite')):
            download(self.client, self.case.ledger)
        self.assertEqual(self.row()['status'], 'failed')
        self.assertEqual(self.row()['retryable'], 0)
        self.assertEqual(self.target.read_bytes(), b'new')

    def test_verified_update_recovers_staging_then_archives_previous_version(self):
        self.case.updated()
        staged = self.case.base/'runtime/download_ready'
        replace = os.replace
        def interrupt(source, target):
            result = replace(source, target)
            if Path(target).is_relative_to(staged):
                raise KeyboardInterrupt()
            return result
        with patch('fusion_client.os.replace', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                download(self.client, self.case.ledger, max_files=1)
        self.assertEqual(self.case.old.read_bytes(), b'old')
        with patch.object(self.client, 'download', side_effect=AssertionError('redownload forbidden')):
            download(self.client, self.case.ledger)
        self.assertEqual(self.case.old.read_bytes(), b'new')
        history = self.case.ledger.one('SELECT path FROM file_history')[0]
        self.assertEqual((self.case.base/history).read_bytes(), b'old')
