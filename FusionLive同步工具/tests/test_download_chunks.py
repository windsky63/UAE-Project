"""Large-file transport boundaries, recovery, and real HTTP chunk streaming."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

import requests

from chunked_download import download_url
from fusion_client import DownloadPartial, FusionClient, WindowClosed


class ChunkDownloadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.target = self.base/'file.zip'
        self.runtime = self.base/'runtime'
        self.data = b'PK\x03\x04fixture-content-for-ranges'
        self.item = dict(id='20317143', filename='file.zip', contentSize=str(len(self.data)),
                         readAccessStatus='Available', contentFormatCode='zip', revision='A')
        self.client = FusionClient('account','')
        self.client.project_id = '17580'
        self.client.rendition_security = False
        self.client.check_access_window = Mock()
        self.addCleanup(self.client.http.close)
        for target, value in [('fusion_client.RUNTIME',self.runtime),
                              ('chunked_download.LARGE_FILE_BYTES',1),
                              ('chunked_download.DOWNLOAD_CHUNK_BYTES',8)]:
            self.enterContext(patch(target,value))
        self.calls = []
        self.responses = []
        self.etag = None
        self.modify_response = None

    def descriptor(self):
        return dict(errors=[],multiple=False,useChunks=False,filesizes=[len(self.data)],
                    totalSize=len(self.data),openUrls=[
                    '/servlet/GetContentPart/20317143?auditAsDownload=true&objectType=DOCUMENT&DMW_OBJECTID=20317143&clientPId=17580'])

    def request(self, method, path, **kwargs):
        self.calls.append((method,path,kwargs))
        response = requests.Response()
        response.status_code = 200
        response.url = 'https://us.fusion.live'+path
        response.headers = requests.structures.CaseInsensitiveDict({'Content-Type':'application/zip'})
        if method == 'POST':
            response._content = json.dumps(self.descriptor()).encode()
        else:
            start,end = map(int,kwargs['headers']['Range'][6:].split('-'))
            response.status_code = 206
            response.headers.update({'Content-Range':f'bytes {start}-{end}/{len(self.data)}',
                                     'Content-Length':str(end-start+1)})
            if self.etag:
                response.headers['etag'] = self.etag
            response._content = self.data[start:end+1]
            if self.modify_response:
                self.modify_response(response,start,end)
        response._content_consumed = True
        response.close = Mock(wraps=response.close)
        self.responses.append(response)
        return response

    def run_download(self, **kwargs):
        with patch.object(self.client,'request',side_effect=self.request):
            return self.client.download(self.item,self.target,**kwargs)

    def parts(self):
        return list((self.runtime/'download_parts').glob('*.part'))

    def test_full_transfer_checks_ranges_final_short_chunk_and_publish_callback(self):
        def verified(result):
            self.assertFalse(self.target.exists())
            self.assertEqual(result['bytes'],len(self.data))
        result = self.run_download(verified=verified)
        self.assertEqual(self.target.read_bytes(),self.data)
        self.assertEqual(result['sha256'],hashlib.sha256(self.data).hexdigest())
        gets=[call for call in self.calls if call[0]=='GET']
        self.assertEqual([c[2]['headers']['Range'] for c in gets],
                         [f'bytes={start}-{min(start+8,len(self.data))-1}' for start in range(0,len(self.data),8)])
        self.assertTrue(all('GetContentPart' in c[1] for c in gets))
        self.assertTrue(all(r.close.called for r in self.responses))
        self.assertEqual(self.parts(),[])

    def test_small_files_keep_existing_endpoint(self):
        with patch('chunked_download.LARGE_FILE_BYTES',len(self.data)+1):
            response = requests.Response()
            response.status_code=200
            response.headers={'Content-Length':str(len(self.data))}
            response._content=self.data
            response._content_consumed=True
            with patch.object(self.client,'request',return_value=response) as request:
                self.client.download(self.item,self.target)
            self.assertEqual(request.call_args.args[1],'/servlet/HtmlDownloadServlet')
        self.assertEqual(self.target.read_bytes(),self.data)

    def test_wrong_status_range_length_encoding_or_html_never_publishes(self):
        for change in [lambda r:setattr(r,'status_code',200),
                       lambda r:r.headers.update({'Content-Range':'bytes 1-8/99'}),
                       lambda r:r.headers.update({'Content-Length':'999'}),
                       lambda r:r.headers.update({'Content-Encoding':'gzip'}),
                       lambda r:r.headers.update({'Content-Type':'text/html'})]:
            with self.subTest(change=change):
                self.modify_response=lambda response,start,end:change(response)
                with self.assertRaises(RuntimeError): self.run_download()
                self.assertFalse(self.target.exists())
                self.assertEqual(self.parts(),[])
                self.assertTrue(all(r.close.called for r in self.responses))

    def test_truncated_oversized_and_changed_later_segments_never_publish(self):
        for mode in ('truncated','oversized','etag'):
            with self.subTest(mode=mode):
                self.etag='"A"'
                def change(response,start,end):
                    if start==8:
                        if mode=='truncated': response._content=response._content[:-1]
                        elif mode=='oversized': response._content+=b'!'
                        else: response.headers['ETag']='"B"'
                self.modify_response=change
                with self.assertRaises((RuntimeError,requests.ConnectionError)): self.run_download()
                self.assertFalse(self.target.exists())
                # A truncated strong-validator prefix may be retained, but never published.
                if self.parts():
                    for part in self.parts(): part.unlink()
                    for state in (self.runtime/'download_parts').glob('*.json'): state.unlink()

    def test_no_validator_stop_discards_prefix_and_next_run_restarts_at_zero(self):
        progress=Mock(side_effect=WindowClosed('stop'))
        with self.assertRaises(WindowClosed): self.run_download(progress=progress)
        self.assertEqual(self.parts(),[])
        self.client.check_access_window.reset_mock()
        self.calls.clear()
        self.run_download()
        self.assertEqual([c for c in self.calls if c[0]=='GET'][0][2]['headers']['Range'],'bytes=0-7')

    def test_strong_validator_checkpoint_resumes_and_confirms_complete_tail(self):
        self.etag='"A"'
        with self.assertRaises(WindowClosed):
            self.run_download(progress=Mock(side_effect=WindowClosed('stop')))
        self.assertEqual(self.parts()[0].read_bytes(),self.data[:8])
        self.calls.clear()
        resumed=Mock()
        with self.assertRaises(OSError):
            self.run_download(resumed=resumed,verified=Mock(side_effect=OSError('publish failed')))
        resumed.assert_called_once_with(8,len(self.data))
        first=[c for c in self.calls if c[0]=='GET'][0]
        self.assertEqual(first[2]['headers'],{'Accept-Encoding':'identity','Range':'bytes=8-15','If-Range':'"A"'})
        self.calls.clear()
        self.run_download()
        self.assertEqual([c for c in self.calls if c[0]=='GET'][0][2]['headers']['Range'],f'bytes={len(self.data)-1}-{len(self.data)-1}')
        self.assertEqual(self.target.read_bytes(),self.data)

    def test_old_endpoint_validator_cannot_authorize_new_endpoint_prefix(self):
        partial=DownloadPartial(self.runtime/'download_parts','17580','account',self.item,lambda:None)
        partial.validator='"old-endpoint"'
        with partial.path.open('wb') as output:
            output.write(self.data[:8]);partial.digest.update(self.data[:8]);partial.size=8
            partial.checkpoint(output,force=True)
        self.run_download()
        ranges=[c[2]['headers']['Range'] for c in self.calls if c[0]=='GET']
        self.assertEqual(ranges[:2],['bytes=8-15','bytes=0-7'])
        self.assertEqual(self.target.read_bytes(),self.data)

    def test_descriptor_rejects_wrong_size_multiple_objects_and_foreign_urls(self):
        base=self.descriptor()
        for changes in [dict(filesizes=[len(self.data)+1]),dict(totalSize=1),dict(multiple=True),
                        dict(errors=['denied']),dict(openUrls=['https://other.test/servlet/GetContentPart/20317143']),
                        dict(openUrls=['/servlet/GetContentPart/other']),
                        dict(openUrls=['/servlet/GetContentPart/20317143?clientPId=other']),
                        dict(openUrls=['/servlet/GetContentPart/20317143?DMW_OBJECTID=other'])]:
            with self.subTest(changes=changes),patch.object(self.client,'request') as request:
                request.return_value.__enter__.return_value.json.return_value={**base,**changes}
                with self.assertRaises(RuntimeError): download_url(self.client,self.item,len(self.data))
                self.assertEqual(request.call_count,1)

    def test_stop_between_segments_prevents_next_network_request(self):
        stopped=False
        def progress(done,total):
            nonlocal stopped
            stopped=True
        def check():
            if stopped: raise WindowClosed('stop between chunks')
        self.client.check_access_window.side_effect=check
        with self.assertRaises(WindowClosed): self.run_download(progress=progress)
        self.assertEqual(len([c for c in self.calls if c[0]=='GET']),1)
        self.assertTrue(all(r.close.called for r in self.responses))

    def test_denied_original_permission_never_requests_a_download_url(self):
        for changes in ({'readAccessStatus':'Denied'},{}):
            with self.subTest(changes=changes),patch.object(self.client,'request') as request:
                self.client.rendition_security=not bool(changes)
                self.client.native_user=False
                with self.assertRaises(RuntimeError):
                    self.client.download({**self.item,**changes},self.target)
                request.assert_not_called()

    def test_virtual_link_uses_its_authorized_object_and_project(self):
        virtual={**self.item,'isVirtualLink':'true','virtualLinkId':'link'}
        with patch.object(self.client,'request') as request:
            request.return_value.__enter__.return_value.json.return_value=self.descriptor()
            url=download_url(self.client,virtual,len(self.data))
        self.assertIn('/GetContentPart/20317143',url)
        self.assertEqual(request.call_args.kwargs['data']['versionIds'],'link')
        self.assertEqual(request.call_args.kwargs['data']['objectTypes'],'VIRTUAL_LINK')

    def test_real_http_server_ranges_and_broken_stream(self):
        data=self.data
        hits=[]
        broken=False
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def log_message(self,*args): pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length','0')))
                body=json.dumps({'errors':[],'multiple':False,'useChunks':False,'filesizes':[len(data)],
                                 'totalSize':len(data),'openUrls':['/servlet/GetContentPart/20317143']}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            def do_GET(self):
                hits.append(self.headers.get('Range'))
                start,end=map(int,self.headers['Range'][6:].split('-'))
                body=data[start:end+1]
                self.send_response(206)
                self.send_header('Content-Type','application/zip')
                self.send_header('Content-Length',str(len(body)))
                self.send_header('Content-Range',f'bytes {start}-{end}/{len(data)}')
                self.end_headers()
                self.wfile.write(body[:2] if broken else body)
                self.wfile.flush()
                if broken:
                    self.close_connection=True
                    self.connection.shutdown(socket.SHUT_RDWR)
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        self.addCleanup(thread.join,2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_port}'
        def request(method,path,**kwargs):
            return self.client.http.request(method,base+urlsplit(path).path,timeout=(2,2),**kwargs)
        with patch.object(self.client,'request',side_effect=request):
            result=self.client.download(self.item,self.target)
            self.assertEqual(result['sha256'],hashlib.sha256(data).hexdigest())
            self.assertEqual(self.target.read_bytes(),data)
            self.target.unlink()
            broken=True
            with self.assertRaises(requests.RequestException): self.client.download(self.item,self.target)
        self.assertFalse(self.target.exists())
        self.assertEqual(self.parts(),[])
        self.assertEqual(hits[0],'bytes=0-7')


if __name__=='__main__': unittest.main()
