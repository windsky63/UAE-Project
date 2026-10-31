"""Actual IPC and subprocess sharing, with a deterministic fake CLI only."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import time

import psutil
import pytest

from wechat_local.app_server import Client as WechatClient, CodexBackendError
from wechat_local.shared_backend import SharedConnection, RpcError
from wechat_local.shared_backend import RpcTimeout, MAX_THREADS_PER_CLIENT


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    cwd = tmp_path / 'workspace'
    cwd.mkdir()
    log = tmp_path / 'rpc.jsonl'
    command = [sys.executable, str(Path(__file__).with_name('fake_shared_server.py')), str(log)]
    clients = []
    def connect(scope='web', **kwargs):
        events = queue.Queue()
        client = SharedConnection(command, cwd, scope, events.put, **kwargs)
        clients.append(client)
        return client, events
    try:
        yield command, cwd, log, connect, clients
    finally:
        for client in clients:
            client.close()


def start(client, ephemeral=False, permission='project_tools_read'):
    return client.rpc('thread/start', dict(permissions=permission, approvalPolicy='never', ephemeral=ephemeral))['thread']['id']


def turn(client, thread, answer, **kwargs):
    return client.rpc('turn/start', dict(threadId=thread, permissions='project_tools_read', approvalPolicy='never',
        input=[dict(type='text', text=json.dumps(dict(answer=answer, **kwargs)))]))


def completed(events):
    seen = []
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        event = events.get(timeout=4)
        seen.append(event)
        if event['method'] == 'turn/completed':
            return seen
    raise AssertionError('Fixture did not complete')


def rows(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]


def test_broker_reads_unicode_paths_when_python_utf8_is_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv('PYTHONUTF8', '0')
    monkeypatch.delenv('PYTHONIOENCODING', raising=False)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))
    cwd = tmp_path / '中文项目'
    cwd.mkdir()
    command = [sys.executable, str(Path(__file__).with_name('fake_shared_server.py')), str(tmp_path / 'rpc.jsonl')]
    client = SharedConnection(command, cwd, 'wechat', lambda event: None)
    try:
        assert client.rpc('thread/start', dict(permissions='project_tools_read',
                          approvalPolicy='never', ephemeral=True))['thread']['id']
    finally:
        client.close()


def test_business_tool_request_is_routed_only_to_owning_web_peer(rig):
    _, _, log, connect, _ = rig
    web, events = connect('web')
    other, _ = connect('web')
    thread = start(web)
    turn(web, thread, 'DONE', tool=True)
    request = events.get(timeout=4)
    while request['method'] != 'item/tool/call':
        request = events.get(timeout=4)
    result = dict(success=True, contentItems=[dict(type='inputText', text='fixture status')])
    with pytest.raises(RpcError):
        other.rpc('project/tool-response', dict(requestId=request['id'], result=result))
    web.rpc('project/tool-response', dict(requestId=request['id'], result=result))
    completed(events)
    assert any(r.get('id') == request['id'] and r.get('result') == result for r in rows(log))


def test_wechat_listener_cannot_register_business_tools(rig):
    _, _, _, connect, _ = rig
    client, _ = connect('wechat')
    with pytest.raises(RpcError):
        client.rpc('thread/start',dict(permissions='project_tools_read', approvalPolicy='never',
                   ephemeral=True, dynamicTools=[dict(name='project_briefing')]))


def test_both_real_adapters_share_one_cli_and_isolate_notifications(rig, monkeypatch):
    command, cwd, log, _, clients = rig
    root = Path(__file__).resolve().parents[2]
    monkeypatch.syspath_prepend(str(root / '管理中心'))
    from codex_chat import AppServer
    web_events = queue.Queue()
    web = AppServer(command, cwd, web_events.put)
    wechat = WechatClient(command, cwd)
    clients.extend([web, wechat])
    assert web.process.pid == wechat.process.pid
    web_thread = start(web)
    wechat_thread = start(wechat, ephemeral=True)
    turn(web, web_thread, 'WEB_ONLY')
    turn(wechat, wechat_thread, 'WECHAT_ONLY')
    web_seen, wechat_seen = completed(web_events), completed(wechat.events)
    assert 'WECHAT_ONLY' not in json.dumps(web_seen)
    assert 'WEB_ONLY' not in json.dumps(wechat_seen)
    assert 'PRIVATE_REASONING' not in json.dumps(wechat_seen)
    requests = rows(log)
    assert sum('startup' in row for row in requests) == 1
    assert sum(row.get('method') == 'initialize' for row in requests) == 1


def test_closing_one_entry_does_not_kill_peer_backend(rig):
    _, _, log, connect, _ = rig
    first, _ = connect('wechat')
    second, events = connect()
    pid = second.process.pid
    first.close()
    assert second.process.pid == pid and second.process.poll() is None
    turn(second, start(second), 'still alive')
    assert completed(events)[-1]['params']['turn']['status'] == 'completed'
    assert sum('startup' in row for row in rows(log)) == 1
    second.close()
    assert not psutil.pid_exists(pid)


def test_closing_idle_entry_does_not_interrupt_other_entry_active_generation(rig):
    _, _, _, connect, _ = rig
    web, _ = connect()
    wechat, events = connect('wechat')
    thread = start(wechat, ephemeral=True)
    turn(wechat, thread, 'active peer survives', delay=.3)
    web.close()
    seen = completed(events)
    assert seen[-1]['params']['turn']['status'] == 'completed'
    assert 'active peer survives' in json.dumps(seen)


def test_closing_active_entry_interrupts_only_its_own_turn(rig):
    _, _, log, connect, _ = rig
    first, _ = connect()
    second, events = connect('wechat')
    first_thread, second_thread = start(first), start(second, ephemeral=True)
    turn(first, first_thread, 'cancel this entry', delay=.4)
    turn(second, second_thread, 'keep this entry', delay=.3)
    first.close()
    seen = completed(events)
    assert seen[-1]['params']['turn']['status'] == 'completed'
    interrupts = [row['params']['threadId'] for row in rows(log) if row.get('method') == 'turn/interrupt']
    assert first_thread in interrupts and second_thread not in interrupts


def test_entry_cannot_access_peer_thread_or_escalate_wechat_permissions(rig):
    _, _, _, connect, _ = rig
    web, _ = connect()
    wechat, _ = connect('wechat')
    thread = start(web, permission='project_tools_edit')
    with pytest.raises(RpcError, match='其他入口'):
        turn(wechat, thread, 'must not run')
    with pytest.raises(RpcError, match='授权'):
        start(wechat, ephemeral=True, permission='project_tools_edit')


def test_global_generation_cap_does_not_start_or_replay_rejected_turn(rig):
    _, _, log, connect, _ = rig
    connections = [connect() for _ in range(3)]
    threads = [start(client) for client, _ in connections]
    for index in (0, 1):
        turn(connections[index][0], threads[index], str(index), delay=.6)
    with pytest.raises(RpcError, match='上限'):
        turn(connections[2][0], threads[2], 'rejected')
    for _, events in connections[:2]:
        completed(events)
    assert sum(row.get('method') == 'turn/start' for row in rows(log)) == 2
    turn(connections[2][0], threads[2], 'explicit new attempt')
    completed(connections[2][1])


def test_idle_backend_is_reaped_without_rotation_or_new_generation(rig):
    _, _, log, connect, _ = rig
    client, _ = connect(idle_seconds=.25)
    pid = client.process.pid
    deadline = time.monotonic() + 4
    while client.process.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert client.process.poll() is not None
    client.close()
    deadline = time.monotonic() + 3
    while psutil.pid_exists(pid) and time.monotonic() < deadline:
        time.sleep(.05)
    assert not psutil.pid_exists(pid)
    assert not any(row.get('method') == 'turn/start' for row in rows(log))


def test_orphan_client_disconnect_does_not_kill_other_entry(rig):
    command, cwd, log, connect, _ = rig
    client, _ = connect()
    script = ('import json,os,sys; from wechat_local.shared_backend import SharedConnection; '
              "p=json.load(sys.stdin); c=SharedConnection(p['command'],p['cwd'],'wechat',lambda _:None); print(c.process.pid,flush=True); os._exit(0)")
    result = subprocess.run([sys.executable, '-c', script], input=json.dumps(dict(command=command, cwd=str(cwd))),
        text=True, capture_output=True, timeout=10,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1])})
    assert result.returncode == 0, result.stderr
    assert int(result.stdout.strip()) == client.process.pid
    assert client.rpc('config/read', {})['config']['model_provider'] == 'openai'
    assert sum('startup' in row for row in rows(log)) == 1


def test_incompatible_launch_settings_use_separate_processes(rig):
    command, cwd, _, connect, clients = rig
    first, _ = connect()
    second = SharedConnection(command + ['--different-profile'], cwd, 'web', lambda _: None)
    clients.append(second)
    assert first.process.pid != second.process.pid


def test_shared_backend_refuses_unscoped_history_and_global_control(rig):
    _, _, _, connect, _ = rig
    client, _ = connect()
    for method in ('thread/list', 'thread/read', 'shutdown'):
        with pytest.raises(RpcError, match='不支持'):
            client.rpc(method, {})


def test_waiting_request_timeout_never_enqueues_a_hidden_turn(rig):
    from concurrent.futures import ThreadPoolExecutor
    _, _, log, connect, _ = rig
    client, _ = connect()
    thread = start(client)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(client.rpc, 'config/read', dict(delay=.4))
        deadline = time.monotonic() + 2
        while not any(row.get('method') == 'config/read' for row in rows(log)) and time.monotonic() < deadline:
            time.sleep(.01)
        with pytest.raises(RpcTimeout, match='未提交'):
            client.rpc('turn/start', dict(threadId=thread, permissions='project_tools_read', approvalPolicy='never',
                input=[dict(type='text', text=json.dumps(dict(answer='must not run')))]), timeout=.05)
        future.result(timeout=2)
    assert not any(row.get('method') == 'turn/start' for row in rows(log))


def test_backend_crash_is_not_replayed_and_next_connection_starts_fresh(rig):
    _, _, log, connect, _ = rig
    first, _ = connect('wechat')
    second, _ = connect()
    with pytest.raises(RpcError):
        turn(first, start(first, ephemeral=True), 'crash once', crash=True)
    deadline = time.monotonic() + 4
    while second.process.poll() is None and time.monotonic() < deadline:
        time.sleep(.05)
    assert second.process.poll() is not None
    recovered, events = connect()
    turn(recovered, start(recovered), 'explicit recovery')
    completed(events)
    requests = rows(log)
    assert sum(row.get('method') == 'turn/start' for row in requests) == 2
    assert sum('startup' in row for row in requests) == 2


def test_loaded_thread_limit_can_be_released_without_rotating_web_history(rig):
    _, _, log, connect, _ = rig
    client, _ = connect()
    threads = [start(client) for _ in range(MAX_THREADS_PER_CLIENT)]
    with pytest.raises(RpcError, match='会话上限'):
        start(client)
    client.rpc('thread/unsubscribe', dict(threadId=threads[0]))
    client.rpc('thread/resume', dict(threadId=threads[0], permissions='project_tools_read', approvalPolicy='never'))
    assert sum(row.get('method') == 'thread/start' for row in rows(log)) == MAX_THREADS_PER_CLIENT


def test_orphan_reaping_never_interrupts_a_live_peer():
    import threading
    from types import SimpleNamespace
    from wechat_local.shared_backend import Broker, ORPHAN_GRACE_SECONDS
    closed, alive = threading.Event(), threading.Event()
    closed.set()
    broker = object.__new__(Broker)
    broker.active = {'orphan': dict(peer=SimpleNamespace(closed=closed), detached_at=0)}
    assert not broker.orphans_expired(ORPHAN_GRACE_SECONDS - 1)
    assert broker.orphans_expired(ORPHAN_GRACE_SECONDS + 1)
    broker.active['live'] = dict(peer=SimpleNamespace(closed=alive))
    assert not broker.orphans_expired(ORPHAN_GRACE_SECONDS + 1)
