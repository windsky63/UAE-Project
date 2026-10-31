from datetime import datetime, timedelta, timezone
from concurrent.futures import Future
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time

import pytest

from wechat_local.config import LocalConfig, WechatChat, load_config
from wechat_local.codex_runner import CodexRunner
from wechat_local.delivery import DeliveryNotSent
from wechat_local.codex_settings import CodexConfigTimeoutError, CodexGenerationTimeoutError
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State


def event(identifier='1', **changes):
    return {'id': identifier, 'chat_id': 'group', 'sender': 'alice', 'sender_id': 'alice',
            'direction': 'incoming', 'content_type': 'text', 'text': '@助手 你好',
            'historical': False, 'observed_at': datetime.now(timezone.utc).isoformat(), **changes}


class Driver:
    account_id = 'wxid-test'
    account_name = '助手'

    def __init__(self):
        self.events, self.sent = [], []
        self.mentions = []
        self.failure = None

    def capture(self, chat_id, spool):
        events = [e for e in self.events if e['chat_id'] == chat_id]
        spool.capture_database('source:' + chat_id, events, {'test': 1})

    def send(self, chat_id, text, mention_user=None):
        self.sent.append((chat_id, text))
        self.mentions.append(mention_user)
        if self.failure:
            raise self.failure


class Runner:
    def __init__(self):
        self.calls = []

    def generate(self, event, chat, recent, workspace):
        self.calls.append((event, chat, recent, workspace))
        return '测试回复', {'input_tokens': 50, 'output_tokens': 10}


@pytest.fixture
def rig(tmp_path):
    config = LocalConfig(chats=[WechatChat(id='group', name='测试群', kind='group', reply=True),
                                WechatChat(id='private', name='朋友', kind='private', reply=True)],
                         codex_enabled=True, allow_send=True, workspaces=str(tmp_path / 'chats'))
    driver, runner = Driver(), Runner()
    spool, state = DesktopSpool(str(tmp_path / 'spool.db')), State(str(tmp_path / 'state.db'))
    listener = Listener(config, driver, spool, state, runner)
    yield config, driver, runner, spool, state, listener
    listener.close()
    spool.db.close()
    state.close()


def finish(listener):
    if listener.active:
        listener.active[1].result(timeout=3)
    listener.tick()


def sent_bodies(driver):
    """Keep existing delivery assertions focused on the answer/notice body."""
    return [(chat, '正在思考中……' if text.endswith('正在思考中……')
             else '正在排队中……' if text.endswith('正在排队中……')
             else text.split('回答如下：\n', 1)[-1]) for chat, text in driver.sent]


def test_idle_and_repeated_capture_do_not_repeat_codex_or_send(rig):
    config, driver, runner, spool, state, listener = rig
    listener.tick()
    assert runner.calls == []
    driver.events = [event()]
    listener.tick()
    finish(listener)
    for _ in range(3):
        listener.tick()
    assert len(runner.calls) == 1
    assert sent_bodies(driver) == [('group', '正在思考中……'), ('group', '测试回复')]
    row = state.db.execute('SELECT status,usage FROM jobs').fetchone()
    assert row['status'] == 'confirmed'
    assert json.loads(row['usage'])['input_tokens'] == 50


@pytest.mark.parametrize('changes', [
    {'historical': True}, {'direction': 'outgoing'}, {'content_type': 'image'},
    {'text': '没有艾特'}, {'text': '@助手 /help'}, {'text': '@助手 /persona set x'},
    {'text': '@助手其他人 hello'}, {'text': ''}, {'chat_id': 'unknown'},
    {'observed_at': (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()},
])
def test_untriggered_messages_never_call_codex(rig, changes):
    config, driver, runner, spool, state, listener = rig
    assert not listener.eligible(event(**changes))
    driver.events = [event(**changes)]
    listener.tick()
    assert driver.sent == [] and runner.calls == []


def test_group_preview_sends_no_thinking_acknowledgement(rig):
    config, driver, runner, spool, state, listener = rig
    config.allow_send = False
    driver.events = [event()]
    listener.tick()
    finish(listener)
    assert driver.sent == []
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'preview'


def test_private_chat_and_preview_mode(rig):
    config, driver, runner, spool, state, listener = rig
    config.allow_send = False
    driver.events = [event(chat_id='private', text='你好')]
    listener.tick()
    finish(listener)
    assert len(runner.calls) == 1
    assert driver.sent == []
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'preview'
    config.allow_send = True
    listener.tick()
    assert driver.sent == []  # Preview is never silently sent after a config switch.


def test_capture_only_never_calls_codex(rig):
    config, driver, runner, spool, state, listener = rig
    config.codex_enabled = False
    driver.events = [event()]
    listener.tick()
    assert runner.calls == []
    assert state.db.execute('SELECT count(*) FROM archive').fetchone()[0] == 1


def test_capture_continues_during_codex_generation(rig):
    config, driver, runner, spool, state, listener = rig
    release = threading.Event()
    original = runner.generate

    def slow(*args):
        release.wait(3)
        return original(*args)

    runner.generate = slow
    driver.events = [event()]
    try:
        listener.tick()
        driver.events.append(event('2', text='@助手 第二条'))
        listener.tick()
        assert state.db.execute("SELECT count(*) FROM archive WHERE direction='incoming'").fetchone()[0] == 2
        assert sent_bodies(driver) == [('group', '正在思考中……'), ('group', '正在排队中……')]
        assert state.job('2')['status'] == 'pending'
        assert state.job('2')['started_at'] is None
    finally:
        release.set()
    finish(listener)
    finish(listener)
    assert len(runner.calls) == 2


def task_clock(monkeypatch):
    clock = [datetime.now(timezone.utc)]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)
    monkeypatch.setattr('wechat_local.listener.datetime', Clock)
    monkeypatch.setattr('wechat_local.state.utcnow', lambda: clock[0].isoformat())
    return clock


def test_queue_wait_does_not_expire_job_and_notices_share_identity(rig, monkeypatch):
    config, driver, runner, spool, state, listener = rig
    clock = task_clock(monkeypatch)
    config.reply_max_age_seconds = 10
    listener.active = ('busy-task', Future())
    driver.events = [event(sender='张三', sender_id='wxid-alice')]
    listener.tick()
    assert driver.sent == [('group', '任务编号：1\n回复对象：@张三\n任务排序：第1位\n正在排队中……')]
    assert driver.mentions == ['wxid-alice']
    assert state.job('1')['started_at'] is None
    clock[0] += timedelta(hours=1)
    listener.active = None
    listener.tick()
    started = state.job('1')['started_at']
    assert started == clock[0].isoformat()
    assert driver.sent[1][1].startswith('任务编号：1\n回复对象：@张三\n任务开始时间：')
    assert driver.sent[1][1].endswith('（北京时间）\n正在思考中……')
    clock[0] += timedelta(seconds=3)
    finish(listener)
    assert state.job('1')['status'] == 'confirmed'
    assert len(runner.calls) == 1
    assert driver.sent[2][1].startswith('任务编号：1\n回复对象：@张三\n任务结束时间：')
    assert driver.sent[2][1].endswith('（北京时间）\n回答如下：\n测试回复')
    assert state.job('1')['finished_at'] == clock[0].isoformat()
    assert state.job('1')['started_at'] == started
    assert state.memory_rows('group')[0]['answer'] == '测试回复'
    state.archive(event('captured-notice', direction='outgoing', text='@张三 ' + driver.sent[1][1]))
    assert 'captured-notice' not in {row['id'] for row in state.recent('group', 20, 'unused')}
    assert all(not row['id'].startswith(('thinking:', 'queued:', 'querying:'))
               for row in state.recent('group', 20, 'unused'))


def test_reply_lifetime_still_expires_after_execution_starts(rig, monkeypatch):
    config, driver, runner, spool, state, listener = rig
    clock = task_clock(monkeypatch)
    config.reply_max_age_seconds = 10
    driver.events = [event()]
    listener.tick()
    listener.active[1].result(timeout=3)
    clock[0] += timedelta(seconds=11)
    listener.tick()
    assert state.job('1')['status'] == 'expired'
    assert len(driver.sent) == 1
    assert driver.sent[0][1].endswith('正在思考中……')


def test_pending_job_survives_restart_and_queue_wait(rig, monkeypatch):
    config, driver, runner, spool, state, listener = rig
    clock = task_clock(monkeypatch)
    state.enqueue(event())
    clock[0] += timedelta(hours=2)
    database = state.db.execute('PRAGMA database_list').fetchone()[2]
    reloaded = State(database)
    try:
        listener.state = reloaded
        listener.start_next()
        finish(listener)
        assert reloaded.job('1')['status'] == 'confirmed'
        assert reloaded.job('1')['started_at'] == clock[0].isoformat()
    finally:
        listener.state = state
        reloaded.close()


def test_queue_rank_is_snapshot_and_notice_is_deduplicated(rig):
    config, driver, runner, spool, state, listener = rig
    for identifier in ('1', '2'):
        state.enqueue(event(identifier))
    second = state.job('2')
    listener.queue_notice(event('2'), second)
    assert '任务排序：第2位' in driver.sent[0][1]
    state.set_job('1', 'failed')
    listener.queue_notice(event('2'), state.job('2'))
    assert len(driver.sent) == 1
    assert state.queue_position('2') == 2


def test_short_task_numbers_survive_restart_and_duplicate_intake(tmp_path):
    path = tmp_path / 'numbers.db'
    first, second = 'f' * 64, 'a' * 64
    state = State(path)
    state.enqueue(event(first))
    state.enqueue(event(first))
    assert state.task_number(first) == '1'
    state.close()
    state = State(path)
    try:
        state.enqueue(event(second))
        assert state.task_number(first) == '1'
        assert state.task_number(second) == '2'
    finally:
        state.close()


def test_generation_starts_before_slow_acknowledgement(rig):
    _, driver, runner, _, _, listener = rig
    started = threading.Event()
    original_generate = runner.generate
    original_send = driver.send
    def generate(*args):
        started.set()
        return original_generate(*args)
    def send(*args, **kwargs):
        assert started.wait(2), 'UI acknowledgement blocked model startup'
        return original_send(*args, **kwargs)
    runner.generate = generate
    driver.send = send
    driver.events = [event()]
    listener.tick()
    finish(listener)
    assert driver.mentions == ['alice', 'alice']


@pytest.mark.parametrize('failure,expected', [(RuntimeError('unknown'), 'uncertain'), (DeliveryNotSent('blocked'), 'not_sent')])
def test_failed_ui_send_is_not_automatically_repeated(rig, failure, expected):
    config, driver, runner, spool, state, listener = rig
    driver.failure = failure
    driver.events = [event()]
    listener.tick()
    finish(listener)
    listener.tick()
    assert len(driver.sent) == 2  # One acknowledgement and one final reply, neither retried.
    assert len(runner.calls) == 1
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == expected


def test_unsent_reply_retries_without_regenerating(rig, monkeypatch):
    config, driver, runner, spool, state, listener = rig
    clock = [1000.0]
    monkeypatch.setattr('wechat_local.spool.time.time', lambda: clock[0])
    driver.failure = DeliveryNotSent('window changed')
    driver.events = [event()]
    listener.tick()
    finish(listener)
    driver.failure = None
    listener.tick()
    assert len(driver.sent) == 2
    clock[0] += config.send_retry_seconds
    listener.tick()
    listener.tick()
    assert sent_bodies(driver) == [('group', '正在思考中……'), ('group', '测试回复'), ('group', '测试回复')]
    assert driver.sent[1] == driver.sent[2]  # Stable task metadata on send retry.
    assert len(runner.calls) == 1
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'confirmed'


def test_send_retry_budget_survives_restart(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('wechat_local.spool.time.time', lambda: clock[0])
    path = str(tmp_path / 'spool.db')
    driver = Driver()
    driver.failure = DeliveryNotSent('window changed')
    command = dict(id='reply:1', chat_id='group', text='reply')
    for _ in range(5):
        spool = DesktopSpool(path)
        assert spool.execute(command, driver, max_attempts=3)['status'] == 'not_sent'
        spool.db.close()
        clock[0] += 5
    assert len(driver.sent) == 3


def test_uncertain_send_reconciles_without_resending(rig):
    config, driver, runner, spool, state, listener = rig
    driver.failure = RuntimeError('confirmation delayed')
    driver.events = [event()]
    listener.tick()
    finish(listener)
    driver.delivery_confirmed = lambda chat_id, text, since, **kwargs: True
    listener.tick()
    listener.tick()
    assert len(driver.sent) == 2
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'confirmed'


def test_expired_unsent_reply_does_not_retry(rig):
    config, driver, runner, spool, state, listener = rig
    old = event(observed_at=(datetime.now(timezone.utc) - timedelta(hours=1)).isoformat())
    state.enqueue(old)
    state.set_job('1', 'not_sent', reply='old reply')
    listener.deliver()
    assert driver.sent == []
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'expired'


def test_generation_timeout_reports_stage_and_limit(rig):
    config, driver, runner, spool, state, listener = rig
    def timeout(*args):
        raise CodexGenerationTimeoutError('generation timed out')
    runner.generate = timeout
    driver.events = [event()]
    listener.tick()
    with pytest.raises(TimeoutError):
        listener.active[1].result(timeout=3)
    listener.tick()
    row = state.db.execute('SELECT status,error FROM jobs').fetchone()
    assert row['status'] == 'failed'
    assert str(config.codex_timeout_seconds) in row['error']
    assert '尚未进入微信发送阶段' in row['error']
    assert sent_bodies(driver) == [('group', '正在思考中……')]


def test_configuration_timeout_is_not_reported_as_generation_timeout(rig):
    *_, listener = rig
    error = listener.generation_error(CodexConfigTimeoutError('读取 Codex 配置超时：config/read 等待响应超过 15 秒'))
    assert 'config/read' in error
    assert '15 秒' in error
    assert '尚未启动模型生成' in error
    assert '生成超过' not in error
    assert '具体阶段未确定' in listener.generation_error(TimeoutError())


@pytest.mark.parametrize('submitted', [False, True])
def test_ui_interrupt_classification(submitted):
    from wechat_local.wechat_uia import WindowsUIASender
    sender = object.__new__(WindowsUIASender)
    def interrupted(name, text, mark_submitted):
        if submitted:
            mark_submitted()
        raise TimeoutError('UI interrupted')
    sender._send = interrupted
    with pytest.raises(TimeoutError if submitted else DeliveryNotSent):
        sender.send('group', 'reply')


def test_old_spool_schema_is_upgraded_without_repeating_confirmed_send(tmp_path):
    path = str(tmp_path / 'old.db')
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE ui_actions(id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '')")
    db.close()
    spool = DesktopSpool(path)
    driver = Driver()
    command = dict(id='reply:1', chat_id='group', text='reply')
    try:
        assert spool.execute(command, driver, max_attempts=3)['status'] == 'confirmed'
        assert spool.execute(command, driver, max_attempts=3)['status'] == 'confirmed'
        assert len(driver.sent) == 1
    finally:
        spool.db.close()


def test_database_confirmation_requires_matching_account_text_chat_and_time():
    from types import SimpleNamespace
    from wechat_local.wechat_capture import LocalDatabaseDriver
    driver = object.__new__(LocalDatabaseDriver)
    driver.account_id = 'bot'
    driver.targets = {'group': 'native-group'}
    rows = []
    driver.reader = SimpleNamespace(account_id='bot', recent=lambda target: rows if target == 'native-group' else [])
    for sender, text, timestamp in [('alice', 'reply', 101), ('bot', 'other', 101), ('bot', 'reply', 99)]:
        rows[:] = [dict(sender_username=sender, content=text, create_time=timestamp)]
        assert not driver.delivery_confirmed('group', 'reply', 100)
    rows[:] = [dict(sender_username='bot', content='reply', create_time=101)]
    assert driver.delivery_confirmed('group', 'reply', 100)


def test_restart_preserves_generated_reply_and_deduplicates_send(tmp_path):
    state = State(str(tmp_path / 'state.db'))
    state.enqueue(event())
    state.set_job('1', 'generated', reply='persisted')
    state.close()
    state = State(str(tmp_path / 'state.db'))
    assert state.generated()[0]['reply'] == 'persisted'
    state.set_job('1', 'running')
    # Read-only status inspection must not alter live job states.
    second = State(str(tmp_path / 'state.db'))
    assert second.db.execute('SELECT status FROM jobs').fetchone()[0] == 'running'
    second.close()
    state.recover_interrupted()
    assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'failed'
    state.close()


def test_chat_snapshot_and_account_isolation(rig, tmp_path):
    config, driver, runner, spool, state, listener = rig
    state.archive(event(text='本群内容'))
    state.archive(event('other', chat_id='private', text='其他聊天机密'))
    target = tmp_path / 'snapshot/memory.sqlite3'
    state.export_chat('group', target)
    conn = sqlite3.connect(target)
    assert conn.execute('SELECT text FROM messages').fetchall() == [('本群内容',)]
    conn.close()
    # A late-arriving message with an earlier timestamp must still be exported.
    state.archive(event('late', text='迟到的记录', observed_at='2026-01-01T00:00:00+00:00'))
    state.export_chat('group', target)
    state.export_chat('group', target)
    conn = sqlite3.connect(target)
    assert conn.execute('SELECT count(*) FROM messages').fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM messages WHERE text='其他聊天机密'").fetchone()[0] == 0
    conn.close()
    with pytest.raises(ValueError):
        state.bind_account(config.bot_id, 'another-account')


def test_config_defaults_need_no_server_or_model(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{}', encoding='utf-8')
    config = load_config(path)
    assert config.codex_enabled and config.allow_send
    assert config.database_account == ''
    assert not hasattr(config, 'server_url')
    assert Path(config.state_path).parent == tmp_path / 'data'
    with pytest.raises(ValueError):
        LocalConfig(chats=[{'id':'x','name':'one','kind':'group'}, {'id':'x','name':'two','kind':'group'}])


def test_codex_subprocess_uses_final_answer_and_reports_usage(tmp_path):
    fake = tmp_path / 'fake_codex.py'
    fake.write_text('''import sys,json,pathlib
args=sys.argv
prompt=sys.stdin.read()
assert 'Use $wechat-chat' in prompt
assert 'Shared project evidence instructions (web and WeChat)' in prompt
assert '--workspace' in prompt
assert 'default_permissions=\"project_tools_read\"' in args
pathlib.Path(args[args.index('--output-last-message')+1]).write_text('final reply',encoding='utf-8')
print(json.dumps({'type':'item.completed','item':{'text':'must not be sent'}}))
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':123}}))
''', encoding='utf-8')
    config = LocalConfig(codex_command=[sys.executable, str(fake)], codex_auto_access_dirs=False, codex_backend='exec')
    reply, usage = CodexRunner(config).generate(event(), WechatChat(id='group',name='test',kind='group'), [], tmp_path / 'workspace')
    assert reply == 'final reply'
    assert usage == {'input_tokens':123}
    assert (tmp_path / 'workspace/.agents/skills/wechat-chat/SKILL.md').is_file()
    assert (tmp_path / 'workspace/.agents/skills/wechat-chat/project-qa.md').is_file()


@pytest.mark.parametrize('transport,supports_ws', [('https',False), ('websocket',True)])
def test_wechat_runner_applies_shared_transport_and_read_directories(tmp_path, monkeypatch, transport, supports_ws):
    fake = tmp_path/'inspect_codex.py'
    fake.write_text('''import sys,json,tomllib,pathlib
settings={}
for i,arg in enumerate(sys.argv):
    if arg!='-c':continue
    parsed=tomllib.loads(sys.argv[i+1])
    if 'permissions' in parsed:settings.setdefault('permissions',{}).update(parsed['permissions'])
    else:settings.update(parsed)
payload=json.loads(sys.stdin.read().split('Task data follows:\\n')[1])
provider=settings['model_providers'][settings['model_provider']]
result=dict(supports_websockets=provider['supports_websockets'],directories=payload['accessible_directories'],
            filesystem=settings['permissions']['project_tools_read']['filesystem'])
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(result),encoding='utf-8')
''',encoding='utf-8')
    folder=tmp_path/'project data'
    folder.mkdir()
    calls=[]
    def effective(command,cwd):
        calls.append(command)
        return {}
    monkeypatch.setattr('wechat_local.codex_runner.read_codex_config',effective)
    config=LocalConfig(codex_command=[sys.executable,str(fake)],codex_auto_access_dirs=False,codex_backend='exec',
                       codex_access_dirs=[str(folder)],codex_transport=transport)
    answer,_=CodexRunner(config).generate(event(),WechatChat(id='group',name='test',kind='group'),[],tmp_path/'task')
    result=json.loads(answer)
    assert result['supports_websockets'] is supports_ws
    assert result['directories']==[str(folder.resolve())]
    assert result['filesystem'][str(folder.resolve())]=='read'
    assert result['filesystem'][':workspace_roots']['.']=='read'
    assert len(calls)==1


def test_read_only_history_and_file_search(rig, tmp_path):
    config, driver, runner, spool, state, listener = rig
    helper = Path(__file__).resolve().parents[1] / '.agents/skills/wechat-chat/search.py'
    spec = importlib.util.spec_from_file_location('chat_search', helper)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state.archive(event('1', text='下周三开会'))
    state.archive(event('2', text='会议需要带资料'))
    workspace = tmp_path / 'search'
    state.export_chat('group', workspace / 'memory.sqlite3')
    assert module.search_history(workspace, '开会')['total'] == 1
    assert module.search_history(workspace, "' OR 1=1 --")['total'] == 0
    assert len(module.search_history(workspace, '', offset=1, limit=1)['items']) == 1
    assert module.search_history(workspace, around='1')['items']
    (workspace / 'files').mkdir()
    (workspace / 'files/notes.txt').write_text('约定', encoding='utf-8')
    assert module.attachment(workspace, 'notes.txt').read_text(encoding='utf-8') == '约定'
    with pytest.raises(ValueError):
        module.attachment(workspace, '../memory.sqlite3')
    with pytest.raises(ValueError):
        module.attachment(workspace, '../../secret.txt')
