import json
from datetime import datetime
from pathlib import Path
import tempfile
import requests
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

from fusion_client import FusionClient, LOCAL, LogoutError, WindowClosed, check_window, AuthenticationRequiredError
from ledger import Ledger, safe_name
from sync import download, file_target, refresh_associations


class SessionTests(unittest.TestCase):
    def test_all_endpoints_use_90_seconds_for_connection_and_read_after_redirect(self):
        client = FusionClient('fixture', '')
        self.addCleanup(client.http.close)
        redirect = Mock(status_code=302, headers={'Location': '/pol/association-result'})
        response = Mock(status_code=200)
        with patch.object(client.http, 'request', side_effect=[redirect, response, response]) as request:
            self.assertIs(client.raw('GET', '/pol/getRelatedContentFSAction'), response)
            client.raw('GET', '/pol/viewDocumentManagerTreeFSAction')
        self.assertEqual([call.kwargs['timeout'] for call in request.call_args_list], [(90, 90)] * 3)

    def test_download_scratch_file_cannot_overwrite_platform_part_filename(self):
        client = FusionClient('fixture', '')
        self.addCleanup(client.http.close)
        client.rendition_security = False
        client.project_id = 'fixture'
        response = Mock(headers={'Content-Length': '3'})
        response.iter_content.return_value = [b'pdf']
        context = Mock()
        context.__enter__ = Mock(return_value=response)
        context.__exit__ = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            target = base/'drawing.pdf'
            peer = base/'drawing.pdf.part'
            peer.write_bytes(b'platform peer')
            with patch('fusion_client.RUNTIME', base/'runtime'), patch.object(client, 'request', return_value=context), \
                 patch.object(client, 'check_access_window'):
                result = client.download(dict(id='fixture', readAccessStatus='Available', contentSize='3'), target)
            self.assertEqual(result['bytes'], 3)
            self.assertEqual(target.read_bytes(), b'pdf')
            self.assertEqual(peer.read_bytes(), b'platform peer')
            self.assertEqual(list((base/'runtime/download_parts').glob('*.part')), [])

    def test_beijing_time_boundaries(self):
        with tempfile.TemporaryDirectory() as d, patch('fusion_client.RUNTIME', Path(d)):
            for hour, minute in [(2, 0), (10, 29)]:
                check_window(datetime(2026, 10, 20, hour, minute, tzinfo=LOCAL))
            for hour, minute in [(1, 0), (1, 59), (10, 30), (11, 0)]:
                with self.assertRaises(WindowClosed):
                    check_window(datetime(2026, 10, 20, hour, minute, tzinfo=LOCAL))
            check_window(datetime(2026, 10, 20, 10, 44, tzinfo=LOCAL), stop_minute=645)
            with self.assertRaises(WindowClosed):
                check_window(datetime(2026, 10, 20, 10, 45, tzinfo=LOCAL), stop_minute=645)

    def test_login_failure_still_attempts_cleanup(self):
        client = FusionClient('test', 'not-a-real-password')
        with patch.object(client, 'login', side_effect=RuntimeError('failed')), patch.object(client, 'logout') as logout:
            with self.assertRaises(RuntimeError):
                with client:
                    pass
            logout.assert_called_once()
        client.http.close()

    def test_body_failure_and_keyboard_interrupt_log_out(self):
        for exception in (RuntimeError('failed'), KeyboardInterrupt()):
            client = FusionClient('test', '')
            with patch.object(client, 'login', return_value=client), patch.object(client, 'logout') as logout:
                with self.assertRaises(type(exception)):
                    with client:
                        raise exception
                logout.assert_called_once()
            client.http.close()

    def test_logout_does_not_accept_http200_directory(self):
        client = FusionClient('test', '')
        client.auth_attempted = True
        response = Mock(status_code=200, text='<tree><tree id="1"/></tree>')
        with tempfile.TemporaryDirectory() as d, patch('fusion_client.RUNTIME', Path(d)), patch.object(client, 'raw', return_value=response), patch('fusion_client.time.sleep'):
            with self.assertRaises(LogoutError):
                client.logout()
            result = json.loads((Path(d) / 'last_logout.json').read_text(encoding='utf-8'))
            self.assertFalse(result['confirmed'])
            self.assertEqual(result['attempts'], 3)

    def test_logout_checks_same_cookie_before_clearing(self):
        client = FusionClient('test', '')
        client.auth_attempted = True
        client.http.cookies.set('session', 'fake')
        checked = []
        def response(method, path, **kwargs):
            if 'Tree' in path:
                checked.append(client.http.cookies.get('session'))
                return Mock(status_code=418, text='denied')
            return Mock(status_code=200, text='logged out')
        with tempfile.TemporaryDirectory() as d, patch('fusion_client.RUNTIME', Path(d)), patch.object(client, 'raw', side_effect=response):
            client.logout()
        self.assertEqual(checked, ['fake'])
        self.assertEqual(len(client.http.cookies), 0)
        self.assertTrue(client.logout_result['confirmed'])

    def test_business_failure_is_not_empty_directory(self):
        client = FusionClient('test', '')
        with patch.object(client, 'request', return_value=Mock(content=b'<success isActionSucessful="false"/>')):
            with self.assertRaises(RuntimeError):
                client.folders('1')
        client.http.close()

    def test_business_failure_reports_safe_response_evidence(self):
        client = FusionClient('test', '')
        self.addCleanup(client.http.close)
        response = Mock(status_code=200, content=(
            b'<success isActionSucessful="false"><errorCode>CONTENT_DENIED</errorCode>'
            b'<message>private-token-and-cookie</message></success>'))
        with patch.object(client, 'request', return_value=response):
            with self.assertRaises(RuntimeError) as error:
                client.related('fixture-version')
        self.assertIn('HTTP=200', str(error.exception))
        self.assertIn('errorCode=CONTENT_DENIED', str(error.exception))
        self.assertIn('响应摘要=', str(error.exception))
        self.assertNotIn('private-token', str(error.exception))

    def test_scheduled_network_logout_recovers_same_session_without_login_pause(self):
        client = FusionClient('test', '')
        client.auth_attempted = True
        client.http.cookies.set('session', 'fixture-cookie')
        errors = [requests.exceptions.SSLError('UNEXPECTED_EOF_WHILE_READING') for _ in range(3)]
        logout = Mock(status_code=200)
        denied = Mock(status_code=401)
        cookies = []
        responses = iter([*errors, logout, denied])
        def raw(*args, **kwargs):
            cookies.append(client.http.cookies.get('session'))
            value = next(responses)
            if isinstance(value, Exception):
                raise value
            return value
        at = datetime(2026, 11, 7, 9, tzinfo=LOCAL)
        with tempfile.TemporaryDirectory() as folder, patch('fusion_client.RUNTIME', Path(folder)), \
                patch('fusion_client.now', return_value=at), \
                patch.dict('os.environ', {'FUSIONLIVE_SCHEDULED': '1'}), \
                patch('fusion_client.time.sleep') as sleep, patch.object(client, 'raw', side_effect=raw), \
                patch.object(client, 'login') as login:
            client.logout()
            self.assertFalse((Path(folder) / 'login.paused').exists())
            result = json.loads((Path(folder) / 'last_logout.json').read_text(encoding='utf-8'))
        self.assertTrue(result['confirmed'])
        self.assertFalse(result['recovery_pending'])
        self.assertEqual(result['attempts'], 4)
        self.assertEqual(cookies, ['fixture-cookie'] * 5)
        self.assertEqual(sleep.call_count, 62)
        login.assert_not_called()
        self.assertEqual(len(client.http.cookies), 0)

    def test_network_logout_wait_obeys_window_manual_stop_and_pause_markers(self):
        for stop in ('stop.request', 'scheduler.exit.request', 'login.paused', 'window_end'):
            with self.subTest(stop=stop), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                client = FusionClient('test', '')
                client.auth_attempted = True
                at = [datetime(2026, 11, 7, 9, tzinfo=LOCAL)]
                sleeps = []
                def sleep(seconds):
                    sleeps.append(seconds)
                    if len(sleeps) == 3:
                        if stop == 'window_end':
                            at[0] = at[0].replace(hour=10, minute=30)
                        else:
                            (root / stop).write_text('manual', encoding='utf-8')
                with patch('fusion_client.RUNTIME', root), patch('fusion_client.now', side_effect=lambda: at[0]), \
                        patch.dict('os.environ', {'FUSIONLIVE_SCHEDULED': '1'}), \
                        patch('fusion_client.time.sleep', side_effect=sleep), \
                        patch.object(client, 'raw', side_effect=requests.ReadTimeout) as raw:
                    with self.assertRaises(LogoutError):
                        client.logout()
                self.assertEqual(raw.call_count, 3)
                self.assertEqual(len(sleeps), 3)
                self.assertFalse(client.logout_result['confirmed'])
                self.assertFalse(client.logout_result['recovery_pending'])

    def test_certificate_and_accessible_old_session_never_enter_automatic_recovery(self):
        for error in ('certificate', 'still_accessible'):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as folder:
                client = FusionClient('test', '')
                client.auth_attempted = True
                response = Mock(status_code=200, text='<tree/>')
                effect = requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED') if error == 'certificate' else None
                with patch('fusion_client.RUNTIME', Path(folder)), \
                        patch('fusion_client.now', return_value=datetime(2026, 11, 7, 9, tzinfo=LOCAL)), \
                        patch.dict('os.environ', {'FUSIONLIVE_SCHEDULED': '1'}), \
                        patch('fusion_client.time.sleep') as sleep, \
                        patch.object(client, 'raw', side_effect=effect, return_value=response):
                    with self.assertRaises(LogoutError):
                        client.logout()
                self.assertEqual(sleep.call_count, 2)
                self.assertFalse(client.logout_result['recovery_pending'])

    def test_authentication_error_survives_failed_logout_for_manual_pause(self):
        client = FusionClient('test', '')
        self.addCleanup(client.http.close)
        with patch.object(client, 'login', side_effect=AuthenticationRequiredError('MFA required')), \
                patch.object(client, 'logout', side_effect=LogoutError('cleanup failed')):
            with self.assertRaises(AuthenticationRequiredError):
                with client:
                    pass
        self.assertFalse(client.logout_recovery_allowed)

    def test_related_count_mismatch_is_not_accepted_as_complete(self):
        client = FusionClient('test', '')
        tree = ET.fromstring('<collection><totalCount>2</totalCount><ContentItem><id>1</id></ContentItem></collection>')
        with patch.object(client, 'xml', return_value=tree):
            with self.assertRaises(RuntimeError):
                client.related('1')
        client.http.close()

    def test_interrupted_download_does_not_create_final_file(self):
        client = FusionClient('test', '')
        client.rendition_security = False
        response = Mock(headers={'Content-Type': 'application/pdf', 'Content-Length': '20'})
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        def chunks(*_):
            yield b'%PDF-'
            raise KeyboardInterrupt()
        response.iter_content = chunks
        with tempfile.TemporaryDirectory() as d, patch.object(client, 'request', return_value=response), patch('fusion_client.check_window'):
            target = Path(d) / 'file.pdf'
            with self.assertRaises(KeyboardInterrupt):
                client.download({'id': '1', 'readAccessStatus': 'Available', 'contentFormatCode': 'pdf', 'contentSize': '20'}, target)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_name(target.name + '.part').exists())
        client.http.close()


class LedgerTests(unittest.TestCase):
    def setUp(self):
        background=patch('analysis_worker.ensure_background_worker',return_value=None)
        background.start()
        self.addCleanup(background.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.temp.name) / 'ledger.sqlite3')
        self.window = patch('sync.check_window')
        self.window.start()
        self.addCleanup(self.window.stop)

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_page_checkpoint_survives_reopen_and_is_idempotent(self):
        client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(client)
        self.ledger.save_page('p', 'r', 2, [{'id': 'v1', 'fileName': 'file.pdf', 'contentSize': '10'}], 1, False)
        self.ledger.close()
        self.ledger = Ledger(Path(self.temp.name) / 'ledger.sqlite3')
        self.assertEqual(self.ledger.one('SELECT next_start FROM folders')['next_start'], 1)
        self.ledger.save_page('p', 'r', 2, [{'id': 'v1', 'fileName': 'file.pdf', 'contentSize': '10'}], 1, False)
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM documents')['n'], 1)

    def test_bad_page_is_rolled_back_without_advancing(self):
        self.ledger.root(Mock(project_id='p', cabinet_id='r', project_name='project'))
        with self.assertRaises(RuntimeError):
            self.ledger.save_page('p', 'r', 2, [{'id': 'ok'}, {'fileName': 'missing-id'}], 2, True)
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM documents')['n'], 0)
        self.assertEqual(self.ledger.one('SELECT next_start FROM folders')['next_start'], 0)

    def test_new_run_recovers_in_progress_files(self):
        self.ledger.db.execute("INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,attempts) VALUES('p','c','v','r','x','{}','downloading',3)")
        self.ledger.db.commit()
        self.ledger.start_run('download')
        self.assertEqual(self.ledger.one('SELECT status FROM files')['status'], 'pending')
        self.assertEqual(self.ledger.one('SELECT attempts FROM files')['attempts'], 0)

    def test_windows_filename_traversal_and_reserved_names(self):
        for name in ['../../x', 'CON', 'NUL.txt', 'a:b?.pdf', '...']:
            result = safe_name(name)
            self.assertNotRegex(result, r'[<>:"/\\|?*]')
            self.assertNotIn(result.upper(), ['CON', 'NUL', '..'])

    def test_preregister_placeholder_is_not_downloaded(self):
        client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(client)
        self.ledger.save_page('p', 'r', 1, [{'id': 'v', 'hasContent': 'false', 'hasAttachments': 'false', 'contentSize': '99'}], 1, True)
        refresh_associations(client, self.ledger)
        client.related.assert_not_called()
        self.assertEqual(self.ledger.one('SELECT related_state FROM documents')['related_state'], 'no_content')

    def test_download_target_preserves_extension_separator(self):
        client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(client)
        item = {'project': 'p', 'folder_id': 'r', 'filename': 'example.pdf', 'revision': '0', 'id': 'c'}
        self.assertEqual(file_target(client, self.ledger, item).suffix, '.pdf')
        item['filename'] = 'README'
        self.assertEqual(file_target(client, self.ledger, item).suffix, '')

    def test_stopped_third_attempt_remains_retryable(self):
        client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(client)
        self.ledger.save_page('p', 'r', 1, [{'id': 'v', 'hasContent': 'true'}], 1, True)
        document = self.ledger.one('SELECT * FROM documents')
        self.ledger.save_related(document, [{'id': 'c', 'filename': 'x.pdf', 'readAccessStatus': 'Available', 'contentSize': '1'}])
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET attempts=2')
        client.download.side_effect = WindowClosed('stop')
        with self.assertRaises(WindowClosed):
            download(client, self.ledger, max_files=1)
        item = self.ledger.one('SELECT status,attempts FROM files')
        self.assertEqual((item['status'], item['attempts']), ('pending', 0))

    def test_rescan_can_read_existing_documents_and_detect_current_page_duplicates(self):
        client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(client)
        self.ledger.save_page('p', 'r', 2, [{'id': 'v1'}], 1, False)
        self.ledger.save_page('p', 'r', 2, [{'id': 'v2'}], 2, True)
        self.ledger.root(client, rescan=True)
        self.ledger.save_page('p', 'r', 2, [{'id': 'v1'}], 1, False)
        with self.assertRaises(RuntimeError):
            self.ledger.save_page('p', 'r', 2, [{'id': 'v1'}], 2, True)
        self.ledger.save_page('p', 'r', 2, [{'id': 'v2'}], 2, True)
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM documents')['n'], 2)


if __name__ == '__main__':
    unittest.main()
