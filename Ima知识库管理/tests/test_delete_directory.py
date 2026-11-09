import copy

import pytest

from ima_manager.api import ImaError
from ima_manager.sync import Engine, Store, scan, move_local_to_deleted


class Cloud:
    client_id = 'delete-directory-test-account'

    def __init__(self):
        self.directories = {'': [{'media_id': 'own-media', 'title': 'a.txt', 'media_type': 13}]}
        self.calls = []
        self.timeout_after_move = False

    def listing(self, kb, folder=''):
        return copy.deepcopy(self.directories[folder])

    def repeated(self, *_):
        return False

    def repeated_many(self, kb, folder, params):
        return {item['name']: False for item in params}

    def redact(self, text):
        return text

    def call(self, endpoint, body, *, experimental=False):
        assert experimental is True
        assert endpoint in {'create_folder', 'move_knowledge'}
        self.calls.append((endpoint, body))
        if endpoint == 'create_folder':
            parent = body.get('folder_id', '')
            fid = 'folder_' + str(len(self.directories))
            self.directories[parent].append({'media_id': fid, 'title': body['name'], 'media_type': 99})
            self.directories[fid] = []
            return {'media_id': fid}
        mid = body['infos'][0]['media_id']
        own = next(row for row in self.directories[''] if row['media_id'] == mid)
        self.directories[''].remove(own)
        self.directories[body['dst_folder_id']].append(own)
        if self.timeout_after_move:
            self.timeout_after_move = False
            raise ImaError('移动已生效但响应丢失')
        return {'move_results': {mid: {'ret_code': 0}}}


@pytest.fixture
def state(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    file = source / 'a.txt'
    file.write_text('uploaded content', encoding='utf-8')
    cloud = Cloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    row = scan(source)[0]
    engine.store.put(engine.account, 'kb', '', row, 'done', 'own-media')
    return source, file, cloud, engine


def test_local_move_preserves_bytes_and_archive_is_never_uploaded(state):
    source, file, cloud, engine = state
    original = file.read_bytes()
    archived = move_local_to_deleted(source, file)
    assert not file.exists()
    assert archived == source / '删除' / 'a.txt'
    assert archived.read_bytes() == original
    rows = scan(source)
    assert len(rows) == 1
    assert rows[0]['status'] == 'deleted_local'
    assert rows[0]['action'] == 'delete'
    assert rows[0]['original_path'] == str(file)


def test_preview_is_read_only_then_move_is_verified_and_idempotent(state):
    source, file, cloud, engine = state
    move_local_to_deleted(source, file)
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_delete'
    assert cloud.calls == []
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'deleted'
    assert [endpoint for endpoint, _ in cloud.calls] == ['create_folder', 'move_knowledge']
    assert cloud.directories[''][0]['title'] == '删除'
    assert cloud.directories['folder_1'][0]['media_id'] == 'own-media'
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'deleted'
    engine.execute(rows, 'kb')
    assert len(cloud.calls) == 2


def test_existing_cloud_delete_directory_is_reused(state):
    source, file, cloud, engine = state
    cloud.directories[''].append({'media_id': 'existing-delete', 'title': '删除', 'media_type': 99})
    cloud.directories['existing-delete'] = []
    move_local_to_deleted(source, file)
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'deleted'
    assert [endpoint for endpoint, _ in cloud.calls] == ['move_knowledge']


def test_timeout_after_move_recovers_without_repeating_write(state):
    source, file, cloud, engine = state
    move_local_to_deleted(source, file)
    cloud.timeout_after_move = True
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_delete'
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'deleted'
    assert len(cloud.calls) == 2


def test_copy_is_not_a_deletion_marker(state):
    source, file, cloud, engine = state
    (source / '删除').mkdir()
    (source / '删除' / 'a.txt').write_bytes(file.read_bytes())
    rows = engine.plan(source, 'kb')
    assert next(r for r in rows if r.get('action') == 'delete')['status'] == 'blocked'
    engine.execute(rows, 'kb')
    assert cloud.calls == []


def test_untracked_or_changed_archived_content_never_moves_cloud(state):
    source, file, cloud, engine = state
    archived = move_local_to_deleted(source, file)
    archived.write_bytes(b'not the uploaded content')
    rows = engine.plan(source, 'kb')
    assert rows[0]['status'] == 'deleted_local'
    engine.execute(rows, 'kb')
    assert cloud.calls == []


def test_receipt_for_other_account_is_not_used(state):
    source, file, cloud, engine = state
    move_local_to_deleted(source, file)
    cloud.client_id = 'other-account'
    other = Engine(cloud, engine.store)
    rows = other.plan(source, 'kb')
    assert rows[0]['status'] == 'deleted_local'
    assert cloud.calls == []


def test_same_name_collision_is_isolated_without_renaming(state):
    source, file, cloud, engine = state
    cloud.directories[''].append({'media_id': 'existing-delete', 'title': '删除', 'media_type': 99})
    cloud.directories['existing-delete'] = [{'media_id': 'older', 'title': 'a.txt'}]
    move_local_to_deleted(source, file)
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'deleted'
    assert cloud.directories['existing-delete'][0] == {'media_id': 'older', 'title': 'a.txt'}
    assert any(r.get('media_id') == 'own-media' for r in cloud.directories['folder_2'])


def test_local_delete_does_not_overwrite_existing_file(state):
    source, file, cloud, engine = state
    (source / '删除').mkdir()
    (source / '删除' / 'a.txt').write_bytes(b'older archive')
    with pytest.raises(ImaError, match='不会覆盖'):
        move_local_to_deleted(source, file)
    assert file.exists()
    assert (source / '删除' / 'a.txt').read_bytes() == b'older archive'


def test_nested_path_is_preserved_when_archiving(state):
    source, file, cloud, engine = state
    nested = source / 'sub' / 'b.txt'
    nested.parent.mkdir()
    nested.write_bytes(b'nested')
    archived = move_local_to_deleted(source, nested)
    assert archived == source / '删除' / 'sub' / 'b.txt'
    marker = next(r for r in scan(source, recursive=False) if r.get('action') == 'delete')
    assert marker['original_path'] == str(nested)


def test_original_restored_after_preview_prevents_move(state):
    source, file, cloud, engine = state
    move_local_to_deleted(source, file)
    rows = engine.plan(source, 'kb')
    file.write_bytes(b'restored')
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'failed'
    assert cloud.calls == []
