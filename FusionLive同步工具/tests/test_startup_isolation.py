"""Exercise actual offline child processes; never start login or download workers."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import preflight


class StartupIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.home = Path(directory.name)
        self.messages = []

    def check(self, code, **kwargs):
        return preflight.check_before_login(home=self.home, log=self.messages.append,
            _command=[sys.executable, '-u', '-c', code], **kwargs)

    def test_native_dependencies_load_on_child_main_thread_when_called_from_monitor_thread(self):
        failures = []
        def monitor():
            try:
                self.check("import threading,numpy,openpyxl; assert threading.current_thread() is threading.main_thread(); print('native imports on main thread')", timeout=30)
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=monitor)
        worker.start()
        worker.join(timeout=35)
        self.assertFalse(worker.is_alive())
        self.assertEqual(failures,[])
        self.assertTrue(any('native imports on main thread' in m for m in self.messages))

    def test_failed_child_preserves_error_details_and_blocks_login(self):
        with self.assertRaisesRegex(RuntimeError,'退出码4'):
            self.check("print('dependency failed'); raise SystemExit(4)")
        self.assertTrue(any('dependency failed' in m for m in self.messages))
        self.assertFalse(any('准备启动同步' in m for m in self.messages))

    def test_hung_native_check_times_out_and_reaps_only_its_child(self):
        actual = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            child = actual(*args, **kwargs)
            children.append(child)
            return child
        with patch('preflight.subprocess.Popen',side_effect=spawn):
            with self.assertRaisesRegex(RuntimeError,'超时'):
                self.check("import time; print('loading numpy'); time.sleep(30)",timeout=.4)
        self.assertEqual(len(children),1)
        self.assertIsNotNone(children[0].poll())
        self.assertTrue(any('loading numpy' in m for m in self.messages))

    def test_stop_before_check_never_starts_a_process(self):
        with patch('preflight.subprocess.Popen') as spawn:
            with self.assertRaises(preflight.CheckCancelled):
                self.check('raise AssertionError()',cancelled=lambda:True)
        spawn.assert_not_called()

    def test_stop_during_dependency_load_returns_promptly(self):
        started = time.monotonic()
        with self.assertRaises(preflight.CheckCancelled):
            self.check('import time; time.sleep(30)',
                cancelled=lambda: time.monotonic()-started >= .3,timeout=20)
        self.assertLess(time.monotonic()-started,5)

    def test_large_output_does_not_fill_a_pipe_or_drop_unterminated_last_line(self):
        self.check("import sys; print('x'*100000); sys.stdout.write('final line')",timeout=10)
        self.assertTrue(any(m.endswith('final line') for m in self.messages))
        self.assertTrue(any(len(m)>100000 for m in self.messages))

    def test_progress_callback_reports_the_active_check_stage(self):
        phases = []
        report = preflight.run_checks(self.home,runtime=False,progress=phases.append)
        self.assertFalse(report['ok'])
        self.assertTrue(any('SHA256' in p for p in phases))
