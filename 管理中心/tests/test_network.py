"""LAN hosts retain session and same-origin checks; no business tasks are run."""
from argparse import Namespace
from http.server import ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import handler, network_settings


class NetworkTests(unittest.TestCase):
    def test_lan_direct_access_without_cookie_or_host_allowlist(self):
        manager = Mock()
        manager.overview.return_value = {'tools': []}
        manager.save_config.return_value = {'saved': True}
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(manager, 'unused', direct_access=True))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
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
            thread = threading.Thread(target=server.serve_forever, daemon=True)
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
