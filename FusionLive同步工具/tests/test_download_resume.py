"""Range recovery across clients, durable checkpoints and a real broken HTTP stream."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import requests

from fusion_client import DownloadPartial, FusionClient, WindowClosed
from ledger import Ledger
from progress import DownloadProgress


class DownloadResumeTests(unittest.TestCase):
    def test_complete_checkpoint_handles_one_byte_file_and_uncommitted_tail(self):
        for data, extra in ((b'x', b''), (self.data, b'uncommitted tail')):
            with self.subTest(size=len(data), extra=bool(extra)):
                self.target.unlink(missing_ok=True)
                item = dict(self.item, contentFormatCode=None, contentSize=str(len(data)))
                part = DownloadPartial(self.runtime/'download_parts', 'project', 'fixture-account', item, lambda: None)
                part.validator = '"version-A"'
                with part.path.open('wb') as stream:
                    stream.write(data); part.digest.update(data); part.size = len(data)
                    part.checkpoint(stream, force=True)
                    stream.write(extra)
                response = self.response(data[-1:], status=206, **{
                    'Content-Range': f'bytes {len(data)-1}-{len(data)-1}/{len(data)}', 'Content-Length': '1'})
                client = self.client()
                with patch.object(client, 'request', return_value=response):
                    result = client.download(item, self.target)
                self.assertEqual(self.target.read_bytes(), data)
                self.assertEqual(result['sha256'], hashlib.sha256(data).hexdigest())

    def test_successful_small_file_keeps_only_original_data_fsync(self):
        client = self.client()
        with patch.object(client, 'request', return_value=self.response()), \
             patch('fusion_client.os.fsync', wraps=__import__('os').fsync) as fsync:
            client.download(self.item, self.target)
        self.assertEqual(fsync.call_count, 1)
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(list((self.runtime/'download_parts').glob('*.json')), [])

    def test_complete_checkpoint_confirms_only_tail_then_publishes_after_restart(self):
        client = self.client()
        with patch.object(client, 'request', return_value=self.response()):
            with self.assertRaises(KeyboardInterrupt):
                client.download(self.item, self.target, verified=Mock(side_effect=KeyboardInterrupt()))
        self.assertEqual(self.parts()[0].read_bytes(), self.data)
        self.assertFalse(self.target.exists())
        response = self.response(self.data[-1:], status=206, **{
            'Content-Range': f'bytes {len(self.data)-1}-{len(self.data)-1}/{len(self.data)}', 'Content-Length': '1'})
        restarted = self.client()
        resumed = Mock()
        with patch.object(restarted, 'request', return_value=response) as request:
            result = restarted.download(self.item, self.target, resumed=resumed)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.kwargs['headers']['Range'], f'bytes={len(self.data)-1}-')
        resumed.assert_called_once_with(len(self.data)-1, len(self.data))
        self.assertEqual(result['sha256'], hashlib.sha256(self.data).hexdigest())
        self.assertEqual(self.target.read_bytes(), self.data)
        self.assertEqual(self.parts(), [])

    def test_complete_checkpoint_rejects_wrong_tail_and_preserves_data_on_confirmation_stop(self):
        client = self.client()
        with patch.object(client, 'request', return_value=self.response()):
            with self.assertRaises(KeyboardInterrupt):
                client.download(self.item, self.target, verified=Mock(side_effect=KeyboardInterrupt()))
        original = self.parts()[0].read_bytes()
        with patch.object(client, 'request', side_effect=WindowClosed('stop before confirmation')):
            with self.assertRaises(WindowClosed):
                client.download(self.item, self.target)
        self.assertEqual(self.parts()[0].read_bytes(), original)
        response = self.response(b'?', status=206, **{
            'Content-Range': f'bytes {len(self.data)-1}-{len(self.data)-1}/{len(self.data)}', 'Content-Length': '1'})
        with patch.object(client, 'request', return_value=response):
            with self.assertRaises(RuntimeError):
                client.download(self.item, self.target)
        self.assertFalse(self.target.exists())

    def test_complete_checkpoint_uses_full_response_when_server_declines_range(self):
        client = self.client()
        with patch.object(client, 'request', return_value=self.response()):
            with self.assertRaises(OSError):
                client.download(self.item, self.target, verified=Mock(side_effect=OSError('database unavailable')))
        self.assertEqual(self.parts()[0].read_bytes(), self.data)
        changed = self.data[:-1]+b'!'
        with patch.object(client, 'request', return_value=self.response(changed, ETag='"version-B"')):
            result = client.download(self.item, self.target)
        self.assertEqual(self.target.read_bytes(), changed)
        self.assertEqual(result['sha256'], hashlib.sha256(changed).hexdigest())

    def test_periodic_checkpoints_are_less_frequent_but_stop_forces_save(self):
        from fusion_client import PARTIAL_CHECKPOINT_BYTES, PARTIAL_CHECKPOINT_SECONDS
        self.assertEqual(PARTIAL_CHECKPOINT_BYTES, 128*1024*1024)
        self.assertEqual(PARTIAL_CHECKPOINT_SECONDS, 30)
        part = DownloadPartial(self.runtime/'download_parts', 'project', 'account', self.item, lambda: None)
        part.validator = '"version-A"'
        with part.path.open('wb') as stream, patch('fusion_client.time.monotonic', return_value=100):
            part.saved_at = 100
            stream.write(self.prefix); part.digest.update(self.prefix); part.size = len(self.prefix)
            with patch('fusion_client.os.fsync', wraps=__import__('os').fsync) as fsync:
                for at in (101, 105, 110, 129):
                    with patch('fusion_client.time.monotonic', return_value=at):
                        part.checkpoint(stream)
                self.assertFalse(part.state_path.exists())
                self.assertFalse(fsync.called)
                part.checkpoint(stream, force=True)
                self.assertEqual(json.loads(part.state_path.read_text())['size'], len(self.prefix))
                self.assertEqual(fsync.call_count, 2)
                stream.write(b'x'); part.digest.update(b'x'); part.size += 1
                with patch('fusion_client.time.monotonic', return_value=129):
                    part.checkpoint(stream)
                self.assertEqual(fsync.call_count, 2)
                with patch('fusion_client.time.monotonic', return_value=130):
                    part.checkpoint(stream)
                self.assertEqual(json.loads(part.state_path.read_text())['size'], len(self.prefix)+1)
                self.assertEqual(fsync.call_count, 4)

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runtime = self.base/'runtime'
        self.target = self.base/'drawing.pdf'
        self.data = b'%PDF-fixture-complete'
        self.prefix = self.data[:8]
        self.item = dict(id='version', revision='A', readAccessStatus='Available',
                         contentFormatCode='pdf', contentSize=str(len(self.data)))
        runtime = patch('fusion_client.RUNTIME', self.runtime)
        runtime.start()
        self.addCleanup(runtime.stop)

    def client(self):
        client = FusionClient('fixture-account', '')
        client.project_id = 'project'
        client.rendition_security = False
        client.check_access_window = Mock()
        self.addCleanup(client.http.close)
        return client

    def response(self, data=None, status=200, **headers):
        response = Mock(status_code=status)
        response.headers = {'ETag': '"version-A"', 'Content-Type': 'application/pdf', **headers}
        response.iter_content.return_value = [self.data if data is None else data]
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        return response

    def interrupt(self, exception=None, headers=None):
        client = self.client()
        response = self.response(**(headers or {}))
        exception = exception or requests.ConnectionError('broken stream')
        def chunks(*args):
            yield self.prefix
            raise exception
        response.iter_content = chunks
        with patch.object(client, 'request', return_value=response):
            with self.assertRaises(type(exception)):
                client.download(self.item, self.target)
        self.assertFalse(self.target.exists())

    def remaining_response(self, **headers):
        return self.response(self.data[len(self.prefix):], status=206,
            **{'Content-Range': f'bytes {len(self.prefix)}-{len(self.data)-1}/{len(self.data)}',
               'Content-Length': str(len(self.data)-len(self.prefix)), **headers})

    def parts(self):
        return list((self.runtime/'download_parts').glob('*.part'))

    def test_network_stop_and_window_interrupt_resume_after_client_restart(self):
        for exception in (requests.ConnectionError('disconnect'), KeyboardInterrupt(), WindowClosed('window')):
            with self.subTest(exception=type(exception).__name__):
                self.target.unlink(missing_ok=True)
                self.interrupt(exception)
                self.assertEqual(self.parts()[0].read_bytes(), self.prefix)
                client = self.client()
                resumed = Mock()
                with patch.object(client, 'request', return_value=self.remaining_response()) as request:
                    result = client.download(self.item, self.target, resumed=resumed)
                self.assertEqual(request.call_args.kwargs['headers']['Range'], f'bytes={len(self.prefix)}-')
                self.assertEqual(request.call_args.kwargs['headers']['If-Range'], '"version-A"')
                resumed.assert_called_once_with(len(self.prefix), len(self.data))
                self.assertEqual(self.target.read_bytes(), self.data)
                self.assertEqual(result['sha256'], hashlib.sha256(self.data).hexdigest())
                self.assertEqual(self.parts(), [])

    def test_server_ignoring_range_or_changing_validator_replaces_instead_of_appending(self):
        self.interrupt()
        client = self.client()
        with patch.object(client, 'request', return_value=self.response(ETag='"version-B"')):
            client.download(self.item, self.target)
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_changed_metadata_and_modified_local_prefix_restart_from_zero(self):
        for change in ('metadata', 'prefix', 'state'):
            with self.subTest(change=change):
                self.target.unlink(missing_ok=True)
                self.interrupt()
                item = dict(self.item)
                if change == 'metadata':
                    item['revision'] = 'B'
                elif change == 'prefix':
                    self.parts()[0].write_bytes(b'BADBYTES')
                else:
                    self.parts()[0].with_suffix('.json').write_text('{bad', encoding='utf-8')
                client = self.client()
                with patch.object(client, 'request', return_value=self.response()) as request:
                    client.download(item, self.target)
                self.assertNotIn('Range', request.call_args.kwargs['headers'])
                self.assertEqual(self.target.read_bytes(), self.data)

    def test_uncheckpointed_tail_is_truncated_before_resuming(self):
        self.interrupt()
        with self.parts()[0].open('ab') as stream:
            stream.write(b'uncommitted bytes')
        client = self.client()
        with patch.object(client, 'request', return_value=self.remaining_response()):
            client.download(self.item, self.target)
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_bad_range_validator_total_and_encoding_never_publish(self):
        for headers in ({'Content-Range': 'bytes 0-19/20'}, {'ETag': '"changed"'},
                        {'Content-Range': 'bytes 8-99/100'}, {'Content-Encoding': 'gzip'},
                        {'Content-Length': '1'}):
            with self.subTest(headers=headers):
                self.interrupt()
                client = self.client()
                with patch.object(client, 'request', return_value=self.remaining_response(**headers)):
                    with self.assertRaises(RuntimeError):
                        client.download(self.item, self.target)
                self.assertFalse(self.target.exists())
                self.assertEqual(self.parts(), [])

    def test_missing_or_weak_validator_does_not_leave_unsafe_partial(self):
        for etag in ('', 'W/"version-A"'):
            with self.subTest(etag=etag):
                self.interrupt(headers={'ETag': etag})
                self.assertEqual(self.parts(), [])

    def test_old_last_modified_can_authorize_resume_but_recent_date_cannot(self):
        self.assertEqual(DownloadPartial.response_validator({
            'Last-Modified': 'Tue, 03 Nov 2026 00:00:00 GMT',
            'Date': 'Tue, 03 Nov 2026 00:02:00 GMT'}), 'Tue, 03 Nov 2026 00:00:00 GMT')
        self.assertIsNone(DownloadPartial.response_validator({
            'Last-Modified': 'Tue, 03 Nov 2026 00:00:00 GMT',
            'Date': 'Tue, 03 Nov 2026 00:00:20 GMT'}))

    def test_connection_failure_before_response_keeps_existing_checkpoint(self):
        self.interrupt()
        client = self.client()
        with patch.object(client, 'request', side_effect=requests.ConnectionError('offline')):
            with self.assertRaises(requests.ConnectionError):
                client.download(self.item, self.target)
        self.assertEqual(self.parts()[0].read_bytes(), self.prefix)

    def test_range_416_falls_back_to_one_full_request(self):
        self.interrupt()
        client = self.client()
        error = requests.HTTPError(response=Mock(status_code=416))
        with patch.object(client, 'request', side_effect=[error, self.response()]) as request:
            client.download(self.item, self.target)
        self.assertEqual(request.call_count, 2)
        self.assertNotIn('Range', request.call_args.kwargs['headers'])
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_completed_run_cleanup_preserves_resume_pair_but_removes_legacy_scratch(self):
        self.interrupt()
        orphan = self.runtime/'download_parts/legacy.part'
        orphan.write_bytes(b'legacy')
        ledger = Ledger(self.runtime/'ledger.sqlite3', base_dir=self.base)
        self.addCleanup(ledger.close)
        ledger.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,metadata)
            VALUES('project','version','document','folder','drawing.pdf',?)''', (json.dumps(self.item),))
        ledger.db.commit()
        ledger.clean_partial_downloads()
        self.assertFalse(orphan.exists())
        self.assertEqual(self.parts()[0].read_bytes(), self.prefix)
        ledger.db.execute("UPDATE files SET active=0")
        ledger.clean_partial_downloads()
        self.assertEqual(self.parts(), [])

    def test_short_stream_without_content_length_retains_resumable_prefix(self):
        client = self.client()
        with patch.object(client, 'request', return_value=self.response(self.prefix)):
            with self.assertRaises(requests.ConnectionError):
                client.download(self.item, self.target)
        self.assertEqual(self.parts()[0].read_bytes(), self.prefix)

    def test_resumed_bytes_do_not_inflate_current_run_network_speed(self):
        ledger = Mock()
        ledger.one.return_value = dict(total=1, total_bytes=20, done=0, done_bytes=0)
        with patch('progress.emit'):
            progress = DownloadProgress(ledger, 'project')
            progress.begin(dict(filename='file', size=20))
            progress.resume(8, 20)
            progress.update(12, 20)
            progress.complete(20)
        self.assertEqual(progress.wire_bytes, 12)
        self.assertEqual(progress.done_bytes, 20)

    def test_checkpoint_survives_abrupt_process_exit(self):
        script = r'''
import os,sys,json
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from fusion_client import DownloadPartial
part=DownloadPartial(Path(sys.argv[2]),'project','fixture-account',json.loads(sys.argv[3]),lambda:None)
part.validator='"version-A"'
with part.path.open('wb') as stream:
    stream.write(b'%PDF-fix'); part.digest.update(b'%PDF-fix'); part.size=8
    part.checkpoint(stream,force=True)
    stream.write(b'uncommitted tail'); stream.flush()
    os._exit(17)
'''
        app = Path(__file__).resolve().parents[1]/'app'
        child = subprocess.run([sys.executable, '-B', '-c', script, str(app),
            str(self.runtime/'download_parts'), json.dumps(self.item)], capture_output=True, timeout=30)
        self.assertEqual(child.returncode, 17, child.stderr)
        client = self.client()
        with patch.object(client, 'request', return_value=self.remaining_response()):
            client.download(self.item, self.target)
        self.assertEqual(self.target.read_bytes(), self.data)

    def test_real_http_disconnect_then_range_request(self):
        data = b'%PDF-' + b'x'*(768*1024)
        requests_seen = []
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                offset = int(self.headers.get('Range', 'bytes=0-')[6:-1])
                requests_seen.append((offset, self.headers.get('If-Range')))
                self.send_response(206 if offset else 200)
                self.send_header('ETag', '"real-stream"')
                self.send_header('Content-Length', str(len(data)-offset))
                if offset:
                    self.send_header('Content-Range', f'bytes {offset}-{len(data)-1}/{len(data)}')
                self.end_headers()
                if not offset:
                    self.wfile.write(data[:300*1024]); self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                else:
                    self.wfile.write(data[offset:])
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        item = dict(self.item, contentSize=str(len(data)))
        for attempt in range(2):
            client = self.client()
            def request(method, path, **kwargs):
                return client.http.get(f'http://127.0.0.1:{server.server_port}/file',
                    headers=kwargs['headers'], stream=True, timeout=(5, 5))
            with patch.object(client, 'request', side_effect=request):
                if not attempt:
                    with self.assertRaises(requests.RequestException):
                        client.download(item, self.target)
                else:
                    client.download(item, self.target)
        self.assertEqual(requests_seen, [(0, None), (256*1024, '"real-stream"')])
        self.assertEqual(self.target.read_bytes(), data)


if __name__ == '__main__':
    unittest.main()
