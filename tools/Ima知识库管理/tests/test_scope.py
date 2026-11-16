import copy
import hashlib

import pytest

from ima_manager.api import ImaError
from ima_manager.scope import (cloud_directories, require_directory, require_file,
                               move_local_file, rename_local_file, create_local_folder)
from ima_manager.sync import Engine, Store, scan


class Cloud:
    client_id = 'scope-test-account'

    def __init__(self):
        self.calls = []
        self.reads = []
        self.entries = {'': [{'media_id': 'project', 'title': '项目管理规定', 'media_type': 99}],
                        'project': [{'media_id': f'folder-{i}', 'title': f'{i:02d}', 'media_type': 99} for i in range(1, 9)]}
        self.entries['project'] += [{'media_id': 'owned', 'title': 'a.txt', 'media_type': 13},
                                    {'media_id': 'manual-csv', 'title': '项目.csv', 'media_type': 5}]
        self.entries.update({f'folder-{i}': [] for i in range(1, 9)})

    def listing(self, kb, folder=''):
        self.reads.append(folder)
        assert folder != 'folder-8', 'unmatched manual folder must not be traversed'
        return copy.deepcopy(self.entries[folder])

    def call(self, endpoint, body, **kwargs):
        self.calls.append((endpoint, body))
        raise AssertionError('out-of-scope write')


@pytest.fixture
def context(tmp_path):
    source = tmp_path / '待同步'
    project = source / '项目管理规定'
    project.mkdir(parents=True)
    for i in range(1, 8):
        (project / f'{i:02d}').mkdir()
    file = project / 'a.txt'
    file.write_bytes(b'owned')
    store = Store(tmp_path / 'state.sqlite3')
    client = Cloud()
    account = hashlib.sha256(client.client_id.encode()).hexdigest()
    row = next(r for r in scan(source) if r['name'] == 'a.txt')
    store.put(account, 'kb', 'project', row, 'done', 'owned')
    return source, file, store, client


def test_cloud_manual_08_folder_is_excluded_without_traversing_it(context):
    source, file, store, client = context
    allowed = cloud_directories(client, source, 'kb')
    assert 'project' in allowed
    assert all(f'folder-{i}' in allowed for i in range(1, 8))
    assert 'folder-8' not in allowed
    with pytest.raises(ImaError, match='没有对应目录'):
        require_directory(client, source, 'kb', 'folder-8')
    assert client.calls == []


def test_manual_csv_without_local_counterpart_cannot_be_moved_or_renamed(context):
    source, file, store, client = context
    manual = {'media_id': 'manual-csv', 'title': '项目.csv', 'media_type': 5}
    with pytest.raises(ImaError, match='没有匹配'):
        move_local_file(client, store, source, 'kb', 'project', manual, '')
    with pytest.raises(ImaError, match='没有匹配'):
        rename_local_file(client, store, source, 'kb', 'project', manual, 'renamed.csv')
    assert client.calls == []


def test_same_local_name_without_matching_media_receipt_is_not_owned(context):
    source, file, store, client = context
    with pytest.raises(ImaError, match='没有匹配'):
        require_file(client, store, source, 'kb', 'project', {'media_id': 'unmanaged-a', 'title': 'a.txt'})


def test_owned_file_cannot_be_moved_into_unmatched_manual_08(context):
    source, file, store, client = context
    with pytest.raises(ImaError, match='没有对应目录'):
        move_local_file(client, store, source, 'kb', 'project',
                        {'media_id': 'owned', 'title': 'a.txt'}, 'folder-8')
    assert client.calls == []


def test_cloud_folder_creation_requires_local_directory(context):
    source, file, store, client = context
    with pytest.raises(ImaError, match='不存在对应文件夹'):
        create_local_folder(client, source, 'kb', 'project', '08')
    assert client.calls == []


def test_upload_receipt_does_not_authorize_a_file_removed_locally(context):
    source, file, store, client = context
    file.unlink()
    with pytest.raises(ImaError, match='没有匹配'):
        require_file(client, store, source, 'kb', 'project', {'media_id': 'owned', 'title': 'a.txt'})


def test_automatic_sync_rejects_an_unmatched_selected_cloud_directory(context):
    source, file, store, client = context
    with pytest.raises(ImaError, match='没有对应目录'):
        Engine(client, store).plan(source, 'kb', 'folder-8')
    assert client.calls == []


def test_rename_to_a_name_absent_locally_is_rejected(context):
    source, file, store, client = context
    with pytest.raises(ImaError, match='没有新名称'):
        rename_local_file(client, store, source, 'kb', 'project', {'media_id': 'owned', 'title': 'a.txt'}, 'new.txt')
    assert client.calls == []
