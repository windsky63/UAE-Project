import json
from pathlib import Path
import queue
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from wechat_local.app_server import ResidentBackend, public_notification, CodexBackendError
from wechat_local.codex_runner import CodexRunner
from wechat_local.codex_settings import CodexGenerationTimeoutError, CodexIdleTimeoutError
from wechat_local.codex_settings import CodexConnectionTimeoutError, CodexServiceError
from wechat_local.config import LocalConfig, WechatChat


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'isolated-codex'))
    config = LocalConfig(codex_auto_access_dirs=False, codex_transport='https')
    config._config_dir = tmp_path
    progress = queue.Queue()
    service = ResidentBackend(config, progress)
    command = [sys.executable, str(Path(__file__).with_name('fake_wechat_server.py')), str(tmp_path / 'rpc.jsonl')]
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    try:
        yield service, config, command, workspace, progress
    finally:
        service.close()


def run(backend, text='normal', group='group', **extra):
    service, _, command, workspace, _ = backend
    prompt = 'Task data follows:\n' + json.dumps(dict(message=text, chat_id=group, message_id=text, **extra))
    return service.generate(command, workspace.parent, workspace, [], prompt, text, session_key=group)


def log(backend):
    path = backend[3].parent / 'rpc.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines(keepends=True) if line.endswith('\n')]


def test_one_process_and_one_config_read_serve_multiple_isolated_tasks(backend):
    first = run(backend, 'one', 'group-a')
    second = run(backend, 'two', 'group-b')
    assert first.text == '答复：group-a:one'
    assert second.text == '答复：group-b:two'
    assert not first.process_reused and second.process_reused
    assert not first.config_cache_hit and second.config_cache_hit
    requests = log(backend)
    assert len([r for r in requests if 'startup' in r]) == 1
    assert sum(r.get('method') == 'initialize' for r in requests) == 1
    assert sum(r.get('method') == 'config/read' for r in requests) == 1
    assert sum(r.get('method') == 'thread/start' for r in requests) == 2
    assert sum(r.get('method') == 'thread/unsubscribe' for r in requests) == 0
    turns = [r['params']['threadId'] for r in requests if r.get('method') == 'turn/start']
    assert len(set(turns)) == 2
    assert second.usage['input_tokens'] == 123
    assert second.tool_calls == 1
    assert backend[4].get_nowait() == ('one', 'querying')


def test_workspace_config_change_and_ttl_expiry_refresh_cached_config(backend):
    run(backend)
    config_file = backend[3] / '.codex/config.toml'
    config_file.parent.mkdir()
    config_file.write_text('# changed local layer', encoding='utf-8')
    changed = run(backend)
    assert not changed.config_cache_hit and changed.process_reused
    backend[0].CONFIG_TTL = 0
    expired = run(backend)
    assert not expired.config_cache_hit
    assert sum(r.get('method') == 'config/read' for r in log(backend)) == 3


def test_global_config_change_restarts_backend_before_next_task(backend):
    run(backend)
    home = Path(__import__('os').environ['CODEX_HOME'])
    home.mkdir()
    (home / 'config.toml').write_text('# changed global config', encoding='utf-8')
    assert not run(backend).process_reused
    assert len([r for r in log(backend) if 'startup' in r]) == 2


def test_same_process_uses_distinct_effective_config_cache_for_each_workspace(backend):
    run(backend)
    service, _, command, workspace, _ = backend
    other = workspace.parent / 'other-chat'
    other.mkdir()
    prompt = 'Task data follows:\n' + json.dumps(dict(message='normal', chat_id='other'))
    result = service.generate(command, workspace.parent, other, [], prompt, 'other')
    assert result.process_reused and not result.config_cache_hit
    assert sum(r.get('method') == 'config/read' for r in log(backend)) == 2


def test_crashed_generation_is_not_replayed_and_next_task_restarts(backend):
    with pytest.raises(RuntimeError):
        run(backend, 'crash')
    result = run(backend, 'recovered')
    assert result.text.endswith('recovered') and not result.process_reused
    turns = [r for r in log(backend) if r.get('method') == 'turn/start']
    assert len(turns) == 2


def test_configuration_failures_are_not_cached_or_silently_replayed(backend):
    backend[2].append('--fail-config-once')
    with pytest.raises(RuntimeError, match='config/read'):
        run(backend)
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 0
    result = run(backend)
    assert not result.config_cache_hit and not result.process_reused
    assert sum(r.get('method') == 'config/read' for r in log(backend)) == 2


@pytest.mark.parametrize('mode,expected', [('hang', CodexGenerationTimeoutError), ('idle', CodexIdleTimeoutError)])
def test_resident_timeouts_stop_backend_without_replaying_task(backend, mode, expected):
    backend[1].codex_timeout_seconds = .7
    backend[1].codex_idle_timeout_seconds = .2
    with pytest.raises(expected):
        run(backend, mode)
    assert backend[0].client is None
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 1


def test_explicit_network_retries_are_bounded_and_next_task_can_reconnect(backend):
    backend[1].codex_connect_timeout_seconds = .15
    with pytest.raises(CodexConnectionTimeoutError):
        run(backend, 'network-down')
    assert backend[0].client is None
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 1
    assert run(backend, 'recovered').text.endswith('recovered')


@pytest.mark.parametrize('mode', ['account-quota', 'account-login'])
def test_account_rejection_stops_without_waiting_for_network_deadline(backend, mode):
    with pytest.raises(CodexServiceError) as error:
        run(backend, mode)
    assert error.value.reason == 'account'
    assert 'PRIVATE' not in str(error.value)
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 1


@pytest.mark.parametrize('mode', ['slow-initial', 'transient-network-slow'])
def test_slow_thinking_does_not_trigger_network_failure(backend, mode):
    backend[1].codex_connect_timeout_seconds = .1
    assert backend[1].codex_timeout_seconds == backend[1].codex_idle_timeout_seconds == 0
    assert run(backend, mode).text.endswith(mode)


def test_operator_can_stop_a_task_without_a_generation_deadline(backend):
    service = backend[0]
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, backend, 'idle')
        deadline = time.monotonic() + 5
        while not any(row.get('method') == 'turn/start' for row in log(backend)):
            assert time.monotonic() < deadline
            time.sleep(.02)
        service.request_stop()
        with pytest.raises(CodexBackendError, match='主动停止'):
            future.result(timeout=5)


def test_service_error_notification_retains_only_fixed_classification():
    event = public_notification(dict(method='error', params=dict(threadId='t', willRetry=True,
        error=dict(codexErrorInfo={'httpConnectionFailed': {'httpStatusCode': 401}}, message='PRIVATE_SECRET'))))
    assert event['params']['serviceFailure'] == 'account'
    assert 'PRIVATE_SECRET' not in json.dumps(event)


def test_cleanup_failure_keeps_completed_answer_and_recycles_process(backend):
    backend[1].codex_context_window_tokens = 100
    assert run(backend, 'cleanup-fail').text.endswith('cleanup-fail')
    assert run(backend).text.endswith('normal')
    for future in backend[0].cleanups:
        future.result(timeout=3)
    assert not run(backend).process_reused


def test_only_complete_final_answer_is_accepted(backend):
    with pytest.raises(RuntimeError, match='最终回复'):
        run(backend, 'bad-final')
    assert backend[0].client is None


def test_interactive_requests_are_denied_and_retry_notices_do_not_replay(backend):
    assert run(backend, 'approval').text.endswith('approval')
    denial = next(r for r in log(backend) if r.get('id') == 'request-approval' and 'error' in r)
    assert denial['error']['code'] == -32601
    assert run(backend, 'retry').text.endswith('retry')
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 2


def test_planned_recycling_and_close_release_owned_process(backend):
    backend[0].MAX_RETIRED_THREADS = 1
    backend[1].codex_context_window_tokens = 100
    run(backend)
    previous = backend[0].client.process
    run(backend)
    assert not run(backend).process_reused
    assert previous.poll() is not None
    current = backend[0].client.process
    backend[0].close()
    assert current.poll() is not None


def test_raw_reasoning_and_tool_content_are_not_queued():
    reasoning = public_notification(dict(method='item/reasoning/textDelta', params=dict(threadId='t', delta='PRIVATE')))
    tool = public_notification(dict(method='item/completed', params=dict(threadId='t',
        item=dict(type='commandExecution', id='x', command='PRIVATE', aggregatedOutput='PRIVATE'))))
    assert 'PRIVATE' not in json.dumps([reasoning, tool])


def test_runner_default_backend_reuses_process_and_preserves_memory_contract(tmp_path, monkeypatch):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'isolated-codex'))
    config = LocalConfig(codex_command=[sys.executable, str(Path(__file__).with_name('fake_wechat_server.py')),
                                       str(tmp_path / 'runner-rpc.jsonl')],
                         codex_auto_access_dirs=False, codex_transport='https')
    config._config_dir = tmp_path
    runner = CodexRunner(config)
    workspace = tmp_path / 'chat'
    chat = WechatChat(id='group', name='Group', kind='group')
    try:
        for identity in ('one', 'two'):
            result = runner.generate(dict(id=identity, text=identity, sender='alice'), chat, [], workspace)
            assert result.reply == '答复：group:' + identity
            assert result.memory_note == '内部短摘要'
        process = runner.backend.client.process
        metrics = json.loads((workspace / 'generation-metrics.json').read_text(encoding='utf-8'))
        assert metrics['backend'] == 'app-server'
        assert metrics['backend_process_reused'] and metrics['connection_config_cache_hit']
        assert metrics['session_reused']
        assert metrics['tool_calls'] == 1
        requests = [json.loads(line) for line in (tmp_path / 'runner-rpc.jsonl').read_text(encoding='utf-8').splitlines()]
        assert sum(row.get('method') == 'thread/start' for row in requests) == 1
    finally:
        runner.close()
    assert process.poll() is not None


def test_same_chat_reuses_one_thread_without_cumulative_billing_triggering_rotation(backend):
    for i in range(4):
        result = run(backend, str(i), context_used=100, context_window=1000, cumulative_used=999999)
        assert result.session_metrics['context_percent'] == 10
        assert result.session_metrics['session_reused'] is (i > 0)
    assert sum(r.get('method') == 'thread/start' for r in log(backend)) == 1
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 4


def test_prewarm_occurs_during_generation_and_is_used_only_after_rotation_threshold(backend):
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(run, backend, 'first', early_context_used=550, context_used=600,
                               context_window=1000, work_seconds=.6)
        deadline = time.monotonic() + 3
        while sum(r.get('method') == 'thread/start' for r in log(backend)) < 2:
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert not first.done()
        first.result(timeout=3)
    second = run(backend, 'second', context_used=750, context_window=1000)
    assert second.session_metrics['session_reused']
    third = run(backend, 'third', context_used=100, context_window=1000)
    assert not third.session_metrics['session_reused']
    assert third.session_metrics['session_prewarmed']
    assert third.session_metrics['session_transition'] == 'context_threshold'
    requests = log(backend)
    assert sum(r.get('method') == 'thread/start' for r in requests) == 2
    turn_threads = [r['params']['threadId'] for r in requests if r.get('method') == 'turn/start']
    assert turn_threads[0] == turn_threads[1] and turn_threads[2] != turn_threads[1]


def test_custom_threshold_manual_window_and_disabled_prewarm(backend):
    config = backend[1]
    config.codex_session_rotate_percent = 40
    config.codex_session_prewarm = False
    config.codex_context_window_tokens = 2000
    first = run(backend, context_used=800, context_window=1000)
    assert first.session_metrics['context_percent'] == 40
    assert sum(r.get('method') == 'thread/start' for r in log(backend)) == 1
    second = run(backend, 'next', context_used=100, context_window=1000)
    assert not second.session_metrics['session_reused']
    assert not second.session_metrics['session_prewarmed']


def test_unknown_usage_uses_explicit_fallback_without_fabricating_percentage(backend):
    backend[1].codex_session_unknown_max_turns = 2
    backend[1].codex_session_prewarm = False
    first = run(backend, 'one')
    assert first.session_metrics['context_percent'] is None
    assert run(backend, 'two').session_metrics['session_reused']
    third = run(backend, 'three')
    assert not third.session_metrics['session_reused']
    assert third.session_metrics['session_transition'] == 'unknown_usage_turn_limit'


def test_continuation_omits_duplicate_context_and_rotation_receives_fresh_handoff(backend):
    backend[1].codex_session_prewarm = False
    run(backend, 'one', context_used=100, context_window=1000,
        recent=[dict(id='m1', text='old')], related_chat_memory=[dict(job_id='old-qa', note='known')],
        handoff_memory=[dict(job_id='older', note='earlier confirmed answer')])
    run(backend, 'two', context_used=750, context_window=1000,
        recent=[dict(id='m1', text='old'), dict(id='one', text='question one'), dict(id='m2', text='new')],
        related_chat_memory=[dict(job_id='old-qa', note='known'), dict(job_id='new-qa', note='new')],
        handoff_memory=[dict(job_id='must-not-repeat', note='old handoff')])
    run(backend, 'three', context_used=100, context_window=1000,
        handoff_memory=[dict(job_id='two', note='latest confirmed summary')])
    turns = [r['params'] for r in log(backend) if r.get('method') == 'turn/start']
    second_payload = json.loads(turns[1]['input'][0]['text'].split('Task data follows:\n')[1])
    third_payload = json.loads(turns[2]['input'][0]['text'].split('Task data follows:\n')[1])
    assert [row['id'] for row in second_payload['recent']] == ['m2']
    assert [row['job_id'] for row in second_payload['related_chat_memory']] == ['new-qa']
    assert 'handoff_memory' not in second_payload
    assert third_payload['handoff_memory'][0]['note'] == 'latest confirmed summary'


def test_prewarm_failure_keeps_current_answer_and_creates_fresh_on_rotation(backend):
    backend[2].append('--fail-prewarm-once')
    assert run(backend, 'first', context_used=750, context_window=1000).text.endswith('first')
    slot = next(iter(backend[0].sessions.values()))
    with pytest.raises(RuntimeError):
        slot.standby.result(timeout=3)
    result = run(backend, 'next', context_used=100, context_window=1000)
    assert result.text.endswith('next')
    assert not result.session_metrics['session_prewarmed']
    assert sum(r.get('method') == 'turn/start' for r in log(backend)) == 2


def test_config_change_retires_precreated_session_without_using_stale_settings(backend):
    run(backend, context_used=600, context_window=1000)
    slot = next(iter(backend[0].sessions.values()))
    stale = slot.standby.result(timeout=3)
    path = backend[3] / '.codex/config.toml'
    path.parent.mkdir()
    path.write_text('# new settings', encoding='utf-8')
    result = run(backend, 'new', context_used=100, context_window=1000)
    assert result.session_metrics['session_transition'] == 'settings_changed'
    assert not result.session_metrics['session_prewarmed']
    for future in backend[0].cleanups:
        future.result(timeout=3)
    requests = log(backend)
    turns = [r['params']['threadId'] for r in requests if r.get('method') == 'turn/start']
    assert stale.thread not in turns
    assert any(r.get('method') == 'thread/unsubscribe' and r['params']['threadId'] == stale.thread for r in requests)


def test_late_usage_is_processed_before_next_question_selects_session(backend):
    backend[1].codex_session_prewarm = False
    run(backend, context_used=100, context_window=1000)
    current = next(iter(backend[0].sessions.values())).current
    backend[0].client.events.put(public_notification(dict(method='thread/tokenUsage/updated', params=dict(
        threadId=current.thread, tokenUsage=dict(last=dict(totalTokens=750), modelContextWindow=1000)))))
    result = run(backend, 'next', context_used=100, context_window=1000)
    assert result.session_metrics['session_transition'] == 'context_threshold'


def test_each_chat_rotates_independently(backend):
    backend[1].codex_session_prewarm = False
    run(backend, 'a1', group='a', context_used=750, context_window=1000)
    run(backend, 'b1', group='b', context_used=100, context_window=1000)
    a = run(backend, 'a2', group='a', context_used=100, context_window=1000)
    b = run(backend, 'b2', group='b', context_used=100, context_window=1000)
    assert not a.session_metrics['session_reused'] and b.session_metrics['session_reused']
    turns = [r['params']['threadId'] for r in log(backend) if r.get('method') == 'turn/start']
    assert turns[0] != turns[2] and turns[1] == turns[3]


def test_missing_telemetry_does_not_guess_percentage_from_manual_capacity_alone(backend):
    backend[1].codex_context_window_tokens = 1000
    backend[1].codex_session_unknown_max_turns = 2
    backend[1].codex_session_prewarm = False
    for text in ('one', 'two'):
        result = run(backend, text, omit_usage=True)
        assert result.session_metrics['context_percent'] is None
    assert not run(backend, 'three', omit_usage=True).session_metrics['session_reused']


def test_ordinary_turn_count_does_not_recycle_a_healthy_chat_below_threshold(backend):
    for i in range(34):
        result = run(backend, str(i), context_used=100, context_window=1000)
    assert result.session_metrics['session_turns'] == 34
    assert sum('startup' in r for r in log(backend)) == 1
    assert sum(r.get('method') == 'thread/start' for r in log(backend)) == 1


def test_context_rotation_settings_are_validated():
    with pytest.raises(ValueError, match='提前创建'):
        LocalConfig(codex_session_rotate_percent=50, codex_session_prewarm_percent=55)
    config = LocalConfig(codex_session_rotate_percent=1, codex_session_prewarm=False)
    assert config.codex_session_rotate_percent == 1
