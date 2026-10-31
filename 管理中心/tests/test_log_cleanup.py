"""Cleanup uses isolated fixtures; never remove workspace/server logs."""
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import BEIJING, Manager, TOOLS


class LogCleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root, self.root / 'admin/runtime')
        for key in TOOLS:
            (self.manager.folder(key) / 'runtime').mkdir(parents=True)
        self.live = patch.object(self.manager, 'live', return_value='stopped')
        self.live.start()
        self.addCleanup(self.live.stop)

    def file(self, relative, *, old=True):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fixture record\n')
        if old:
            stamp = (datetime.now(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0)
                     - timedelta(hours=1)).timestamp()
            os.utime(path, (stamp, stamp))
        return path

    def test_history_is_scoped_and_preserves_state_reports_and_today(self):
        base = TOOLS['sync']['folder']
        old = self.file(f'{base}/logs/yesterday/scheduler.log')
        old_runtime = self.file(f'{base}/runtime/file_analysis/progress.log')
        admin = self.file('admin/runtime/old-sync-start-one.log')
        today = self.file(f'{base}/logs/today/report.log', old=False)
        kept = [today, self.file(f'{base}/runtime/briefing_schedule.json'),
                self.file(f'{base}/runtime/ledger.sqlite3'), self.file(f'{base}/logs/yesterday/report.xlsx'),
                self.file(f'{base}/logs/yesterday/微信推送文.txt'),
                self.file(f'{base}/downloads/original.log'),
                self.file(f'{TOOLS["wechat"]["folder"]}/logs/listener.log')]
        result = self.manager.clear_logs('sync')
        self.assertEqual(result['deleted'], 3)
        self.assertEqual(result['kept'], 1)
        self.assertEqual(result['freed_bytes'], 3 * len(b'fixture record\n'))
        self.assertTrue(all(not path.exists() for path in (old, old_runtime, admin)))
        self.assertTrue(all(path.exists() for path in kept))
        self.assertTrue(self.manager.tasks_path.exists())

    def test_all_includes_today_and_log_display_tolerates_removed_files(self):
        self.file('admin/runtime/launcher.log', old=False)
        self.file(f'{TOOLS["nas"]["folder"]}/runtime/uploader.log', old=False)
        self.assertEqual(self.manager.logs()['total'], 2)
        result = self.manager.clear_logs(scope='all')
        self.assertEqual(result['deleted'], 2)
        self.assertEqual(self.manager.logs()['items'], [])

    def test_active_tools_are_skipped_while_other_tools_are_cleaned(self):
        running = self.file(f'{TOOLS["sync"]["folder"]}/logs/scheduler.log')
        self.file(f'{TOOLS["nas"]["folder"]}/logs/uploader.log')
        with patch.object(self.manager, 'live', side_effect=lambda key: 'external' if key == 'sync' else 'stopped'):
            result = self.manager.clear_logs(scope='all')
        self.assertEqual(result['deleted'], 1)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(running.exists())

    def test_background_worker_lock_protects_sync_logs(self):
        path = self.file(f'{TOOLS["sync"]["folder"]}/runtime/file_analysis/worker.log')
        with patch('manager.locked', return_value=True):
            result = self.manager.clear_logs('sync', 'all')
        self.assertEqual(result['deleted'], 0)
        self.assertEqual(result['skipped'], 1)
        self.assertTrue(path.exists())

    def test_alias_directories_are_not_traversed_even_inside_project(self):
        path = self.file(f'{TOOLS["sync"]["folder"]}/logs/alias/original.log')
        alias = path.parent
        original = Path.is_junction
        with patch.object(Path, 'is_junction', lambda candidate: candidate == alias or original(candidate)):
            self.assertEqual(self.manager.clear_logs('sync', 'all')['deleted'], 0)
        self.assertTrue(path.exists())

    def test_log_symlink_is_never_deleted_or_read(self):
        original = self.file('protected/original.log')
        link = self.manager.folder('sync') / 'runtime/alias.log'
        try:
            link.symlink_to(original)
        except OSError:
            self.skipTest('This Windows account cannot create symlinks')
        self.assertEqual(self.manager.logs('sync')['total'], 0)
        self.assertEqual(self.manager.clear_logs('sync', 'all')['deleted'], 0)
        self.assertTrue(link.is_symlink())
        self.assertTrue(original.exists())

    def test_one_locked_file_does_not_prevent_cleaning_others(self):
        blocked = self.file('admin/runtime/blocked.log')
        other = self.file('admin/runtime/other.log')
        unlink = Path.unlink
        def remove(path, *args, **kwargs):
            if path == blocked:
                raise PermissionError('fixture file is open')
            return unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', remove):
            result = self.manager.clear_logs(scope='all')
        self.assertTrue(blocked.exists())
        self.assertFalse(other.exists())
        self.assertEqual(len(result['errors']), 1)

    def test_invalid_scope_or_tool_never_deletes(self):
        path = self.file('admin/runtime/keep.log')
        for tool, scope in (('../', 'all'), ('', '../'), ('sync', None)):
            with self.assertRaises(ValueError):
                self.manager.clear_logs(tool, scope)
        self.assertTrue(path.exists())


if __name__ == '__main__':
    unittest.main()
