from datetime import datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_local.config import LocalConfig, WechatChat
from wechat_local import diagnostics
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State
from wechat_local.wechat_capture import LocalDatabaseDriver
from wechat_local.wechat_db.reader import WeChatDB


def test_diagnostic_formatter_has_beijing_time_and_redacts_full_credentials():
    import logging
    from wechat_local.operational_logging import DiagnosticFormatter
    record=logging.LogRecord('wechat_local.test',logging.ERROR,__file__,1,
        'password=private-password authorization=Bearer private-token "api_key": "private-key"',(),None)
    record.created=0
    text=DiagnosticFormatter('[%(asctime)s] [%(levelname)s] %(name)s: %(message)s').format(record)
    assert '1970-01-01T08:00:00.000+08:00' in text
    assert 'private-password' not in text and 'private-token' not in text and 'private-key' not in text
    assert '[ERROR]' in text and '[REDACTED]' in text


@pytest.mark.parametrize('persisted', [False, True])
def test_first_capture_after_restart_preserves_fresh_messages_only_with_saved_baseline(tmp_path, persisted):
    class Reader:
        account_id, account_name = 'bot-id', 'bot'
        rows = []
        def contacts(self, name):
            return [dict(username='123@chatroom', nick_name='test', remark='')]
        def read(self, *args):
            return self.rows, {'message_0': len(self.rows)}
    config = LocalConfig(database_cache=str(tmp_path/'cache'),
                         chats=[WechatChat(id='test', name='test', kind='group', reply=True)])
    reader = Reader()
    spool = DesktopSpool(str(tmp_path/'spool.db'))
    try:
        if persisted:
            LocalDatabaseDriver(config, reader=reader).capture('test', spool)
        restarted = LocalDatabaseDriver(config, reader=reader)
        reader.rows = [dict(local_id=index, shard='message_0', sender_username='alice',
                            type='文本', content='@bot hello', create_time=restarted.started+offset)
                       for index, offset in ((1,-1),(2,1))]
        restarted.capture('test', spool)
        assert [row['historical'] for row in spool.pending()] == [True, not persisted]
    finally:
        spool.db.close()


@pytest.mark.parametrize('native_type,expected,calls', [
    (1, 'text', 1), (3, 'image', 0), (34, 'voice', 0),
    (49, 'file', 0), (10000, 'other', 0),
])
def test_real_reader_conversion_through_capture_and_listener(tmp_path, native_type, expected, calls):
    """Use the shipped reader's output, rather than pre-normalized fake events."""
    row = dict(message_content='@bot hello', local_type=native_type, real_sender_id=7,
               create_time=datetime.now(timezone.utc).timestamp(), local_id=1,
               sort_seq=1, shard='message_0')
    converted = WeChatDB._msg_row_to_dict(object(), row, {'message_0': {7: 'alice'}})

    class Reader:
        account_id, account_name = 'bot-id', 'bot'
        rows = []

        def contacts(self, name):
            return [dict(username='123@chatroom', nick_name='test', remark='')]

        def read(self, *args):
            return self.rows, {'message_0': len(self.rows)}

    class Runner:
        count = 0

        def generate(self, *args):
            self.count += 1
            return 'reply', {}

    config = LocalConfig(database_cache=str(tmp_path / 'cache'), codex_enabled=True,
                         allow_send=False, workspaces=str(tmp_path / 'chats'),
                         chats=[WechatChat(id='test', name='test', kind='group', reply=True)])
    reader, runner = Reader(), Runner()
    driver = LocalDatabaseDriver(config, reader=reader)
    driver.started = row['create_time'] - 1
    spool, state = DesktopSpool(str(tmp_path / 'spool.db')), State(str(tmp_path / 'state.db'))
    listener = Listener(config, driver, spool, state, runner)
    try:
        listener.tick()  # establish the historical baseline
        reader.rows = [converted]
        listener.tick()
        if listener.active:
            listener.active[1].result(timeout=3)
        listener.tick()
        archived = state.db.execute('SELECT content_type FROM archive').fetchone()
        assert archived['content_type'] == expected
        assert runner.count == calls
        statuses = state.db.execute('SELECT status FROM jobs').fetchall()
        assert [r['status'] for r in statuses] == (['preview'] if calls else [])
        # Re-reading the same native row must not generate another reply.
        listener.tick()
        assert runner.count == calls
    finally:
        listener.close()
        spool.db.close()
        state.close()


@pytest.fixture
def preflight(tmp_path, monkeypatch):
    from wechat_local import wechat_capture
    root = tmp_path / 'wechat'
    storage = root / 'account' / 'db_storage'
    storage.mkdir(parents=True)
    (storage / 'contact.db').write_bytes(b'encrypted placeholder')
    profile = tmp_path / 'uia.json'
    profile.write_text(json.dumps({'dll_sha256': 'a' * 64, 'gate_rva': 123}), encoding='utf-8')
    config = LocalConfig(database_root=str(root), database_account='account', codex_enabled=False,
                         database_cache=str(tmp_path / 'new-cache'),
                         state_path=str(tmp_path / 'new-state/listener.sqlite3'),
                         workspaces=str(tmp_path / 'new-chats'), uia_profile=str(profile),
                         chats=[WechatChat(id='test', name='test', kind='group', username='123@chatroom')])
    monkeypatch.setattr(diagnostics, 'dependency_status', lambda: {
        name: {'available': True, 'version': 'test'} for name in diagnostics.DEPENDENCIES})
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda command: 'C:/test/codex.exe')
    monkeypatch.setattr(wechat_capture, 'load_backend', lambda: SimpleNamespace(
        list_accounts=lambda path: [{'account': 'account'}]))

    def forbidden(*args, **kwargs):
        raise AssertionError('Preflight must not instantiate capture drivers or execute subprocesses')

    monkeypatch.setattr(wechat_capture, 'LocalDatabaseReader', forbidden)
    import subprocess
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    return config


def test_preflight_is_read_only_and_does_not_claim_live_validation(preflight):
    report = diagnostics.check_environment(preflight)
    assert report['errors'] == []
    assert report['ready_for_capture_attempt'] is True
    assert report['live_validation_complete'] is False
    assert len(report['unverified']) == 3
    assert report['database']['files_readable'] is True
    for key in ('database_cache', 'state_path', 'workspaces'):
        assert not Path(getattr(preflight, key)).exists()


def test_preflight_reports_all_missing_dependencies(preflight, monkeypatch):
    monkeypatch.setattr(diagnostics, 'dependency_status', lambda: {
        name: {'available': name not in ('psutil', 'zstandard', 'pywin32'), 'version': None}
        for name in diagnostics.DEPENDENCIES})
    report = diagnostics.check_environment(preflight)
    assert not report['ready_for_capture_attempt']
    assert not report['windows_driver_available']
    assert all(name in report['errors'][0] for name in ('psutil', 'zstandard', 'pywin32'))


def test_preflight_rejects_wrong_account_and_bad_output_paths(preflight, tmp_path):
    preflight.database_account = 'wrong-account'
    blocked = tmp_path / 'not-a-directory'
    blocked.write_text('file', encoding='utf-8')
    preflight.workspaces = str(blocked)
    report = diagnostics.check_environment(preflight)
    assert not report['ready_for_capture_attempt']
    assert any('database_account' in message for message in report['errors'])
    assert any('workspaces' in message for message in report['errors'])


def test_preflight_codex_required_only_when_enabled(preflight, monkeypatch):
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda command: None)
    assert diagnostics.check_environment(preflight)['errors'] == []
    preflight.codex_enabled = True
    report = diagnostics.check_environment(preflight)
    assert not report['codex_found']
    assert any('Codex' in message for message in report['errors'])


def test_empty_chat_preflight_does_not_require_capture_dependencies_or_database(preflight, monkeypatch):
    preflight.chats = []
    preflight.codex_enabled = True
    monkeypatch.setattr(diagnostics.shutil, 'which', lambda command: None)
    monkeypatch.setattr(diagnostics, 'dependency_status', lambda: {
        name: {'available':name not in ('psutil','pywin32'), 'version':None} for name in diagnostics.DEPENDENCIES})
    from wechat_local import wechat_capture
    monkeypatch.setattr(wechat_capture, 'load_backend', lambda: (_ for _ in ()).throw(AssertionError('Idle preflight must not inspect databases')))
    report = diagnostics.check_environment(preflight)
    assert report['errors'] == []
    assert report['idle_mode'] and report['ready_for_listener']
    assert not report['ready_for_capture_attempt']
    assert report['database']['skipped']


def test_check_exit_code_and_invalid_config(preflight, monkeypatch, capsys, tmp_path):
    from wechat_local.__main__ import main
    from wechat_local import config
    monkeypatch.setattr(config, 'load_config', lambda path: preflight)
    monkeypatch.setattr('sys.argv', ['wechat_local', '--check'])
    main()
    assert json.loads(capsys.readouterr().out)['ready_for_capture_attempt']
    preflight.chats = []
    main()
    idle = json.loads(capsys.readouterr().out)
    assert idle['errors'] == []
    assert idle['idle_mode']
    assert idle['ready_for_listener']
    assert not idle['ready_for_capture_attempt']

    def invalid(path):
        raise ValueError('invalid config')

    monkeypatch.setattr(config, 'load_config', invalid)
    with pytest.raises(SystemExit) as result:
        main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)['config_valid'] is False
