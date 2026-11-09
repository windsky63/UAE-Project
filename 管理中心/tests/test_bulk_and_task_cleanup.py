"""Use isolated task databases and mocked launches; never start real tools."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import Manager, TOOLS, writing


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

    def test_start_skips_running_tool_and_continues_after_one_failure(self):
        with patch.object(self.manager, 'live', side_effect=lambda t: 'external' if t == 'sync' else 'stopped'), \
             patch.object(self.manager, 'action', side_effect=[OSError('fixture failure'), {'id': 'wechat-task'}, {'id': 'ima-task'}]) as action:
            result = self.manager.bulk_action('start')
        self.assertEqual([i['state'] for i in result['items']], ['skipped', 'failed', 'submitted', 'submitted'])
        self.assertEqual([call.args for call in action.call_args_list], [('nas', 'start'), ('wechat', 'start'), ('ima', 'start')])

    def test_stop_submits_to_all_tools_even_without_main_scheduler(self):
        with patch.object(self.manager, 'action', return_value={'id': 'stop'}) as action:
            result = self.manager.bulk_action('stop')
        self.assertEqual([call.args for call in action.call_args_list], [(t, 'stop') for t in TOOLS])
        self.assertTrue(all(i['state'] == 'submitted' for i in result['items']))

    def test_repeated_active_operation_is_skipped_and_invalid_bulk_is_rejected(self):
        self.task('active', 'running')
        with patch.object(self.manager, 'action', return_value={'id': 'task'}) as action:
            self.assertEqual(self.manager.bulk_action('start')['items'][0]['state'], 'skipped')
            with self.assertRaises(ValueError):
                self.manager.bulk_action('check')
        self.assertEqual(action.call_count, 3)

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
