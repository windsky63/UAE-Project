"""Offline end-to-end checks of stage boundaries and cross-day continuation."""
from contextlib import redirect_stdout, nullcontext
import csv
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fusion_client import LOCAL, WindowClosed
from ledger import Ledger
from sync import run_stages
from workflow import Workflow
from scope_review import export_scope_review


def document(identifier):
    return {'id': identifier, 'fileName': identifier + '.pdf', 'reference': identifier,
            'hasContent': 'true', 'hasAttachments': 'false', 'contentSize': '3', 'revision': '0'}


def content(identifier):
    return {'id': identifier, 'filename': identifier + '.pdf', 'readAccessStatus': 'Available',
            'contentSize': '3', 'revision': '0'}


class StagedFixture:
    def setUp(self):
        background=patch('analysis_worker.ensure_background_worker',return_value=None)
        background.start()
        self.addCleanup(background.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base / 'runtime' / 'ledger.sqlite3', base_dir=self.base)
        self.events = []
        self.docs = [document('v1'), document('v2')]
        self.related = {'v1': [content('v1')], 'v2': [content('v2')]}
        self.at = datetime(2026, 10, 20, 3, tzinfo=LOCAL)
        parent = self
        class Client:
            project_id, cabinet_id, project_name = 'p', 'r', 'project'
            def folders(self, identifier):
                parent.events.append('tree:' + identifier)
                return [{'id': 'f', 'text': 'Folder'}] if identifier == 'r' else []
            def document_page(self, identifier, start, limit):
                parent.events.append('page:' + str(start))
                return len(parent.docs), parent.docs[start:start + limit]
            def related(self, identifier, virtual=None):
                parent.events.append('related:' + identifier)
                return parent.related[identifier]
            def download(self, item, target, progress=None, verified=None):
                parent.events.append('download:' + item['id'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(b'pdf')
                return {'path': str(target), 'bytes': 3, 'sha256': hashlib.sha256(b'pdf').hexdigest()}
        self.client = Client()
        # These positive download tests model a previously confirmed whole-project scope.
        with self.ledger.db:
            self.ledger.db.execute('INSERT INTO download_scopes VALUES(?,?,?)', ('p', '{"mode":"all"}', self.at.isoformat()))

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def run_stage(self, action='auto', **limits):
        args = SimpleNamespace(action=action, rescan=False, retry_failed=True,
                               max_files=None, max_folders=None, max_associations=None)
        for name, value in limits.items():
            setattr(args, name, value)
        with patch('sync.check_window'), patch('workflow.now', side_effect=lambda: self.at), patch('sync.export_csv'), redirect_stdout(io.StringIO()):
            return run_stages(self.client, self.ledger, args)

    def state(self):
        return dict(self.ledger.one('SELECT * FROM workflows'))

    def reopen(self):
        self.ledger.close()
        self.ledger = Ledger(self.base / 'runtime' / 'ledger.sqlite3', base_dir=self.base)

class StagedWorkflowTests(StagedFixture, unittest.TestCase):
    def test_initial_associations_resume_then_bulk_skips_all_inventory_across_days(self):
        self.assertEqual(self.run_stage(max_associations=1), ('inventory_incomplete', 2))
        self.assertEqual(self.state()['phase'], 'associations')
        snapshot = self.state()['snapshot_id']
        self.assertFalse(any(e.startswith('download:') for e in self.events))
        self.events.clear()
        self.reopen()
        self.at = datetime(2026, 10, 21, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(max_files=1), ('download_pending', 2))
        self.assertEqual(self.events, ['related:v2', 'download:v1'])
        self.assertEqual(self.state()['stage'], 'bulk_download')
        self.assertEqual(self.state()['snapshot_id'], snapshot)
        report = next((self.base / 'exports').glob('项目文件体量-*.json'))
        self.assertFalse(json.loads(report.read_text(encoding='utf-8'))['volume']['total_is_lower_bound'])
        self.docs.append(document('v3'))  # Changes are intentionally deferred during initial bulk download.
        self.related['v3'] = [content('v3')]
        self.events.clear()
        self.reopen()
        self.at = datetime(2026, 10, 22, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(max_files=1), ('done', 0))
        self.assertEqual(self.events, ['download:v2'])
        self.assertEqual(self.state()['stage'], 'maintenance')
        self.assertEqual(self.state()['phase'], 'idle')
        self.events.clear()
        self.run_stage()
        self.assertEqual(self.events, [])  # No redundant full scan on the completion day.
        self.at = datetime(2026, 10, 23, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertIn('tree:r', self.events)
        self.assertIn('related:v3', self.events)
        self.assertEqual([e for e in self.events if e.startswith('download:')], ['download:v3'])

    def test_download_only_refuses_incomplete_inventory(self):
        with self.assertRaisesRegex(RuntimeError, '完整清单'):
            self.run_stage('download')
        self.assertEqual(self.events, [])

    def test_scan_only_creates_baseline_then_download_only_skips_metadata(self):
        self.assertEqual(self.run_stage('scan'), ('done', 0))
        self.assertFalse(any(e.startswith('download:') for e in self.events))
        self.events.clear()
        self.assertEqual(self.run_stage('download'), ('done', 0))
        self.assertEqual(self.events, ['download:v1', 'download:v2'])

    def test_legacy_partial_inventory_is_adopted_without_rescanning(self):
        self.ledger.root(self.client, rescan=True)
        from sync import scan, refresh_associations
        with patch('sync.check_window'), redirect_stdout(io.StringIO()):
            self.assertTrue(scan(self.client, self.ledger))
            self.assertFalse(refresh_associations(self.client, self.ledger, 1))
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM workflows')['n'], 0)
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertEqual(self.events, ['related:v2', 'download:v1', 'download:v2'])

    def test_failed_downloads_cannot_advance_to_maintenance(self):
        self.run_stage('scan')
        with patch.object(self.client, 'download', side_effect=RuntimeError('failed')), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(), ('download_pending', 2))
        self.assertEqual(self.state()['stage'], 'bulk_download')
        self.assertEqual(self.ledger.one("SELECT COUNT(*) n FROM files WHERE status='failed' AND attempts=3")['n'], 2)

    def test_deadline_keeps_bulk_stage_and_next_day_does_not_rescan(self):
        self.run_stage('scan')
        self.events.clear()
        with patch.object(self.client, 'download', side_effect=WindowClosed('deadline')):
            with self.assertRaises(WindowClosed):
                self.run_stage()
        self.assertEqual(self.state()['stage'], 'bulk_download')
        self.assertEqual(self.state()['phase'], 'download')
        self.at = datetime(2026, 10, 21, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertFalse(any(e.startswith(('tree:', 'page:', 'related:')) for e in self.events))

    def test_missing_completed_file_is_requeued_without_metadata_requests(self):
        self.run_stage()
        item = self.ledger.one("SELECT path FROM files WHERE id='v1'")
        from paths import resolve_path
        resolve_path(item['path'], self.base).unlink()
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertEqual(self.events, ['download:v1'])

    def test_maintenance_finds_new_attachment_without_main_document_change(self):
        self.run_stage()
        self.at = datetime(2026, 10, 21, 3, tzinfo=LOCAL)
        self.related['v1'].append(content('extra'))
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertEqual([e for e in self.events if e.startswith('download:')], ['download:extra'])
        self.events.clear()
        self.run_stage()
        self.assertEqual(self.events, [])

    def test_initial_directory_checkpoint_survives_reopen(self):
        self.assertEqual(self.run_stage(max_folders=1), ('inventory_incomplete', 2))
        self.assertEqual(self.state()['phase'], 'catalogue')
        self.reopen()
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertNotIn('tree:r', self.events)

    def test_new_snapshot_and_workflow_checkpoint_commit_together(self):
        workflow = Workflow(self.ledger, self.client)
        original = self.ledger.root
        def failed_root(client, **kwargs):
            callback = kwargs['checkpoint']
            def fail(snapshot, cycle):
                callback(snapshot, cycle)
                raise RuntimeError('simulated failure before commit')
            return original(client, rescan=True, checkpoint=fail)
        with patch.object(self.ledger, 'root', side_effect=failed_root):
            with self.assertRaises(RuntimeError):
                workflow.begin_inventory()
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM snapshots')['n'], 0)
        self.assertIsNone(self.state()['snapshot_id'])

    def test_daily_association_round_resumes_across_days_without_restarting(self):
        self.run_stage()
        self.at = datetime(2026, 10, 21, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(max_associations=1), ('inventory_incomplete', 2))
        snapshot = self.state()['snapshot_id']
        self.events.clear()
        self.reopen()
        self.at = datetime(2026, 10, 22, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertEqual(self.events, ['related:v2'])
        self.assertEqual(self.state()['snapshot_id'], snapshot)

    def test_failed_association_retries_only_unfinished_records(self):
        self.run_stage(max_associations=1)
        with patch.object(self.client, 'related', side_effect=RuntimeError('network')), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(), ('inventory_incomplete', 2))
        self.assertEqual(self.state()['phase'], 'associations')
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertEqual(self.events, ['related:v2', 'download:v1', 'download:v2'])

    def test_restricted_files_are_reported_and_never_marked_successful(self):
        self.related['v2'][0]['readAccessStatus'] = 'Restricted'
        self.assertEqual(self.run_stage(), ('done', 0))
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v2'")['status'], 'restricted')
        self.assertEqual(self.state()['stage'], 'maintenance')
        self.assertEqual([e for e in self.events if e.startswith('download:')], ['download:v1'])

    def test_ready_snapshot_is_rejected_if_document_checks_are_incomplete(self):
        self.run_stage('scan')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE documents SET related_state='pending' WHERE id='v1'")
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError, '完整清单'):
            self.run_stage('download')
        self.assertEqual(self.events, [])


class ScopeReviewTests(StagedFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        with self.ledger.db:
            self.ledger.db.execute('DELETE FROM download_scopes')

    def test_complete_inventory_stops_without_any_download(self):
        self.assertEqual(self.run_stage(), ('awaiting_scope', 0))
        self.assertEqual(self.state()['stage'], 'scope_review')
        self.assertEqual(self.state()['phase'], 'review')
        self.assertTrue(any(e.startswith('related:') for e in self.events))
        self.assertFalse(any(e.startswith('download:') for e in self.events))

        self.assertTrue((self.base / 'exports/目录范围汇总.csv').is_file())
        self.assertTrue((self.base / 'exports/人工范围确认说明.txt').is_file())
        self.events.clear()
        self.reopen()
        self.at = datetime(2026, 10, 21, 3, tzinfo=LOCAL)
        self.assertEqual(self.run_stage(), ('awaiting_scope', 0))
        self.assertEqual(self.events, [])

    def test_waiting_state_blocks_direct_download_command(self):
        self.run_stage()
        self.events.clear()
        self.assertEqual(self.run_stage('download'), ('awaiting_scope', 0))
        self.assertEqual(self.events, [])

    def test_resume_repair_stops_for_scope_review_without_downloading(self):
        self.assertEqual(self.run_stage('resume-scan'), ('awaiting_scope', 0))
        self.assertEqual(self.state()['stage'], 'scope_review')
        self.assertFalse(any(e.startswith('download:') for e in self.events))

    def test_restart_waits_offline_without_constructing_http_client(self):
        self.run_stage()
        from sync import main
        factory = lambda: Ledger(self.base / 'runtime' / 'ledger.sqlite3', base_dir=self.base)
        with patch('sync.RunLock', return_value=nullcontext()), patch('sync.RUNTIME', self.base / 'runtime'), patch('sync.Ledger', side_effect=factory), patch('sync.signal.signal'), patch('sync.export_csv'), patch('sync.FusionClient', side_effect=AssertionError('must not log in')), redirect_stdout(io.StringIO()):
            self.assertEqual(main(['auto']), 0)
            self.assertEqual(main(['download']), 0)

    def test_scope_summary_counts_shared_attachments_once_per_subtree(self):
        self.related['v1'].append(content('shared'))
        self.related['v2'].append(content('shared'))
        self.assertEqual(self.run_stage(), ('awaiting_scope', 0))
        with (self.base / 'exports/目录范围汇总.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = {row['目录ID']: row for row in csv.DictReader(stream)}
        self.assertEqual(rows['r']['含子目录文档记录数'], '2')
        self.assertEqual(rows['r']['含子目录原件附件去重数'], '3')
        self.assertEqual(rows['r']['含子目录文件总字节'], '9')
        self.assertEqual(rows['f']['本目录原件附件去重数'], '3')

    def test_existing_bulk_state_without_scope_is_stopped(self):
        self.run_stage()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE workflows SET stage='bulk_download',phase='download'")
        self.events.clear()
        self.assertEqual(self.run_stage(), ('awaiting_scope', 0))
        self.assertEqual(self.events, [])

    def test_rescan_can_refresh_inventory_but_cannot_start_downloading(self):
        self.run_stage()
        self.events.clear()
        self.assertEqual(self.run_stage(rescan=True), ('awaiting_scope', 0))
        self.assertIn('tree:r', self.events)
        self.assertFalse(any(e.startswith('download:') for e in self.events))


class ResumeRepairTests(StagedFixture, unittest.TestCase):
    def test_stale_version_is_replaced_but_completed_associations_are_preserved(self):
        self.run_stage(max_associations=1)
        cycle = self.state()['cycle']
        replacement = document('v3')
        replacement['reference'] = 'v2'
        replacement['revision'] = '2'
        self.docs[1] = replacement
        self.related['v3'] = [content('v3')]
        self.events.clear()
        self.assertEqual(self.run_stage('resume-scan'), ('done', 0))
        self.assertIn('tree:r', self.events)
        self.assertIn('related:v3', self.events)
        self.assertNotIn('related:v1', self.events)
        self.assertNotIn('related:v2', self.events)
        self.assertFalse(any(e.startswith('download:') for e in self.events))
        self.assertEqual(self.state()['cycle'], cycle)
        self.assertEqual(self.ledger.one("SELECT active FROM documents WHERE id='v2'")['active'], 0)

    def test_permanent_failure_does_not_block_other_documents_and_is_exported(self):
        original = self.client.related
        calls = []
        def related(identifier, virtual=None):
            calls.append(identifier)
            if identifier == 'v1':
                raise RuntimeError('platform failed')
            return original(identifier, virtual)
        with patch.object(self.client, 'related', side_effect=related), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage('resume-scan'), ('inventory_incomplete', 2))
        self.assertEqual(calls, ['v1', 'v1', 'v2', 'v1', 'v1'])
        self.assertEqual(self.ledger.one("SELECT related_state FROM documents WHERE id='v2'")['related_state'], 'done')
        self.assertEqual(self.ledger.one("SELECT status FROM snapshots")['status'], 'catalogued')
        self.assertFalse(any(e.startswith('download:') for e in self.events))
        from sync import export_csv
        with patch('sync.HOME', self.base), redirect_stdout(io.StringIO()):
            export_csv(self.ledger)
        with (self.base / 'exports/关联核对异常.csv').open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), 1)
        self.assertIn('platform failed', rows[0]['关联核对错误'])
        from openpyxl import load_workbook
        workbook = load_workbook(self.base / 'exports/文件台账.xlsx', read_only=True, data_only=True)
        try:
            rows = workbook['完整字段'].iter_rows(min_row=5, values_only=True)
            fields = next(rows)
            failures = [row for values in rows if (row := dict(zip(fields, values)))['关联文件状态'] == 'failed']
        finally:
            workbook.close()
        self.assertIn('platform failed', failures[0]['错误'])

    def test_transient_failure_recovers_in_first_pass(self):
        original = self.client.related
        calls = []
        def related(identifier, virtual=None):
            calls.append(identifier)
            if len(calls) == 1:
                raise RuntimeError('temporary')
            return original(identifier, virtual)
        with patch.object(self.client, 'related', side_effect=related), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage('resume-scan'), ('done', 0))
        self.assertEqual(calls, ['v1', 'v1', 'v2'])
        self.assertFalse(any(e.startswith('download:') for e in self.events))

    def test_failed_record_can_recover_after_other_records_finish(self):
        original = self.client.related
        calls = []
        def related(identifier, virtual=None):
            calls.append(identifier)
            if identifier == 'v1' and len(calls) <= 2:
                raise RuntimeError('temporary')
            return original(identifier, virtual)
        with patch.object(self.client, 'related', side_effect=related), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage('resume-scan'), ('done', 0))
        self.assertEqual(calls, ['v1', 'v1', 'v2', 'v1'])
        self.assertEqual(self.ledger.one("SELECT COUNT(*) n FROM documents WHERE related_state='failed'")['n'], 0)

    def test_authentication_errors_are_fatal_without_retrying_other_records(self):
        import requests
        response = requests.Response()
        response.status_code = 401
        with patch.object(self.client, 'related', side_effect=requests.HTTPError('expired', response=response)) as related, patch('sync.time.sleep'):
            with self.assertRaises(requests.HTTPError):
                self.run_stage('resume-scan')
            self.assertEqual(related.call_count, 1)

    def test_session_expiry_is_not_treated_as_a_document_error(self):
        from fusion_client import SessionExpiredError
        with patch.object(self.client, 'related', side_effect=SessionExpiredError('expired')) as related:
            with self.assertRaises(SessionExpiredError):
                self.run_stage('resume-scan')
            self.assertEqual(related.call_count, 1)

    def test_time_window_closes_during_retry_pause_without_losing_checkpoint(self):
        self.run_stage(max_associations=1)
        from sync import refresh_associations
        with patch.object(self.client, 'related', side_effect=RuntimeError('temporary')), patch('sync.check_window', side_effect=[None, None, WindowClosed('deadline')]), redirect_stdout(io.StringIO()):
            with self.assertRaises(WindowClosed):
                refresh_associations(self.client, self.ledger)
        self.assertEqual(self.ledger.one("SELECT related_state FROM documents WHERE id='v1'")['related_state'], 'done')
        self.assertEqual(self.ledger.one("SELECT related_state FROM documents WHERE id='v2'")['related_state'], 'pending')

    def test_document_limit_also_bounds_failed_records(self):
        with patch.object(self.client, 'related', side_effect=RuntimeError('failed')) as related, patch('sync.time.sleep'):
            self.assertEqual(self.run_stage('resume-scan', max_associations=1), ('inventory_incomplete', 2))
            self.assertEqual(related.call_count, 2)


class ApprovedBulkPlanTests(StagedFixture, unittest.TestCase):
    def prepare_plan(self):
        with self.ledger.db:
            self.ledger.db.execute('DELETE FROM download_scopes')
        self.related['v1'].append(content('attachment'))
        self.assertEqual(self.run_stage(max_associations=1), ('inventory_incomplete', 2))
        with self.ledger.db:
            self.ledger.db.execute("UPDATE documents SET related_state='failed',error='old version' WHERE id='v2'")
        self.plan = {'project':'p', 'mode':'all', 'approved':True,
                     'expected_counts': {'folders':2, 'documents':2, 'content_files':2},
                     'deferred_associations':[{'version_id':'v2','reference':'v2'}]}
        self.save_plan()
        self.events.clear()

    def save_plan(self):
        (self.base/'approved_download_plan.json').write_text(json.dumps(self.plan), encoding='utf-8')

    def test_approved_baseline_downloads_across_days_without_metadata_and_defers_failure(self):
        self.prepare_plan()
        snapshot = self.state()['snapshot_id']
        self.assertEqual(self.run_stage(max_files=1), ('download_pending', 2))
        self.assertEqual(self.events, ['download:v1'])
        self.assertEqual(self.state()['stage'], 'bulk_download')
        self.reopen()
        self.at = datetime(2026,10,21,3,tzinfo=LOCAL)
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done',0))
        self.assertEqual(self.events, ['download:attachment'])
        self.assertEqual(self.state()['stage'], 'maintenance')
        self.assertEqual(self.ledger.one("SELECT related_state FROM documents WHERE id='v2'")[0], 'failed')
        self.assertEqual(self.ledger.one('SELECT status FROM snapshots WHERE id=?',(snapshot,))[0], 'catalogued')
        self.events.clear()
        self.assertEqual(self.run_stage(), ('done',0))
        self.assertEqual(self.events, [])
        self.at = datetime(2026,10,22,3,tzinfo=LOCAL)
        self.assertEqual(self.run_stage(), ('done',0))
        self.assertIn('tree:r', self.events)
        self.assertIn('related:v2', self.events)
        self.assertIn('download:v2', self.events)
        self.assertEqual(self.ledger.one("SELECT related_state FROM documents WHERE id='v2'")[0], 'done')

    def test_explicit_download_uses_approved_exception_without_rescanning(self):
        self.prepare_plan()
        self.assertEqual(self.run_stage('download'), ('done',0))
        self.assertEqual(self.events, ['download:v1','download:attachment'])

    def test_wrong_baseline_is_rejected_before_network_and_without_approval(self):
        self.prepare_plan()
        self.plan['expected_counts']['content_files'] = 3
        self.save_plan()
        with self.assertRaisesRegex(RuntimeError,'基线检查'):
            self.run_stage()
        self.assertEqual(self.events, [])
        self.assertEqual(self.ledger.one('SELECT COUNT(*) FROM download_scopes')[0],0)

    def test_undeclared_or_pending_association_cannot_be_ignored(self):
        self.prepare_plan()
        for state in ('pending', 'failed'):
            with self.ledger.db:
                self.ledger.db.execute('UPDATE documents SET related_state=? WHERE id=?',(state,'v1'))
            with self.assertRaisesRegex(RuntimeError,'基线检查'):
                self.run_stage()
        self.assertEqual(self.events, [])

    def test_unfinished_folder_blocks_plan(self):
        self.prepare_plan()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE folders SET list_state='pending' WHERE id='f'")
        with self.assertRaisesRegex(RuntimeError,'基线检查'):
            self.run_stage()
        self.assertEqual(self.events, [])

    def test_content_without_checked_parent_blocks_plan(self):
        self.prepare_plan()
        with self.ledger.db:
            self.ledger.db.execute("DELETE FROM document_files WHERE file_id='attachment'")
        with self.assertRaisesRegex(RuntimeError,'基线检查'):
            self.run_stage()
        self.assertEqual(self.events, [])

    def test_new_cycle_cannot_reuse_initial_exception(self):
        self.prepare_plan()
        self.run_stage(max_files=1)
        with self.ledger.db:
            self.ledger.db.execute('UPDATE refresh_cycles SET cycle=cycle+1')
        self.events.clear()
        with self.assertRaisesRegex(RuntimeError,'完整清单检查'):
            self.run_stage()
        self.assertEqual(self.events, [])

    def test_wrong_project_plan_cannot_unlock_review(self):
        self.prepare_plan()
        self.plan['project'] = 'other'
        self.save_plan()
        with patch.object(self.client, 'related', side_effect=RuntimeError('still failed')), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(), ('inventory_incomplete',2))
        self.assertEqual(self.ledger.one('SELECT COUNT(*) FROM download_scopes')[0],0)
        self.assertFalse(any(e.startswith('download:') for e in self.events))


class DailyInventoryBulkTests(StagedFixture, unittest.TestCase):
    def enable_daily(self):
        (self.base/'approved_download_plan.json').write_text(json.dumps({
            'project':'p','mode':'all','approved':True,'daily_inventory_before_download':True}),encoding='utf-8')

    def test_next_day_bulk_inventory_finds_attachment_then_resumes_download(self):
        self.run_stage('scan')
        self.enable_daily()
        self.run_stage(max_files=1)
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.related['v1'].append(content('new-attachment'))
        self.events.clear()
        self.assertEqual(self.run_stage(max_files=1),('download_pending',2))
        self.assertIn('tree:r',self.events)
        self.assertIn('related:v1',self.events)
        self.assertIn('related:v2',self.events)
        self.assertEqual(self.state()['cycle'],2)
        self.assertEqual(self.state()['stage'],'bulk_download')
        from reporting import system_report_stats
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_new'],{'files':1,'bytes':3})
        self.events.clear()
        self.reopen()
        self.assertEqual(self.run_stage(),('done',0))
        self.assertEqual(self.events,['download:new-attachment'])

    def test_partial_daily_inventory_resumes_its_round_and_does_not_download_early(self):
        self.run_stage('scan')
        self.run_stage(max_files=1)
        self.enable_daily()
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.events.clear()
        self.assertEqual(self.run_stage(max_associations=1),('inventory_incomplete',2))
        snapshot=self.state()['snapshot_id']
        self.assertFalse(any(e.startswith('download:') for e in self.events))
        self.events.clear()
        self.reopen()
        self.assertEqual(self.run_stage(),('done',0))
        self.assertEqual(self.state()['snapshot_id'],snapshot)
        self.assertNotIn('tree:r',self.events)
        self.assertNotIn('related:v1',self.events)
        self.assertIn('related:v2',self.events)

    def test_scheduled_daily_round_increments_even_an_incomplete_legacy_cycle(self):
        self.run_stage(max_associations=1)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE workflows SET stage='bulk_download',phase='download'")
        self.enable_daily()
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.events.clear()
        self.assertEqual(self.run_stage(),('done',0))
        self.assertEqual(self.state()['cycle'],2)
        self.assertIn('related:v1',self.events)
        self.assertIn('related:v2',self.events)


if __name__ == '__main__':
    unittest.main()
