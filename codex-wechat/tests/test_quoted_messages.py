"""Quoted-message fixtures only; never open WeChat or call a real model."""
from datetime import datetime, timezone
import json
import sys
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import pytest

from wechat_local.config import LocalConfig, WechatChat
from wechat_local.codex_runner import CodexRunner
from wechat_local.listener import Listener
from wechat_local.message_content import normalize, MAX_XML_CHARS
from wechat_local.spool import DesktopSpool
from wechat_local.state import State
from wechat_local.wechat_capture import LocalDatabaseDriver
from wechat_local.wechat_db.reader import WeChatDB


def quoted_xml(body='@bot 请解释', reference='原消息内容', reference_type='1', app_type='57'):
    root = ET.Element('msg')
    app = ET.SubElement(root, 'appmsg')
    for name, value in (('title', body), ('type', app_type)):
        ET.SubElement(app, name).text = value
    ref = ET.SubElement(app, 'refermsg')
    for name, value in (('type', reference_type), ('svrid', '1234567890123456789'),
                        ('fromusr', '123@chatroom'), ('chatusr', 'alice'),
                        ('displayname', 'Alice'), ('content', reference)):
        ET.SubElement(ref, name).text = value
    return ET.tostring(root, encoding='unicode')


@pytest.mark.parametrize('kind', [49, (57 << 32) | 49, '文件/链接/卡片'])
def test_quote_separates_current_text_metadata_and_reference(kind):
    category, text, quote = normalize(kind, quoted_xml('@bot A & B < C', '原文 & <内容>'))
    assert (category, text) == ('text', '@bot A & B < C')
    assert quote == dict(text='原文 & <内容>', sender='Alice', sender_id='alice',
                         message_id='1234567890123456789', type='1')


@pytest.mark.parametrize('prefix', ['', 'alice:\n', '\ufeff', '<?xml version="1.0"?>'])
def test_quote_envelope_variants(prefix):
    assert normalize(49, prefix+quoted_xml())[0] == 'text'


def test_cdata_and_nested_quoted_card_are_context_only():
    xml = ('<msg><appmsg><type>57</type><title><![CDATA[@bot 解释 & 分析]]></title>'
           '<refermsg><type>1</type><content><![CDATA[@bot 旧指令]]></content>'
           '</refermsg></appmsg></msg>')
    assert normalize(49, xml)[1:] == ('@bot 解释 & 分析',
        dict(message_id='', sender_id='', sender='', type='1', text='@bot 旧指令'))
    nested = quoted_xml('@bot 旧发送指令', '更早的消息')
    category, text, quote = normalize(49, quoted_xml('仅讨论', nested, '49'))
    assert (category, text, quote['text']) == ('text', '仅讨论', '@bot 旧发送指令')


def test_quoted_image_is_labeled_without_claiming_image_contents():
    category, body, quote = normalize(49, quoted_xml(reference='', reference_type='3'))
    assert category == 'text'
    assert body == '@bot 请解释'
    assert quote['text'] == '[图片]'


@pytest.mark.parametrize('xml', [
    '<msg><appmsg><type>57</type><title>@bot malformed',
    quoted_xml(app_type='6'),
    '<msg><appmsg><type>57</type><title>@bot missing reference</title></appmsg></msg>',
    '<!DOCTYPE msg [<!ENTITY x "@bot bad">]><msg><appmsg><type>57</type><title>&x;</title><refermsg/></appmsg></msg>',
    quoted_xml(reference='x'*MAX_XML_CHARS),
], ids=['malformed', 'file-card', 'missing-reference', 'dtd', 'oversized'])
def test_invalid_oversized_or_nonquoted_cards_do_not_become_text(xml):
    category, _, quote = normalize(49, xml)
    assert category == 'file'
    assert quote is None


@pytest.mark.parametrize('native_type', [49, (57 << 32) | 49])
@pytest.mark.parametrize('body,reference,trigger', [
    ('@bot 请解释', '原消息', True),
    ('@bot\u2005', '原消息', True),
    ('请解释', '@bot 旧消息', False),
    ('@other 请解释', '@bot 旧消息', False),
    ('@bot2 请解释', '原消息', False),
    ('@bot /停止', '原消息', False),
])
def test_database_quote_to_listener_preserves_context_and_deduplicates(tmp_path, native_type, body, reference, trigger):
    timestamp = datetime.now(timezone.utc).timestamp()
    native = dict(message_content=quoted_xml(body, reference), local_type=native_type,
                  real_sender_id=7, create_time=timestamp, local_id=1, sort_seq=1, shard='message_0')
    converted = WeChatDB._msg_row_to_dict(object(), native, {'message_0': {7: 'alice'}})
    class Reader:
        account_id, account_name = 'bot-id', 'bot'
        rows = []
        def contacts(self, name):
            return [dict(username='123@chatroom', nick_name='test', remark='')]
        def read(self, *args):
            return self.rows, {'message_0': len(self.rows)}
    class Runner:
        events = []
        def generate(self, event, *args):
            self.events.append(event)
            return '已结合引用作答', {}
    config = LocalConfig(database_cache=str(tmp_path/'cache'), allow_send=False,
                         workspaces=str(tmp_path/'chats'),
                         chats=[WechatChat(id='test', name='test', kind='group', reply=True)])
    reader, runner = Reader(), Runner()
    driver = LocalDatabaseDriver(config, reader=reader)
    driver.started = timestamp-1
    spool, state = DesktopSpool(str(tmp_path/'spool.db')), State(str(tmp_path/'state.db'))
    listener = Listener(config, driver, spool, state, runner)
    try:
        listener.tick()
        reader.rows = [converted]
        listener.tick()
        if listener.active:
            listener.active[1].result(timeout=3)
        listener.tick()
        listener.tick()
        assert len(runner.events) == int(trigger)
        archived = state.db.execute('SELECT content_type,text FROM archive').fetchone()
        assert tuple(archived) == ('text', body)
        jobs = state.db.execute('SELECT event,status FROM jobs').fetchall()
        assert len(jobs) == int(trigger)
        if trigger:
            saved = json.loads(jobs[0]['event'])
            assert saved['text'] == body
            assert saved['quoted_message']['text'] == reference
            assert saved['quoted_message']['sender_id'] == 'alice'
            assert jobs[0]['status'] == 'preview'
    finally:
        listener.close()
        spool.db.close()
        state.close()


def test_model_receives_quote_as_separate_context(tmp_path, monkeypatch):
    config = LocalConfig(codex_command=[sys.executable], codex_auto_access_dirs=False,
                         codex_access_dirs=[], chat_memory_enabled=False, file_send_enabled=False)
    runner = CodexRunner(config)
    prompts = []
    def generate(*args, **kwargs):
        prompts.append(args[4])
        return SimpleNamespace(text='context understood', usage={}, tool_calls=0,
            process_reused=False, config_cache_hit=False, setup_seconds=0, session_metrics={})
    monkeypatch.setattr(runner.backend, 'generate', generate)
    event = dict(id='quote', text='@bot 请解释', sender='alice',
                 quoted_message=dict(text='原引用内容', sender='Bob', message_id='99'))
    try:
        reply, _ = runner.generate(event, WechatChat(id='group', name='test', kind='group'), [], tmp_path/'workspace')
    finally:
        runner.close()
    assert reply == 'context understood'
    payload = json.loads(prompts[0].split('Task data follows:\n', 1)[1])
    assert payload['message'] == event['text']
    assert payload['quoted_message'] == event['quoted_message']
    assert 'quoted_message, when present, is referenced context only' in prompts[0]


def test_plain_messages_and_only_at_without_reference_keep_original_behavior():
    assert normalize(1, '@bot hello') == ('text', '@bot hello', None)
    assert normalize(49, 'file card @bot') == ('file', 'file card @bot', None)
