"""HTTP session for the observed FusionLive web endpoints (not a public SDK)."""

from app_paths import HOME, CODE
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
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote, urljoin, urlsplit

import requests

BASE = 'https://us.fusion.live'
LOCAL = timezone(timedelta(hours=8))
RUNTIME = HOME / 'runtime'


class WindowClosed(RuntimeError):
    pass


class LogoutError(RuntimeError):
    pass


class SessionExpiredError(RuntimeError):
    pass


def now():
    return datetime.now(LOCAL)


def check_window(at=None, stop_minute=630):
    if (RUNTIME / 'stop.request').exists():
        raise WindowClosed('收到停止请求。')
    at = at or now()
    minute = at.hour * 60 + at.minute
    if not 60 <= minute < stop_minute:
        raise WindowClosed('仅允许北京时间 01:00—10:30 发起新任务。')


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
        r = self.raw(method, path, **kwargs)
        if self.watchdog:
            self.update_watchdog()
        r.raise_for_status()
        if not kwargs.get('stream') and 'fs-login-username' in r.text[:100000] and '/resources/' not in path:
            raise SessionExpiredError('登录会话失效，已停止请求。')
        return r

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
            raise RuntimeError('平台未确认认证成功；停止，不自动重复尝试密码。')
        self.start_watchdog()
        if data.get('isMfaEnabled') or data.get('hasPasswordExpired'):
            raise RuntimeError('需要额外认证或密码处理；已停止。')
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
            raise RuntimeError('平台返回操作失败：' + path)
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

    def download(self, item, target, progress=None, verified=None):
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
        if shutil.disk_usage(target.parent).free < expected + 32 * 1024 * 1024:
            raise OSError(errno.ENOSPC, '磁盘空间不足，停止下载并注销。')
        # Platform names can themselves end in .part; never use a neighbouring file as scratch space.
        temporary_folder = RUNTIME / 'download_parts'
        temporary_folder.mkdir(parents=True, exist_ok=True)
        temp = temporary_folder / (uuid.uuid4().hex + '.part')
        params = {'zip': 'false', 'saveAs': 'filename', 'appendRevision': 'false',
                  'appendStatus': 'false', 'rObjectId': item.get('virtualLinkId') if item.get('isVirtualLink') == 'true' else item['id'],
                  'checkout': 'false', 'objectType': 'VIRTUAL_LINK' if item.get('isVirtualLink') == 'true' else 'DOCUMENT',
                  'clientPId': self.project_id, 'guid': str(uuid.uuid4())}
        if self.rendition_security:
            params.update({'saveRendition': 'false', 'isRenditionSecurityEnabled': 'true'})
        digest = hashlib.sha256()
        size = 0
        try:
            with self.request('GET', '/servlet/HtmlDownloadServlet', params=params,
                              stream=True, headers={'Accept-Encoding': 'identity'}) as response:
                content_type = response.headers.get('Content-Type', '').lower()
                disposition = response.headers.get('Content-Disposition', '')
                if 'html' in content_type and item.get('contentFormatCode') not in ('html', 'htm'):
                    raise RuntimeError('下载返回 HTML，可能是登录页或错误页。')
                expected = int(item.get('contentSize') or 0)
                reported = int(response.headers.get('Content-Length') or 0)
                with temp.open('wb') as out:
                    first = True
                    for chunk in response.iter_content(256 * 1024):
                        self.check_access_window()
                        if not chunk:
                            continue
                        if first:
                            prefix = chunk[:500].lstrip().lower()
                            if item.get('contentFormatCode') == 'pdf' and not prefix.startswith(b'%pdf-'):
                                raise RuntimeError('PDF 文件头不正确。')
                            if item.get('contentFormatCode') in ('xlsx', 'docx', 'pptx', 'zip') and not chunk.startswith(b'PK'):
                                raise RuntimeError('Office/ZIP 文件头不正确。')
                            if not disposition and (prefix.startswith(b'<!doctype html') or prefix.startswith(b'<html')):
                                raise RuntimeError('下载返回网页，拒绝标记成功。')
                            first = False
                        out.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
                        if progress:
                            progress(size, expected)
                    out.flush()
                    os.fsync(out.fileno())
                if not size or (expected and size != expected) or (reported and size != reported):
                    raise RuntimeError(f'文件大小校验失败：实际 {size}，元数据 {expected}，响应 {reported}。')
            result = {'bytes': size, 'sha256': digest.hexdigest(), 'path': str(target.resolve())}
            if verified:
                verified(result)  # Commit recovery identity/hash before publishing bytes.
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)
        return result

    def logout(self):
        if not self.auth_attempted:
            self.http.close()
            return
        result = {'time_beijing': now().isoformat(), 'confirmed': False, 'attempts': 0}
        try:
            for attempt in range(3):
                result['attempts'] = attempt + 1
                try:
                    r = self.raw('GET', '/pol/polLogOutFsAction')
                    r.raise_for_status()
                    # Probe with the SAME session and cookie jar, before clearing them.
                    r = self.raw('GET', '/pol/viewDocumentManagerTreeFSAction', params={
                        'folderId': self.cabinet_id or '', 'jsClientProjectId': self.project_id or ''})
                    result['protected_status'] = r.status_code
                    denied = r.status_code in (401, 403, 418)
                    login_page = r.status_code == 200 and 'fs-login-username' in r.text
                    if not (denied or login_page):
                        raise LogoutError('注销后受保护资源未明确拒绝访问。')
                    result['confirmed'] = True
                    break
                except (requests.RequestException, LogoutError) as exc:
                    result['error'] = type(exc).__name__
                    if attempt < 2:
                        time.sleep(1)
            self.logout_result = result
            RUNTIME.mkdir(exist_ok=True)
            (RUNTIME / 'last_logout.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
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
        except BaseException:
            self.logout()
            raise

    def __exit__(self, exc_type, exc, tb):
        self.logout()
        return False
