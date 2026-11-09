import hashlib
from unittest.mock import Mock

import pytest

from ima_manager.api import ImaClient, ImaError, folder_id
from ima_manager.scope import create_folder, move_file, rename_file
from ima_manager import experiment


def test_real_directory_shape_is_recognized():
    assert folder_id({'media_type': 99, 'media_id': 'folder_123', 'title': '测试'}) == 'folder_123'
    assert folder_id({'media_type': 13, 'media_id': 'txt_123'}) == ''


def test_advanced_operations_still_require_explicit_opt_in_and_never_enable_delete():
    session = Mock()
    session.post.return_value = Mock(status_code=200)
    session.post.return_value.json.return_value = {'code': 0, 'data': {}}
    client = ImaClient('fake-client', 'fake-key', session=session, rate_gate=Mock())
    with pytest.raises(ImaError):
        client.call('move_knowledge', {})
    with pytest.raises(ImaError):
        client.call('delete_knowledge', {}, experimental=True)
    assert client.call('rename_knowledge', {}, experimental=True) == {}
    assert session.post.call_count == 1


def test_move_rejects_empty_results_even_when_top_level_says_success():
    client = Mock()
    client.listing.side_effect = [[{'media_id': 'own', 'title': 'a.txt'}], []]
    client.call.return_value = {'move_results': {}}
    with pytest.raises(ImaError, match='未确认'):
        move_file(client, 'kb', '', 'own', 'folder')


def test_move_verifies_both_directories():
    client = Mock()
    own = {'media_id': 'own', 'title': 'a.txt'}
    client.listing.side_effect = [[own], [], [own], []]
    client.call.return_value = {'move_results': {'own': {'ret_code': 0}}}
    with pytest.raises(ImaError, match='位置未能确认'):
        move_file(client, 'kb', '', 'own', 'folder')


def test_move_accepts_only_real_effect():
    client = Mock()
    own = {'media_id': 'own', 'title': 'a.txt'}
    client.listing.side_effect = [[own], [], [], [own]]
    client.call.return_value = {'move_results': {'own': {'ret_code': 0}}}
    move_file(client, 'kb', '', 'own', 'folder')
    assert client.call.call_args.kwargs == {'experimental': True}


def test_rename_no_effect_is_failure():
    client = Mock()
    client.listing.return_value = [{'media_id': 'own', 'title': 'a.txt'}]
    client.call.return_value = {}
    with pytest.raises(ImaError, match='改名结果未能确认'):
        rename_file(client, 'kb', '', 'own', 'b.txt')


@pytest.mark.parametrize('operation', ['rename', 'move'])
def test_folders_are_not_mutated(operation):
    client = Mock()
    client.listing.return_value = [{'media_id': 'folder', 'media_type': 99, 'title': 'folder'}]
    with pytest.raises(ImaError):
        if operation == 'rename':
            rename_file(client, 'kb', '', 'folder', 'new')
        else:
            move_file(client, 'kb', '', 'folder', 'destination')
    client.call.assert_not_called()


def test_create_folder_uses_verified_id_not_just_matching_name():
    client = Mock()
    client.listing.side_effect = [[], [{'media_type': 99, 'media_id': 'other', 'title': 'test'}]]
    client.call.return_value = {'media_id': 'created'}
    with pytest.raises(ImaError, match='结果未能确认'):
        create_folder(client, 'kb', '', 'test')


def test_experiment_cannot_use_arbitrary_files_or_another_account(tmp_path, monkeypatch):
    monkeypatch.setattr(experiment, 'DEFAULT_SOURCE', tmp_path)
    path = tmp_path / 'ima工具测试_run.txt'
    path.write_bytes(b'test-content')
    client = ImaClient('fake-client', 'fake-key')
    record = {'name': path.name, 'run_id': 'run', 'local_path': str(path),
              'upload_verified': True, 'media_id': 'media',
              'account': hashlib.sha256(client.client_id.encode()).hexdigest(),
              'upload': [{'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]}
    experiment.validate_manifest(record, client)
    with pytest.raises(ImaError, match='账号'):
        experiment.validate_manifest(dict(record, account='another'), client)
    with pytest.raises(ImaError, match='文件名'):
        experiment.validate_manifest(dict(record, name='existing-user-file.txt'), client)
    path.write_bytes(b'changed')
    with pytest.raises(ImaError, match='已经改变'):
        experiment.validate_manifest(record, client)


def test_experiment_requests_share_the_normal_account_gate():
    session, gate = Mock(), Mock()
    session.post.return_value.status_code = 403
    session.post.return_value.headers = {}
    session.post.return_value.json.return_value = {'code': 200001, 'msg': '请求频率超限'}
    client = ImaClient('account', 'key', session=session, rate_gate=gate)
    result = experiment.probe(client, 'create_folder', {})
    gate.wait.assert_called_once_with('create_folder', client.sleep)
    gate.defer.assert_called_once_with(120)
    gate.penalize.assert_called_once_with()
    assert session.post.call_count == 1
    assert result['response']['code'] == 200001


def test_duplicate_destination_prevents_any_write():
    client = Mock()
    client.listing.side_effect = [[{'media_id': 'own', 'title': 'A.txt'}],
                                [{'media_id': 'existing', 'title': 'a.txt'}]]
    with pytest.raises(ImaError, match='同名'):
        move_file(client, 'kb', '', 'own', 'folder')
    client.call.assert_not_called()
