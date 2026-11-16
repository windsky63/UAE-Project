import importlib
import json
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from fusion_client import FusionClient, LOCAL, WindowClosed
from ledger import Ledger
from login_test import TestClient as LoginClient, check_test_window, download_one, load_window, save_window
import logout_watchdog


class LoginTestTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.runtime = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def at(self, hour, minute=0):
        return datetime(2026, 10, 24, hour, minute, tzinfo=LOCAL)

    def test_settings_and_boundaries(self):
        self.assertEqual(load_window(self.runtime), ('01:00', '10:30'))
        save_window('02:00', '09:30', self.runtime)
        self.assertEqual(load_window(self.runtime), ('02:00', '09:30'))
        for at in (self.at(2), self.at(9, 29)):
            check_test_window(at, self.runtime)
        for at in (self.at(1, 59), self.at(9, 30), self.at(23)):
            with self.assertRaises(WindowClosed):
                check_test_window(at, self.runtime)
            check_test_window(at, self.runtime, manual_override=True)

    def test_bad_settings_do_not_replace_saved_window(self):
        save_window('02:00', '09:30', self.runtime)
        for start, end in [('00:00', '10:30'), ('02:00', '11:00'), ('09:00', '02:00'),
                           ('02:60', '09:30'), ('2:00', '09:30')]:
            with self.assertRaises(ValueError):
                save_window(start, end, self.runtime)
        self.assertEqual(load_window(self.runtime), ('02:00', '09:30'))

    def test_manual_override_respects_pause_and_stop(self):
        for marker in ('login.paused', 'stop.request'):
            path = self.runtime / marker
            path.touch()
            with self.assertRaises(WindowClosed):
                check_test_window(self.at(23), self.runtime, manual_override=True)
            path.unlink()

    def test_override_applies_to_test_client_only_and_logout_is_available(self):
        with patch('login_test.RUNTIME', self.runtime), patch('fusion_client.RUNTIME', self.runtime), \
                patch('login_test.now', return_value=self.at(23)), patch('fusion_client.now', return_value=self.at(23)):
            normal = FusionClient('fake', 'fake')
            test = LoginClient('fake', 'fake', manual_override=True)
            try:
                with self.assertRaises(WindowClosed):
                    normal.check_access_window()
                test.check_access_window()
                test.manual_override = False
                with patch.object(test, 'raw') as raw:
                    with self.assertRaises(WindowClosed):
                        test.request('GET', '/unused')
                    raw.assert_not_called()
            finally:
                normal.http.close()
                test.http.close()

    def test_downloads_exactly_one_file_without_changing_queue(self):
        ledger = Ledger(self.runtime / 'ledger.sqlite3', base_dir=self.runtime)
        try:
            for identifier in ('a', 'b'):
                metadata = {'id': identifier, 'filename': identifier + '.pdf', 'readAccessStatus': 'Available'}
                ledger.db.execute("""INSERT INTO files(project,id,document_id,folder_id,filename,metadata)
                    VALUES('p',?,'doc','folder',?,?)""", (identifier, metadata['filename'], json.dumps(metadata)))
            ledger.db.commit()
            before = [dict(row) for row in ledger.all('SELECT * FROM files')]
            client = Mock(project_id='p')
            client.download.side_effect = lambda item, target: {'bytes': 10, 'sha256': 'fake', 'path': str(target)}
            with patch('login_test.RUNTIME', self.runtime):
                self.assertEqual(download_one(client, ledger), ('done', 0))
            client.download.assert_called_once()
            result = json.loads((self.runtime / 'login_test_result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['content_id'], 'a')
            self.assertEqual(before, [dict(row) for row in ledger.all('SELECT * FROM files')])
            client.project_id = 'other'
            with self.assertRaisesRegex(RuntimeError, '没有当前项目'):
                download_one(client, ledger)
            self.assertEqual(client.download.call_count, 1)
        finally:
            ledger.close()

    def test_gui_outside_window_requires_manual_confirmation(self):
        gui = importlib.import_module('台账')
        for confirmed in (False, True):
            app = gui.App.__new__(gui.App)
            app.process = None
            app.account = Mock(get=Mock(return_value='fake'))
            app.password = Mock(get=Mock(return_value='fake'))
            app.save_test_window = Mock(return_value=True)
            app.refresh_test_state = Mock()
            app.foot = Mock()
            with patch.object(gui, 'RUNTIME', self.runtime), \
                    patch.object(gui, 'check_test_window', side_effect=WindowClosed('outside')), \
                    patch.object(gui, 'load_window', return_value=('02:00', '09:00')), \
                    patch.object(gui.messagebox, 'askyesno', return_value=confirmed) as warning, \
                    patch.object(gui.subprocess, 'Popen') as launch:
                gui.App.start(app, 'login-test')
                warning.assert_called_once()
                if confirmed:
                    launch.assert_called_once()
                    self.assertIn('--manual-test-outside-window', launch.call_args.args[0])
                    app.log_handle.close()
                else:
                    launch.assert_not_called()


    def test_watchdog_manual_test_outside_window_and_eof(self):
        for manual, eof in ((True, False), (True, True), (False, False)):
            messages = Mock()
            messages.get.side_effect = [
                {'cookies': [], 'manual_test': manual},
                {'eof': True} if eof else {'done': True}]
            with patch.object(logout_watchdog.queue, 'Queue', return_value=messages), \
                    patch.object(logout_watchdog.threading, 'Thread'), \
                    patch.object(logout_watchdog, 'now', return_value=self.at(23)), \
                    patch.object(logout_watchdog, 'FusionClient') as factory:
                logout_watchdog.main()
                if eof or not manual:
                    factory.return_value.logout.assert_called_once()
                else:
                    factory.return_value.logout.assert_not_called()
                    factory.return_value.http.close.assert_called_once()


if __name__ == '__main__':
    unittest.main()
