"""Use isolated task databases and mocked launches; never start real tools."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import Manager, TOOLS, writing, locked, now


class BulkAndCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root, self.root / 'runtime')

    def task(self, identity, state='succeeded', tool='sync', log=None):
        log = log or identity + '.log'
        with writing(self.manager.tasks_path) as db:
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (identity, tool, 'start', 'fixture', state, 0, 0, '2026-10-28', None, log))
        path = self.manager.runtime / log
        path.write_text('fixture', encoding='utf-8')
        return path

    def test_clear_finished_records_and_logs_preserves_active_and_other_tools(self):
        removed = self.task('done')
        missing = self.task('missing', 'failed')
        missing.unlink()
        kept = [self.task('active', 'running'), self.task('unknown', 'detached'),
                self.task('nas', tool='nas')]
        business = self.manager.runtime / 'ledger.sqlite3'
        business.write_bytes(b'business')
        result = self.manager.clear_tasks('sync')
        self.assertEqual((result['deleted'], result['deleted_logs'], result['skipped']), (3, 1, 1))
        self.assertFalse(removed.exists())
        self.assertTrue(all(path.exists() for path in kept + [business]))
        self.assertEqual({r['id'] for r in self.manager.tasks()}, {'active', 'nas'})

    def test_file_error_keeps_record_and_other_records_are_still_cleared(self):
        blocked = self.task('blocked')
        removed = self.task('done')
        original = Path.unlink
        def unlink(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('fixture busy file')
            return original(path, *args, **kwargs)
        with patch.object(Path, 'unlink', unlink):
            result = self.manager.clear_tasks('sync')
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(len(result['errors']), 1)
        self.assertTrue(blocked.exists())
        self.assertFalse(removed.exists())
        self.assertEqual([row['id'] for row in self.manager.tasks()], ['blocked'])

    def test_untrusted_paths_and_unknown_tool_are_not_deleted(self):
        protected = self.task('outside', log='../outside.log')
        with self.assertRaises(ValueError):
            self.manager.clear_tasks('../')
        self.assertEqual(self.manager.clear_tasks('sync')['deleted'], 0)
        self.assertTrue(protected.exists())
        self.assertEqual(len(self.manager.tasks()), 1)

    def test_ima_clears_today_tasks_and_rotated_logs_without_touching_receipts(self):
        import sqlite3
        from contextlib import closing
        import ima_control
        legacy = self.task('today-ima-ended', tool='ima')
        current = self.root / 'logs' / now()[:10] / 'ima' / legacy.name
        with writing(self.manager.tasks_path) as db:
            db.execute("UPDATE tasks SET started=? WHERE id='today-ima-ended'", (now(),))
        current.parent.mkdir(parents=True)
        legacy.rename(current)
        rotated = current.with_name(current.stem + '.1.log')
        rotated.write_bytes(b'rotated task output')
        active = self.task('active-ima-task', state='running', tool='ima')
        base = self.manager.folder('ima') / 'runtime'
        base.mkdir(parents=True)
        receipts = base / 'state.sqlite3'
        with closing(sqlite3.connect(receipts)) as db, db:
            db.execute('CREATE TABLE receipts(kb,folder,path,status,media_id,note,updated)')
            db.execute("INSERT INTO receipts VALUES('kb','','reviewed.pdf','done','media','keep',1)")
        before = receipts.read_bytes()
        plan = base / 'preview-plan.json'
        plan.write_bytes(b'keep reviewed plan')
        result = self.manager.clear_tasks('ima')
        self.assertEqual((result['deleted'], result['deleted_logs'], result['skipped']), (1, 2, 1))
        self.assertEqual(result['errors'], [])
        self.assertFalse(current.exists())
        self.assertFalse(rotated.exists())
        self.assertTrue(active.exists())
        self.assertEqual(receipts.read_bytes(), before)
        self.assertEqual(ima_control.records(self.manager, 'history')['total'], 1)
        self.assertEqual(plan.read_bytes(), b'keep reviewed plan')

    def test_task_cleanup_keeps_logs_referenced_by_a_running_task(self):
        shared = self.task('completed-ima', tool='ima')
        self.task('active-ima', state='running', tool='ima', log=shared.name)
        result = self.manager.clear_tasks('ima')
        self.assertEqual((result['deleted'], result['skipped']), (0, 2))
        self.assertTrue(shared.exists())

    def test_stale_running_database_row_is_not_proof_of_a_running_tool(self):
        self.task('stale-ima-start', state='running', tool='ima')
        self.assertEqual(self.manager.live('ima'), 'stopped')

    def test_lock_permission_failure_is_unknown_and_still_protects_logs(self):
        import errno
        path = self.manager.folder('ima') / 'runtime/watch.lock'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'0')
        log = self.root / 'logs/today/ima/ended.log'
        log.parent.mkdir(parents=True)
        log.write_bytes(b'keep when status unknown')
        original = Path.open
        def denied(file, *args, **kwargs):
            if file == path:
                raise PermissionError(errno.EACCES, 'fixture read permission denied')
            return original(file, *args, **kwargs)
        with patch.object(Path, 'open', denied):
            self.assertEqual(locked(path), 'unknown')
            self.assertEqual(self.manager.live('ima'), 'unknown')
            result = self.manager.clear_logs('ima', 'all')
            self.assertEqual(result['skipped'], 1)
            self.assertTrue(log.exists())

    def test_real_external_lock_and_simultaneous_polling_report_actual_state(self):
        import subprocess
        import time
        from concurrent.futures import ThreadPoolExecutor
        path = self.manager.folder('ima') / 'runtime/watch.lock'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'0')
        ready = path.with_suffix('.ready')
        script = ('import os,sys; from pathlib import Path; f=open(sys.argv[1],"r+b"); '
                  'exec("import msvcrt; msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)" if os.name=="nt" '
                  'else "import fcntl; fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"); '
                  'Path(sys.argv[2]).write_text("ready"); sys.stdin.readline(); f.close()')
        process = subprocess.Popen([sys.executable, '-u', '-c', script, str(path), str(ready)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            self.assertIs(locked(path), True)
            self.assertEqual(self.manager.live('ima'), 'external')
        finally:
            process.communicate('\n', timeout=5)
        self.assertIs(locked(path), False)
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda _: self.manager.live('ima'), range(32)))
        self.assertEqual(set(states), {'stopped'})

    def test_real_one_off_child_is_busy_and_only_start_child_is_running(self):
        for action, expected in [('plan', 'busy'), ('sync-once', 'busy'), ('start', 'running')]:
            command = [sys.executable, '-u', '-c', 'import sys; sys.stdin.readline()']
            with self.subTest(action=action), patch.object(self.manager, 'command', return_value=(command, self.root)):
                task = self.manager.action('ima', action)
                process = self.manager.children[task['id']]
                try:
                    self.assertEqual(self.manager.live('ima'), expected)
                    process.stdin.write(b'\n')
                    process.stdin.flush()
                    process.wait(timeout=5)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=5)
                    self.manager.tasks()
                self.assertEqual(self.manager.live('ima'), 'stopped')

    def test_start_skips_running_tool_and_continues_after_one_failure(self):
        with patch.object(self.manager, 'live', side_effect=lambda t: 'external' if t == 'sync' else 'stopped'), \
             patch.object(self.manager, 'action', side_effect=[{'id':'analysis-task'},OSError('fixture failure'), {'id': 'wechat-task'}, {'id': 'ima-task'}]) as action:
            result = self.manager.bulk_action('start')
        self.assertEqual([i['state'] for i in result['items']], ['submitted','skipped', 'failed', 'submitted', 'submitted'])
        self.assertEqual([call.args for call in action.call_args_list], [('analysis','start'),('nas', 'start'), ('wechat', 'start'), ('ima', 'start')])

    def test_stop_submits_to_all_tools_even_without_main_scheduler(self):
        with patch.object(self.manager, 'action', return_value={'id': 'stop'}) as action:
            result = self.manager.bulk_action('stop')
        self.assertEqual([call.args for call in action.call_args_list], [(t, 'stop') for t in TOOLS])
        self.assertTrue(all(i['state'] == 'submitted' for i in result['items']))

    def test_repeated_active_operation_is_skipped_and_invalid_bulk_is_rejected(self):
        self.task('active', 'running')
        with patch.object(self.manager, 'action', return_value={'id': 'task'}) as action:
            self.assertEqual(next(i for i in self.manager.bulk_action('start')['items'] if i['tool']=='sync')['state'], 'skipped')
            with self.assertRaises(ValueError):
                self.manager.bulk_action('check')
        self.assertEqual(action.call_count, 4)

    def test_selected_tools_only_and_duplicate_selection_runs_once(self):
        for operation in ('start', 'stop'):
            with self.subTest(operation=operation), \
                 patch.object(self.manager, 'live', return_value='stopped'), \
                 patch.object(self.manager, 'action', return_value={'id': 'selected'}) as action:
                result = self.manager.bulk_action(operation, ['ima', 'nas', 'ima'])
                self.assertEqual([item['tool'] for item in result['items']], ['ima', 'nas'])
                self.assertEqual([call.args for call in action.call_args_list],
                                 [('ima', operation), ('nas', operation)])

    def test_invalid_selection_rejected_before_any_action(self):
        with patch.object(self.manager, 'action') as action:
            for selection in ([], 'sync', {}, ['sync', 'unknown'], ['sync', None], [['sync']]):
                with self.subTest(selection=selection), self.assertRaises(ValueError):
                    self.manager.bulk_action('start', selection)
            action.assert_not_called()


if __name__ == '__main__':
    unittest.main()
