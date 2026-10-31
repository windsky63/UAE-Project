import json
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
import io
from unittest.mock import Mock, patch

from analysis_settings import DEFAULTS, load, validate


class AnalysisSettingsTests(unittest.TestCase):
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
                 patch('analysis_worker.cached_snapshot', return_value=({}, {'weld_table_file':'fixture.csv','welds':[]}, None)), \
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
