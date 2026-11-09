"""Offline regression tests for stopped downloads and isolated emergency actions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager
import os
import scheduler
import sync
from briefing_schedule import DailyBriefing
import subprocess


class StopRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir()

    def test_stop_saves_status_and_summary_without_full_excel_export(self):
        (self.runtime / 'stop.request').touch()
        ledger = Mock()
        with patch('sync.RUNTIME', self.runtime), patch('sync.write_summary') as summary, patch('sync.export_csv') as export:
            sync.finish_run(ledger, 7, 'stopped', True, None)
        ledger.end_run.assert_called_once_with(7, 'stopped', True, None)
        summary.assert_called_once_with(ledger, 7)
        export.assert_not_called()
        ledger.clean_partial_downloads.assert_not_called()

    def test_late_scheduled_child_keeps_stop_markers_and_never_opens_ledger(self):
        (self.runtime / 'scheduler.exit.request').touch()
        (self.runtime / 'stop.request').touch()
        with patch('sync.RUNTIME', self.runtime), patch('sync.RunLock'), patch('sync.Ledger') as ledger, \
             patch.dict(os.environ, FUSIONLIVE_SCHEDULED='1'):
            self.assertEqual(sync.main(['auto']), 2)
        ledger.assert_not_called()
        self.assertTrue((self.runtime / 'stop.request').exists())

    def test_emergency_only_targets_this_install_lock_holder(self):
        import psutil
        ours, unrelated, stopper = Mock(pid=101), Mock(pid=102), Mock(pid=103)
        for process, script, extra in ((ours, str(scheduler.CODE/'sync.py'), []),
                                      (unrelated, str(self.base/'other.py'), []),
                                      (stopper, str(scheduler.CODE/'scheduler.py'), ['--stop'])):
            process.info = dict(name='python.exe', cmdline=['python', script, *extra])
            process.open_files.return_value = [Mock(path=str(self.runtime/'sync.lock'))]
        with patch('scheduler.RUNTIME', self.runtime), patch('scheduler.HOME', self.base), \
             patch('scheduler.request_full_stop'), patch('scheduler.log'), \
             patch('psutil.process_iter', return_value=[ours, unrelated, stopper]), \
             patch('psutil.wait_procs', return_value=([ours], [])), patch('scheduler.wait_for_full_exit', return_value=True):
            self.assertEqual(scheduler.force_stop(), 0)
        ours.kill.assert_called_once()
        unrelated.kill.assert_not_called()
        stopper.kill.assert_not_called()
        self.assertTrue((self.runtime/'login.paused').exists())

    def test_stop_wait_timeout_reports_failure_without_killing(self):
        @contextmanager
        def occupied(*args):
            yield False
        with patch('scheduler.worker_lock', occupied), patch('scheduler.log') as log:
            self.assertFalse(scheduler.wait_for_full_exit(timeout=0))
        self.assertIn('仍未退出', log.call_args.args[0])

    def test_report_child_escalates_termination_with_bounded_wait(self):
        service = DailyBriefing(self.base, Mock(), analysis_start=Mock())
        service.state = {'date': '2026-10-29'}
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('fixture', 10), 0]
        service.process = child
        service.close()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertIsNone(service.process)

    def test_scheduled_images_do_not_pre_generate_daily_text_even_with_legacy_flag(self):
        import briefing
        database = self.runtime / 'ledger.sqlite3'
        database.touch()
        ledger = Mock()
        with patch('ledger.Ledger', return_value=ledger) as factory, \
             patch('reporting.try_write_push_text') as text, \
             patch('briefing.generate', return_value=dict(workbook='fixture.xlsx', images=[], ready=True)) as report:
            briefing.main(['--database', str(database), '--daily-text'])
        factory.assert_not_called()
        text.assert_not_called()
        ledger.close.assert_not_called()
        report.assert_called_once()

    def test_daily_text_failure_does_not_cancel_pipeline_report(self):
        import briefing
        database = self.runtime / 'ledger.sqlite3'
        database.touch()
        with patch('ledger.Ledger', side_effect=OSError('fixture unavailable')), \
             patch('briefing.generate', return_value=dict(workbook='fixture.xlsx', images=[], ready=True)) as report:
            briefing.main(['--database', str(database), '--daily-text'])
        report.assert_called_once()
