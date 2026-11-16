"""No real platform access: bounded error evidence and header-only diagnostics."""
import importlib.util
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests
from download_diagnostics import download_parameters, response_evidence
from fusion_client import FusionClient

spec = importlib.util.spec_from_file_location('diagnose_downloads',
    Path(__file__).resolve().parents[1] / 'devtools/diagnose_downloads.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class DownloadDiagnosticsTests(unittest.TestCase):
    def response(self, body=b'<html>Access denied</html>', status=403):
        response = requests.Response()
        response.status_code = status
        response.headers = {'Content-Type': 'text/html', 'Server': 'fixture', 'Set-Cookie': 'private-cookie'}
        response._content, response._content_consumed = body, True
        response.url = 'https://us.fusion.live/servlet/HtmlDownloadServlet?guid=private-guid'
        return response

    def test_error_evidence_redacts_secrets_and_discards_scripts_and_hidden_inputs(self):
        response = self.response(b'''<html><script>private-script</script>
            <input name="token" value="private-hidden">
            AccessDenied token=private-token Authorization: Bearer private-bearer
            https://us.fusion.live/error?guid=private-url user@example.test</html>''')
        evidence = response_evidence(response)
        value = json.dumps(evidence)
        for secret in ('private-cookie', 'private-script', 'private-hidden', 'private-token',
                       'private-bearer', 'private-url', 'user@example.test'):
            self.assertNotIn(secret, value)
        self.assertIn('AccessDenied', value)
        self.assertEqual(evidence['headers']['Server'], 'fixture')

    def test_large_error_is_bounded_and_binary_error_body_is_not_read(self):
        response = Mock(status_code=403, headers={'Content-Type': 'text/plain'})
        response.iter_content.return_value = iter([b'a' * 8192, b'b' * 8192])
        evidence = response_evidence(response)
        self.assertTrue(evidence['body_limit_reached'])
        self.assertLessEqual(len(evidence['body_excerpt']), 1500)
        self.assertEqual(next(response.iter_content.return_value), b'b' * 8192)
        response.headers = {'Content-Type': 'application/octet-stream'}
        response.iter_content.reset_mock()
        self.assertIn('body_note', response_evidence(response))
        response.iter_content.assert_not_called()

    def test_response_read_failure_keeps_original_http_status(self):
        response = Mock(status_code=403, headers={'Content-Type': 'text/plain'})
        response.iter_content.side_effect = requests.ReadTimeout('fixture')
        evidence = response_evidence(response)
        self.assertEqual(evidence['status'], 403)
        self.assertEqual(evidence['body_read_error'], 'ReadTimeout')

    def test_request_retains_http_error_type_and_closes_failed_download_response(self):
        client = FusionClient('fixture', '')
        self.addCleanup(client.http.close)
        response = self.response()
        with patch.object(client, 'check_access_window'), patch('fusion_client.time.sleep'), \
             patch.object(client, 'read_with_retries', return_value=response), \
             patch.object(response, 'close', wraps=response.close) as close:
            with self.assertRaises(requests.HTTPError) as caught:
                client.request('GET', '/servlet/HtmlDownloadServlet', stream=True)
        self.assertEqual(caught.exception.response.status_code, 403)
        self.assertIn('Access denied', str(caught.exception))
        self.assertNotIn('private-guid', str(caught.exception))
        self.assertEqual(caught.exception.download_evidence['status'], 403)
        close.assert_called_once()

    def test_shared_parameters_preserve_native_and_virtual_download_contract(self):
        client = Mock(project_id='p', rendition_security=False)
        self.assertEqual(download_parameters(client, {'id': 'file'})['rObjectId'], 'file')
        virtual = download_parameters(client, {'id': 'file', 'isVirtualLink': 'true', 'virtualLinkId': 'link'})
        self.assertEqual((virtual['rObjectId'], virtual['objectType']), ('link', 'VIRTUAL_LINK'))
        client.rendition_security = True
        self.assertEqual(download_parameters(client, {'id': 'file'})['saveRendition'], 'false')

    def test_successful_probe_reads_headers_only_and_does_not_change_database(self):
        response = self.response(status=200)
        response.iter_content = Mock(side_effect=AssertionError('file bytes must not be read'))
        client = Mock(project_id='p', rendition_security=False)
        client.request.return_value = response
        item = dict(id='file', readAccessStatus='Available')
        row = dict(project='p', id='file', filename='large.zip', size=7000000000, metadata=json.dumps(item))
        result = diagnostics.probe_one(client, row)
        self.assertEqual(result['status'], 200)
        self.assertFalse(result['body_saved'])
        response.iter_content.assert_not_called()
        self.assertTrue(client.request.call_args.kwargs['stream'])

    def test_403_checks_catalogue_and_401_stops_without_more_probes(self):
        item = dict(id='file', readAccessStatus='Available')
        row = dict(project='p', id='file', filename='large.zip', size=7000000000, metadata=json.dumps(item))
        client = Mock(project_id='p', rendition_security=False)
        for status in (403, 401):
            response = self.response(status=status)
            error = requests.HTTPError('fixture', response=response)
            error.download_evidence = {'status': status}
            client.request.side_effect = error
            client.folders.reset_mock()
            if status == 403:
                self.assertEqual(diagnostics.probe_one(client, row)['protected_catalogue_access'], 'ok')
                client.folders.assert_called_once()
            else:
                with self.assertRaises(requests.HTTPError):
                    diagnostics.probe_one(client, row)
                client.folders.assert_not_called()

    def test_offline_command_does_not_login_or_modify_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            database, output = Path(directory) / 'ledger.sqlite3', Path(directory) / 'evidence.json'
            with closing(sqlite3.connect(database)) as db, db:
                db.execute('CREATE TABLE files(project,id,filename,status,size,folder_id,metadata,error,active)')
                for identity, state, size in [('small', 'done', 1800), ('large', 'pending', 1900)]:
                    db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?)',
                        ('p', identity, identity+'.zip', state, size, 'folder',
                         json.dumps({'readAccessStatus': 'Available'}), None, 1))
            before = database.read_bytes()
            with patch('local_credentials.get_credentials', side_effect=AssertionError('no login')):
                self.assertEqual(diagnostics.main(['--database', str(database), '--output', str(output)]), 0)
            self.assertEqual(database.read_bytes(), before)
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['mode'], 'offline')
            self.assertEqual(report['minimum_incomplete_size'], 1900)


if __name__ == '__main__':
    unittest.main()
