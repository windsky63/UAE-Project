import json
import threading
import time
import subprocess
import sys
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from unittest.mock import Mock

import pytest
import requests

from ima_manager.api import ImaClient, ImaError, ImaQuotaError, ImaRateLimitError, RequestPacer


def response(payload, status=200):
    result = Mock(status_code=status)
    result.json.return_value = payload
    return result


def client_with(*responses):
    session = Mock()
    session.post.side_effect = responses
    return ImaClient('test-client', 'test-key', session=session, sleep=lambda _: None, rate_gate=Mock())


def test_read_retry_but_add_knowledge_never_retries():
    client = client_with(requests.Timeout(), response({'code': 0, 'data': {}}))
    assert client.call('get_media_info', {'media_id': 'id'}) == {}
    assert client.session.post.call_count == 2
    client = client_with(requests.Timeout(), response({'code': 0, 'data': {}}))
    with pytest.raises(ImaError):
        client.call('add_knowledge', {})
    assert client.session.post.call_count == 1


def test_allowlist_prevents_guessing_destructive_endpoints():
    client = client_with()
    for name in ('delete_knowledge', 'move_knowledge', 'rename_knowledge', 'update_knowledge_base_permission'):
        with pytest.raises(ImaError, match='未在已核验'):
            client.call(name, {})
    client.session.post.assert_not_called()


def test_pagination_collects_all_and_rejects_repeated_cursor():
    client = client_with(response({'code': 0, 'data': {'addable_knowledge_base_list': [{'id': 'one'}], 'is_end': False, 'next_cursor': 'next'}}),
                         response({'code': 0, 'data': {'addable_knowledge_base_list': [{'id': 'two'}], 'is_end': True}}))
    assert [r['id'] for r in client.bases()] == ['one', 'two']
    client = client_with(*[response({'code': 0, 'data': {'knowledge_list': [], 'is_end': False, 'next_cursor': 'same'}})] * 2)
    with pytest.raises(ImaError, match='分页游标'):
        client.listing('kb')


@pytest.mark.parametrize('results', [[], [{'name': 'other.txt', 'is_repeated': False}], [{'name': 'a.txt'}]])
def test_incomplete_duplicate_check_fails_closed(results):
    client = client_with(response({'code': 0, 'data': {'results': results}}))
    with pytest.raises(ImaError):
        client.repeated('kb', '', 'a.txt', 13)


def test_errors_redact_credentials():
    client = client_with(response({'code': 110030, 'msg': 'test-key test-client'}))
    with pytest.raises(ImaError) as error:
        client.bases()
    assert 'test-key' not in str(error.value)
    assert 'test-client' not in str(error.value)


def test_http_forbidden_identifies_endpoint_without_exposing_credentials():
    client = client_with(response({}, status=403))
    with pytest.raises(ImaError, match=r'HTTP 403（接口 get_knowledge_list）') as error:
        client.call('get_knowledge_list', {})
    assert 'test-key' not in str(error.value) and 'test-client' not in str(error.value)
    assert client.session.post.call_count == 1


@pytest.mark.parametrize('http_status', [200, 403])
def test_explicit_read_rate_limit_waits_and_retries(http_status):
    client = client_with(response({'code': 200001, 'msg': '请求频率超限'}, status=http_status),
                         response({'code': 200001, 'msg': '请求频率超限'}, status=http_status),
                         response({'code': 0, 'data': {'results': [{'name': 'a.txt', 'is_repeated': False}]}}))
    client.sleep = Mock()
    assert client.repeated('kb', '', 'a.txt', 13) is False
    assert [call.args[0] for call in client.sleep.call_args_list] == [30, 60]


def test_write_rate_limit_retries_are_bounded_and_keep_redacted_server_reason():
    client = client_with(*[response({'code': 200001, 'msg': '请求频率超限 test-key'}, status=403)] * 3)
    with pytest.raises(ImaError, match='200001：请求频率超限') as error:
        client.call('add_knowledge', {})
    assert 'test-key' not in str(error.value)
    assert client.session.post.call_count == 3


@pytest.mark.parametrize('endpoint', ['get_knowledge_list', 'create_media', 'add_knowledge', 'move_knowledge'])
@pytest.mark.parametrize('http_status', [200, 403, 503])
def test_quota_requires_three_rejections_and_identifies_endpoint(endpoint, http_status):
    rejection = {'code': 220021, 'msg': '资料获取次数已达上限 test-key'}
    client = client_with(*[response(rejection, http_status)] * 3)
    client.sleep = Mock()
    with pytest.raises(ImaQuotaError) as error:
        client.call(endpoint, {}, experimental=endpoint == 'move_knowledge')
    assert client.session.post.call_count == 3
    assert [call.args[0] for call in client.sleep.call_args_list] == [5, 10]
    assert error.value.code == 220021 and error.value.endpoint == endpoint
    assert endpoint in str(error.value) and 'test-key' not in str(error.value)


@pytest.mark.parametrize('code', [220021, 200001])
@pytest.mark.parametrize('http_status', [200, 403])
def test_explicit_rejection_can_recover_and_success_resets_quota(code, http_status):
    reject = response({'code': code, 'msg': '稍后重试'}, http_status)
    success = response({'code': 0, 'data': {'media_id': 'ok'}})
    client = client_with(reject, reject, success, reject, reject, success)
    for _ in range(2):
        assert client.call('add_knowledge', {}) == {'media_id': 'ok'}
    assert client.session.post.call_count == 6


def test_write_timeout_after_rate_rejection_is_never_replayed():
    client = client_with(response({'code': 200001, 'msg': '限流'}), requests.Timeout(),
                         response({'code': 0, 'data': {}}))
    with pytest.raises(ImaError, match='结果可能需要人工核对'):
        client.call('add_knowledge', {})
    assert client.session.post.call_count == 2


def test_mixed_rejections_do_not_report_three_consecutive_quota_errors():
    client = client_with(response({'code': 220021}), response({'code': 200001}), response({'code': 220021}))
    with pytest.raises(ImaError) as error:
        client.call('create_media', {})
    assert not isinstance(error.value, ImaQuotaError)


def test_read_rate_limit_retries_are_bounded():
    client = client_with(*[response({'code': 200001, 'msg': '请求频率超限'}, status=403)] * 3)
    with pytest.raises(ImaError, match='200001：请求频率超限'):
        client.repeated('kb', '', 'a.txt', 13)
    assert client.session.post.call_count == 3


def test_shared_account_pacing_applies_to_new_clients_and_name_checks(tmp_path):
    clock = [100.0]
    slept = []
    def sleep(seconds):
        slept.append(seconds)
        clock[0] += seconds
    first = RequestPacer('account', tmp_path / 'pacing.sqlite3', clock=lambda: clock[0])
    second = RequestPacer('account', first.path, clock=lambda: clock[0])
    first.wait('check_repeated_names', sleep)
    second.wait('check_repeated_names', sleep)
    assert slept == [3]
    second.wait('create_media', sleep)
    assert slept == [3, 1]
    first.defer(60)
    second.wait('get_knowledge_list', sleep)
    assert slept == [3, 1, 60]
    other = RequestPacer('other-account', first.path, clock=lambda: clock[0])
    other.wait('check_repeated_names', sleep)
    assert slept == [3, 1, 60]


def test_rate_limit_respects_retry_after_and_stops_entire_batch():
    rejection = response({'code': 200001, 'msg': '请求频率超限'}, 403)
    rejection.headers = {'Retry-After': '75'}
    client = client_with(rejection, rejection, rejection)
    client.sleep = Mock()
    client.rate_gate = Mock()
    with pytest.raises(ImaRateLimitError) as error:
        client.repeated('kb', '', 'file.txt', 13)
    assert error.value.code == 200001 and error.value.endpoint == 'check_repeated_names'
    assert [call.args[0] for call in client.sleep.call_args_list] == [75, 75]
    assert [call.args[0] for call in client.rate_gate.defer.call_args_list] == [75, 75, 120]
    assert client.rate_gate.wait.call_count == 3


def test_waiting_request_rechecks_cooldown_added_by_another_process(tmp_path):
    clock = [100.0]
    sleeps = []
    path = tmp_path / 'shared.sqlite3'
    first = RequestPacer('account', path, clock=lambda: clock[0])
    second = RequestPacer('account', path, clock=lambda: clock[0])
    first.wait('check_repeated_names', lambda _: None)
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
        if len(sleeps) == 1:
            second.defer(20)
    first.wait('check_repeated_names', sleep)
    assert sleeps == [3, 20]
    assert clock[0] == 123


def test_each_throttle_increases_current_run_intervals_by_half_second(tmp_path):
    clock = [100.0]
    delays = []
    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds
    first = RequestPacer('account', tmp_path / 'rate.sqlite3', clock=lambda: clock[0])
    second = RequestPacer('account', first.path, clock=lambda: clock[0])
    first.wait('check_repeated_names', sleep)
    assert first.penalize() == .5
    first.wait('check_repeated_names', sleep)
    first.wait('create_media', sleep)
    assert delays == [3.5, 1.5]
    assert first.penalize() == 1.0
    first.wait('create_media', sleep)
    assert delays[-1] == 2.0
    other = RequestPacer('other-account', first.path, clock=lambda: clock[0])
    other.wait('create_media', sleep)
    assert len(delays) == 3
    assert second.extra_seconds == 0.0


def test_success_preserves_current_run_bonus_but_new_run_starts_at_base(tmp_path):
    clock, dispatched = [0.0], []
    def sleep(seconds):
        clock[0] += seconds
    rejection = response({'code': 200001, 'msg': '请求频率超限'}, 403)
    success = response({'code': 0, 'data': {'results': [{'name': 'a.txt', 'is_repeated': False}]}})
    responses = iter([rejection, rejection, success, success, success, success])
    session = Mock()
    def post(*args, **kwargs):
        dispatched.append(clock[0])
        return next(responses)
    session.post.side_effect = post
    path = tmp_path / 'rate.sqlite3'
    client = ImaClient('account', 'key', session=session, sleep=sleep,
                       rate_gate=RequestPacer('account', path, clock=lambda: clock[0]))
    for _ in range(2):
        assert client.repeated('kb', '', 'a.txt', 13) is False
    assert client.rate_gate.extra_seconds == 1.0
    clock[0] = 100
    restarted = ImaClient('account', 'key', session=session, sleep=sleep,
                          rate_gate=RequestPacer('account', path, clock=lambda: clock[0]))
    for _ in range(2):
        assert restarted.repeated('kb', '', 'a.txt', 13) is False
    assert restarted.rate_gate.extra_seconds == 0.0
    assert dispatched == [0, 30, 90, 94, 100, 103]


def test_next_watch_batch_resets_bonus_without_bypassing_server_cooldown(tmp_path):
    clock = [0.0]
    delays = []
    def sleep(seconds):
        delays.append(seconds)
        clock[0] += seconds
    gate = RequestPacer('account', tmp_path / 'rate.sqlite3', clock=lambda: clock[0])
    gate.penalize()
    gate.penalize()
    gate.defer(60)
    client = ImaClient('account', 'key', session=Mock(), sleep=sleep, rate_gate=gate)
    client.reset_interval()
    gate.wait('check_repeated_names', sleep)
    gate.wait('check_repeated_names', sleep)
    assert gate.extra_seconds == 0.0
    assert delays == [60, 3]


def test_previous_version_persisted_bonus_is_ignored(tmp_path):
    clock = [0.0]
    path = tmp_path / 'legacy-rate.sqlite3'
    gate = RequestPacer('account', path, clock=lambda: clock[0])
    import sqlite3
    with sqlite3.connect(path) as db:
        db.execute('CREATE TABLE pacing_policy(account TEXT PRIMARY KEY, extra_seconds REAL)')
        db.execute('INSERT INTO pacing_policy VALUES(?,100)', (gate.account,))
    def sleep(seconds):
        clock[0] += seconds
    gate.wait('check_repeated_names', sleep)
    gate.wait('check_repeated_names', sleep)
    assert clock[0] == 3
    with sqlite3.connect(path) as db:
        assert db.execute('SELECT extra_seconds FROM pacing_policy').fetchone()[0] == 100


@pytest.mark.parametrize('status,code', [(403, 200001), (200, 110021), (429, None)])
def test_each_explicit_limit_response_increases_spacing_once(status, code):
    client = client_with(*[response({'code': code, 'msg': '请求频率超限'}, status)] * 3)
    with pytest.raises(ImaRateLimitError):
        client.call('check_repeated_names', {})
    assert client.rate_gate.penalize.call_count == 3


@pytest.mark.parametrize('status,code', [(403, 110030), (403, 220021)])
def test_permission_and_quota_errors_do_not_increase_frequency_penalty(status, code):
    client = client_with(*[response({'code': code, 'msg': '拒绝'}, status)] * 3)
    with pytest.raises(ImaError):
        client.call('check_repeated_names', {})
    client.rate_gate.penalize.assert_not_called()


def test_all_successful_http_requests_are_paced_before_dispatch(tmp_path):
    clock = [0.0]
    sent = []
    def sleep(seconds):
        clock[0] += seconds
    gate = RequestPacer('account', tmp_path / 'rate.sqlite3', clock=lambda: clock[0])
    session = Mock()
    def post(url, **kwargs):
        sent.append((clock[0], url.rsplit('/', 1)[-1]))
        return response({'code': 0, 'data': {}})
    session.post.side_effect = post
    clients = [ImaClient('account', 'key', session=session, sleep=sleep, rate_gate=gate),
               ImaClient('account', 'key', session=session, sleep=sleep,
                         rate_gate=RequestPacer('account', gate.path, clock=lambda: clock[0]))]
    for index in range(100):
        for endpoint in ('check_repeated_names', 'create_media', 'add_knowledge'):
            clients[index % 2].call(endpoint, {})
    assert len(sent) == 300
    assert min(right[0] - left[0] for left, right in zip(sent, sent[1:])) >= 1
    checks = [at for at, endpoint in sent if endpoint == 'check_repeated_names']
    assert min(right-left for left, right in zip(checks, checks[1:])) >= 3
    assert sent[-1][0] == 299


def test_custom_transport_does_not_implicitly_disable_request_pacing():
    client = ImaClient('account', 'key', session=Mock())
    assert isinstance(client.rate_gate, RequestPacer)


def test_real_processes_share_the_same_duplicate_check_interval(tmp_path):
    code = ('import sys,time; from ima_manager.api import RequestPacer; '
            'RequestPacer("shared-fixture", sys.argv[1]).wait("check_repeated_names", time.sleep); '
            'print(time.monotonic(), flush=True)')
    processes = [subprocess.Popen([sys.executable, '-c', code, str(tmp_path / 'rate.sqlite3')],
                 cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                 creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                 for _ in range(3)]
    try:
        timestamps = []
        for process in processes:
            out, error = process.communicate(timeout=15)
            assert process.returncode == 0, error
            timestamps.append(float(out.strip()))
        timestamps.sort()
        assert min(right-left for left, right in zip(timestamps, timestamps[1:])) >= 2.9
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.communicate()


def test_very_long_server_cooldown_defers_future_clients_without_long_worker_sleep():
    rejection = response({'code': 200001, 'msg': '请求频率超限'}, 403)
    rejection.headers = {'Retry-After': '3600'}
    client = client_with(rejection)
    client.sleep = Mock()
    client.rate_gate = Mock()
    with pytest.raises(ImaRateLimitError):
        client.call('check_repeated_names', {})
    client.rate_gate.defer.assert_called_once_with(3600)
    client.sleep.assert_not_called()
    assert client.session.post.call_count == 1


def test_persisted_long_cooldown_rejects_new_client_without_sleep_or_request(tmp_path):
    first = RequestPacer('account', tmp_path / 'rate.sqlite3', clock=lambda: 100)
    first.defer(3600)
    second = RequestPacer('account', first.path, clock=lambda: 100)
    sleep = Mock()
    with pytest.raises(ImaRateLimitError, match='冷却期'):
        second.wait('check_repeated_names', sleep)
    sleep.assert_not_called()


def test_plain_permission_denial_is_never_misclassified_as_rate_limit():
    client = client_with(response({'code': 110030, 'msg': 'permission denied'}, 403))
    with pytest.raises(ImaError) as error:
        client.call('check_repeated_names', {})
    assert not isinstance(error.value, ImaRateLimitError)
    assert client.session.post.call_count == 1


def test_batch_duplicate_check_splits_at_official_limit_and_matches_by_name():
    params = [{'name': f'{index}.pdf', 'media_type': 1} for index in range(2001)]
    results = [{'name': item['name'], 'is_repeated': index == 3} for index, item in enumerate(params)]
    client = client_with(response({'code': 0, 'data': {'results': list(reversed(results[:2000]))}}),
                         response({'code': 0, 'data': {'results': results[2000:]}}))
    checked = client.repeated_many('kb', 'folder', params)
    assert len(checked) == 2001 and checked['3.pdf'] is True and checked['2000.pdf'] is False
    bodies = [call.kwargs['json'] for call in client.session.post.call_args_list]
    assert [len(body['params']) for body in bodies] == [2000, 1]
    assert all(body['knowledge_base_id'] == 'kb' and body['folder_id'] == 'folder' for body in bodies)


@pytest.mark.parametrize('results', [
    [{'name': 'a.txt', 'is_repeated': False}],
    [{'name': 'a.txt', 'is_repeated': False}] * 2,
    [{'name': 'a.txt', 'is_repeated': False}, {'name': 'other.txt', 'is_repeated': False}],
    [{'name': 'a.txt', 'is_repeated': False}, {'name': 'b.txt', 'is_repeated': 'false'}]])
def test_incomplete_or_mismatched_batch_never_allows_upload(results):
    client = client_with(response({'code': 0, 'data': {'results': results}}))
    with pytest.raises(ImaError, match='重名检查结果'):
        client.repeated_many('kb', '', [{'name': 'a.txt', 'media_type': 13}, {'name': 'b.txt', 'media_type': 13}])


def test_http_transport_unicode_and_no_redirect_or_secret_to_cos(monkeypatch):
    captures = []
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            captures.append((self.command, self.path, dict(self.headers), raw))
            payload = json.dumps({'code': 0, 'data': {'results': [{'name': '中文.txt', 'is_repeated': False}]}}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def do_PUT(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            captures.append((self.command, self.path, dict(self.headers), raw))
            self.send_response(200)
            self.send_header('Content-Length', '0')
            self.end_headers()
        def log_message(self, *_):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01})
    thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    original_put = requests.put
    class Transport:
        def post(self, endpoint, **kwargs):
            assert endpoint.startswith('https://ima.qq.com/openapi/wiki/v1/')
            assert kwargs['allow_redirects'] is False
            return requests.post(url + '/api', **kwargs)
    try:
        client = ImaClient('local-client', 'local-key', session=Transport(), rate_gate=Mock())
        assert client.repeated('kb', '', '中文.txt', 13) is False
        def put(endpoint, **kwargs):
            assert endpoint == 'https://bucket-123.cos.ap-test.myqcloud.com/test.txt'
            assert kwargs['allow_redirects'] is False
            assert 'ima-openapi-apikey' not in kwargs['headers']
            assert 'local-key' not in json.dumps(kwargs['headers'])
            assert 'q-signature=' in kwargs['headers']['Authorization']
            return original_put(url + '/cos', **kwargs)
        monkeypatch.setattr(requests, 'put', put)
        raw = '保留原始字节'.encode('utf-8')
        client.upload_object(BytesIO(raw), len(raw), 'text/plain',
            {'bucket_name': 'bucket-123', 'region': 'ap-test', 'cos_key': 'test.txt',
             'secret_id': 'temporary-id', 'secret_key': 'temporary-key', 'token': 'temporary-token',
             'start_time': int(time.time()) - 5, 'expired_time': int(time.time()) + 3600})
        assert json.loads(captures[0][3])['params'][0]['name'] == '中文.txt'
        assert captures[1][3] == raw
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_cos_expired_and_invalid_host_rejected_without_network(monkeypatch):
    put = Mock()
    monkeypatch.setattr(requests, 'put', put)
    client = client_with()
    credential = {'bucket_name': 'bucket-123', 'region': 'ap-test', 'cos_key': 'test.txt',
                  'secret_id': 'id', 'secret_key': 'secret', 'token': 'token',
                  'start_time': 1, 'expired_time': 2}
    with pytest.raises(ImaError, match='过期'):
        client.upload_object(BytesIO(b'a'), 1, 'text/plain', credential)
    credential['bucket_name'] = 'evil.com/x'
    with pytest.raises(ImaError, match='地址异常'):
        client.upload_object(BytesIO(b'a'), 1, 'text/plain', credential)
    put.assert_not_called()
