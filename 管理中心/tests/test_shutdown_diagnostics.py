"""Read-only stop checks do not create runtime state or signal processes."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from diagnose_shutdown import diagnose


class ShutdownDiagnosticsTests(unittest.TestCase):
    def test_missing_project_is_reported_without_creating_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('psutil.process_iter', return_value=[]):
                result = diagnose(root)
            self.assertEqual(len(result['locks']), 4)
            self.assertTrue(all(not row['occupied_or_inaccessible'] for row in result['locks']))
            self.assertEqual(list(root.iterdir()), [])

    def test_existing_stop_marker_and_logs_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / 'FusionLive同步工具/runtime'
            runtime.mkdir(parents=True)
            marker = runtime / 'scheduler.exit.request'
            marker.write_text('stop requested', encoding='utf-8')
            progress = runtime / 'file_analysis/progress.log'
            progress.parent.mkdir()
            progress.write_text('后台解压、解析已退出', encoding='utf-8')
            before = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
            with patch('psutil.process_iter', return_value=[]):
                result = diagnose(root)
            after = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
            self.assertEqual(before, after)
            self.assertTrue(result['stop_requests'][0]['exists'])
            self.assertIn('已退出', result['logs'][0]['tail'])
