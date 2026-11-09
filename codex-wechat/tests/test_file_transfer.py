"""Model intent contract and actual file-delivery plumbing, without live sends."""
from concurrent.futures import Future
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import zipfile
from types import SimpleNamespace

import pytest

from wechat_local import file_transfer as transfer
from wechat_local.codex_runner import CodexRunner, GenerationResult
from wechat_local.config import LocalConfig, SyncSource, WechatChat
from wechat_local.delivery import DeliveryNotSent
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State
from wechat_local.sync_files import prepare_catalog, search
from wechat_local.wechat_capture import LocalDatabaseDriver
from wechat_local.wechat_uia import WindowsUIASender


@pytest.mark.parametrize('draft', ['\ufffc', '', 'user draft', '\ufffc\ufffc'])
def test_inline_attachment_submit_requires_one_object_in_expected_chat(monkeypatch, draft):
    from wechat_local import wechat_uia
    calls = []
    edit = SimpleNamespace(Name='Test', SendKeys=lambda *a, **k: calls.append(a))
    sender = object.__new__(WindowsUIASender)
    sender.input = lambda root: edit
    def missing_dialog(*args):
        raise RuntimeError('No separate confirmation dialog')
    sender.file_confirmation = missing_dialog
    monkeypatch.setattr(wechat_uia, 'read_value', lambda _: draft)
    if draft == '\ufffc':
        sender.submit_pasted_file(None, 'demo.txt', 'Test')
        assert calls == [('{Enter}',)]
    else:
        with pytest.raises(RuntimeError):
            sender.submit_pasted_file(None, 'demo.txt', 'Test')
        assert calls == []


def test_inline_attachment_never_submits_to_changed_chat():
    sender = object.__new__(WindowsUIASender)
    sender.input = lambda root: SimpleNamespace(Name='Other')
    sender.file_confirmation = lambda *args: pytest.fail('must reject changed recipient first')
    with pytest.raises(RuntimeError, match='目标会话已变化'):
        sender.submit_pasted_file(None, 'demo.txt', 'Test')


@pytest.fixture
def files(tmp_path):
    root = tmp_path / 'project'
    (root / 'downloads').mkdir(parents=True)
    (root / 'runtime').mkdir()
    path = root / 'downloads/demo.pcf'
    path.write_text('original model data')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(root / 'runtime/ledger.sqlite3') as db:
        db.execute('CREATE TABLE files(project,id,filename,path,bytes,sha256,status,active)')
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)', ('p','one',path.name,'downloads/demo.pcf',path.stat().st_size,sha,'done',1))
    chat = WechatChat(id='chat', name='Test', kind='private', reply=True, sync_sources=['project'])
    config = LocalConfig(chats=[chat], codex_auto_access_dirs=False, codex_access_dirs=[str(root)],
        sync_sources=[SyncSource(id='project', root=str(root), projects=['p'])], workspaces=str(tmp_path / 'chats'))
    workspace = tmp_path / 'workspace'
    prepare_catalog(config, chat, workspace, access_dirs=[str(root)])
    identity = search(workspace)['items'][0]['id']
    return config, chat, workspace, identity, path


def event(text='劳驾，刚才那份给我一份吧', identifier='send', sender='alice'):
    return dict(id=identifier, chat_id='chat', sender=sender, sender_id=sender, text=text,
        direction='incoming', content_type='text', historical=False, observed_at=datetime.now(timezone.utc).isoformat())


def action(identity, message, confirmed=True):
    return dict(action='send', file_ids=[identity], send_confirmed=confirmed, authorization_quote=message['text'])


def test_quoted_old_instruction_is_not_current_file_delivery_authorization(files):
    config, chat, workspace, identity, _ = files
    message = event('请解释引用内容')
    message['quoted_message'] = dict(text='把这份文件发给我', sender='someone else')
    decision = dict(action='send', file_ids=[identity], send_confirmed=True,
                    authorization_quote=message['quoted_message']['text'])
    result = transfer.prepare_action(config, chat, message, workspace, decision, 'reply')
    assert not result.file_result or result.file_result.get('kind') != 'send'


def multiple_files(files, count, content=None):
    config, chat, workspace, _, path = files
    with sqlite3.connect(path.parent.parent / 'runtime/ledger.sqlite3') as db:
        db.execute('DELETE FROM files')
        for index in range(count):
            relative = f'downloads/{index}/demo.pcf'
            target = path.parent.parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content if content is not None else f'data {index}'.encode())
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                       ('p', str(index), target.name, relative, target.stat().st_size,
                        hashlib.sha256(target.read_bytes()).hexdigest(), 'done', 1))
    prepare_catalog(config, chat, workspace, access_dirs=config.codex_access_dirs)
    return [entry['id'] for entry in search(workspace, limit=100)['items']]


@pytest.mark.parametrize('count,packed', [(1, True), (2, False), (3, False), (4, True), (9, True)])
def test_multiple_files_and_requested_zip_preserve_same_names(files, count, packed):
    config, chat, workspace, _, _ = files
    identities = multiple_files(files, count)
    message = event('把这些文件发给我')
    decision = dict(action(identities[0], message), file_ids=identities, pack_as_zip=count == 1)
    result = transfer.prepare_action(config, chat, message, workspace, decision, 'ready')
    payload = result.file_result
    assert payload is not None
    transfer.validate(config, chat, message, workspace, payload)
    if packed:
        with zipfile.ZipFile(payload['path']) as archive:
            assert len(archive.namelist()) == count
            assert len(set(archive.namelist())) == count
            assert all(name.endswith('/demo.pcf') for name in archive.namelist())
        Path(payload['sources'][-1]['path']).write_bytes(b'changed')
        # The archive snapshot remains valid; source catalog versions still apply.
        with sqlite3.connect(files[-1].parent.parent / 'runtime/ledger.sqlite3') as db:
            db.execute("UPDATE files SET sha256='changed' WHERE id=?", (str(count - 1),))
        with pytest.raises(ValueError):
            transfer.validate(config, chat, message, workspace, payload)
    else:
        assert len(payload['files']) == count


def test_oversize_source_is_reported_before_staging_any_file(files):
    config, chat, workspace, _, _ = files
    config.file_send_max_mib = 1
    identities = multiple_files(files, 2, b'x' * (1024**2 + 1))
    message = event('把这几份发给我')
    result = transfer.prepare_action(config, chat, message, workspace,
        dict(action(identities[0], message), file_ids=identities), 'ready')
    assert result.file_result is None
    assert '微信上传大小限制' in result.reply and '1 MiB' in result.reply
    assert '所在目录：' in result.reply
    assert str(files[-1].parent / '0' / 'demo.pcf') in result.reply
    assert str(files[-1].parent / '1' / 'demo.pcf') in result.reply
    assert not (workspace / 'outgoing_files').exists()


def test_aggregate_over_limit_is_split_into_independent_zips(files):
    import random
    config, chat, workspace, _, _ = files
    config.file_send_max_mib = 1
    identities = multiple_files(files, 4, random.Random(7).randbytes(400000))
    message = event('全部发给我')
    result = transfer.prepare_action(config, chat, message, workspace,
        dict(action(identities[0], message), file_ids=identities), 'ready')
    payload = result.file_result
    assert len(payload['files']) == 2
    transfer.validate(config, chat, message, workspace, payload)
    members = []
    for entry in payload['files']:
        assert Path(entry['path']).stat().st_size <= 1024**2
        with zipfile.ZipFile(entry['path']) as archive:
            assert archive.testzip() is None
            members.extend(archive.namelist())
            assert all(archive.read(name) == random.Random(7).randbytes(400000) for name in archive.namelist())
    assert len(members) == len(set(members)) == 4


def test_large_compressible_sources_can_fit_in_one_zip(files):
    config, chat, workspace, _, _ = files
    config.file_send_max_mib = 1
    identities = multiple_files(files, 4, b'x' * (1024**2 + 1))
    message = event('全部发给我')
    result = transfer.prepare_action(config, chat, message, workspace,
        dict(action(identities[0], message), file_ids=identities), 'ready')
    assert result.file_result['bytes'] < 1024**2
    transfer.validate(config, chat, message, workspace, result.file_result)


def test_indivisible_oversize_zip_reports_original_download_locations(files):
    import random
    config, chat, workspace, _, _ = files
    config.file_send_max_mib = 1
    identities = multiple_files(files, 4, random.Random(8).randbytes(1024**2 + 1))
    message = event('全部发给我')
    result = transfer.prepare_action(config, chat, message, workspace,
        dict(action(identities[0], message), file_ids=identities), 'ready')
    assert result.file_result is None
    assert '微信上传大小限制' in result.reply
    for index in range(4):
        assert str(files[-1].parent / str(index) / 'demo.pcf') in result.reply


@pytest.mark.parametrize('failure', ['disabled', 'changed', 'missing', 'unknown'])
def test_blocked_send_reports_local_location_or_explicitly_unavailable(files, failure):
    config, chat, workspace, identity, path = files
    if failure == 'disabled':
        config.file_send_enabled = False
    elif failure == 'changed':
        path.write_text('changed bytes')
    elif failure == 'missing':
        path.unlink()
    else:
        identity = 'unknown'
    message = event()
    result = transfer.prepare_action(config, chat, message, workspace, action(identity, message), 'ready')
    assert result.file_result is None
    if failure == 'unknown':
        assert '暂无法确认' in result.reply
    else:
        assert str(path) in result.reply and str(path.parent) in result.reply
    assert 'outgoing_files' not in result.reply


def test_blocked_reply_does_not_disclose_revoked_source(files):
    config, chat, workspace, identity, path = files
    config.codex_access_dirs = []
    reply = transfer.blocked_reply(config, chat, workspace, [identity], '未发送')
    assert str(path) not in reply
    assert '暂无法确认' in reply


def test_split_archives_pause_after_three_and_require_sender_confirmation(files):
    import random
    config, chat, workspace, _, path = files
    config.file_send_max_mib = 1
    content = random.Random(9).randbytes(700000)
    identities = multiple_files(files, 7, content)
    originals = {p: p.read_bytes() for p in path.parent.glob('*/demo.pcf')}
    state = State(str(workspace / 'host-state.sqlite3'))
    spool = DesktopSpool(str(workspace / 'spool.sqlite3'))
    calls, notices = [], []
    driver = SimpleNamespace(account_id='bot', account_name='bot',
        send_file=lambda *args: calls.append(args), send=lambda *args, **kwargs: notices.append(args))
    listener = Listener(config, driver, spool, state, None)
    listener.workspace_for = lambda _: workspace
    try:
        message = event('这些文件全部发给我', identifier='batch1')
        payload = transfer.prepare_action(config, chat, message, workspace,
            dict(action(identities[0], message), file_ids=identities), 'ready').file_result
        assert len(payload['files']) == 3
        assert len(payload['remaining']) == 4
        state.enqueue(message)
        state.set_job(message['id'], 'generated', reply='ready', file_result=payload)
        listener.deliver()
        listener.deliver()
        assert len(calls) == 3 and len(notices) == 1
        assert '继续发送' in notices[0][1]
        assert state.file_selection(chat.id, 'bob') is None
        pending = state.file_selection(chat.id, 'alice')
        assert pending['kind'] == 'pending_send'
        unconfirmed = dict(event('剩下的能发吗', identifier='question'), file_selection=pending)
        decision = dict(action='continue', file_ids=[], send_confirmed=False, authorization_quote=unconfirmed['text'])
        assert transfer.prepare_action(config, chat, unconfirmed, workspace, decision, 'ready').file_result is None
        stranger = event('继续发送', identifier='stranger', sender='bob')
        decision = dict(action='continue', file_ids=[], send_confirmed=True, authorization_quote=stranger['text'])
        assert transfer.prepare_action(config, chat, stranger, workspace, decision, 'ready').file_result is None
        for batch, expected_count in [(2, 6), (3, 7)]:
            message = dict(event('请继续发送', identifier=f'batch{batch}'),
                           file_selection=state.file_selection(chat.id, 'alice'))
            decision = dict(action='continue', file_ids=[], send_confirmed=True, authorization_quote=message['text'])
            payload = transfer.prepare_action(config, chat, message, workspace, decision, 'ready').file_result
            assert payload is not None
            state.enqueue(message)
            state.set_job(message['id'], 'generated', reply='ready', file_result=payload)
            listener.deliver()
            listener.deliver()
            assert len(calls) == expected_count
        assert state.file_selection(chat.id, 'alice') == {}
        assert len(notices) == 2
        delivered = []
        for _, archive_path, _ in calls:
            assert Path(archive_path).stat().st_size <= 1024**2
            with zipfile.ZipFile(archive_path) as archive:
                assert archive.testzip() is None
                delivered.extend(archive.read(name) for name in archive.namelist())
        assert len(delivered) == 7 and all(data == content for data in delivered)
        assert len(originals) == 7
        assert all(p.read_bytes() == data for p, data in originals.items())
    finally:
        listener.close()
        state.close()
        spool.db.close()


def test_natural_language_model_decision_stages_exact_file_without_command_matching(files):
    config, chat, workspace, identity, path = files
    message = event()
    result = transfer.prepare_action(config, chat, message, workspace, action(identity, message), '已准备')
    assert result.file_result['kind'] == 'send'
    assert Path(result.file_result['path']).read_bytes() == path.read_bytes()
    assert Path(result.file_result['path']) != path
    transfer.validate(config, chat, message, workspace, result.file_result)
    assert not hasattr(transfer, 'request')  # No lexical command/keyword intent gate.


@pytest.mark.parametrize('text', ['找一下 demo.pcf', '发送文件的功能可以用吗', '不要发送文件', '找到后先告诉我', '以后再发送'])
def test_unconfirmed_model_intent_never_stages_a_send_even_with_send_keywords(files, text):
    config, chat, workspace, identity, _ = files
    message = event(text)
    result = transfer.prepare_action(config, chat, message, workspace, action(identity, message, False), '请确认')
    assert result.file_result is None
    assert not (workspace / 'outgoing_files').exists()


def test_provenance_and_unambiguous_file_are_required(files):
    config, chat, workspace, identity, _ = files
    message = event()
    for decision in [dict(action(identity, message), authorization_quote='historical approval'),
                     dict(action(identity, message), file_ids=[identity,identity])]:
        result = transfer.prepare_action(config, chat, message, workspace, decision, '确认一下')
        assert result.file_result is None


def test_search_results_are_metadata_only(files):
    config, chat, workspace, identity, _ = files
    result = transfer.prepare_action(config, chat, event('找找文件'), workspace,
        dict(action='search_results', file_ids=[identity]), '找到 demo.pcf')
    assert result.file_result['kind'] == 'search'
    assert not (workspace / 'outgoing_files').exists()


@pytest.mark.parametrize('whitespace', [' ', '\t', '\n', '\u3000', '\u00a0', ' ك', '-_!?', '中文', '１２'])
def test_search_id_special_characters_are_removed_before_catalog_match(files, whitespace):
    config, chat, workspace, identity, _ = files
    malformed = whitespace + identity[:5] + whitespace + identity[5:] + whitespace
    reply = '文件名中 可以保留 空格。'
    result = transfer.prepare_action(config, chat, event('找文件'), workspace,
        dict(action='search_results', file_ids=[malformed]), reply)
    assert result.reply == reply
    assert [item['id'] for item in result.file_result['candidates']] == [identity]
    assert not (workspace / 'outgoing_files').exists()


@pytest.mark.parametrize('bad_position', [0, 1])
@pytest.mark.parametrize('suffix', [' كunknown', '-unknown', '0'])
def test_invalid_search_id_preserves_reply_and_clears_entire_selection(files, bad_position, suffix):
    config, chat, workspace, identity, _ = files
    ids = [identity]
    ids.insert(bad_position, identity + suffix)
    reply = '找到以下文件，请核对。'
    result = transfer.prepare_action(config, chat, event('找找文件'), workspace,
        dict(action='search_results', file_ids=ids), reply)
    assert result.reply == reply
    assert result.file_result['kind'] == 'search'
    assert result.file_result['candidates'] == []
    assert not (workspace / 'outgoing_files').exists()


def test_send_id_special_characters_are_removed_and_staged_file_is_validated(files):
    config, chat, workspace, identity, _ = files
    message = event()
    result = transfer.prepare_action(config, chat, message, workspace,
        action(identity + ' ك', message), 'prepared')
    assert result.file_result['id'] == identity
    transfer.validate(config, chat, message, workspace, result.file_result)


def test_version_change_rejected_before_delivery(files):
    config, chat, workspace, identity, path = files
    message = event()
    result = transfer.prepare_action(config, chat, message, workspace, action(identity, message), '已准备')
    with sqlite3.connect(path.parent.parent / 'runtime/ledger.sqlite3') as db:
        db.execute("UPDATE files SET sha256='new-version'")
    with pytest.raises(ValueError):
        transfer.validate(config, chat, message, workspace, result.file_result)


def test_followup_does_not_silently_send_changed_version_from_prior_search(files):
    config, chat, workspace, identity, path = files
    found = transfer.prepare_action(config, chat, event('find'), workspace,
        dict(action='search_results', file_ids=[identity]), 'found').file_result
    path.write_text('new model version')
    with sqlite3.connect(path.parent.parent / 'runtime/ledger.sqlite3') as db:
        db.execute('UPDATE files SET bytes=?,sha256=?', (path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()))
    prepare_catalog(config, chat, workspace, access_dirs=config.codex_access_dirs)
    message = dict(event(), file_selection=found)
    result = transfer.prepare_action(config, chat, message, workspace, action(identity, message), 'ready')
    assert result.file_result is None
    assert not (workspace / 'outgoing_files').exists()


def test_corruption_switches_and_authorization_are_enforced(files):
    config, chat, workspace, identity, path = files
    message = event()
    result = transfer.prepare_action(config, chat, message, workspace, action(identity, message), 'ready')
    staged = Path(result.file_result['path'])
    staged.write_bytes(b'x' * staged.stat().st_size)
    with pytest.raises(ValueError):
        transfer.verify_staged(staged, result.file_result['sha256'])
    config.file_send_enabled = False
    with pytest.raises(ValueError):
        transfer.validate(config, chat, message, workspace, result.file_result)
    assert path.read_text() == 'original model data'


def test_file_spool_is_durable_and_does_not_retry_uncertain_delivery(tmp_path):
    spool = DesktopSpool(str(tmp_path / 'spool.db'))
    calls = []
    def send(*args):
        calls.append(args)
        raise RuntimeError('submitted, awaiting DB confirmation')
    driver = SimpleNamespace(send_file=send, file_delivery_confirmed=lambda *a: False)
    command = dict(id='file:one', chat_id='chat', kind='file', path='snapshot.pcf', sha256='digest', filename='snapshot.pcf', bytes=10)
    try:
        assert spool.execute(command, driver, max_attempts=3)['status'] == 'uncertain'
        assert spool.execute(command, driver, max_attempts=3)['status'] == 'uncertain'
        assert len(calls) == 1
        driver.file_delivery_confirmed = lambda *a: True
        assert spool.execute(command, driver)['status'] == 'confirmed'
        assert spool.execute(command, driver)['status'] == 'confirmed'
        assert len(calls) == 1
    finally:
        spool.db.close()


def test_file_card_confirmation_requires_native_file_type_name_and_size():
    xml = '<msg><appmsg><type>6</type><title>demo.pcf</title><appattach><totallen>10</totallen></appattach></appmsg></msg>'
    assert LocalDatabaseDriver.file_matches(xml, 'demo.pcf', 10)
    assert not LocalDatabaseDriver.file_matches(xml, 'other.pcf', 10)
    assert not LocalDatabaseDriver.file_matches(xml, 'demo.pcf', 11)
    assert not LocalDatabaseDriver.file_matches('demo.pcf', 'demo.pcf', 10)


def test_file_sender_uses_desktop_protection_and_original_filename(monkeypatch):
    from wechat_local import desktop_guard
    sender = object.__new__(WindowsUIASender)
    sender._protected = True
    calls = []
    monkeypatch.setattr(desktop_guard, 'protected_send', lambda *a, **k: calls.append((a,k)))
    sender.send_file('Test', 'demo.pcf', expected_sha='sha')
    assert calls[0][0][1] == 'send_file'
    assert calls[0][1]['expected_sha'] == 'sha'
    assert WindowsUIASender.dropfiles(Path('demo.pcf')).endswith(('demo.pcf\0\0').encode('utf-16-le'))


@pytest.mark.parametrize('corrupt_id', [False, True])
def test_prompt_and_schema_require_semantic_confirmation_and_current_user_quote(files, monkeypatch, corrupt_id):
    config, chat, workspace, identity, _ = files
    config.codex_command = ['python']
    runner = CodexRunner(config)
    observed = {}
    def backend(*args, **kwargs):
        observed.update(prompt=args[4], schema=args[6])
        return SimpleNamespace(text=json.dumps(dict(reply='只找到了文件，还没有发送。', memory_note='',
            file_action=dict(action='search_results', file_ids=[identity + (' كunknown' if corrupt_id else '')], send_confirmed=False, authorization_quote=''))),
            usage={}, tool_calls=0, process_reused=False, config_cache_hit=False, setup_seconds=0, session_metrics={})
    monkeypatch.setattr(runner.backend, 'generate', backend)
    try:
        result = runner.generate(event('帮我找下文件'), chat, [], workspace)
        assert result.file_result['kind'] == 'search'
        assert result.reply == '只找到了文件，还没有发送。'
        assert bool(result.file_result['candidates']) is not corrupt_id
        assert observed['schema']['properties']['file_action'] == transfer.ACTION_SCHEMA
        assert 'not by matching a keyword or command format' in observed['prompt']
        assert '能发送吗' in observed['prompt'] and '别发' in observed['prompt']
        assert 'authorization_quote' in observed['prompt']
    finally:
        runner.close()


def test_listener_delivers_file_once_and_never_sends_internal_metadata_as_text(files):
    config, chat, workspace, identity, _ = files
    message = event()
    payload = transfer.prepare_action(config, chat, message, workspace, action(identity, message), 'prepared').file_result
    state = State(str(workspace / 'host-state.sqlite3'))
    spool = DesktopSpool(str(workspace / 'spool.sqlite3'))
    calls = []
    driver = SimpleNamespace(account_id='bot', account_name='bot',
        send=lambda *a, **k: calls.append(('text', a)), send_file=lambda *a: calls.append(('file', a)))
    listener = Listener(config, driver, spool, state, None)
    listener.workspace_for = lambda _: workspace
    try:
        state.enqueue(message)
        state.set_job(message['id'], 'generated', reply='prepared', file_result=payload)
        listener.deliver()
        listener.deliver()
        assert [c[0] for c in calls] == ['file']
        assert state.job(message['id'])['status'] == 'confirmed'
        assert state.db.execute("SELECT content_type FROM archive WHERE direction='outgoing'").fetchone()[0] == 'file'
    finally:
        listener.close()
        state.close()
        spool.db.close()


@pytest.mark.parametrize('failure', ['disabled_after_staging', 'ui_rejected'])
def test_delivery_block_sends_local_download_location(files, failure):
    config, chat, workspace, identity, path = files
    config.send_max_attempts = 1
    message = event()
    payload = transfer.prepare_action(config, chat, message, workspace, action(identity, message), 'ready').file_result
    state = State(str(workspace / 'host-state.sqlite3'))
    spool = DesktopSpool(str(workspace / 'spool.sqlite3'))
    replies = []
    def reject(*args):
        raise DeliveryNotSent('upload unavailable')
    driver = SimpleNamespace(account_id='bot', account_name='bot', send_file=reject,
                             send=lambda *args, **kwargs: replies.append(args[1]))
    listener = Listener(config, driver, spool, state, None)
    listener.workspace_for = lambda _: workspace
    try:
        state.enqueue(message)
        state.set_job(message['id'], 'generated', reply='ready', file_result=payload)
        if failure == 'disabled_after_staging':
            config.file_send_enabled = False
        listener.deliver()
        listener.deliver()
        assert len(replies) == 1
        assert str(path) in replies[0] and str(path.parent) in replies[0]
        assert 'outgoing_files' not in replies[0]
        assert state.job(message['id'])['status'] == 'confirmed'
    finally:
        listener.close()
        state.close()
        spool.db.close()


def test_batch_retry_skips_confirmed_files_and_waits_for_uncertain_file(files):
    config, chat, workspace, _, _ = files
    identities = multiple_files(files, 3)
    message = event('这三份都发给我')
    payload = transfer.prepare_action(config, chat, message, workspace,
        dict(action(identities[0], message), file_ids=identities), 'ready').file_result
    state = State(str(workspace / 'host-state.sqlite3'))
    spool = DesktopSpool(str(workspace / 'spool.sqlite3'))
    calls = []
    def send(*args):
        calls.append(args)
        if len(calls) == 2:
            raise RuntimeError('submission outcome unknown')
    driver = SimpleNamespace(account_id='bot', account_name='bot', send_file=send,
                             file_delivery_confirmed=lambda *a: False)
    listener = Listener(config, driver, spool, state, None)
    listener.workspace_for = lambda _: workspace
    try:
        state.enqueue(message)
        state.set_job(message['id'], 'generated', reply='ready', file_result=payload)
        listener.deliver()
        listener.deliver()
        assert len(calls) == 2
        assert state.job(message['id'])['status'] == 'uncertain'
        driver.file_delivery_confirmed = lambda *a: True
        listener.deliver()
        listener.deliver()
        assert len(calls) == 3
        assert len({call[1] for call in calls}) == 3
        assert state.job(message['id'])['status'] == 'confirmed'
    finally:
        listener.close()
        state.close()
        spool.db.close()


@pytest.mark.parametrize('corrupt_id', [False, True])
def test_search_context_is_saved_only_after_confirmed_reply_and_is_sender_scoped(files, corrupt_id):
    config, chat, workspace, identity, _ = files
    message = event('寻找 demo.pcf', identifier='find')
    payload = transfer.prepare_action(config, chat, message, workspace,
        dict(action='search_results', file_ids=[identity]), 'found').file_result
    if corrupt_id:
        payload = transfer.prepare_action(config, chat, message, workspace,
            dict(action='search_results', file_ids=[identity + ' كunknown']), 'found').file_result
    state = State(str(workspace / 'host-state.sqlite3'))
    spool = DesktopSpool(str(workspace / 'spool.sqlite3'))
    def blocked(*a, **k):
        raise DeliveryNotSent('not submitted')
    driver = SimpleNamespace(account_id='bot', account_name='bot', send=blocked)
    listener = Listener(config, driver, spool, state, None)
    listener.workspace_for = lambda _: workspace
    try:
        state.enqueue(message)
        state.set_job(message['id'], 'generated', reply='found', file_result=payload)
        listener.deliver()
        assert state.file_selection(chat.id, 'alice') is None
        driver.send = lambda *a, **k: None
        with spool.db:
            spool.db.execute('UPDATE ui_actions SET attempted_at=0')
        listener.deliver()
        selection = state.file_selection(chat.id, 'alice')['candidates']
        assert selection == [] if corrupt_id else selection[0]['id'] == identity
        assert state.file_selection(chat.id, 'bob') is None
        assert state.file_selection('another-chat', 'alice') is None
    finally:
        listener.close()
        state.close()
        spool.db.close()


def test_preview_mode_does_not_submit_file(files):
    config, chat, workspace, identity, _ = files
    message = event()
    payload = transfer.prepare_action(config, chat, message, workspace, action(identity, message), 'prepared').file_result
    config.allow_send = False
    state = State(str(workspace / 'host-state.sqlite3'))
    spool = DesktopSpool(str(workspace / 'spool.sqlite3'))
    driver = SimpleNamespace(account_id='bot', account_name='bot', send_file=lambda *a: pytest.fail('preview sent file'))
    listener = Listener(config, driver, spool, state, None)
    try:
        state.enqueue(message)
        state.set_job(message['id'], 'generated', reply='prepared', file_result=payload)
        listener.deliver()
        assert state.job(message['id'])['status'] == 'preview'
    finally:
        listener.close()
        state.close()
        spool.db.close()


def test_existing_draft_prevents_file_paste_and_submission(tmp_path):
    path = tmp_path / 'demo.pcf'
    path.write_bytes(b'content')
    sender = object.__new__(WindowsUIASender)
    edit = SimpleNamespace(GetValuePattern=lambda: SimpleNamespace(Value='user draft'))
    sender.open_chat = lambda _: (object(), edit)
    with pytest.raises(DeliveryNotSent):
        sender.send_file('chat', path, hashlib.sha256(b'content').hexdigest())


def test_file_confirmation_requires_exact_recipient_and_filename():
    def node(name='', children=(), kind='TextControl', cls=''):
        return SimpleNamespace(Name=name, ProcessId=1, ClassName=cls, ControlTypeName=kind, GetChildren=lambda: children)
    send = node('发送', kind='ButtonControl')
    wrong = node(children=[node('demo.pcf'), node('Other'), send], cls='mmui::SendFilesWnd')
    correct = node(children=[node('demo.pcf'), node('Test'), send], cls='mmui::SendFilesWnd')
    desktop = node(children=[wrong])
    sender = object.__new__(WindowsUIASender)
    sender.auto = SimpleNamespace(GetRootControl=lambda: desktop)
    with pytest.raises(RuntimeError):
        sender.file_confirmation(node(), 'demo.pcf', 'Test')
    desktop.GetChildren = lambda: [wrong, correct]
    assert sender.file_confirmation(node(), 'demo.pcf', 'Test') is send
