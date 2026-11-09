import unittest
from unittest.mock import Mock, patch

import requests

from fusion_client import FusionClient, WindowClosed


class NetworkRetryTests(unittest.TestCase):
    def setUp(self):
        self.client = FusionClient('fixture', '')
        self.addCleanup(self.client.http.close)
        self.window = patch.object(self.client, 'check_access_window').start()
        self.addCleanup(patch.stopall)
        self.sleep = patch('fusion_client.time.sleep').start()

    def test_catalogue_request_recovers_from_read_timeout(self):
        response = Mock(status_code=200, text='<tree/>')
        with patch.object(self.client, 'raw', side_effect=[requests.ReadTimeout(), response]) as raw:
            self.assertIs(self.client.request('GET', '/pol/viewDocumentManagerTreeFSAction',
                                              params={'folderId': 'fixture'}), response)
        self.assertEqual(raw.call_count, 2)
        self.assertEqual(raw.call_args_list[0], raw.call_args_list[1])

    def test_network_failures_have_bounded_backoff(self):
        for error in (requests.ReadTimeout, requests.ConnectTimeout, requests.ConnectionError):
            with self.subTest(error=error), patch.object(self.client, 'raw', side_effect=error) as raw:
                self.sleep.reset_mock()
                with self.assertRaises(error):
                    self.client.read_with_retries('GET', '/pol/list')
                self.assertEqual(raw.call_count, 4)
                self.assertEqual(self.sleep.call_count, 50)

    def test_temporary_server_errors_close_before_retry(self):
        responses = [Mock(status_code=code) for code in (502, 503, 504, 200)]
        with patch.object(self.client, 'raw', side_effect=responses):
            self.assertIs(self.client.read_with_retries('GET', '/pol/list'), responses[-1])
        for response in responses[:-1]:
            response.close.assert_called_once()
        responses[-1].close.assert_not_called()

    def test_final_server_error_reaches_request_status_check(self):
        response = Mock(status_code=503)
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(self.client, 'raw', return_value=response) as raw:
            with self.assertRaises(requests.HTTPError):
                self.client.request('GET', '/pol/list')
        self.assertEqual(raw.call_count, 4)

    def test_authentication_post_is_never_replayed(self):
        with patch.object(self.client, 'raw', side_effect=requests.ReadTimeout) as raw:
            with self.assertRaises(requests.ReadTimeout):
                self.client.read_with_retries('POST', '/pol/authenticateFSAction', data={})
        raw.assert_called_once()
        self.sleep.assert_not_called()

    def test_permission_rate_limit_and_certificate_errors_are_not_retried(self):
        for code in (401, 403, 418, 429):
            response = Mock(status_code=code)
            with patch.object(self.client, 'raw', return_value=response) as raw:
                self.assertIs(self.client.read_with_retries('GET', '/pol/list'), response)
            raw.assert_called_once()
        with patch.object(self.client, 'raw', side_effect=requests.exceptions.SSLError) as raw:
            with self.assertRaises(requests.exceptions.SSLError):
                self.client.read_with_retries('GET', '/pol/list')
        raw.assert_called_once()

    def test_stop_or_window_end_interrupts_retry_wait(self):
        self.window.side_effect = [None, None, WindowClosed('stopped')]
        with patch.object(self.client, 'raw', side_effect=requests.ReadTimeout) as raw:
            with self.assertRaises(WindowClosed):
                self.client.read_with_retries('GET', '/pol/list')
        raw.assert_called_once()
        self.sleep.assert_called_once_with(1)

    def test_ssl_eof_get_is_retried_but_post_and_certificate_errors_are_not(self):
        eof = requests.exceptions.SSLError('[SSL: UNEXPECTED_EOF_WHILE_READING]')
        response = Mock(status_code=200)
        with patch.object(self.client, 'raw', side_effect=[eof, response]) as raw:
            self.assertIs(self.client.read_with_retries('GET', '/pol/list'), response)
        self.assertEqual(raw.call_count, 2)
        for method, error in [('POST', eof), ('GET', requests.exceptions.SSLError('CERTIFICATE_VERIFY_FAILED'))]:
            with self.subTest(method=method), patch.object(self.client, 'raw', side_effect=error) as raw:
                with self.assertRaises(requests.exceptions.SSLError):
                    self.client.read_with_retries(method, '/pol/list')
                raw.assert_called_once()

    def test_ssl_eof_has_bounded_retries(self):
        with patch.object(self.client, 'raw', side_effect=requests.exceptions.SSLError('SSLEOFError')) as raw:
            with self.assertRaises(requests.exceptions.SSLError):
                self.client.read_with_retries('GET', '/pol/list')
        self.assertEqual(raw.call_count, 4)


if __name__ == '__main__':
    unittest.main()
