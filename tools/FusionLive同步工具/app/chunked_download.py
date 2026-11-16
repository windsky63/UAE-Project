"""Version-scoped, bounded HTTP ranges exposed to the normal download verifier."""
from contextlib import contextmanager
import re
from urllib.parse import parse_qs, urlsplit

import requests


# Decimal bytes: the historical whole-object failures start below 2 GiB.
LARGE_FILE_BYTES = 1_800_000_000
DOWNLOAD_CHUNK_BYTES = 300_000_000


def download_url(client, item, expected):
    virtual = item.get('isVirtualLink') == 'true'
    object_id = item.get('virtualLinkId') if virtual else item['id']
    if not object_id:
        raise RuntimeError('大文件下载缺少平台对象标识。')
    with client.request('POST', '/pol/getDocumentDownloadUrlAction', data={
        'versionIds': object_id, 'objectTypes': 'VIRTUAL_LINK' if virtual else 'DOCUMENT',
        'saveAs': 'filename', 'appendRevision': 'false', 'appendStatus': 'false',
        'clientPId': client.project_id,
    }) as response:
        descriptor = response.json()
    if (not isinstance(descriptor, dict) or descriptor.get('errors')
            or descriptor.get('multiple') is not False
            or descriptor.get('filesizes') != [expected]
            or descriptor.get('totalSize') != expected
            or not isinstance(descriptor.get('openUrls'), list)
            or len(descriptor['openUrls']) != 1):
        raise RuntimeError('平台大文件下载描述与单个原件大小不符。')
    # useChunks=false is also returned for the tested 1.92 GB sample; its
    # GetContentPart URL still supports bounded Range requests.
    url = descriptor['openUrls'][0]
    if not isinstance(url, str):
        raise RuntimeError('平台大文件下载地址无效。')
    parsed = urlsplit(url)
    allowed_ids = {str(item['id']), str(object_id)}
    if ((parsed.scheme or parsed.netloc) and (parsed.scheme, parsed.netloc) != ('https', 'us.fusion.live')
            or parsed.username or parsed.password or parsed.fragment
            or parsed.path not in {f'/servlet/GetContentPart/{value}' for value in allowed_ids}):
        raise RuntimeError('拒绝非平台或其他对象的大文件下载地址。')
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (('clientPId' in query and query['clientPId'] != [str(client.project_id)])
            or ('jsClientProjectId' in query and query['jsClientProjectId'] != [str(client.project_id)])
            or ('DMW_OBJECTID' in query and query['DMW_OBJECTID'] != [parsed.path.rsplit('/', 1)[-1]])):
        raise RuntimeError('平台大文件下载地址中的项目或对象不符。')
    return url


class ChunkedResponse:
    """A logical full/suffix response, streamed one validated segment at a time."""
    def __init__(self, client, url, expected, start, response, validator, validator_for):
        self.client, self.url, self.expected, self.start = client, url, expected, start
        self.response, self.validator, self.validator_for = response, validator, validator_for
        self.status_code = 206 if start else 200
        self.headers = requests.structures.CaseInsensitiveDict(response.headers)
        self.headers.pop('Content-Range', None)
        self.headers['Content-Length'] = str(expected-start)
        if start:
            self.headers['Content-Range'] = f'bytes {start}-{expected-1}/{expected}'

    def close(self):
        if self.response is not None:
            self.response.close()
            self.response = None

    def iter_content(self, chunk_size):
        start = self.start
        while start < self.expected:
            end = min(start+DOWNLOAD_CHUNK_BYTES, self.expected)-1
            self.client.check_access_window()
            if self.response is None:
                headers = {'Accept-Encoding':'identity', 'Range':f'bytes={start}-{end}'}
                if self.validator:
                    headers['If-Range'] = self.validator
                self.response = self.client.request('GET', self.url, headers=headers, stream=True)
            response = self.response
            validate_range(response, start, end, self.expected)
            if self.validator_for(response.headers) != self.validator:
                raise RuntimeError('大文件分段版本标识发生变化，拒绝拼接。')
            received = 0
            for block in response.iter_content(chunk_size):
                self.client.check_access_window()
                if not block:
                    continue
                received += len(block)
                if received > end-start+1:
                    raise RuntimeError('大文件分段超出请求范围，拒绝保存。')
                yield block
            if received != end-start+1:
                raise requests.ConnectionError('大文件分段提前结束。')
            self.close()
            start = end+1


def validate_range(response, start, end, expected):
    if (response.status_code != 206
            or response.headers.get('Content-Range') != f'bytes {start}-{end}/{expected}'
            or response.headers.get('Content-Length') != str(end-start+1)
            or response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', '')):
        raise RuntimeError('大文件分段响应状态、字节范围或大小不符。')
    if 'html' in response.headers.get('Content-Type', '').lower():
        raise RuntimeError('大文件分段返回网页，拒绝保存。')


@contextmanager
def open_chunked_download(client, item, expected, headers, validator_for):
    url = download_url(client, item, expected)
    match = re.fullmatch(r'bytes=(\d+)-', headers.get('Range', ''))
    start = int(match[1]) if match else 0
    if start >= expected:
        raise RuntimeError('大文件续传起点超出文件范围。')
    response = None
    logical = None
    try:
        def request_at(offset):
            end = min(offset+DOWNLOAD_CHUNK_BYTES, expected)-1
            request_headers = {'Accept-Encoding':'identity', 'Range':f'bytes={offset}-{end}'}
            if offset and headers.get('If-Range'):
                request_headers['If-Range'] = headers['If-Range']
            return client.request('GET', url, headers=request_headers, stream=True)
        response = request_at(start)
        validator = validator_for(response.headers)
        if start and (response.status_code == 200 or not validator or validator != headers.get('If-Range')):
            # A partial made by the other endpoint cannot authorize concatenation
            # unless this endpoint confirms the same strong validator.
            response.close()
            start = 0
            response = request_at(start)
            validator = validator_for(response.headers)
        validate_range(response, start, min(start+DOWNLOAD_CHUNK_BYTES, expected)-1, expected)
        logical = ChunkedResponse(client, url, expected, start, response, validator, validator_for)
        yield logical
    finally:
        if logical is not None:
            logical.close()
        elif response is not None:
            response.close()
