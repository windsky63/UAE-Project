"""A forbidden content file must not block the remaining download queue."""
from contextlib import redirect_stdout
import io
import unittest
from unittest.mock import patch

import requests

from sync import download
from tests import test_download_replacement as replacement


class ForbiddenDownloadTests(unittest.TestCase):
    def setUp(self):
        self.case = replacement.ReplacementTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.case.scan(
            [replacement.doc(),
             replacement.doc('blocked', firstVersionId='blocked-first', fileName='blocked.pdf'),
             replacement.doc('later', firstVersionId='later-first', fileName='later.pdf')],
            [replacement.content(), replacement.content('blocked', filename='blocked.pdf'),
             replacement.content('later', filename='later.pdf')])

    def error(self, status):
        response = requests.Response()
        response.status_code = status
        return requests.HTTPError(f'{status} fixture error', response=response)

    def row(self, identifier):
        return self.case.ledger.one('SELECT * FROM files WHERE id=?', (identifier,))

    def test_forbidden_file_is_skipped_once_and_retried_on_next_run(self):
        calls = []

        def receive(item, target, **kwargs):
            calls.append(item['id'])
            if item['id'] == 'blocked':
                raise self.error(403)
            return self.case.receive(item, target, **kwargs)

        self.case.client.download.side_effect = receive
        output = io.StringIO()
        with redirect_stdout(output):
            download(self.case.client, self.case.ledger)
        self.assertEqual(calls, ['blocked', 'later'])
        blocked = self.row('blocked')
        self.assertEqual((blocked['status'], blocked['attempts'], blocked['retryable']), ('failed', 1, 1))
        self.assertIn('403', blocked['error'])
        self.assertFalse((self.case.base / blocked['target_path']).exists())
        self.assertEqual(self.row('later')['status'], 'done')
        self.assertEqual(self.case.old.read_bytes(), b'old')
        self.assertIn('HTTP 403', output.getvalue())
        self.case.client.download.side_effect = self.case.receive
        download(self.case.client, self.case.ledger)
        self.assertEqual(self.row('blocked')['status'], 'done')
        self.assertEqual(self.row('blocked')['attempts'], 2)

    def test_all_forbidden_files_finish_without_repeating_queue(self):
        self.case.client.download.side_effect = self.error(403)
        download(self.case.client, self.case.ledger)
        self.assertEqual(self.case.client.download.call_count, 2)
        for identifier in ('blocked', 'later'):
            self.assertEqual((self.row(identifier)['status'], self.row(identifier)['attempts']), ('failed', 1))

    def test_http_errors_skip_file_and_continue_independent_downloads(self):
        for status in (401, 418, 429):
            with self.subTest(status=status):
                with self.case.ledger.db:
                    self.case.ledger.db.execute("UPDATE files SET status='pending',attempts=0 WHERE id IN ('blocked','later')")
                self.case.client.download.reset_mock()
                calls = []
                def receive(item, target, **kwargs):
                    calls.append(item['id'])
                    if item['id'] == 'blocked':
                        raise self.error(status)
                    return self.case.receive(item, target, **kwargs)
                self.case.client.download.side_effect = receive
                # Remove prior subtest's successful bytes before resetting its row.
                if self.row('later')['path']:
                    (self.case.base/self.row('later')['path']).unlink(missing_ok=True)
                download(self.case.client, self.case.ledger)
                self.assertEqual(calls, ['blocked', 'later'])
                self.assertEqual(self.row('blocked')['status'], 'failed')
                self.assertEqual(self.row('blocked')['attempts'], 1)
                self.assertEqual(self.row('later')['status'], 'done')

    def test_history_identity_conflict_does_not_block_independent_downloads(self):
        with self.case.ledger.db:
            self.case.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
                SELECT project,id,path,bytes,?,'fixture' FROM files WHERE id='v1' ''',
                (replacement.hashlib.sha256(b'different version').hexdigest(),))
        self.case.client.download.side_effect = self.case.receive
        self.assertEqual(download(self.case.client, self.case.ledger), 2)
        self.assertEqual(self.row('later')['status'], 'done')
        self.assertEqual(self.case.old.read_bytes(), b'old')
        history = self.case.ledger.one('SELECT * FROM file_history')
        # An exhaustive scan found no bytes for this history hash. Its stale
        # local reference clears, while the version/hash evidence is retained.
        self.assertIsNone(history['path'])
        self.assertNotEqual(history['sha256'], self.row('v1')['sha256'])

    def test_one_bad_publication_does_not_delay_other_publications(self):
        import file_layout as layout
        from paths import relative_path
        other = layout.target_for(self.case.ledger, self.row('later'))
        other.parent.mkdir(parents=True, exist_ok=True)
        other.write_bytes(b'old')
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE files SET status='done',path=?,bytes=3,sha256=? WHERE id='later'",
                                       (relative_path(other, self.case.base), replacement.hashlib.sha256(b'old').hexdigest()))
        self.case.scan(
            [replacement.doc(revision='1'), replacement.doc('later', revision='1', firstVersionId='later-first', fileName='later.pdf')],
            [replacement.content(revision='1'), replacement.content('later', revision='1', filename='later.pdf')])
        original = layout.plan
        def plan(ledger, **kwargs):
            if kwargs.get('keys') and ('p','v1') in kwargs['keys'] and self.row('v1')['status']=='done':
                raise layout.LayoutError('fixture publication conflict')
            return original(ledger, **kwargs)
        self.case.client.download.side_effect = self.case.receive
        with patch('file_layout.plan', side_effect=plan):
            self.assertEqual(download(self.case.client, self.case.ledger), 2)
        self.assertTrue(self.row('v1')['path'].startswith('runtime/download_ready/'))
        self.assertEqual(self.row('later')['status'], 'done')
        self.assertFalse(self.row('later')['path'].startswith('runtime/'))
        self.assertEqual(other.read_bytes(), b'new')
        archived = self.case.ledger.one("SELECT path FROM file_history WHERE file_id='later'")['path']
        self.assertTrue(archived.startswith('history/'))
        self.assertEqual((self.case.base/archived).read_bytes(), b'old')
        self.assertEqual(self.case.old.read_bytes(), b'old')
        self.assertFalse((self.case.base/'runtime/layout.pending').exists())

    def test_completion_log_failure_does_not_redownload_or_stop_queue(self):
        self.case.client.download.side_effect = self.case.receive
        with patch('sync.record_download', side_effect=OSError('fixture log unavailable')):
            self.assertEqual(download(self.case.client, self.case.ledger), 2)
        self.assertEqual(self.case.calls, ['blocked','later'])
        self.assertEqual(self.row('blocked')['status'], 'done')
        self.assertEqual(self.row('later')['status'], 'done')
        self.assertIn('后处理暂缓', self.row('blocked')['error'])

    def test_existing_exact_version_is_reconciled_without_http(self):
        import file_layout as layout
        target = layout.target_for(self.case.ledger,self.row('blocked'))
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b'new')
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE refresh_cycles SET status='running'")
            self.case.ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                SELECT project,id,fingerprint,'downloads/missing.pdf',3,?,'fixture' FROM files WHERE id='blocked' ''',
                (replacement.hashlib.sha256(b'new').hexdigest(),))
        self.case.client.download.side_effect = self.case.receive
        download(self.case.client,self.case.ledger)
        self.assertEqual(self.case.calls,['later'])
        self.assertEqual(self.row('blocked')['status'],'done')
        self.assertEqual(target.read_bytes(),b'new')

    def test_normal_download_does_not_schedule_or_walk_conflict_recovery(self):
        self.case.client.download.side_effect = self.case.receive
        with patch('local_reconcile.DiskReconciler.schedule',side_effect=AssertionError('scheduled normal download')), \
             patch('local_reconcile.DiskReconciler._entries',side_effect=AssertionError('scanned normal download')):
            self.assertEqual(download(self.case.client,self.case.ledger),2)

    def test_old_target_exists_failure_requeues_after_unknown_blocker_is_archived(self):
        import file_layout as layout
        target = layout.target_for(self.case.ledger,self.row('blocked'))
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b'unknown previous content')
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE files SET status='failed',retryable=0,error='下载目标已存在，未覆盖' WHERE id='blocked'")
        self.case.client.download.side_effect = self.case.receive
        self.assertEqual(download(self.case.client,self.case.ledger),2)
        self.assertEqual(self.row('blocked')['status'],'done')
        self.assertEqual(target.read_bytes(),b'new')
        copies = list((self.case.base/'history').rglob('*'))
        self.assertTrue(any(p.is_file() and p.read_bytes()==b'unknown previous content' for p in copies))

    def test_bulk_reference_checks_never_run_history_cleanup(self):
        documents = [replacement.doc()]
        contents = [replacement.content()]
        for index in range(23):
            identifier = 'bulk-%02d'%index
            documents.append(replacement.doc(identifier,firstVersionId=identifier+'-first',fileName=identifier+'.pdf'))
            contents.append(replacement.content(identifier,filename=identifier+'.pdf'))
        self.case.scan(documents,contents)
        self.case.client.download.side_effect = self.case.receive
        boundaries = []
        from sync import maintain_local_references
        def maintain(ledger):
            boundaries.append(len(self.case.calls))
            maintain_local_references(ledger)
        with patch('sync.maintain_local_references',side_effect=maintain), \
             patch('history_retention.prune_history',side_effect=AssertionError('automatic history deletion')):
            self.assertEqual(download(self.case.client,self.case.ledger),23)
        self.assertEqual(boundaries,[20,23])

    def test_failed_migration_recovery_still_downloads_to_separate_staging(self):
        from paths import relative_path
        legacy = self.case.base/'downloads/legacy.pdf'
        self.case.old.rename(legacy)
        legacy.write_bytes(b'bad')  # Same size, wrong hash: preserve for inspection.
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE files SET path=? WHERE id='v1'",
                                        (relative_path(legacy, self.case.base),))
        calls = []
        def receive(item, target, **kwargs):
            calls.append(item['id'])
            self.assertTrue(str(target).startswith(str(self.case.base/'runtime/download_ready')))
            target.write_bytes(b'new')
            return dict(bytes=3,sha256=replacement.hashlib.sha256(b'new').hexdigest(),path=str(target))
        self.case.client.download.side_effect = receive
        self.assertEqual(download(self.case.client, self.case.ledger), 2)
        self.assertEqual(calls, ['blocked','later'])
        self.assertEqual(legacy.read_bytes(), b'bad')
        self.assertTrue((self.case.base/'runtime/layout.pending').exists())
        for identifier in calls:
            row = self.row(identifier)
            self.assertEqual(row['status'], 'done')
            self.assertTrue(row['path'].startswith('runtime/download_ready/'))
            self.assertEqual((self.case.base/row['path']).read_bytes(), b'new')
        # Reopening a batch keeps the broken journal without downloading twice.
        self.case.client.download.reset_mock()
        self.assertEqual(download(self.case.client, self.case.ledger), 0)
        self.case.client.download.assert_not_called()

    def test_layout_conflict_keeps_both_files_and_downloads_independent_items(self):
        from paths import relative_path
        legacy = self.case.base / 'downloads/legacy.pdf'
        self.case.old.rename(legacy)
        self.case.old.write_bytes(b'occupied')
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE files SET path=? WHERE id='v1'",
                                        (relative_path(legacy, self.case.base),))
            # An incomplete refresh must preserve untracked destination bytes.
            self.case.ledger.db.execute("UPDATE refresh_cycles SET status='running'")
        # This fixture deliberately has different bytes at the canonical path.
        def receive(item, target, **kwargs):
            self.assertEqual(self.case.old.read_bytes(), b'occupied')
            target.write_bytes(b'new')
            return dict(bytes=3, sha256=replacement.hashlib.sha256(b'new').hexdigest(), path=str(target))
        self.case.client.download.side_effect = receive
        output = io.StringIO()
        with redirect_stdout(output):
            completed = download(self.case.client, self.case.ledger)
        self.assertEqual(completed, 2, output.getvalue())
        self.assertEqual(legacy.read_bytes(), b'old')
        self.assertEqual(self.case.old.read_bytes(), b'occupied')
        self.assertEqual(self.row('v1')['path'], relative_path(legacy, self.case.base))
        self.assertEqual(self.row('blocked')['status'], 'done')
        self.assertEqual(self.row('later')['status'], 'done')
        self.assertIn('按依赖组准备', output.getvalue())

    def test_conflicting_predecessor_group_is_skipped_before_network_transfer(self):
        from paths import relative_path
        legacy = self.case.base / 'downloads/legacy.pdf'
        self.case.old.rename(legacy)
        self.case.old.write_bytes(b'occupied')
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE files SET path=? WHERE id='v1'",
                                        (relative_path(legacy, self.case.base),))
        import file_layout as layout
        blocked_source = self.case.base / 'downloads/blocked-legacy.pdf'
        blocked_source.write_bytes(b'previous')
        blocked_target = layout.target_for(self.case.ledger, self.row('blocked'))
        blocked_target.write_bytes(b'occupied-blocked')
        with self.case.ledger.db:
            self.case.ledger.db.execute("UPDATE refresh_cycles SET status='running'")
            self.case.ledger.db.execute("UPDATE files SET path=? WHERE id='blocked'",
                                        (relative_path(blocked_source, self.case.base),))
        calls = []
        def receive(item, target, **kwargs):
            calls.append(item['id'])
            target.write_bytes(b'new')
            return dict(bytes=3, sha256=replacement.hashlib.sha256(b'new').hexdigest(), path=str(target))
        self.case.client.download.side_effect = receive
        self.assertEqual(download(self.case.client, self.case.ledger), 1)
        self.assertEqual(calls, ['later'])
        self.assertEqual((self.row('blocked')['status'], self.row('blocked')['retryable']), ('failed', 0))
        self.assertIn('迁移目标已有文件', self.row('blocked')['error'])
        self.assertEqual(legacy.read_bytes(), b'old')
        self.assertEqual(self.case.old.read_bytes(), b'occupied')
        self.assertEqual(blocked_source.read_bytes(), b'previous')
        self.assertEqual(blocked_target.read_bytes(), b'occupied-blocked')


if __name__ == '__main__':
    unittest.main()
