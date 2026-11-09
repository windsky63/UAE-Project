"""Official ima Skills OpenAPI v1.1.10 and COS PUT transport."""
import hashlib
import hmac
import re
import time
import sqlite3
import sys
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import quote

import requests


class ImaError(RuntimeError):
    def __init__(self, message, *, code=None, endpoint=None):
        super().__init__(message)
        self.code, self.endpoint = code, endpoint


class ImaQuotaError(ImaError):
    """Repeated explicit quota rejections must abort the current sync batch."""


class ImaRateLimitError(ImaError):
    """Persistent throttling stops the batch, rather than trying every file."""


class RequestPacer:
    """Grant a current request slot after rechecking shared frequency/cooldowns."""
    def __init__(self, account, path=None, clock=time.time):
        self.account = hashlib.sha256(account.encode()).hexdigest()
        self.path = Path(path) if path is not None else Path(__file__).resolve().parents[1] / 'runtime/api-rate.sqlite3'
        self.clock = clock
        self.extra_seconds = 0.0
        self.last_sent = {}

    def wait(self, endpoint, sleep):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            with sqlite3.connect(self.path, timeout=10) as db:
                db.execute('CREATE TABLE IF NOT EXISTS pacing(account TEXT,endpoint TEXT,next_at REAL,PRIMARY KEY(account,endpoint))')
                db.execute('BEGIN IMMEDIATE')
                now = self.clock()
                extra = self.extra_seconds
                deadlines = dict(db.execute('SELECT endpoint,next_at FROM pacing WHERE account=? AND endpoint IN (?,?)',
                                            (self.account, '*', endpoint)))
                local_due = max([now] + [self.last_sent[key] + interval + extra
                    for key, interval in (('*', 1.0), (endpoint, 3.0 if endpoint == 'check_repeated_names' else 1.0))
                    if key in self.last_sent])
                due = max(local_due, deadlines.get('*', now), deadlines.get(endpoint, now))
                if due - now > 300:
                    raise ImaRateLimitError(f'ima 账号仍在限流冷却期（接口 {endpoint}）；约{due-now:.0f}秒后再试。',
                                            code=200001, endpoint=endpoint)
                if due <= now:
                    for key, interval in (('*', 1.0 + extra), (endpoint, (3.0 if endpoint == 'check_repeated_names' else 1.0) + extra)):
                        db.execute('INSERT OR REPLACE INTO pacing VALUES(?,?,?)', (self.account, key, now + interval))
                    self.last_sent.update({'*': now, endpoint: now})
            if due <= now:
                return
            # Another process may add cooldown or use the next slot while we
            # sleep. Never dispatch based on a future reservation made earlier.
            sleep(due - now)

    def penalize(self):
        """Increase only this run's interval; never persist the adaptive bonus."""
        self.extra_seconds += .5
        return self.extra_seconds

    def reset_interval(self):
        self.extra_seconds = 0.0

    def defer(self, seconds):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS pacing(account TEXT,endpoint TEXT,next_at REAL,PRIMARY KEY(account,endpoint))')
            db.execute('BEGIN IMMEDIATE')
            db.execute('''INSERT INTO pacing VALUES(?,?,?) ON CONFLICT(account,endpoint)
                DO UPDATE SET next_at=MAX(next_at,excluded.next_at)''', (self.account, '*', self.clock() + seconds))


READ_ENDPOINTS = {
    'get_knowledge_base', 'get_knowledge_list', 'search_knowledge',
    'search_knowledge_base', 'get_addable_knowledge_base_list',
    'check_repeated_names', 'get_media_info',
}
WRITE_ENDPOINTS = {'create_media', 'add_knowledge', 'import_urls'}
EXPERIMENTAL_ENDPOINTS = {'create_folder', 'move_knowledge', 'rename_knowledge'}


class ImaClient:
    def __init__(self, client_id, api_key, session=None, sleep=time.sleep, rate_gate=None):
        if not client_id or not api_key:
            raise ImaError('请先配置 Client ID 和 API Key。')
        self.client_id, self.api_key = client_id, api_key
        self.session = session or requests.Session()
        self.sleep = sleep
        # Custom HTTP transports must obey the same gate. Tests can explicitly
        # inject a fake gate; supplying a session never disables pacing.
        self.rate_gate = rate_gate if rate_gate is not None else RequestPacer(client_id)

    def reset_interval(self):
        if self.rate_gate is not None:
            self.rate_gate.reset_interval()

    def increase_interval(self):
        if self.rate_gate is not None:
            extra = self.rate_gate.penalize()
            if isinstance(extra, (int, float)):
                print(f'ima 限流：账号请求间隔增加0.5秒；普通接口至少{1+extra:.1f}秒，'
                      f'重名检查至少{3+extra:.1f}秒。', file=sys.stderr, flush=True)

    def retry_delay(self, response, minimum):
        headers = getattr(response, 'headers', {})
        value = headers.get('Retry-After') if isinstance(headers, dict) or hasattr(headers, 'get') else None
        if isinstance(value, str):
            try:
                delay = float(value)
            except ValueError:
                try:
                    delay = parsedate_to_datetime(value).timestamp() - time.time()
                except (ValueError, TypeError, OverflowError):
                    delay = 0
            minimum = max(minimum, min(max(delay, 0), 86400))
        return minimum

    def redact(self, message):
        text = str(message)
        for secret in (self.client_id, self.api_key):
            text = text.replace(secret, '[已隐藏]')
        return text

    def call(self, endpoint, body, *, experimental=False):
        allowed = READ_ENDPOINTS | WRITE_ENDPOINTS | (EXPERIMENTAL_ENDPOINTS if experimental else set())
        if endpoint not in allowed:
            raise ImaError('此操作未在已核验的官方接口中开放。')
        read_retry = endpoint in READ_ENDPOINTS
        attempts = 3
        headers = {'ima-openapi-clientid': self.client_id,
                   'ima-openapi-apikey': self.api_key,
                   'ima-openapi-ctx': 'skill_version=1.1.10'}
        quota_rejections = 0
        for attempt in range(attempts):
            if self.rate_gate is not None:
                self.rate_gate.wait(endpoint, self.sleep)
            try:
                response = self.session.post(
                    'https://ima.qq.com/openapi/wiki/v1/' + endpoint,
                    json=body, headers=headers, timeout=(15, 60), allow_redirects=False)
            except requests.RequestException:
                if read_retry and attempt + 1 < attempts:
                    self.sleep(2 ** attempt)
                    continue
                raise ImaError('ima 网络请求失败；写入请求的结果可能需要人工核对。') from None
            try:
                payload = response.json()
            except ValueError:
                payload = None
            code = payload.get('code') if isinstance(payload, dict) else None
            quota_rejections = quota_rejections + 1 if code == 220021 else 0
            # Replay writes only after an explicit service rejection, never
            # after a timeout or an ambiguous HTTP error.
            limited = code in (200001, 110021) or (read_retry and response.status_code == 429)
            if code in (200001, 110021) or response.status_code == 429:
                self.increase_interval()
            if (limited or code == 220021) and attempt + 1 < attempts:
                delay = self.retry_delay(response, 30 * 2 ** attempt if limited else 5 * 2 ** attempt)
                response.close()
                if limited and self.rate_gate is not None:
                    self.rate_gate.defer(delay)
                # Do not keep a UI worker asleep for an unusually long service cooldown.
                if limited and delay > 300:
                    raise ImaRateLimitError(self.redact(f'ima 限流（接口 {endpoint}）；服务端要求等待{delay:.0f}秒，已停止本轮请求。'),
                                            code=code, endpoint=endpoint)
                self.sleep(delay)
                continue
            if (read_retry and code not in (200001, 220021)
                    and response.status_code in (429, 500, 502, 503, 504)
                    and attempt + 1 < attempts):
                response.close()
                self.sleep(2 ** attempt)
                continue
            error_type = ImaRateLimitError if limited else ImaQuotaError if quota_rejections == 3 else ImaError
            if limited and self.rate_gate is not None:
                self.rate_gate.defer(self.retry_delay(response, 120))
            if response.status_code != 200:
                failure = payload if isinstance(payload, dict) else {}
                detail = ''
                if isinstance(failure.get('msg'), str):
                    detail = f"；ima {failure.get('code')}：{failure['msg'][:500]}"
                raise error_type(self.redact(f'ima HTTP {response.status_code}（接口 {endpoint}）{detail}；请检查接口返回原因。'),
                                 code=code, endpoint=endpoint)
            if not isinstance(payload, dict) or 'code' not in payload:
                raise ImaError('ima 返回格式异常。') from None
            if read_retry and code in (110010, 110021) and attempt + 1 < attempts:
                response.close()
                self.sleep(2 ** attempt)
                continue
            if code != 0:
                raise error_type(self.redact(f"ima {code}: {payload.get('msg', '请求失败')}（接口 {endpoint}）"),
                                 code=code, endpoint=endpoint)
            data = payload.get('data')
            if not isinstance(data, dict):
                raise ImaError('ima 返回的 data 不是对象。')
            return data

    def pages(self, endpoint, field, **body):
        cursor, seen, result = '', set(), []
        for _ in range(10000):
            data = self.call(endpoint, dict(body, cursor=cursor))
            entries = data.get(field)
            if not isinstance(entries, list):
                raise ImaError(f'ima 返回缺少列表字段 {field}。')
            result.extend(entries)
            if data.get('is_end') is True:
                return result
            following = data.get('next_cursor')
            if not isinstance(following, str) or not following or following in seen:
                raise ImaError('ima 分页游标异常，已停止，避免漏读后继续上传。')
            seen.add(following)
            cursor = following
        raise ImaError('分页数量异常，已停止。')

    def bases(self):
        return self.pages('get_addable_knowledge_base_list', 'addable_knowledge_base_list', limit=50)

    def listing(self, kb, folder=''):
        return self.pages('get_knowledge_list', 'knowledge_list', limit=50, **target(kb, folder))

    def search(self, kb, query):
        if not query.strip():
            raise ImaError('请输入搜索关键词。')
        return self.pages('search_knowledge', 'info_list', knowledge_base_id=kb, query=query)

    def repeated(self, kb, folder, name, media_type):
        return self.repeated_many(kb, folder, [{'name': name, 'media_type': media_type}])[name]

    def repeated_many(self, kb, folder, params):
        destination = target(kb, folder)
        names = [item['name'] for item in params]
        if len(set(names)) != len(names):
            raise ImaError('重名检查包含重复文件名，已停止上传。')
        checked = {}
        # The official endpoint accepts 1–2000 files per request. Do not issue one
        # request per scanned file: ordinary project previews exceed its rate limit.
        for start in range(0, len(params), 2000):
            batch = params[start:start + 2000]
            data = self.call('check_repeated_names', dict(destination, params=batch))
            results = data.get('results')
            if not isinstance(results, list) or len(results) != len(batch):
                raise ImaError('重名检查结果不完整，已停止上传。')
            expected = {item['name'] for item in batch}
            for item in results:
                if (not isinstance(item, dict) or not isinstance(item.get('name'), str)
                        or item['name'] not in expected or item['name'] in checked
                        or type(item.get('is_repeated')) is not bool):
                    raise ImaError('重名检查结果不匹配，已停止上传。')
                checked[item['name']] = item['is_repeated']
        return checked

    def upload_object(self, stream, size, content_type, credential):
        required = ('bucket_name', 'region', 'cos_key', 'secret_id', 'secret_key',
                    'token', 'start_time', 'expired_time')
        if any(k not in credential for k in required):
            raise ImaError('COS 临时凭证不完整。')
        bucket, region = credential['bucket_name'], credential['region']
        if not re.fullmatch(r'[a-zA-Z0-9-]+', bucket) or not re.fullmatch(r'[a-zA-Z0-9-]+', region):
            raise ImaError('COS 上传地址异常。')
        host = f'{bucket}.cos.{region}.myqcloud.com'
        path = '/' + credential['cos_key'].lstrip('/')
        # Reject URL control syntax; encode each object-key segment exactly once.
        wire_path = quote(path, safe='/~')
        key_time = f"{int(credential['start_time'])};{int(credential['expired_time'])}"
        if int(credential['expired_time']) <= time.time():
            raise ImaError('COS 临时凭证已过期。')
        sign_key = hmac.new(credential['secret_key'].encode(), key_time.encode(), hashlib.sha1).hexdigest()
        header_string = f'content-length={size}&host={host}'
        http_string = f'put\n{path}\n\n{header_string}\n'
        to_sign = f'sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n'
        signature = hmac.new(sign_key.encode(), to_sign.encode(), hashlib.sha1).hexdigest()
        auth = (f'q-sign-algorithm=sha1&q-ak={credential["secret_id"]}'
                f'&q-sign-time={key_time}&q-key-time={key_time}'
                f'&q-header-list=content-length;host&q-url-param-list=&q-signature={signature}')
        # Separate session: never send ima credentials or its cookies to COS.
        try:
            response = requests.put('https://' + host + wire_path, data=stream,
                                    headers={'Authorization': auth, 'x-cos-security-token': credential['token'],
                                             'Content-Length': str(size), 'Content-Type': content_type},
                                    timeout=(15, 300), allow_redirects=False)
        except requests.RequestException:
            raise ImaError('COS 上传中断；尚未确认入库。') from None
        if not 200 <= response.status_code < 300:
            raise ImaError(f'COS 上传失败：HTTP {response.status_code}。')


def target(kb, folder=''):
    if not kb:
        raise ImaError('请先选择目标知识库。')
    result = {'knowledge_base_id': kb}
    if folder:
        result['folder_id'] = folder
    return result


def folder_id(item):
    info = item.get('folder_info') or {}
    return (info.get('folder_id') or item.get('folder_id') or
            (item.get('media_id') if item.get('media_type') == 99 else '') or '')
