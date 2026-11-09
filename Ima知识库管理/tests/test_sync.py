import copy
import hashlib
import sqlite3
import threading
from pathlib import Path

import pytest

from ima_manager.api import ImaError, ImaQuotaError, ImaRateLimitError
from ima_manager.sync import Engine, Store, scan


class Client:
    client_id = 'experiment-account'

    def __init__(self):
        self.calls = []
        self.remote = False
        self.uncertain = False
        self.uploads = []

    def repeated(self, *args):
        return self.remote

    def repeated_many(self, kb, folder, params):
        return {item['name']: self.repeated(kb, folder, item['name'], item['media_type']) for item in params}

    def listing(self, kb, folder=''):
        return [{'media_id': 'cloud-folder', 'media_type': 99, 'title': 'source'}] if not folder else []

    def redact(self, text):
        return text

    def call(self, endpoint, body):
        self.calls.append((endpoint, copy.deepcopy(body)))
        if endpoint == 'create_media':
            return {'media_id': 'created-media', 'cos_credential': {'cos_key': 'object-key'}}
        if self.uncertain:
            raise ImaError('模拟入库请求超时')
        self.remote = True
        return {'media_id': 'linked-media'}

    def upload_object(self, stream, size, mime, credential):
        self.uploads.append(stream.read())


@pytest.fixture
def setup(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    file = source / '测试资料.txt'
    file.write_text('实验资料，不修改原文。', encoding='utf-8')
    client = Client()
    engine = Engine(client, Store(tmp_path / 'state.sqlite3'))
    return source, file, client, engine


def test_preview_does_not_upload_then_execute_preserves_bytes_and_folder(setup):
    source, file, client, engine = setup
    rows = engine.plan(source, 'kb', 'cloud-folder')
    assert rows[0]['status'] == 'ready'
    assert client.calls == []
    engine.execute(rows, 'kb', 'cloud-folder')
    assert rows[0]['status'] == 'done'
    assert client.uploads == [file.read_bytes()]
    add = client.calls[-1][1]
    assert add['folder_id'] == 'cloud-folder'
    assert add['title'] == add['file_info']['file_name'] == file.name
    assert engine.plan(source, 'kb', 'cloud-folder')[0]['status'] == 'unchanged'


@pytest.mark.parametrize('failed_endpoint', ['create_media', 'add_knowledge'])
def test_quota_aborts_remaining_files_and_next_run_resumes_from_receipts(setup, failed_endpoint):
    source, file, client, engine = setup
    file.rename(source / 'a-first.txt')
    for name in ['b-quota.txt', 'c-last.txt']:
        (source / name).write_text(name, encoding='utf-8')
    client.repeated = lambda *_: False
    original_call = client.call
    def reject(endpoint, body):
        if endpoint == failed_endpoint and body.get('file_name', body.get('title')) == 'b-quota.txt':
            raise ImaQuotaError('ima 220021: 资料获取次数已达上限', code=220021, endpoint=endpoint)
        return original_call(endpoint, body)
    client.call = reject
    rows = engine.plan(source, 'kb')
    messages = []
    with pytest.raises(ImaQuotaError):
        engine.execute(rows, 'kb', notify=messages.append)
    assert [row['status'] for row in rows] == ['done', 'failed', 'ready']
    assert any('中止本轮同步' in text for text in messages)
    assert engine.store.get(engine.account, 'kb', '', rows[0])[0] == 'done'
    assert not engine.store.pending(engine.account, 'kb', '', rows[1])
    assert not any(body.get('file_name', body.get('title')) == 'c-last.txt' for _, body in client.calls)
    # A new process/engine can reopen the durable receipts and skip success.
    replacement = Client()
    replacement.repeated = lambda *_: False
    resumed = Engine(replacement, Store(engine.store.path))
    next_rows = resumed.plan(source, 'kb')
    assert [row['status'] for row in next_rows] == ['unchanged', 'ready', 'ready']
    resumed.execute(next_rows, 'kb')
    assert [row['status'] for row in next_rows] == ['unchanged', 'done', 'done']
    assert len(replacement.uploads) == 2


def test_rate_limit_name_check_aborts_remaining_files_and_preserves_receipts(setup):
    source, file, client, engine = setup
    file.rename(source / 'a-first.txt')
    for name in ('b-limited.txt', 'c-last.txt'):
        (source / name).write_text(name, encoding='utf-8')
    rows = engine.plan(source, 'kb')
    checked = []
    def repeated(kb, folder, name, media_type):
        checked.append(name)
        if name == 'b-limited.txt':
            raise ImaRateLimitError('ima 200001: 请求频率超限', code=200001, endpoint='check_repeated_names')
        return False
    client.repeated = repeated
    with pytest.raises(ImaRateLimitError):
        engine.execute(rows, 'kb')
    assert [row['status'] for row in rows] == ['done', 'failed', 'ready']
    assert checked == ['a-first.txt', 'b-limited.txt']
    assert engine.store.get(engine.account, 'kb', '', rows[0])[0] == 'done'
    assert len(client.uploads) == 1


def test_quota_during_plan_is_not_swallowed_as_a_blocked_file(setup):
    source, file, client, engine = setup
    nested = source / 'nested'
    nested.mkdir()
    file.rename(nested / file.name)
    def listing(*_):
        raise ImaQuotaError('ima 220021', code=220021, endpoint='get_knowledge_list')
    client.listing = listing
    with pytest.raises(ImaQuotaError):
        engine.plan(source, 'kb')
    assert not client.uploads


def test_quota_cli_saves_partial_report_and_exits_failed(setup, monkeypatch):
    import json
    from ima_manager import __main__ as cli, collect
    source, file, client, engine = setup
    (source / 'z-later.txt').write_text('later', encoding='utf-8')
    runtime = source.parent / 'runtime'
    monkeypatch.setattr(cli, 'RUNTIME', runtime)
    monkeypatch.setattr(cli, 'load_settings', lambda: {'source': str(source), 'kb': 'kb', 'folder': '',
                                                     'collect_enabled': False})
    monkeypatch.setattr(cli, 'load_credentials', lambda: ('test-client', 'test-key'))
    monkeypatch.setattr(cli, 'ImaClient', lambda *_: client)
    monkeypatch.setattr(cli, 'Store', lambda *_: engine.store)
    monkeypatch.setattr(cli, 'Engine', lambda *_: engine)
    monkeypatch.setattr(collect, 'prepare_documents', lambda *_args, **_kwargs: {'files': [], 'counts': {}})
    def reject(*_):
        raise ImaQuotaError('ima 220021', code=220021, endpoint='create_media')
    client.call = reject
    assert cli.main(['sync', '--execute']) == 1
    report = json.loads((runtime / 'latest-plan.json').read_text(encoding='utf-8'))
    assert report['aborted'] and report['error_code'] == 220021
    assert sorted(row['status'] for row in report['files']) == ['failed', 'ready']
    assert not client.uploads
    # The existing daily admission rule still rejects another same-day run.
    assert cli.main(['sync', '--execute']) == 1


def test_preview_batches_project_files_and_upload_rechecks_names(setup):
    source, file, client, engine = setup
    for number in range(205):
        (source / f'project-{number}.pdf').write_bytes(b'%PDF-test')
    batches = []
    def batch(kb, folder, params):
        batches.append((kb, folder, params))
        return {item['name']: item['name'] == 'project-0.pdf' for item in params}
    client.repeated_many = batch
    rows = engine.plan(source, 'kb')
    assert len(batches) == 1 and len(batches[0][2]) == 206
    assert next(row for row in rows if row['name'] == 'project-0.pdf')['status'] == 'conflict'
    assert client.calls == [] and client.uploads == []
    client.remote = True  # A name can appear after the preview completed.
    selected = next(row for row in rows if row['name'] == file.name)
    engine.execute([selected], 'kb')
    assert selected['status'] == 'conflict' and client.uploads == []


@pytest.mark.parametrize(('command', 'cloud_failure', 'expected'), [('plan', False, 0), ('sync', False, 0), ('sync', True, 1)])
def test_collection_file_errors_do_not_fail_preview_or_prevent_valid_uploads(setup, monkeypatch, command, cloud_failure, expected):
    import json
    from openpyxl import Workbook
    from ima_manager import __main__ as cli, collect
    source, file, client, engine = setup
    runtime = source.parent / 'runtime'
    downloads = source.parent / 'downloads'
    downloads.mkdir()
    ledger = source.parent / 'ledger.xlsx'
    book = Workbook()
    sheet = book.active
    sheet.title = '文件台账'
    sheet.append(['文件名', '业务类型', '业务专业', '目录'])
    sheet.append(['PP-missing.pdf', '项目程序文件', '项目管理', 'missing'])
    book.save(ledger)
    book.close()
    monkeypatch.setattr(cli, 'RUNTIME', runtime)
    monkeypatch.setattr(collect, 'RUNTIME', runtime)
    monkeypatch.setattr(cli, 'load_settings', lambda: {'source': str(source), 'kb': 'kb', 'folder': '',
        'collect_enabled': True, 'ledger_path': str(ledger), 'downloads_path': str(downloads)})
    monkeypatch.setattr(cli, 'load_credentials', lambda: ('test-client', 'test-key'))
    monkeypatch.setattr(cli, 'ImaClient', lambda *_: client)
    monkeypatch.setattr(cli, 'Store', lambda *_: engine.store)
    monkeypatch.setattr(cli, 'Engine', lambda *_: engine)
    client.uncertain = cloud_failure
    assert cli.main([command, '--ledger', str(ledger), *(['--execute'] if command == 'sync' else [])]) == expected
    report = json.loads((runtime / 'latest-plan.json').read_text(encoding='utf-8'))
    assert report['collection']['has_issues'] and report['collection']['counts'] == {'error': 1}
    assert len(report['files']) == 1
    assert report['files'][0]['status'] == ('ready' if command == 'plan' else 'uncertain' if cloud_failure else 'done')
    assert client.uploads == ([] if command == 'plan' else [file.read_bytes()])


def test_receipts_are_scoped_to_account_and_target(setup):
    source, file, client, engine = setup
    engine.execute(engine.plan(source, 'kb'), 'kb')
    client.remote = False
    assert engine.plan(source, 'other-kb')[0]['status'] == 'ready'
    other = Client()
    other.client_id = 'another-account'
    assert Engine(other, engine.store).plan(source, 'kb')[0]['status'] == 'ready'


def test_uncertain_add_blocks_retry_even_if_local_file_changes(setup):
    source, file, client, engine = setup
    client.uncertain = True
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'uncertain'
    file.write_text('新版', encoding='utf-8')
    assert engine.plan(source, 'kb')[0]['status'] == 'uncertain'
    assert len(client.uploads) == 1
    engine.store.resolve(engine.account, 'kb', '', file, uploaded=False)
    client.uncertain = False
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert len(client.uploads) == 2


def test_manual_confirm_uploaded_skips_second_upload(setup):
    source, file, client, engine = setup
    client.uncertain = True
    engine.execute(engine.plan(source, 'kb'), 'kb')
    engine.store.resolve(engine.account, 'kb', '', file, uploaded=True)
    assert engine.plan(source, 'kb')[0]['status'] == 'unchanged'


def test_same_name_remote_is_never_overwritten(setup):
    source, file, client, engine = setup
    client.remote = True
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'conflict'
    assert client.calls == []


def test_file_changed_after_preview_is_not_uploaded(setup):
    source, file, client, engine = setup
    rows = engine.plan(source, 'kb')
    file.write_text('不同内容', encoding='utf-8')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert client.calls == []


def test_racing_cloud_same_name_is_rechecked(setup):
    source, file, client, engine = setup
    rows = engine.plan(source, 'kb')
    client.remote = True
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'conflict'
    assert client.uploads == []


def test_cancel_between_files_keeps_unstarted_files_ready(setup):
    source, file, client, engine = setup
    (source / '第二份.txt').write_text('second', encoding='utf-8')
    rows = engine.plan(source, 'kb')
    stop = threading.Event()
    engine.execute(rows, 'kb', stop=stop, notify=lambda _: stop.set())
    assert [r['status'] for r in rows].count('done') == 1
    assert [r['status'] for r in rows].count('ready') == 1


def test_parallel_process_lock_rejects_upload(setup):
    source, file, client, engine = setup
    rows = engine.plan(source, 'kb')
    lock = sqlite3.connect(engine.store.path.with_name('upload-lock.sqlite3'))
    lock.execute('CREATE TABLE guard (id INTEGER)')
    lock.execute('BEGIN IMMEDIATE')
    try:
        with pytest.raises(ImaError, match='另一个上传任务'):
            engine.execute(rows, 'kb')
    finally:
        lock.close()
    assert client.calls == []


def test_failed_cos_does_not_add_knowledge_and_can_retry(setup):
    source, file, client, engine = setup
    def fail(*_):
        raise ImaError('COS 中断')
    client.upload_object = fail
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert [endpoint for endpoint, _ in client.calls] == ['create_media']
    assert engine.plan(source, 'kb')[0]['status'] == 'ready'


def test_local_same_names_in_different_directories_are_allowed(setup):
    source, file, client, engine = setup
    (source / 'sub').mkdir()
    (source / 'sub' / file.name).write_bytes(file.read_bytes())
    assert all(r['status'] == 'ready' for r in scan(source))
    assert scan(source, recursive=False)[0]['status'] == 'ready'


def test_preflight_rejects_zip_empty_and_fake_image(setup):
    source, file, client, engine = setup
    (source / '资料.zip').write_bytes(b'PK')
    (source / 'empty.txt').touch()
    (source / 'fake.png').write_bytes(b'not a png')
    states = {r['name']: r['status'] for r in scan(source)}
    assert states == {file.name: 'ready', '资料.zip': 'blocked', 'empty.txt': 'blocked', 'fake.png': 'blocked'}


def test_preflight_size_limit_before_hashing(setup):
    source, file, client, engine = setup
    with file.open('wb') as stream:
        stream.truncate(10 * 1024 * 1024 + 1)
    row = scan(source)[0]
    assert row['status'] == 'blocked'
    assert 'sha256' not in row


def test_upload_change_during_stream_does_not_add(setup):
    source, file, client, engine = setup
    rows = engine.plan(source, 'kb')
    def changed(stream, *_):
        stream.read()
        file.write_bytes(b'changed while uploading')
    client.upload_object = changed
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert len(client.calls) == 1
