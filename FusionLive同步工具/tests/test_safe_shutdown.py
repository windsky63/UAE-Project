"""Offline shutdown lifecycle checks; no downloads, credentials or WeChat."""
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import analysis_worker
import scheduler

HOME = Path(__file__).resolve().parents[1]


class SafeShutdownTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir()

    def wait_path(self, path, child):
        deadline = time.monotonic() + 10
        while not path.exists():
            if child.poll() is not None:
                self.fail('child exited early: ' + repr(child.communicate()))
            if time.monotonic() >= deadline:
                child.kill()
                self.fail('child did not reach expected state: ' + str(path))
            time.sleep(.03)

    def spawn(self, code):
        env = {key:value for key,value in os.environ.items() if key not in ('FUSIONLIVE_USERNAME', 'FUSIONLIVE_PASSWORD')}
        env['PYTHONPATH']=str(HOME/'app')+os.pathsep+env.get('PYTHONPATH','')
        child = subprocess.Popen([sys.executable, '-B', '-c', code, str(self.base)], cwd=HOME,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
        def cleanup():
            if child.poll() is None:
                child.kill()  # Test fixture only; never a real downloader/parser.
            child.communicate()
        self.addCleanup(cleanup)
        return child

    def test_safe_exit_command_waits_until_analysis_lock_is_released(self):
        code = "import scheduler,sys; from pathlib import Path; scheduler.HOME=Path(sys.argv[1]); scheduler.RUNTIME=scheduler.HOME/'runtime'; sys.argv=['scheduler.py','--stop']; sys.exit(scheduler.main())"
        with analysis_worker.worker_lock(self.runtime / 'file_analysis') as held:
            self.assertTrue(held)
            child = self.spawn(code)
            self.wait_path(self.runtime / 'file_analysis/stop.request', child)
            self.assertIsNone(child.poll())
        stdout, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertTrue((self.runtime / 'scheduler.exit.request').exists())
        self.assertTrue((self.runtime / 'stop.request').exists())

    def test_analysis_finishes_current_job_saves_and_does_not_start_next_job(self):
        code = '''
import sys,time
from pathlib import Path
import analysis_worker as worker
base=Path(sys.argv[1])
class FixtureQueue:
    version='fixture'
    def counts(self): return {'done':1}
    def describe(self,counts): return str(counts)
    def __init__(self,*args,**kwargs): pass
    def recover(self): pass
    def discover(self): return 0
    def process_next(self):
        if (base/'started').exists(): raise AssertionError('next job must not start')
        (base/'started').touch()
        while not (base/'release').exists(): time.sleep(.02)
        (base/'saved').write_text('current job complete')
        return True
    def close(self, *, defer_exports=False): (base/'closed').touch()
worker.AnalysisQueue=FixtureQueue
worker.cached_snapshot=lambda *args,**kwargs: ({},{'weld_table_file':'fixture.csv','welds':[]},None)
sys.exit(worker.main(['--database',str(base/'runtime/ledger.sqlite3'),'--base-dir',str(base),'--watch']))
'''
        child = self.spawn(code)
        self.wait_path(self.base / 'started', child)
        analysis_worker.request_analysis_stop(self.base)
        self.assertIsNone(child.poll())
        (self.base / 'release').touch()
        _, stderr = child.communicate(timeout=10)
        self.assertEqual(child.returncode, 0, stderr)
        self.assertTrue((self.base / 'saved').exists())
        self.assertTrue((self.base / 'closed').exists())

    def test_late_starting_child_cannot_clear_exit_request(self):
        analysis_worker.request_analysis_stop(self.base)
        with patch('analysis_worker.AnalysisQueue', side_effect=AssertionError('queue must not start')):
            self.assertEqual(analysis_worker.main(['--base-dir', str(self.base)]), 0)
        self.assertTrue((self.runtime / 'file_analysis/stop.request').exists())

    def test_auto_start_is_blocked_until_explicit_resume_and_scheduler_restart(self):
        analysis_worker.request_analysis_stop(self.base)
        with patch('analysis_worker._child', None), patch('analysis_worker.subprocess.Popen') as spawn:
            analysis_worker.ensure_background_worker(self.base, self.runtime / 'ledger.sqlite3')
            spawn.assert_not_called()
            analysis_worker.resume_background_analysis(self.base)
            (self.runtime / 'scheduler.exit.request').touch()
            analysis_worker.ensure_background_worker(self.base, self.runtime / 'ledger.sqlite3')
            spawn.assert_not_called()
            (self.runtime / 'scheduler.exit.request').unlink()
            with patch('analysis_worker._child', None):
                analysis_worker.ensure_background_worker(self.base, self.runtime / 'ledger.sqlite3')
            spawn.assert_called_once()

    def test_stop_today_keeps_analysis_running_but_quit_stops_it(self):
        with patch('scheduler.HOME', self.base), patch('scheduler.RUNTIME', self.runtime), patch('scheduler.log'):
            messages, exiting = queue.Queue(), threading.Event()
            messages.put('s')
            scheduler.consume_commands(messages, exiting)
            self.assertFalse(exiting.is_set())
            self.assertFalse((self.runtime / 'file_analysis/stop.request').exists())
            messages.put('q')
            scheduler.consume_commands(messages, exiting)
            self.assertTrue(exiting.is_set())
            self.assertTrue((self.runtime / 'file_analysis/stop.request').exists())


if __name__ == '__main__':
    unittest.main()
