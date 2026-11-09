import copy

import pytest

from ima_manager.api import ImaError
from ima_manager.sync import Engine, Store, scan


class Cloud:
    client_id = 'version-update-test-account'

    def __init__(self):
        self.entries = {'': [{'media_id': 'old-id', 'title': 'a.txt', 'media_type': 13}]}
        self.calls = []
        self.uploaded = []
        self.fail_upload = False
        self.fail_publish_after_move = False
        self.fail_add_after_commit = False
        self.created = 0

    def listing(self, kb, folder=''):
        return copy.deepcopy(self.entries[folder])

    def repeated(self, kb, folder, name, media_type):
        return any(r.get('title') == name for r in self.entries[folder])

    def repeated_many(self, kb, folder, params):
        return {item['name']: self.repeated(kb, folder, item['name'], item['media_type']) for item in params}

    def redact(self, text):
        return text

    def upload_object(self, stream, *args):
        if self.fail_upload:
            raise ImaError('模拟 COS 上传失败')
        self.uploaded.append(stream.read())

    def call(self, endpoint, body, **kwargs):
        self.calls.append((endpoint, copy.deepcopy(body)))
        if endpoint == 'create_folder':
            folder = 'folder-' + str(len(self.entries))
            self.entries[body.get('folder_id', '')].append({'media_id': folder, 'title': body['name'], 'media_type': 99})
            self.entries[folder] = []
            return {'media_id': folder}
        if endpoint == 'create_media':
            self.created += 1
            return {'media_id': 'new-id' if self.created == 1 else 'new-id-' + str(self.created),
                    'cos_credential': {'cos_key': 'key'}}
        if endpoint == 'add_knowledge':
            self.entries[body.get('folder_id', '')].append({'media_id': body['media_id'], 'title': body['title'], 'media_type': 13})
            if self.fail_add_after_commit:
                self.fail_add_after_commit = False
                raise ImaError('入库已生效但响应丢失')
            return {'media_id': body['media_id']}
        if endpoint == 'move_knowledge':
            mid = body['infos'][0]['media_id']
            src = next(folder for folder, items in self.entries.items() if any(r.get('media_id') == mid for r in items))
            own = next(r for r in self.entries[src] if r['media_id'] == mid)
            destination = body.get('dst_folder_id', '')
            self.entries[src].remove(own)
            self.entries[destination].append(own)
            if destination == '' and self.fail_publish_after_move:
                self.fail_publish_after_move = False
                raise ImaError('新版移动已生效但响应丢失')
            return {'move_results': {mid: {'ret_code': 0}}}
        raise AssertionError(endpoint)


@pytest.fixture
def state(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    file = source / 'a.txt'
    file.write_bytes(b'version-one')
    cloud = Cloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    engine.store.put(engine.account, 'kb', '', scan(source)[0], 'done', 'old-id')
    file.write_bytes(b'version-two')
    return source, file, cloud, engine


def test_new_version_is_uploaded_before_old_is_archived_and_repeated_sync_skips(state):
    source, file, cloud, engine = state
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_update'
    assert cloud.calls == []
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'
    assert cloud.uploaded == [b'version-two']
    root_files = [r for r in cloud.entries[''] if r.get('media_type') != 99]
    assert root_files == [{'media_id': 'new-id', 'title': 'a.txt', 'media_type': 13}]
    job = engine.store.update_job(engine.account, 'kb', '', rows[0])
    assert cloud.entries[job['archive']] == [{'media_id': 'old-id', 'title': 'a.txt', 'media_type': 13}]
    endpoints = [name for name, _ in cloud.calls]
    assert endpoints.index('add_knowledge') < endpoints.index('move_knowledge')
    assert engine.plan(source, 'kb')[0]['status'] == 'unchanged'
    assert job['phase'] == 'done'


def test_new_root_uploads_do_not_require_directory_listing(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for index in range(25):
        (source / f'new-{index:02d}.txt').write_bytes(b'new file')
    cloud = Cloud()
    cloud.entries = {'': []}
    def unavailable(*_):
        raise AssertionError('new root uploads must not consume get_knowledge_list quota')
    cloud.listing = unavailable
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    assert all(row['status'] == 'ready' for row in rows)
    engine.execute(rows, 'kb')
    assert all(row['status'] == 'done' for row in rows)
    assert len(cloud.uploaded) == 25
    assert all(row['status'] == 'unchanged' for row in engine.plan(source, 'kb'))


@pytest.mark.parametrize('selected', ['', 'project'])
def test_nested_uploads_reuse_directory_reads_across_all_files(tmp_path, selected):
    source = tmp_path / 'source'
    leaf = source / 'project' / 'unit' / 'drawings'
    leaf.mkdir(parents=True)
    for index in range(25):
        (leaf / f'new-{index:02d}.txt').write_bytes(b'new file')
    cloud = Cloud()
    cloud.entries = {
        '': [{'media_id': 'project', 'title': 'project', 'media_type': 99}],
        'project': [{'media_id': 'unit', 'title': 'unit', 'media_type': 99}],
        'unit': [{'media_id': 'drawings', 'title': 'drawings', 'media_type': 99}],
        'drawings': [],
    }
    reads = []
    original = cloud.listing
    def listing(kb, folder=''):
        reads.append(folder)
        return original(kb, folder)
    cloud.listing = listing
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb', selected)
    before = len(reads)
    engine.execute(rows, 'kb', selected)
    assert all(row['status'] == 'done' and row['target_folder_id'] == 'drawings' for row in rows)
    assert len(cloud.uploaded) == 25
    assert before <= 4 and len(reads) - before <= 4
    assert len(reads) == (8 if selected else 6)


def test_newly_created_nested_directories_are_reused_for_later_files(tmp_path):
    source = tmp_path / 'source'
    leaf = source / 'project' / 'drawings'
    leaf.mkdir(parents=True)
    for index in range(25):
        (leaf / f'new-{index:02d}.txt').write_bytes(b'new file')
    cloud = Cloud()
    cloud.entries = {'': []}
    reads = []
    original = cloud.listing
    def listing(kb, folder=''):
        reads.append(folder)
        return original(kb, folder)
    cloud.listing = listing
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert all(row['status'] == 'done' for row in rows)
    assert len([call for call in cloud.calls if call[0] == 'create_folder']) == 2
    assert len(reads) <= 5


def test_execution_refreshes_directory_identity_after_preview(tmp_path):
    source = tmp_path / 'source'
    (source / 'project').mkdir(parents=True)
    (source / 'project/a.txt').write_bytes(b'new file')
    cloud = Cloud()
    cloud.entries = {'': [{'media_id': 'old-folder', 'title': 'project', 'media_type': 99}], 'old-folder': []}
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    cloud.entries[''] = [{'media_id': 'replacement', 'title': 'project', 'media_type': 99}]
    cloud.entries['replacement'] = []
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed' and '身份已改变' in rows[0]['reason']
    assert not cloud.calls and not cloud.uploaded


def test_upload_failure_keeps_old_file_in_normal_directory(state):
    source, file, cloud, engine = state
    cloud.fail_upload = True
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert any(r.get('media_id') == 'old-id' for r in cloud.entries[''])
    assert not any(name == 'move_knowledge' for name, _ in cloud.calls)
    cloud.fail_upload = False
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'


def test_publish_timeout_resumes_without_uploading_or_archiving_again(state):
    source, file, cloud, engine = state
    cloud.fail_publish_after_move = True
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    call_count = len(cloud.calls)
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_update'
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'
    assert len(cloud.calls) == call_count
    assert len(cloud.uploaded) == 1


def test_uncertain_staging_add_is_verified_before_any_retry(state):
    source, file, cloud, engine = state
    cloud.fail_add_after_commit = True
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'uncertain'
    assert any(r.get('media_id') == 'old-id' for r in cloud.entries[''])
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'
    assert len(cloud.uploaded) == 1


def test_unmanaged_same_name_never_triggers_update(state):
    source, file, cloud, engine = state
    cloud.entries[''][0]['media_id'] = 'somebody-elses-file'
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'cloud_existing'
    engine.execute(rows, 'kb')
    assert cloud.calls == []


def test_multiple_same_name_files_are_not_automatically_replaced(state):
    source, file, cloud, engine = state
    cloud.entries[''].append({'media_id': 'other-id', 'title': 'a.txt', 'media_type': 13})
    assert engine.plan(source, 'kb')[0]['status'] == 'conflict'
    assert cloud.calls == []


def test_content_can_return_to_an_earlier_revision_without_reusing_finished_job(state):
    source, file, cloud, engine = state
    for contents in (b'version-two', b'version-one', b'version-two'):
        file.write_bytes(contents)
        rows = engine.plan(source, 'kb')
        assert rows[0]['status'] == 'ready_update'
        engine.execute(rows, 'kb')
        assert rows[0]['status'] == 'done', rows
        assert engine.plan(source, 'kb')[0]['status'] == 'unchanged'
    assert cloud.uploaded == [b'version-two', b'version-one', b'version-two']
    root_files = [r for r in cloud.entries[''] if r.get('media_type') != 99]
    assert len(root_files) == 1
    assert root_files[0]['media_id'] == 'new-id-3'


def test_unmatched_manual_folder_and_csv_are_unchanged_during_update(state):
    source, file, cloud, engine = state
    extras = [{'media_id': 'manual-08', 'title': '08', 'media_type': 99},
              {'media_id': 'manual-csv', 'title': '项目.csv', 'media_type': 5}]
    cloud.entries[''].extend(copy.deepcopy(extras))
    cloud.entries['manual-08'] = [{'media_id': 'private', 'title': 'private.txt'}]
    before = copy.deepcopy(cloud.entries['manual-08'])
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'
    assert all(extra in cloud.entries[''] for extra in extras)
    assert cloud.entries['manual-08'] == before
    moved = [body['infos'][0]['media_id'] for name, body in cloud.calls if name == 'move_knowledge']
    assert set(moved) == {'old-id', 'new-id'}


def test_removing_local_file_after_plan_prevents_any_cloud_update(state):
    source, file, cloud, engine = state
    rows = engine.plan(source, 'kb')
    file.unlink()
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert cloud.calls == []


def test_cloud_rename_after_plan_does_not_move_an_unmatched_title(state):
    source, file, cloud, engine = state
    rows = engine.plan(source, 'kb')
    cloud.entries[''][0]['title'] = '项目.csv'
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert any(r.get('media_id') == 'old-id' and r.get('title') == '项目.csv' for r in cloud.entries[''])
    assert not any(name == 'move_knowledge' for name, _ in cloud.calls)


@pytest.mark.parametrize('selected', ['', 'project'])
def test_tree_preview_reuses_manual_nested_folders_and_files(tmp_path, selected):
    source = tmp_path / 'source'
    local = source / '项目管理规定' / '02_质量管理'
    local.mkdir(parents=True)
    (local / 'a.txt').write_bytes(b'manual-file')
    cloud = Cloud()
    cloud.entries = {
        '': [{'media_id': 'project', 'title': '项目管理规定', 'media_type': 99}],
        'project': [{'media_id': 'quality', 'title': '02_质量管理', 'media_type': 99}],
        'quality': [{'media_id': 'manual', 'title': 'a.txt', 'media_type': 13}],
    }
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb', selected)
    assert rows[0]['status'] == 'cloud_existing'
    assert rows[0]['target_folder_id'] == 'quality'
    expected = '02_质量管理/a.txt' if selected else '项目管理规定/02_质量管理/a.txt'
    assert rows[0]['target_path'] == expected
    engine.execute(rows, 'kb', selected)
    assert cloud.calls == [] and cloud.uploaded == []
    assert not engine.store.get(engine.account, 'kb', 'quality', rows[0])


def test_tree_preview_never_creates_and_sync_uploads_to_separate_parents(tmp_path):
    source = tmp_path / 'source'
    for name in ('one', 'two'):
        (source / 'project' / name).mkdir(parents=True)
        (source / 'project' / name / 'same.txt').write_bytes(name.encode())
    cloud = Cloud()
    cloud.entries = {'': []}
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    assert all(row['status'] == 'ready' and row['target_folder_id'] is None for row in rows)
    assert cloud.calls == []
    engine.execute(rows, 'kb')
    assert all(row['status'] == 'done' for row in rows)
    assert len({row['target_folder_id'] for row in rows}) == 2
    assert not any(item.get('title') == 'same.txt' for item in cloud.entries[''])
    assert all(row['status'] == 'unchanged' for row in engine.plan(source, 'kb'))
    assert len([call for call in cloud.calls if call[0] == 'create_folder']) == 3


def test_tree_ambiguous_cloud_directory_blocks_without_writes(tmp_path):
    source = tmp_path / 'source'
    (source / 'project').mkdir(parents=True)
    (source / 'project' / 'a.txt').write_bytes(b'local')
    cloud = Cloud()
    cloud.entries = {'': [{'title': 'project', 'media_type': 99, 'media_id': id_} for id_ in ('p1', 'p2')]}
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'blocked'
    engine.execute(rows, 'kb')
    assert cloud.calls == []


def test_tree_nested_update_and_logical_delete_use_same_parent(tmp_path):
    from ima_manager.sync import move_local_to_deleted
    source = tmp_path / 'source'
    (source / 'project').mkdir(parents=True)
    file = source / 'project' / 'a.txt'
    file.write_bytes(b'one')
    cloud = Cloud()
    cloud.entries = {'': [{'media_id': 'p', 'title': 'project', 'media_type': 99}],
                     'p': [{'media_id': 'old-id', 'title': 'a.txt', 'media_type': 13}]}
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    engine.store.put(engine.account, 'kb', 'p', scan(source)[0], 'done', 'old-id')
    file.write_bytes(b'two')
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_update'
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'
    assert any(item.get('media_id') == rows[0]['media_id'] for item in cloud.entries['p'])
    move_local_to_deleted(source, file)
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_delete' and rows[0]['target_folder_id'] == 'p'
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'deleted'


def test_tree_stale_flat_plan_cannot_upload(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a.txt').write_bytes(b'local')
    cloud = Cloud()
    cloud.entries = {'': []}
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    rows[0].pop('layout')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed' and cloud.calls == []



def test_tree_does_not_reuse_previous_flat_root_receipt(tmp_path):
    source = tmp_path / 'source'
    (source / 'project').mkdir(parents=True)
    (source / 'project' / 'a.txt').write_bytes(b'one')
    cloud = Cloud()
    cloud.entries = {'': [{'media_id': 'p', 'title': 'project', 'media_type': 99},
                           {'media_id': 'flat', 'title': 'a.txt', 'media_type': 13}], 'p': []}
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    engine.store.put(engine.account, 'kb', '', scan(source)[0], 'done', 'flat')
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready' and rows[0]['target_folder_id'] == 'p'
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'done'
    assert any(item.get('media_id') == 'flat' for item in cloud.entries[''])
    assert any(item.get('media_id') == rows[0]['media_id'] for item in cloud.entries['p'])
