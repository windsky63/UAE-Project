import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from unittest.mock import Mock, patch

from analysis_settings import DEFAULTS, load, validate


class AnalysisSettingsTests(unittest.TestCase):
    def test_automatic_worker_waits_past_old_idle_timeout_and_scans_at_saved_interval(self):
        import analysis_worker as worker
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base / 'runtime/wechat_push.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'analysis_poll_seconds': 300,
                                        'analysis_auto_idle_exit_seconds': 120}), encoding='utf-8')
            child = Mock()
            child.poll.return_value = None
            with patch.object(worker, '_child', None), \
                 patch.object(worker, 'analysis_paused_for_schedule', return_value=False), \
                 patch.object(worker.subprocess, 'Popen', return_value=child) as spawn:
                worker.ensure_background_worker(base, base / 'runtime/ledger.sqlite3')
                worker.ensure_background_worker(base, base / 'runtime/ledger.sqlite3')
                spawn.assert_called_once()
                command = spawn.call_args.args[0]
                self.assertEqual(command[command.index('--idle-exit') + 1], '0')
            clock = [0]
            def sleep(seconds):
                clock[0] += seconds
                if clock[0] >= 301:
                    (base / 'runtime/file_analysis/stop.request').write_text('test stop')
            queue = Mock()
            queue.snapshot_pending.return_value = False
            queue.discover_if_changed.return_value = 0
            queue.process_next.return_value = False
            queue.counts.return_value = {}
            queue.db.execute.return_value.fetchone.return_value = (None,)
            with patch.object(worker, 'AnalysisQueue', return_value=queue), \
                 patch.object(worker, 'analysis_paused_for_schedule', return_value=False), \
                 patch.object(worker.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(worker.time, 'sleep', side_effect=sleep), redirect_stdout(io.StringIO()):
                self.assertEqual(worker.main(command[3:]), 0)
            self.assertEqual(clock[0], 301)
            self.assertEqual(queue.discover_if_changed.call_count, 2)
            queue.close.assert_called_once_with(defer_exports=False)

    def test_manual_cli_uses_saved_intervals(self):
        from analysis_worker import main
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            path = base/'runtime/wechat_push.json'
            path.parent.mkdir()
            path.write_text(json.dumps({'analysis_poll_seconds':60,'analysis_idle_log_seconds':90,
                                       'analysis_idle_exit_seconds':300}))
            queue = Mock()
            queue.discover.return_value = 0
            queue.process_next.return_value = False
            queue.counts.return_value = {}
            output = io.StringIO()
            with patch('analysis_worker.AnalysisQueue',return_value=queue), \
                 patch('analysis_worker.analysis_paused_for_schedule',return_value=False), \
                 patch('snapshot_service.cached_snapshot', return_value=({}, {'weld_table_file':'fixture.csv','welds':[]}, None)), \
                 redirect_stdout(output):
                self.assertEqual(main(['--base-dir',str(base),'--database',str(base/'runtime/ledger.sqlite3')]),0)
            self.assertIn('扫描间隔 60 秒',output.getvalue())
            self.assertIn('空闲提示间隔 90 秒',output.getvalue())
            self.assertIn('空闲退出 300 秒',output.getvalue())

    def test_stop_remains_available_if_saved_settings_are_invalid(self):
        from analysis_worker import main
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            base = Path(directory)
            path = base/'runtime/wechat_push.json'
            path.parent.mkdir()
            path.write_text('{"analysis_poll_seconds":0}')
            self.assertEqual(main(['--base-dir',str(base),'--stop']), 0)
            self.assertTrue((base/'runtime/file_analysis/stop.request').exists())

    def test_backward_compatible_defaults_and_persisted_values(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load(directory), DEFAULTS)
            path = Path(directory) / 'runtime/wechat_push.json'
            path.parent.mkdir()
            values = {**DEFAULTS, 'analysis_poll_seconds': 60, 'analysis_idle_log_seconds': 120}
            path.write_text(json.dumps(values))
            self.assertEqual(load(directory), values)

    def test_invalid_intervals_rejected(self):
        for value in (True, '60', -1, 0, float('nan'), float('inf'), 86401):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate({'analysis_poll_seconds': value})
        self.assertEqual(validate({'analysis_idle_exit_seconds': 0})['analysis_idle_exit_seconds'], 0)
