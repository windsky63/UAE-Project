"""Live subprocess progress and native mention handling without model/WeChat access."""
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
import json
import queue
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from wechat_local.codex_runner import CodexRunner
from wechat_local.codex_settings import CodexGenerationTimeoutError, CodexIdleTimeoutError
from wechat_local.codex_settings import CodexConnectionTimeoutError, CodexServiceError
from wechat_local.config import LocalConfig, WechatChat, load_config
from wechat_local.delivery import DeliveryNotSent
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State
from wechat_local.wechat_capture import LocalDatabaseDriver
from wechat_local.wechat_uia import WindowsUIASender


def run_script(runner, script):
    process = subprocess.Popen([sys.executable, '-u', '-c', script], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               encoding='utf-8')
    try:
        return runner._run(process, 'test prompt', 'job-1', {})
    finally:
        assert process.poll() is not None


def test_progress_is_available_before_subprocess_finishes():
    runner = CodexRunner(LocalConfig())
    script = '''import sys,json,time
sys.stdin.read()
print(json.dumps({'type':'item.started','item':{'id':'a','type':'command_execution','command':'secret path'}}),flush=True)
time.sleep(.5)
print(json.dumps({'type':'item.completed','item':{'id':'a','type':'command_execution'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':42}}),flush=True)
'''
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run_script, runner, script)
        assert runner.progress.get(timeout=3) == ('job-1', 'querying')
        assert not future.done()
        assert future.result(timeout=5) == {'input_tokens': 42}
    assert runner.progress.empty()


@pytest.mark.parametrize('active_tool,expected', [
    (False, CodexIdleTimeoutError), (True, CodexGenerationTimeoutError),
])
def test_idle_and_total_timeouts_stop_the_subprocess(active_tool, expected):
    config = LocalConfig().model_copy(update={'codex_timeout_seconds': .8, 'codex_idle_timeout_seconds': .2})
    runner = CodexRunner(config)
    script = 'import sys,json,time\nsys.stdin.read()\n'
    if active_tool:
        script += "print(json.dumps({'type':'item.started','item':{'id':'a','type':'command_execution'}}),flush=True)\n"
    script += 'time.sleep(30)'
    with pytest.raises(expected):
        run_script(runner, script)


def test_tool_execution_can_exceed_idle_limit_and_stderr_is_drained():
    config = LocalConfig().model_copy(update={'codex_timeout_seconds': 5, 'codex_idle_timeout_seconds': .3})
    runner = CodexRunner(config)
    script = '''import sys,json,time
sys.stdin.read()
print(json.dumps({'type':'item.started','item':{'id':'a','type':'mcp_tool_call'}}),flush=True)
sys.stderr.write('x'*200000)
sys.stderr.flush()
time.sleep(.5)
print(json.dumps({'type':'item.completed','item':{'id':'a','type':'mcp_tool_call'}}),flush=True)
print(json.dumps({'type':'turn.completed','usage':{'output_tokens':7}}),flush=True)
'''
    assert run_script(runner, script) == {'output_tokens': 7}


def test_cli_network_retries_stop_without_capping_normal_reply_time():
    config = LocalConfig().model_copy(update={'codex_connect_timeout_seconds': .15})
    runner = CodexRunner(config)
    script = '''import sys,json,time
sys.stdin.read()
for _ in range(600):
 print(json.dumps({'type':'error','message':'error sending request: failed to connect PRIVATE_SECRET'}),flush=True)
 time.sleep(.05)
'''
    with pytest.raises(CodexConnectionTimeoutError):
        run_script(runner, script)


@pytest.mark.parametrize('transient', [False, True])
def test_cli_silence_or_slow_recovery_is_not_a_connection_timeout(transient):
    runner = CodexRunner(LocalConfig().model_copy(update={'codex_connect_timeout_seconds': .1}))
    script = 'import sys,json,time\nsys.stdin.read()\n'
    if transient:
        script += "print(json.dumps({'type':'error','message':'failed to connect'}),flush=True)\n"
    script += "time.sleep(.4)\nprint(json.dumps({'type':'turn.completed','usage':{'output_tokens':3}}),flush=True)\n"
    assert run_script(runner, script) == {'output_tokens': 3}


def test_cli_stderr_account_error_is_sanitized_and_stops_process():
    runner = CodexRunner(LocalConfig())
    script = "import sys,time\nsys.stdin.read()\nsys.stderr.write('not logged in PRIVATE_SECRET\\n')\nsys.stderr.flush()\ntime.sleep(30)"
    with pytest.raises(CodexServiceError) as error:
        run_script(runner, script)
    assert error.value.reason == 'account' and 'PRIVATE_SECRET' not in str(error.value)


def test_cli_account_error_is_preserved_when_the_process_exits_immediately():
    runner = CodexRunner(LocalConfig())
    script = "import sys\nsys.stdin.read()\nsys.stderr.write('not logged in PRIVATE_SECRET\\n')\nsys.stderr.flush()\nsys.exit(1)"
    with pytest.raises(CodexServiceError) as error:
        run_script(runner, script)
    assert error.value.reason == 'account' and 'PRIVATE_SECRET' not in str(error.value)


def incoming(identifier='1', sender='alice', chat_id='group'):
    return dict(id=identifier, sender=sender, sender_id=sender, chat_id=chat_id,
                direction='incoming', content_type='text', text='@bot query', historical=False,
                observed_at=datetime.now(timezone.utc).isoformat())


@pytest.fixture
def listener_rig(tmp_path):
    class Driver:
        account_id, account_name = 'robot', 'bot'
        events, sent = [], []
        def capture(self, chat_id, spool):
            spool.capture_database(chat_id, [e for e in self.events if e['chat_id'] == chat_id], {})
        def send(self, chat_id, text, mention_user=None):
            self.sent.append((chat_id, text, mention_user, threading.get_ident()))
    release = threading.Event()
    started = threading.Event()
    class Runner:
        progress = queue.Queue()
        def generate(self, event, *args):
            self.progress.put((event['id'], 'querying'))
            self.progress.put((event['id'], 'querying'))
            started.set()
            release.wait(5)
            return 'answer', {}
    config = LocalConfig(chats=[WechatChat(id='group', name='Group', kind='group', reply=True),
                               WechatChat(id='private', name='Friend', kind='private', reply=True)],
                         codex_enabled=True, allow_send=True, workspaces=str(tmp_path / 'chats'))
    driver, runner = Driver(), Runner()
    state, spool = State(str(tmp_path / 'state.db')), DesktopSpool(str(tmp_path / 'spool.db'))
    listener = Listener(config, driver, spool, state, runner)
    try:
        yield config, driver, runner, listener, release, started
    finally:
        release.set()
        listener.close()
        state.close()
        spool.db.close()


def test_query_notice_once_and_reply_mentions_original_sender_on_main_thread(listener_rig):
    config, driver, runner, listener, release, started = listener_rig
    driver.events = [incoming()]
    listener.tick()
    assert started.wait(3)
    # A later message must not change the recipient of the first task.
    driver.events.append(incoming('2', sender='bob'))
    listener.tick()
    listener.report_progress()
    assert sum(text == '任务编号：1\n正在查询……' for _, text, _, _ in driver.sent) == 1
    release.set()
    listener.active[1].result(timeout=3)
    listener.tick()
    answers = [entry for entry in driver.sent if entry[1].endswith('回答如下：\nanswer')]
    assert answers[0][2] == 'alice'
    assert all(entry[3] == threading.get_ident() for entry in driver.sent)


@pytest.mark.parametrize('setting', ['query_progress_enabled', 'allow_send'])
def test_query_notice_respects_disabled_settings(listener_rig, setting):
    config, driver, runner, listener, release, started = listener_rig
    setattr(config, setting, False)
    driver.events = [incoming()]
    listener.tick()
    assert started.wait(3)
    listener.report_progress()
    assert not any(text.endswith('正在查询……') for _, text, _, _ in driver.sent)


@pytest.mark.parametrize('private,mention_enabled', [(False, False), (True, True)])
def test_private_or_disabled_mentions_send_plain_reply(listener_rig, private, mention_enabled):
    config, driver, runner, listener, release, started = listener_rig
    config.mention_reply_sender = mention_enabled
    driver.events = [incoming(chat_id='private' if private else 'group')]
    listener.tick()
    assert started.wait(3)
    release.set()
    listener.active[1].result(timeout=3)
    listener.tick()
    assert [entry[2] for entry in driver.sent if entry[1].endswith('回答如下：\nanswer')] == [None]
    assert not any(text.endswith('正在查询……') for _, text, _, _ in driver.sent)


def test_mention_identity_is_part_of_durable_send_deduplication(tmp_path):
    spool = DesktopSpool(str(tmp_path / 'spool.db'))
    sent = []
    driver = SimpleNamespace(send=lambda chat, text, **kwargs: sent.append(kwargs))
    command = dict(id='reply:1', chat_id='group', text='answer', mention_user='alice')
    try:
        assert spool.execute(command, driver)['status'] == 'confirmed'
        assert spool.execute(command, driver)['status'] == 'confirmed'
        assert spool.execute({**command, 'mention_user': 'bob'}, driver)['status'] == 'uncertain'
        assert sent == [{'mention_user': 'alice'}]
    finally:
        spool.db.close()


def test_member_lookup_rejects_unknown_and_duplicate_names():
    driver = object.__new__(LocalDatabaseDriver)
    driver.config = LocalConfig(chats=[WechatChat(id='group', name='Group', kind='group')])
    driver.targets = {'group': 'native-group'}
    members = [dict(username='alice', nick_name='Alice', remark='')]
    driver.reader = SimpleNamespace(members=lambda target: members)
    assert driver.mention_name('group', 'alice') == 'Alice'
    with pytest.raises(DeliveryNotSent):
        driver.mention_name('group', 'unknown')
    members.append(dict(username='bob', nick_name='Alice', remark=''))
    with pytest.raises(DeliveryNotSent):
        driver.mention_name('group', 'alice')
    driver.config.chats[0].sender_aliases = {'alice': 'Alice (group name)'}
    assert driver.mention_name('group', 'alice') == 'Alice (group name)'


def test_inline_mention_confirmation_accepts_wechat_whole_second_timestamp():
    driver = object.__new__(LocalDatabaseDriver)
    driver.verify_identity = lambda: None
    driver.mention_name = lambda *_: 'Alice'
    driver.targets = {'group': 'native-group'}
    driver.account_id = 'bot-id'
    text = '任务编号：7\n回复对象：@wxid-alice\n回答如下：\nanswer'
    driver.reader = SimpleNamespace(recent=lambda _: [dict(sender_username='bot-id',
        content='任务编号：7\n回复对象：@Alice\u2005\n回答如下：\nanswer', create_time=1000)])
    assert driver.delivery_confirmed('group', text, 1000.8, mention_user='alice')
    assert not driver.delivery_confirmed('group', text, 1002.8, mention_user='alice')


def test_inline_native_mention_confirmation_ignores_qt_leading_separator():
    expected = '任务编号：1\n回复对象：@智能机器人\u2005\n回答如下：\n完成'
    actual = '任务编号：1\n回复对象：\u2005@智能机器人\u2005\n回答如下：\n完成'
    assert LocalDatabaseDriver.message_matches(actual, expected)
    assert not LocalDatabaseDriver.message_matches(actual.replace('完成', '不同答案'), expected)
    assert not LocalDatabaseDriver.message_matches(actual.replace('智能机器人', '其他人'), expected)


def test_native_mention_selects_new_popup_item_and_escapes_keys(monkeypatch):
    import wechat_local.wechat_uia as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    sender = object.__new__(WindowsUIASender)
    keys = []
    edit = SimpleNamespace(Name='Group', value='', SendKeys=lambda key, **kwargs: keys.append(key))
    member = 'Alice{Enter}'
    old = SimpleNamespace(Name=member, Click=lambda: pytest.fail('Existing conversation item must not be selected'))
    candidate = SimpleNamespace(Name=member, Click=lambda: setattr(edit, 'value', '@' + member + '\u2005'))
    sender.mention_items = lambda root: {(1,): old, **({(2,): candidate} if keys else {})}
    sender.input = lambda root: edit
    sender.auto = SimpleNamespace(GetFocusedControl=lambda: SimpleNamespace(ProcessId=42),
                                  SendKeys=lambda key, **kwargs: keys.append(key))
    monkeypatch.setattr(module, 'read_value', lambda control: control.value)
    assert sender.select_mention(SimpleNamespace(ProcessId=42), edit, member) == '@' + member + '\u2005'
    assert keys == ['@', 'Alice{{}Enter{}}']


def test_native_mention_absent_does_not_pretend_text_at_is_native(monkeypatch):
    import wechat_local.wechat_uia as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    sender = object.__new__(WindowsUIASender)
    edit = SimpleNamespace(Name='Group', SendKeys=lambda *args, **kwargs: None)
    sender.mention_items = lambda root: {}
    sender.input = lambda root: edit
    sender.auto = SimpleNamespace(GetFocusedControl=lambda: SimpleNamespace(ProcessId=42),
                                  SendKeys=lambda *args, **kwargs: None)
    with pytest.raises(DeliveryNotSent, match='未找到微信原生'):
        sender.select_mention(SimpleNamespace(ProcessId=42), edit, 'Alice')


def test_staged_native_mention_reply_retries_without_duplicate_paste(monkeypatch):
    import wechat_local.wechat_uia as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    clipboard = ['original clipboard']
    monkeypatch.setitem(sys.modules, 'pyperclip', SimpleNamespace(
        paste=lambda: clipboard[0], copy=lambda value: clipboard.__setitem__(0, value)))
    keys, selections, sent = [], [], []
    class Edit:
        Name, value = 'Group', ''
        def Click(self):
            pass
        def SendKeys(self, key, **kwargs):
            keys.append(key)
            if key == '{Ctrl}v':
                self.value += clipboard[0]
            if key == '{Enter}':
                sent.append(self.value)
                self.value = ''
    editor = Edit()
    sender = object.__new__(WindowsUIASender)
    root = object()
    sender.open_chat = lambda name: (root, editor)
    def select(root, edit, name):
        selections.append(name)
        edit.value = '@' + name + '\u2005'
        return edit.value
    sender.select_mention = select
    sender.input = lambda root: SimpleNamespace(Name='Different chat')
    monkeypatch.setattr(module, 'read_value', lambda control: control.value)
    with pytest.raises(DeliveryNotSent):
        sender.send('Group', 'answer', mention='Alice')
    assert not sent
    assert editor.value == '@Alice\u2005answer'
    sender.input = lambda root: editor
    sender.send('Group', 'answer', mention='Alice')
    assert selections == ['Alice']
    assert keys.count('{Ctrl}v') == 1
    assert sent == ['@Alice\u2005answer']
    assert clipboard[0] == 'original clipboard'


def test_new_defaults_and_explicit_settings_are_preserved(tmp_path):
    path = tmp_path / 'config.json'
    path.write_text('{}', encoding='utf-8')
    config = load_config(path)
    assert (config.codex_timeout_seconds, config.reply_max_age_seconds) == (0, 900)
    assert config.mention_reply_sender and config.query_progress_enabled
    path.write_text(json.dumps(dict(codex_timeout_seconds=777, reply_max_age_seconds=1000,
                                   mention_reply_sender=False)), encoding='utf-8')
    config = load_config(path)
    assert config.codex_timeout_seconds == 777
    assert not config.mention_reply_sender
