from concurrent.futures import Future
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys

import pytest

from wechat_local.codex_runner import CodexRunner, GenerationResult
from wechat_local.config import LocalConfig, WechatChat
from wechat_local.listener import Listener
from wechat_local.memory import load_related, load_handoff, parse_answer, select_related, tokens
from wechat_local.spool import DesktopSpool
from wechat_local.state import State


def event(identifier='1', chat_id='group', text='@bot 焊口数量如何查询？', sender='alice'):
    return dict(id=identifier, chat_id=chat_id, text=text, sender=sender, sender_id=sender,
                observed_at=datetime.now(timezone.utc).isoformat(),
                direction='incoming', content_type='text', historical=False)


def complete(state, identifier='1', chat_id='group', status='confirmed', **kwargs):
    message = event(identifier, chat_id, **kwargs)
    state.enqueue(message)
    state.set_job(identifier, status, reply='焊口数据请查解析缓存；旧统计为 3 个。',
                  memory_note='焊口统计口径：查解析缓存，来源 demo.pcf；数量需按最新版本重查。')
    return message


def test_confirmed_memory_survives_restart_and_is_chat_isolated(tmp_path):
    path = str(tmp_path / 'state.db')
    state = State(path)
    complete(state)
    complete(state, 'other', 'other-group', text='其他群的机密信息')
    for status in ('preview', 'generated', 'not_sent', 'uncertain', 'failed', 'expired'):
        complete(state, status, status=status, text='不可作为已告知答案')
    workspace = tmp_path / 'group-workspace'
    state.export_memory('group', workspace)
    state.export_chat('group', workspace / 'memory.sqlite3')
    state.close()
    state = State(path)
    try:
        state.export_memory('group', workspace)
        assert len(state.memory_rows('group')) == 1
        related = load_related(workspace, '@bot 焊口统计', 'alice')
        assert [item['job_id'] for item in related] == ['1']
        assert related[0]['sender'] == 'alice'
        assert [row['job_id'] for row in load_handoff(workspace)] == ['1']
        assert 'demo.pcf' in related[0]['note']
        md = (workspace / 'memory.md').read_text(encoding='utf-8')
        assert 'alice' in md and 'demo.pcf' in md
        assert '其他群' not in md and '不可作为已告知' not in md
    finally:
        state.close()


def test_legacy_job_database_is_migrated_and_confirmed_answers_backfilled(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as db:
        db.execute('''CREATE TABLE jobs(id TEXT PRIMARY KEY,chat_id TEXT,event TEXT,status TEXT,
            reply TEXT,usage TEXT,error TEXT,created_at TEXT,updated_at TEXT)''')
        message = event()
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)', (
            '1', 'group', json.dumps(message), 'confirmed', '旧版已经发出的回复', None, None,
            message['observed_at'], message['observed_at']))
    state = State(str(path))
    try:
        state.export_memory('group', tmp_path / 'workspace')
        assert state.memory_rows('group')[0]['note'] == '原回复摘录：旧版已经发出的回复'
        state.export_memory('group', tmp_path / 'workspace')
        assert len(state.memory_rows('group')) == 1
        state.enqueue(event('new'))
    finally:
        state.close()


def test_related_memory_is_relevant_and_strictly_bounded():
    rows = [dict(job_id=str(i), sender='alice', answered_at='2026-10-28',
                 question='@机器人 查询焊口数量' if i < 8 else '@机器人 查看其他事项',
                 note=('焊口来源 demo.pcf，历史数量需重查。' if i < 8 else '无关内容') * 100)
            for i in range(20)]
    selected = select_related(rows, '@机器人 帮我核对 demo.pcf 焊口数据的统计口径', 'bob')
    assert 1 <= len(selected) <= 3
    assert all(int(row['job_id']) < 8 for row in selected)
    assert len(json.dumps(selected, ensure_ascii=False)) <= 2400
    for budget in range(400, 2500, 37):
        result = select_related(rows, 'demo.pcf 焊口统计口径', 'bob', max_chars=budget)
        assert len(json.dumps(result, ensure_ascii=False)) <= budget
    assert '机器人' not in tokens('@机器人 焊口')
    assert select_related(rows, '@机器人 查询安全帽发放登记的具体流程', 'bob') == []


def test_md_is_bounded_and_full_answers_stay_in_database(tmp_path):
    state = State(str(tmp_path / 'state.db'))
    try:
        for i in range(20):
            complete(state, str(i), text='问题' * 2000)
            state.set_job(str(i), 'confirmed', reply='完整答案' * 1000, memory_note='摘要' * 1000)
        state.export_memory('group', tmp_path / 'workspace')
        md = (tmp_path / 'workspace/memory.md').read_text(encoding='utf-8')
        assert md.count('\n## ') == 8
        assert len(md) < 6500
        assert '完整答案' not in md
        assert len(state.memory_rows('group')) == 20
        state.set_job('0', 'confirmed', reply='unchanged')
        state.export_memory('group', tmp_path / 'workspace')
        assert len(state.memory_rows('group')) == 20
    finally:
        state.close()


def test_recent_messages_exclude_progress_but_keep_real_dialogue(tmp_path):
    state = State(str(tmp_path / 'state.db'))
    try:
        for i, text in enumerate(('正在思考……', '正在查询……', '真实答复')):
            state.archive({**event(str(i), text=text), 'direction': 'outgoing'})
        state.archive(event('incoming', text='正在思考……'))
        recent = state.recent('group', 6, 'none')
        assert [row['text'] for row in recent] == ['真实答复', '正在思考……']
    finally:
        state.close()


@pytest.mark.parametrize('raw', ['{"reply":"半截答案", "memory_note":',
                                 '{"reply":1,"memory_note":"bad"}',
                                 '{"reply":"answer"}'])
def test_broken_memory_envelope_never_becomes_a_wechat_message(raw):
    with pytest.raises(ValueError):
        parse_answer(raw)


def test_single_generation_returns_only_reply_and_keeps_memory_separate(tmp_path):
    state = State(str(tmp_path / 'state.db'))
    workspace = tmp_path / 'workspace'
    try:
        complete(state)
        state.export_chat('group', workspace / 'memory.sqlite3')
    finally:
        state.close()
    calls = tmp_path / 'calls.txt'
    fake = tmp_path / 'fake_codex.py'
    fake.write_text('''import sys,json,pathlib
args=sys.argv
prompt=sys.stdin.read()
payload=json.loads(prompt.split('Task data follows:\\n')[1])
assert payload['related_chat_memory'][0]['sender']=='alice'
assert 'current_project_summary' in payload
assert 'downloaded_records' in payload['current_project_summary']
assert '--output-schema' in args
schema=json.loads(pathlib.Path(args[args.index('--output-schema')+1]).read_text(encoding='utf-8'))
assert set(schema['required'])=={'reply','memory_note','file_action'}
with pathlib.Path(sys.argv[1]).open('a') as stream:stream.write('one call\\n')
pathlib.Path(args[args.index('--output-last-message')+1]).write_text(json.dumps(
    {'reply':'仅发送这条答复','memory_note':'内部摘要，不可发到微信'}),encoding='utf-8')
print(json.dumps({'type':'turn.completed','usage':{'input_tokens':123}}))
''', encoding='utf-8')
    config = LocalConfig(codex_command=[sys.executable, str(fake), str(calls)], codex_auto_access_dirs=False, codex_backend='exec')
    result = CodexRunner(config).generate(event('new'), WechatChat(id='group', name='Group', kind='group'), [], workspace)
    reply, usage = result
    assert reply == '仅发送这条答复'
    assert result.memory_note == '内部摘要，不可发到微信'
    assert usage == {'input_tokens': 123}
    assert calls.read_text().splitlines() == ['one call']
    metrics = json.loads((workspace / 'generation-metrics.json').read_text(encoding='utf-8'))
    assert metrics['related_memory_count'] == 1
    assert metrics['tool_calls'] == 0
    assert metrics['input_tokens'] == 123


def test_listener_commits_md_only_after_delivery_confirmation(tmp_path):
    from types import SimpleNamespace
    config = LocalConfig(chats=[WechatChat(id='group', name='Group', kind='group', reply=True)],
                         codex_enabled=True, allow_send=True, mention_reply_sender=False,
                         workspaces=str(tmp_path / 'chats'))
    driver = SimpleNamespace(account_id='bot-id', account_name='bot', send=lambda *args: None)
    state, spool = State(str(tmp_path / 'state.db')), DesktopSpool(str(tmp_path / 'spool.db'))
    listener = Listener(config, driver, spool, state, None)
    try:
        state.enqueue(event())
        future = Future()
        future.set_result(GenerationResult('可发送答案', {}, '保留这条短摘要'))
        listener.active = ('1', future)
        listener.finish_active()
        workspace = listener.workspace_for('group')
        assert not (workspace / 'memory.md').exists()
        listener.deliver()
        assert state.db.execute('SELECT status FROM jobs').fetchone()[0] == 'confirmed'
        assert '保留这条短摘要' in (workspace / 'memory.md').read_text(encoding='utf-8')
        assert len(state.memory_rows('group')) == 1
        listener.deliver()
        assert len(state.memory_rows('group')) == 1
    finally:
        listener.close()
        state.close()
        spool.db.close()


def test_memory_can_be_disabled_without_deleting_existing_records(tmp_path):
    state = State(str(tmp_path / 'state.db'))
    workspace = tmp_path / 'workspace'
    try:
        complete(state)
        state.export_chat('group', workspace / 'memory.sqlite3')
        assert load_related(workspace, '焊口统计', 'alice')
        state.export_chat('group', workspace / 'memory.sqlite3', include_memory=False)
        assert load_related(workspace, '焊口统计', 'alice') == []
        assert len(state.memory_rows('group')) == 1
        state.set_job('1', 'confirmed', memory_note='')
        complete(state, '2')
        state.set_job('2', 'confirmed', memory_note='')
        state.remember('group')
        assert [r['job_id'] for r in state.memory_rows('group')] == ['1']
    finally:
        state.close()
