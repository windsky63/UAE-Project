"""Offline shutdown lifecycle checks; no downloads, credentials or WeChat."""
import os
from datetime import datetime, timedelta, timezone
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

HOME = Path(__file__).resolve().parents[1]


class SafeShutdownTests(unittest.TestCase):
    def setUp(self):
        # Exercise cooperative stop outside the configured publication window.
        clock = patch('analysis_worker.now', return_value=datetime(2026, 11, 1, 13, tzinfo=timezone(timedelta(hours=8))))
        clock.start()
        self.addCleanup(clock.stop)
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


    def test_analysis_finishes_current_job_saves_and_does_not_start_next_job(self):
        code = '''
import sys,time
from pathlib import Path
import analysis_worker as worker
from datetime import datetime, timedelta, timezone
worker.now=lambda: datetime(2026,11,1,13,tzinfo=timezone(timedelta(hours=8)))
base=Path(sys.argv[1])
class FixtureQueue:
    version='fixture'
    def counts(self): return {'done':1}
    def describe(self,counts): return str(counts)
    def __init__(self,*args,**kwargs): pass
    def recover(self): pass
    def discover(self): return 0
    def discover_if_changed(self): return self.discover()
    def snapshot_pending(self): return False
    def mark_exported(self): pass
    def process_next(self):
        if (base/'started').exists(): raise AssertionError('next job must not start')
        (base/'started').touch()
        while not (base/'release').exists(): time.sleep(.02)
        (base/'saved').write_text('current job complete')
        return True
    def close(self, *, defer_exports=False): (base/'closed').touch()
worker.AnalysisQueue=FixtureQueue
import snapshot_service
snapshot_service.cached_snapshot=lambda *args,**kwargs: ({},{'weld_table_file':'fixture.csv','welds':[]},None)
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




if __name__ == '__main__':
    unittest.main()
