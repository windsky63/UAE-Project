"""HTTP session for the observed FusionLive web endpoints (not a public SDK)."""

from paths import HOME, CODE
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import json
import hashlib
import os
import errno
import re
import subprocess
import shutil
import sys
import time
import uuid
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

BASE = 'https://us.fusion.live'
LOCAL = timezone(timedelta(hours=8))
RUNTIME = HOME / 'runtime'
PARTIAL_CHECKPOINT_BYTES = 128 * 1024 * 1024
PARTIAL_CHECKPOINT_SECONDS = 30


class WindowClosed(RuntimeError):
    pass


class LogoutError(RuntimeError):
    pass


class SessionExpiredError(RuntimeError):
    pass


class AuthenticationRequiredError(RuntimeError):
    """An explicit password/MFA rejection requires configuration, not retries."""
    pass


def temporary_network_error(exc):
    """Retry broken transport, never certificate or TLS configuration errors."""
    if isinstance(exc, requests.exceptions.SSLError):
        evidence = str(exc).upper()
        if any(word in evidence for word in ('CERTIFICATE', 'HOSTNAME', 'WRONG_VERSION', 'PROTOCOL_VERSION')):
            return False
        return 'UNEXPECTED_EOF_WHILE_READING' in evidence or 'SSLEOFERROR' in evidence
    if isinstance(exc, requests.HTTPError):
        return exc.response is not None and exc.response.status_code in (502, 503, 504)
    return isinstance(exc, (requests.Timeout, requests.ConnectionError))


class PlatformActionError(RuntimeError):
    """Safe evidence for a platform business rejection, not a transport failure."""
    def __init__(self, path, response, tree):
        codes = []
        for key in ('errorCode', 'exceptionCode', 'code'):
            value = tree.get(key) or tree.findtext('.//' + key)
            if value and re.fullmatch(r'[A-Za-z0-9_.:-]{1,64}', value):
                codes.append(key + '=' + value)
        digest = hashlib.sha256(response.content).hexdigest()[:16]
        status = response.status_code
        detail = '；'.join(codes) or '平台未提供可识别错误代码'
        super().__init__(f'平台返回操作失败：{path}；HTTP={status}；'
                         f'isActionSucessful={tree.get("isActionSucessful")}；{detail}；响应摘要={digest}')


def now():
    return datetime.now(LOCAL)


def check_window(at=None, stop_minute=630):
    if (RUNTIME / 'stop.request').exists():
        raise WindowClosed('收到停止请求。')
    at = at or now()
    minute = at.hour * 60 + at.minute
    if not 120 <= minute < stop_minute:
        raise WindowClosed('仅允许北京时间 02:00—10:30 发起正式下载任务。')


class Handoff(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}
        self.inside = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == 'form':
            self.inside = a.get('id') == 'logonHandoff'
            if self.inside:
                self.action = a.get('action')
        elif tag == 'input' and self.inside and a.get('name'):
            self.fields[a['name']] = a.get('value', '')

    def handle_endtag(self, tag):
        if tag == 'form':
            self.inside = False


class DownloadPartial:
    """Durable, version-bound prefix; uncheckpointed bytes are never reused."""
    def __init__(self, folder, project, username, item, check):
        from inventory import CONTENT_FIELDS, fingerprint
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(json.dumps([project, username, item['id'],
            item.get('virtualLinkId')], ensure_ascii=False).encode()).hexdigest()
        self.path = folder / (key + '.part')
        self.state_path = folder / (key + '.json')
        self.project, self.file_id = project, item['id']
        self.identity = fingerprint(item, CONTENT_FIELDS)
        self.size, self.digest, self.validator = 0, hashlib.sha256(), None
        self.before_last_digest, self.last_byte = None, None
        self.saved_size, self.saved_at = 0, time.monotonic()
        try:
            state = json.loads(self.state_path.read_text(encoding='utf-8'))
            size = state['size']
            validator = state['validator']
            if (state['identity'] != self.identity or type(size) is not int or size <= 0
                    or self.path.is_symlink() or self.path.stat().st_size < size
                    or not isinstance(validator, str) or not validator
                    or '\r' in validator or '\n' in validator):
                raise ValueError('Invalid partial identity')
            with self.path.open('rb') as stream:
                remaining = size
                while remaining:
                    check()
                    block = stream.read(min(1024 * 1024, remaining))
                    if not block:
                        raise ValueError('Truncated partial')
                    if remaining == len(block):
                        self.before_last_digest = self.digest.copy()
                        self.before_last_digest.update(block[:-1])
                        self.last_byte = block[-1:]
                    self.digest.update(block)
                    remaining -= len(block)
            if self.digest.hexdigest() != state['sha256']:
                raise ValueError('Modified partial')
            self.size = self.saved_size = size
            self.validator = validator
        except (OSError, ValueError, KeyError, TypeError):
            self.discard()

    def discard(self):
        self.path.unlink(missing_ok=True)
        self.state_path.unlink(missing_ok=True)
        self.size, self.digest, self.validator = 0, hashlib.sha256(), None

    @staticmethod
    def response_validator(headers):
        etag = headers.get('ETag', '')
        if etag.startswith('"') and etag.endswith('"'):
            return etag  # Weak ETags cannot authorize byte concatenation.
        modified, date = headers.get('Last-Modified'), headers.get('Date')
        if modified and date:
            try:
                if (parsedate_to_datetime(date) - parsedate_to_datetime(modified)).total_seconds() >= 60:
                    return modified
            except (ValueError, TypeError, OverflowError):
                pass
        return None

    def checkpoint(self, stream, force=False):
        if not self.validator or not self.size:
            return
        if (not force and self.size-self.saved_size < PARTIAL_CHECKPOINT_BYTES
                and time.monotonic()-self.saved_at < PARTIAL_CHECKPOINT_SECONDS):
            return
        stream.flush()
        os.fsync(stream.fileno())
        state = dict(identity=self.identity, size=self.size, sha256=self.digest.hexdigest(),
                     validator=self.validator, project=self.project, file_id=self.file_id)
        temporary = self.state_path.with_suffix('.json.tmp')
        with temporary.open('w', encoding='utf-8') as output:
            json.dump(state, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.state_path)
        self.saved_size, self.saved_at = self.size, time.monotonic()


class FusionClient:
    def __init__(self, username, password):
        self.username = username
        self.password = password
        self.http = requests.Session()
        self.http.headers.update({'User-Agent': 'FusionLive read-only document sync'})
        self.project_id = None
        self.cabinet_id = None
        self.project_name = None
        self.auth_attempted = False
        self.last_request = 0
        self.logout_result = None
        self.rendition_security = None
        self.native_user = None
        self.watchdog = None
        self.watchdog_log = None
        self.logout_recovery_allowed = True

    def raw(self, method, path, **kwargs):
        url = urljoin(BASE, path)
        # Allow slow connections, HTTPS handshakes and responses on every endpoint.
        timeout = kwargs.pop('timeout', (90, 90))
        for _ in range(8):
            if urlsplit(url).netloc != 'us.fusion.live' or urlsplit(url).scheme != 'https':
                raise RuntimeError('拒绝向非平台地址发送请求。')
            r = self.http.request(method, url, timeout=timeout, allow_redirects=False, **kwargs)
            if r.status_code not in (301, 302, 303, 307, 308):
                return r
            target = urljoin(url, r.headers['Location'])
            if r.status_code == 303 or (r.status_code in (301, 302) and method.upper() == 'POST'):
                method = 'GET'
                kwargs.pop('data', None)
                kwargs.pop('json', None)
            kwargs.pop('params', None)
            r.close()
            url = target
        raise RuntimeError('重定向次数过多。')

    def check_access_window(self):
        check_window()

    def request(self, method, path, **kwargs):
        self.check_access_window()
        time.sleep(max(0, 0.5 - (time.monotonic() - self.last_request)))
        self.check_access_window()
        self.last_request = time.monotonic()
        if self.project_id:
            params = dict(kwargs.pop('params', {}))
            params.setdefault('jsClientProjectId', self.project_id)
            kwargs['params'] = params
        r = self.read_with_retries(method, path, **kwargs)
        if self.watchdog:
            self.update_watchdog()
        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            try:
                if (urlsplit(path).path == '/servlet/HtmlDownloadServlet'
                        or urlsplit(path).path.startswith('/servlet/GetContentPart/')):
                    from download_diagnostics import describe_failure
                    evidence, message = describe_failure(r)
                    exc.download_evidence = evidence
                    exc.args = (message,)
            finally:
                r.close()
            raise
        if not kwargs.get('stream') and 'fs-login-username' in r.text[:100000] and '/resources/' not in path:
            raise SessionExpiredError('登录会话失效，已停止请求。')
        return r

    def read_with_retries(self, method, path, **kwargs):
        # Replay only reads. Authentication/workspace POSTs must not be repeated.
        delays = (5, 15, 30)
        for attempt in range(len(delays) + 1):
            self.check_access_window()
            try:
                response = self.raw(method, path, **kwargs)
            except requests.exceptions.SSLError as exc:
                if method.upper() != 'GET' or not temporary_network_error(exc) or attempt == len(delays):
                    raise
                reason = type(exc).__name__
            except (requests.Timeout, requests.ConnectionError) as exc:
                if method.upper() != 'GET' or attempt == len(delays):
                    raise
                reason = type(exc).__name__
            else:
                if method.upper() != 'GET' or response.status_code not in (502, 503, 504):
                    return response
                if attempt == len(delays):
                    return response  # request() reports the final HTTP error.
                reason = f'HTTP {response.status_code}'
                response.close()
            delay = delays[attempt]
            # Log only the endpoint, never query parameters, cookies or credentials.
            print(f'网络请求暂时失败：{method} {urlsplit(path).path}；{reason}；'
                  f'{delay} 秒后重试（{attempt + 1}/{len(delays)}）。', flush=True)
            for _ in range(delay):
                self.check_access_window()
                time.sleep(1)
            self.check_access_window()

    def login(self):
        if (RUNTIME / 'login.paused').exists():
            raise RuntimeError('账号登录已临时禁止（runtime/login.paused）；当前仅允许离线开发和简报生成。')
        self.check_access_window()
        self.request('GET', '/resources/jsps/pol/login/Logon.jsp')
        self.auth_attempted = True
        password = re.sub(r'[%+&]', lambda m: quote(m[0], safe=''), self.password)
        try:
            data = self.request('POST', '/pol/authenticateFSAction', data={
                'system': 'pol', 'actiontype': 'UserLogin', 'username': self.username,
                'password': password, 'pid': '', 'decreptPassword': 'false'}).json()
        finally:
            self.password = None
        if data.get('loginStatus') is not True:
            raise AuthenticationRequiredError('平台未确认认证成功；停止，不自动重复尝试密码。')
        self.start_watchdog()
        if data.get('isMfaEnabled') or data.get('hasPasswordExpired'):
            raise AuthenticationRequiredError('需要额外认证或密码处理；已停止。')
        page = self.request('POST', '/pol/initialiseWorkspaceFSAction', data={
            'logonId': data['loginId'], 'processId': '', 'projectId': '', 'processType': ''})
        parser = Handoff()
        parser.feed(page.text)
        if parser.action:
            page = self.request('POST', parser.action, data=parser.fields)
        for attr, pattern in [('project_id', r'projectId\s*:\s*(\d+)'),
                              ('cabinet_id', r'polDocstarCabinetId\s*:\s*(\d+)'),
                              ('project_name', r'var projectName\s*=\s*"([^"]+)"')]:
            m = re.search(pattern, page.text)
            if not m:
                raise RuntimeError('无法识别工作区信息：' + attr)
            setattr(self, attr, m[1])
        print(f'已登录工作区：{self.project_name}', flush=True)
        self.update_watchdog()
        for attr, field in [('rendition_security', 'isRenditionSecurityEnabled'), ('native_user', 'isRenditionNativeUser')]:
            m = re.search(r'\b' + field + r'\s*:\s*(true|false)', page.text)
            if m:
                setattr(self, attr, m[1] == 'true')
        return self

    def start_watchdog(self):
        RUNTIME.mkdir(exist_ok=True)
        self.watchdog_log = (RUNTIME / 'watchdog.log').open('a', encoding='utf-8')
        env = {key: value for key, value in os.environ.items() if key not in ('FUSIONLIVE_PASSWORD', 'FUSIONLIVE_USERNAME')}
        self.watchdog = subprocess.Popen([sys.executable, str(CODE / 'logout_watchdog.py')],
                                         stdin=subprocess.PIPE, stdout=self.watchdog_log, stderr=subprocess.STDOUT,
                                         text=True, encoding='utf-8', env=env,
                                         creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.update_watchdog()

    def update_watchdog(self):
        if self.watchdog and self.watchdog.stdin:
            message = {'project_id': self.project_id, 'cabinet_id': self.cabinet_id,
                       'manual_test': bool(getattr(self, 'manual_override', False)),
                       'cookies': [{'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path}
                                   for c in self.http.cookies]}
            self.watchdog.stdin.write(json.dumps(message) + '\n')
            self.watchdog.stdin.flush()

    def xml(self, path, **params):
        r = self.request('GET', path, params=params)
        tree = ET.fromstring(r.content)
        if tree.tag == 'success' and tree.get('isActionSucessful') != 'true':
            raise PlatformActionError(path, r, tree)
        if tree.tag.lower() in ('html', 'errors', 'error'):
            raise RuntimeError('接口未返回预期数据：' + path)
        return tree

    def folders(self, folder_id):
        tree = self.xml('/pol/viewDocumentManagerTreeFSAction', folderId=folder_id)
        if tree.tag != 'tree':
            raise RuntimeError('目录接口格式不符。')
        return [dict(e.attrib) for e in tree if e.tag == 'tree']

    def document_page(self, folder_id, start=0, limit=100):
        tree = self.xml('/pol/viewFolderDocumentListFSAction', folderId=folder_id,
                        start=start, limit=limit, fsSort='REFERENCE', dir='ASC',
                        includeLinks='true', fetchPropDetails='false')
        if tree.tag != 'collection' or tree.find('totalCount') is None:
            raise RuntimeError('文件清单缺少记录总数。')
        records = [{e.tag: e.text or '' for e in record} for record in tree
                   if record.tag.endswith('FolderDocumentRecord')]
        return int(tree.findtext('totalCount')), records

    def related(self, version_id, virtual_link_id=None):
        tree = self.xml('/pol/getRelatedContentFSAction', versionIds=virtual_link_id or version_id,
                        documentIds=version_id if virtual_link_id else '',
                        objectTypes='VIRTUAL_LINK' if virtual_link_id else 'DOCUMENT',
                        relationshipTypes=['AllChildAssociations', 'HybridContent', 'Attachments'],
                        includeSourceDocuments='true', fetchFolderPath='false', workflow='false')
        if tree.tag != 'collection':
            raise RuntimeError('关联文件响应格式不符。')
        items = [{e.tag: e.text or '' for e in record} for record in tree.iter('ContentItem')]
        declared = tree.findtext('totalCount')
        if declared is None or int(declared) != len(items):
            raise RuntimeError('关联清单条数与平台总数不一致，停止以避免漏掉原件或附件。')
        return items

    def download(self, item, target, progress=None, verified=None, resumed=None):
        resumed = resumed or getattr(progress, 'resumed', None)
        if item.get('readAccessStatus') != 'Available':
            raise RuntimeError('文件当前不可读取。')
        if self.rendition_security is None:
            raise RuntimeError('尚未识别文件权限设置，停止下载。')
        rendition = self.rendition_security and not self.native_user
        if rendition:
            raise RuntimeError('此账号仅有转换件权限；原件同步暂不支持此模式。')
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        expected = int(item.get('contentSize') or 0)
        # Platform names can themselves end in .part; never use a neighbouring file as scratch space.
        temporary_folder = RUNTIME / 'download_parts'
        partial = DownloadPartial(temporary_folder, self.project_id, self.username, item, self.check_access_window)
        if expected and partial.size > expected:
            partial.discard()
        complete_prefix = bool(expected and partial.size == expected)
        if shutil.disk_usage(target.parent).free < max(0, expected-partial.size) + 32 * 1024 * 1024:
            raise OSError(errno.ENOSPC, '磁盘空间不足，停止下载并注销。')
        from download_diagnostics import download_parameters
        params = {**download_parameters(self, item), 'guid': str(uuid.uuid4())}
        preserve = bool(partial.size)
        validated_complete = False
        try:
            headers = {'Accept-Encoding': 'identity'}
            if partial.size:
                start = partial.size - int(complete_prefix)
                headers.update(Range=f'bytes={start}-', **{'If-Range': partial.validator})
                print(f'尝试字节续传：{partial.size} 字节已保存并复核。', flush=True)
            from chunked_download import LARGE_FILE_BYTES, open_chunked_download
            if expected >= LARGE_FILE_BYTES:
                print(f'大文件使用平台分段接口下载：{expected} 字节。', flush=True)
                transfer = open_chunked_download(self, item, expected, headers,
                                                 DownloadPartial.response_validator)
            else:
                transfer = self.request('GET', '/servlet/HtmlDownloadServlet', params=params,
                                        stream=True, headers=headers)
            with transfer as response:
                preserve = False
                offset = partial.size - int(complete_prefix)
                status = response.status_code
                reported = int(response.headers.get('Content-Length') or 0)
                if response.headers.get('Content-Encoding', 'identity').lower() not in ('identity', ''):
                    raise RuntimeError('下载返回编码压缩内容，无法安全核对字节范围。')
                if status == 206:
                    match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''))
                    if (not partial.size or not match or int(match[1]) != offset
                            or int(match[2]) != int(match[3])-1
                            or (expected and int(match[3]) != expected)
                            or (reported and reported != int(match[3])-offset)
                            or DownloadPartial.response_validator(response.headers) != partial.validator):
                        raise RuntimeError('续传响应范围或版本标识不匹配，拒绝拼接。')
                    expected = expected or int(match[3])
                    if complete_prefix:
                        partial.size, partial.digest = offset, partial.before_last_digest
                    if resumed:
                        resumed(offset, expected)
                else:
                    # Servers may ignore Range or return a new representation for If-Range.
                    if isinstance(status, int) and status != 200:
                        raise RuntimeError(f'下载响应状态不支持：{status}')
                    if offset:
                        print('服务器未确认续传，安全回退为完整下载。', flush=True)
                    partial.discard()
                    complete_prefix = False
                    offset = 0
                    partial.validator = DownloadPartial.response_validator(response.headers)
                    if expected and reported and reported != expected:
                        raise RuntimeError('响应文件大小与当前版本元数据不符。')
                content_type = response.headers.get('Content-Type', '').lower()
                disposition = response.headers.get('Content-Disposition', '')
                if 'html' in content_type and item.get('contentFormatCode') not in ('html', 'htm'):
                    raise RuntimeError('下载返回 HTML，可能是登录页或错误页。')
                with partial.path.open('r+b' if offset or complete_prefix else 'wb') as out:
                    if not complete_prefix:
                        out.truncate(offset)
                        out.seek(offset)
                    elif os.fstat(out.fileno()).st_size != expected:
                        out.truncate(expected)  # Drop only bytes beyond the complete checkpoint.
                    first = not offset
                    try:
                        for chunk in response.iter_content(256 * 1024):
                            self.check_access_window()
                            if not chunk:
                                continue
                            if complete_prefix and chunk != partial.last_byte:
                                raise RuntimeError('完整片段的远端尾字节不一致，拒绝发布。')
                            if first:
                                prefix = chunk[:500].lstrip().lower()
                                if item.get('contentFormatCode') == 'pdf' and not prefix.startswith(b'%pdf-'):
                                    raise RuntimeError('PDF 文件头不正确。')
                                if item.get('contentFormatCode') in ('xlsx', 'docx', 'pptx', 'zip') and not chunk.startswith(b'PK'):
                                    raise RuntimeError('Office/ZIP 文件头不正确。')
                                if not disposition and (prefix.startswith(b'<!doctype html') or prefix.startswith(b'<html')):
                                    raise RuntimeError('下载返回网页，拒绝标记成功。')
                                first = False
                            if not complete_prefix:
                                out.write(chunk)
                            partial.digest.update(chunk)
                            partial.size += len(chunk)
                            if ((expected and partial.size > expected)
                                    or (reported and partial.size-offset > reported)):
                                raise RuntimeError('下载内容超过声明大小，拒绝保存。')
                            partial.checkpoint(out)
                            if progress:
                                progress(partial.size, expected)
                        if ((expected and partial.size < expected)
                                or (reported and partial.size-offset < reported)):
                            raise requests.ConnectionError('文件流提前结束，保存已验证版本的续传进度。')
                    except (requests.RequestException, WindowClosed, KeyboardInterrupt):
                        partial.checkpoint(out, force=True)
                        preserve = bool(partial.validator and partial.size)
                        raise
                    out.flush()
                    os.fsync(out.fileno())
                size = partial.size
                if not size or (expected and size != expected) or (reported and size-offset != reported):
                    raise RuntimeError(f'文件大小校验失败：实际 {size}，元数据 {expected}，响应 {reported}。')
            result = {'bytes': size, 'sha256': partial.digest.hexdigest(), 'path': str(target.resolve())}
            validated_complete = True
            if verified:
                verified(result)  # Commit recovery identity/hash before publishing bytes.
            os.replace(partial.path, target)
            preserve = False
        except requests.HTTPError as exc:
            if partial.size and exc.response is not None and exc.response.status_code == 416:
                exc.response.close()
                partial.discard()
                return self.download(item, target, progress=progress, verified=verified, resumed=resumed)
            raise
        except BaseException:
            if validated_complete and partial.validator and partial.path.exists():
                # Write a final checkpoint only on publication failure, not for every
                # successful small file. A successful publish removes scratch state.
                try:
                    with partial.path.open('r+b') as stream:
                        partial.checkpoint(stream, force=True)
                    preserve = True
                except OSError:
                    preserve = bool(partial.saved_size and partial.state_path.exists())
            raise
        finally:
            if not preserve:
                partial.discard()
        return result

    def can_recover_logout(self):
        at = now()
        return (os.environ.get('FUSIONLIVE_SCHEDULED') == '1'
                and self.logout_recovery_allowed
                and not getattr(self, 'manual_override', False)
                and 120 <= at.hour * 60 + at.minute < 630
                and not any((RUNTIME / name).exists() for name in
                            ('stop.request', 'scheduler.exit.request', 'login.paused')))

    def logout(self):
        if not self.auth_attempted:
            self.http.close()
            return
        result = {'time_beijing': now().isoformat(), 'confirmed': False, 'attempts': 0}
        try:
            RUNTIME.mkdir(exist_ok=True)
            while True:
                retryable = True
                for attempt in range(3):
                    result['attempts'] += 1
                    try:
                        result['failure_stage'] = 'logout_request'
                        r = self.raw('GET', '/pol/polLogOutFsAction')
                        try:
                            r.raise_for_status()
                        finally:
                            r.close()
                        # Keep the SAME session and cookies until denial is confirmed.
                        result['failure_stage'] = 'protected_probe'
                        r = self.raw('GET', '/pol/viewDocumentManagerTreeFSAction', params={
                            'folderId': self.cabinet_id or '', 'jsClientProjectId': self.project_id or ''})
                        try:
                            result['protected_status'] = r.status_code
                            if not (r.status_code in (401, 403, 418)
                                    or (r.status_code == 200 and 'fs-login-username' in r.text)):
                                r.raise_for_status()
                                raise LogoutError('注销后受保护资源未明确拒绝访问。')
                        finally:
                            r.close()
                        result.update(confirmed=True, recovery_pending=False)
                        result.pop('error', None)
                        result.pop('failure_stage', None)
                        break
                    except (requests.RequestException, LogoutError) as exc:
                        result['error'] = type(exc).__name__
                        retryable = retryable and temporary_network_error(exc)
                        if attempt < 2:
                            time.sleep(1)
                recover = not result['confirmed'] and retryable and self.can_recover_logout()
                result['recovery_pending'] = recover
                self.logout_result = result
                (RUNTIME / 'last_logout.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                if not recover:
                    break
                print('注销连接暂时中断：保留原会话，60秒后重试注销确认；确认前不重新登录。'
                      '下载时段内自动恢复，不创建 login.paused。', flush=True)
                for _ in range(60):
                    if not self.can_recover_logout():
                        break
                    time.sleep(1)
                if not self.can_recover_logout():
                    result['recovery_pending'] = False
                    (RUNTIME / 'last_logout.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                    break
            if not result['confirmed']:
                raise LogoutError('注销未确认成功，请人工检查平台会话。详情见 runtime/last_logout.json。')
            print('已注销；同一会话访问目录被拒绝。', flush=True)
        finally:
            if self.watchdog:
                try:
                    if result.get('confirmed'):
                        self.watchdog.stdin.write('{"done":true}\n')
                        self.watchdog.stdin.flush()
                    self.watchdog.stdin.close()
                    self.watchdog.wait(timeout=5 if result.get('confirmed') else 30)
                except (OSError, ValueError, subprocess.TimeoutExpired):
                    pass
                self.watchdog = None
            if self.watchdog_log:
                self.watchdog_log.close()
            self.http.cookies.clear()
            self.http.close()

    def __enter__(self):
        try:
            return self.login()
        except BaseException as exc:
            if isinstance(exc, AuthenticationRequiredError):
                self.logout_recovery_allowed = False
                try:
                    self.logout()
                finally:
                    # Keep the authentication reason even if cleanup also fails;
                    # sync.py must still create its explicit manual-login block.
                    raise exc
            self.logout()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.logout()
        return False
