import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from analysis_worker import ProgressLog


class RuntimeLoggingTests(unittest.TestCase):
    def test_progress_log_has_identical_console_and_disk_with_final_summary(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'progress.log'
            output = ProgressLog(path)
            console = io.StringIO()
            with patch('sys.stdout', console), patch.object(output.gate, 'clock', return_value=0):
                for i in range(100):
                    output(f'开始文件：{i}.pdf')
                output('失败原因：bad.pdf')
                output('本次运行结束：done 99')
            self.assertEqual(console.getvalue(), path.read_text(encoding='utf-8'))
            self.assertIn('99 条', console.getvalue())
            self.assertIn('失败原因：bad.pdf', console.getvalue())
            self.assertLess(len(console.getvalue().splitlines()), 6)


if __name__ == '__main__':
    unittest.main()
