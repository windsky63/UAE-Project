import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from runtime_logging import BoundedLog, EventGate
from reporting import Tee
from analysis_worker import ProgressLog


class RuntimeLoggingTests(unittest.TestCase):
    def test_busy_files_are_sampled_and_suppression_totals_are_exact(self):
        clock = [0]
        gate = EventGate(clock=lambda: clock[0])
        self.assertEqual(gate.accept('下载完成：first.pdf'), ['下载完成：first.pdf'])
        for i in range(10000):
            self.assertEqual(gate.accept(f'下载完成：{i}.pdf'), [])
        clock[0] = 30
        events = gate.accept('下载完成：last.pdf')
        self.assertIn('10000 条', events[0])
        self.assertEqual(events[1], '下载完成：last.pdf')
        self.assertEqual(gate.drain(), [])

    def test_errors_and_stop_are_immediate_while_success_stats_are_sampled(self):
        gate = EventGate(clock=lambda: 0)
        for message in ('下载失败：a.pdf', '解析程序异常：bad', '收到停止请求', '任务已退出'):
            self.assertEqual(gate.accept(message), [message])
            self.assertEqual(gate.accept(message), [message])
        message = '文件结束：a.pdf；失败 0'
        self.assertEqual(gate.accept(message), [message])
        self.assertEqual(gate.accept(message), [])
        self.assertIn('1 条', gate.drain()[0])
        message = '文件结果：管线记录 1（含待核对冲突，非去重总量）'
        self.assertEqual(gate.accept(message), [message])
        self.assertEqual(gate.accept(message), [])

    def test_idle_has_a_longer_interval_and_state_is_bounded(self):
        clock = [0]
        gate = EventGate(clock=lambda: clock[0])
        gate.accept('空闲等待：pending 0')
        clock[0] = 30
        self.assertEqual(gate.accept('空闲等待：pending 0'), [])
        clock[0] = 300
        self.assertEqual(len(gate.accept('空闲等待：pending 0')), 2)
        for i in range(1000):
            gate.accept(str(i))
        self.assertLessEqual(len(gate.entries), 128)

    def test_rotation_limits_utf8_bytes_and_preserves_recent_errors(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'progress.log'
            output = BoundedLog(path, max_bytes=256, backups=2)
            for i in range(200):
                output.write(f'记录{i}：中文日志\n')
            output.write('失败原因：important\n')
            files = list(Path(root).glob('*.log'))
            self.assertLessEqual(len(files), 3)
            self.assertTrue(all(p.stat().st_size <= 256 for p in files))
            self.assertIn('important', path.read_text(encoding='utf-8'))
            for p in files:
                p.read_text(encoding='utf-8')
            output.write('中文' * 1000)
            self.assertLessEqual(path.stat().st_size, 256)
            self.assertIn('截断', path.read_text(encoding='utf-8'))

    def test_progress_protocol_survives_fragmented_writes_without_being_logged(self):
        console, disk = io.StringIO(), io.StringIO()
        tee = Tee(console, disk)
        with patch.object(tee.gate, 'clock', return_value=0):
            tee.write('@@PRO')
            tee.write('GRESS {"phase":"download"}\n下载完成：a')
            tee.write('.pdf\n下载完成：b.pdf\n')
            tee.finish()
        self.assertIn('@@PROGRESS {"phase":"download"}', console.getvalue())
        self.assertNotIn('@@PROGRESS', disk.getvalue())
        self.assertIn('1 条', disk.getvalue())
        self.assertEqual(console.getvalue().split('\n', 1)[1], disk.getvalue())

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

    def test_detailed_mode_restores_events_and_stderr_is_never_sampled(self):
        gate = EventGate(detailed=True)
        self.assertEqual(gate.accept('下载完成：a'), ['下载完成：a'])
        self.assertEqual(gate.accept('下载完成：a'), ['下载完成：a'])
        console, disk = io.StringIO(), io.StringIO()
        tee = Tee(console, disk, important=True)
        tee.write('RuntimeError: fixture\nRuntimeError: fixture\n')
        tee.finish()
        self.assertEqual(disk.getvalue().count('RuntimeError'), 2)

    def test_status_json_structure_is_preserved(self):
        console, disk = io.StringIO(), io.StringIO()
        tee = Tee(console, disk)
        text = '{\n  "a": {\n  },\n  "b": {\n  }\n}\n'
        tee.write(text)
        tee.finish()
        self.assertEqual(console.getvalue(), text)
        self.assertEqual(disk.getvalue(), text)


if __name__ == '__main__':
    unittest.main()
