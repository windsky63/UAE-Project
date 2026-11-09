"""LAN hosts retain session and same-origin checks; no business tasks are run."""
from argparse import Namespace
from http.server import ThreadingHTTPServer
import http.client
import io
import json
import socket
import struct
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import handler, network_settings


class NetworkTests(unittest.TestCase):
    def test_unexpected_api_failure_logs_request_id_and_stack_without_query_or_credentials(self):
        manager=Mock()
        manager.overview.side_effect=RuntimeError('password=private-password')
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(manager,'fixture',direct_access=True))
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        captured=io.StringIO()
        try:
            with patch('server.sys.stderr',captured):
                connection=http.client.HTTPConnection(*server.server_address,timeout=5)
                connection.request('GET','/api/overview?token=private-query-token')
                response=connection.getresponse()
                result=json.loads(response.read());connection.close()
            self.assertEqual(response.status,500)
            self.assertIn(result['request_id'],captured.getvalue())
            self.assertIn('path=/api/overview',captured.getvalue())
            self.assertIn('Traceback',captured.getvalue())
            self.assertNotIn('private-query-token',captured.getvalue())
            self.assertNotIn('private-password',captured.getvalue())
            self.assertIn('[已隐藏]',captured.getvalue())
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_real_client_abort_does_not_log_thread_error_and_server_remains_usable(self):
        manager = Mock()
        manager.overview.return_value = {'tools': [], 'fixture': 'x' * 65536}
        response_ready, release_response, request_finished = (threading.Event() for _ in range(3))
        parent = handler(manager, 'fixture', direct_access=True)

        class PausedHandler(parent):
            def end_headers(self):
                response_ready.set()
                if not release_response.wait(5):
                    raise RuntimeError('fixture did not release response')
                super().end_headers()

            def handle_one_request(self):
                try:
                    super().handle_one_request()
                finally:
                    request_finished.set()

        server = ThreadingHTTPServer(('127.0.0.1', 0), PausedHandler)
        server.handle_error = Mock()
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        client = socket.create_connection(server.server_address, timeout=5)
        try:
            client.sendall(b'GET /api/overview HTTP/1.1\r\nHost: localhost\r\n\r\n')
            self.assertTrue(response_ready.wait(5))
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                              struct.pack('hh' if sys.platform == 'win32' else 'ii', 1, 0))
            client.close()
            release_response.set()
            self.assertTrue(request_finished.wait(5))
            conn = http.client.HTTPConnection(*server.server_address, timeout=5)
            try:
                conn.request('GET', '/api/overview')
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(json.loads(response.read())['tools'], [])
            finally:
                conn.close()
            server.handle_error.assert_not_called()
        finally:
            client.close()
            release_response.set()
            server.shutdown()
            server.server_close()
            thread.join()

    def test_client_disconnect_during_headers_or_body_is_not_reported_as_api_error(self):
        with tempfile.TemporaryDirectory() as directory:
            static = Path(directory)
            (static / 'index.html').write_text('fixture', encoding='utf-8')
            artifact = static / 'fixture.zip'
            artifact.write_bytes(b'fixture download')
            cases = [
                ('GET', '/api/overview', 200),
                ('GET', '/', 200),
                ('GET', '/api/file/fixture', 200),
                ('GET', '/api/session?token=fixture', 303),
                ('GET', '/api/codex/events?id=fixture', 200),
                ('POST', '/api/config', 200),
            ]
            for error in (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                for method, path, status in cases:
                    for failure_at in ((1,) if status == 303 else (1, 2)):
                        with self.subTest(error=error.__name__, method=method, path=path, failure_at=failure_at):
                            manager = Mock()
                            manager.overview.return_value = {'tools': []}
                            manager.save_config.return_value = {'saved': True}
                            manager.artifact.return_value = artifact
                            manager.codex_chat.closed = False
                            manager.codex_chat.events.return_value = [{'seq': 1}]
                            cls = handler(manager, 'fixture', static, direct_access=True)
                            request = cls.__new__(cls)
                            payload = json.dumps(dict(tool='sync', data={}, revision='')).encode()
                            request.rfile = io.BytesIO(
                                f'{method} {path} HTTP/1.1\r\nHost: localhost\r\nX-Admin-Request: 1\r\nContent-Length: {len(payload)}\r\n\r\n'.encode() + payload)
                            request.wfile = Mock()
                            request.wfile.write.side_effect = [None] * (failure_at - 1) + [error('client closed')]
                            request.server = Mock(server_port=8765)
                            request.client_address = ('127.0.0.1', 1)
                            request.connection = Mock()
                            with patch.object(request, 'send_response', wraps=request.send_response) as response:
                                request.handle_one_request()
                            response.assert_called_once_with(status)
                            self.assertTrue(request.close_connection)

    def test_business_errors_still_return_json_error_responses(self):
        manager = Mock()
        manager.overview.side_effect = ValueError('invalid fixture')
        cls = handler(manager, 'fixture', direct_access=True)
        request = cls.__new__(cls)
        request.rfile = io.BytesIO(b'GET /api/overview HTTP/1.1\r\nHost: localhost\r\n\r\n')
        request.wfile = io.BytesIO()
        request.server = Mock(server_port=8765)
        request.client_address = ('127.0.0.1', 1)
        request.connection = Mock()
        request.handle_one_request()
        self.assertIn(b'400 Bad Request', request.wfile.getvalue())
        self.assertIn(b'invalid fixture', request.wfile.getvalue())

    def test_lan_direct_access_without_cookie_or_host_allowlist(self):
        manager = Mock()
        manager.overview.return_value = {'tools': []}
        manager.save_config.return_value = {'saved': True}
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(manager, 'unused', direct_access=True))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            headers = {'Host': f'custom-lan-name:{server.server_port}'}
            conn.request('GET', '/api/overview', headers=headers)
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
            conn.request('POST', '/api/config', body=json.dumps(dict(tool='sync', data={}, revision='')),
                         headers={**headers, 'X-Admin-Request': '1'})
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            conn.close()
            manager.save_config.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_config_defaults_overrides_and_validation(self):
        with tempfile.TemporaryDirectory() as directory, patch('server.HOME', Path(directory)):
            args = Namespace(lan=False, host=None, port=None)
            self.assertEqual(network_settings(args), ('127.0.0.1', 8765, ['127.0.0.1', 'localhost']))
            path = Path(directory) / 'network_config.json'
            path.write_text(json.dumps(dict(host='0.0.0.0', port=8877, allowed_hosts=['tools.lan'])))
            with patch('server.local_addresses', return_value=['127.0.0.1', '192.168.1.20']):
                host, port, allowed = network_settings(args)
            self.assertEqual((host, port), ('0.0.0.0', 8877))
            self.assertIn('192.168.1.20', allowed)
            self.assertIn('tools.lan', allowed)
            args.host, args.port = '127.0.0.1', 8766
            self.assertEqual(network_settings(args)[:2], ('127.0.0.1', 8766))
            args.lan = True
            self.assertEqual(network_settings(args)[0], '0.0.0.0')
            args.port = 70000
            with self.assertRaises(ValueError): network_settings(args)

    def test_lan_session_and_write_auth(self):
        with tempfile.TemporaryDirectory() as directory:
            static = Path(directory)
            (static / 'index.html').write_text('frontend')
            manager = Mock()
            manager.overview.return_value = {'tools': []}
            manager.save_config.return_value = {'saved': True}
            manager.clear_logs.return_value = {'deleted': 2}
            manager.clear_tasks.return_value = {'deleted': 1}
            manager.bulk_action.return_value = {'items': []}
            manager.publications.return_value = {'date': '2026-10-28'}
            manager.push_publications.return_value = {'id': 'fixture-push'}
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler(
                manager, 'test-token', static, allowed_hosts=['127.0.0.1', '192.168.1.20']))
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval':.01}, daemon=True)
            thread.start()

            def request(path, host='192.168.1.20', cookie=None, origin=None, post=False, payload=None):
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                headers = {'Host': f'{host}:{server.server_port}'}
                if cookie: headers['Cookie'] = cookie
                if origin: headers['Origin'] = origin
                data = None
                if post:
                    headers['X-Admin-Request'] = '1'
                    data = json.dumps(payload if payload is not None else dict(tool='sync', action='start', data={}, revision=''))
                conn.request('POST' if post else 'GET', path, body=data, headers=headers)
                response = conn.getresponse()
                result = response.status, dict(response.getheaders()), response.read()
                conn.close()
                return result

            try:
                self.assertEqual(request('/api/overview')[0], 403)
                self.assertEqual(request('/api/session?token=wrong')[0], 403)
                self.assertEqual(request('/api/session?token=test-token', host='evil.example')[0], 403)
                status, headers, _ = request('/api/session?token=test-token')
                self.assertEqual(status, 303)
                self.assertIn('HttpOnly', headers['Set-Cookie'])
                cookie = headers['Set-Cookie'].split(';')[0]
                self.assertEqual(request('/api/overview', cookie=cookie)[0], 200)
                self.assertEqual(request('/api/overview', host='evil.example', cookie=cookie)[0], 403)
                self.assertEqual(request('/api/config', cookie=cookie, origin='http://evil.example', post=True)[0], 403)
                self.assertEqual(request('/api/config', cookie=cookie,
                    origin=f'http://192.168.1.20:{server.server_port}', post=True)[0], 200)
                manager.save_config.assert_called_once()
                self.assertEqual(request('/api/logs/clear', post=True)[0], 403)
                self.assertEqual(request('/api/logs/clear', cookie=cookie, origin='http://evil.example', post=True)[0], 403)
                manager.clear_logs.assert_not_called()
                self.assertEqual(request('/api/logs/clear', cookie=cookie,
                    origin=f'http://192.168.1.20:{server.server_port}', post=True)[0], 200)
                manager.clear_logs.assert_called_once_with('sync', 'history')
                for endpoint, method in (('/api/tasks/clear', manager.clear_tasks),
                                         ('/api/actions/all', manager.bulk_action)):
                    self.assertEqual(request(endpoint, post=True)[0], 403)
                    self.assertEqual(request(endpoint, cookie=cookie, origin='http://evil.example', post=True)[0], 403)
                    method.assert_not_called()
                self.assertEqual(request('/api/tasks/clear', cookie=cookie, post=True)[0], 200)
                manager.clear_tasks.assert_called_once_with('sync')
                self.assertEqual(request('/api/actions/all', cookie=cookie, post=True)[0], 200)
                manager.bulk_action.assert_called_once_with('start')
                self.assertEqual(request('/api/actions/all', cookie=cookie, post=True,
                    payload=dict(action='stop', tools=['nas', 'ima']))[0], 200)
                manager.bulk_action.assert_called_with('stop', tools=['nas', 'ima'])
                calls = manager.bulk_action.call_count
                self.assertEqual(request('/api/actions/all', cookie=cookie, post=True,
                    payload=dict(action='start', tools=None))[0], 400)
                self.assertEqual(manager.bulk_action.call_count, calls)
                self.assertEqual(request('/api/publications?date=2026-10-28', cookie=cookie)[0],200)
                manager.publications.assert_called_once_with('2026-10-28')
                endpoint='/api/publications/push'
                payload=dict(date='2026-10-28',kind='all',confirmed=True)
                self.assertEqual(request(endpoint,post=True,payload=payload)[0],403)
                self.assertEqual(request(endpoint,cookie=cookie,origin='http://evil.example',post=True,payload=payload)[0],403)
                self.assertEqual(request(endpoint,cookie=cookie,post=True,payload={**payload,'confirmed':False})[0],400)
                manager.push_publications.assert_not_called()
                self.assertEqual(request(endpoint,cookie=cookie,post=True,payload=payload)[0],200)
                manager.push_publications.assert_called_once_with('2026-10-28','all')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
