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


@pytest.mark.parametrize('endpoint', ['move_knowledge', 'rename_knowledge'])
@pytest.mark.parametrize('lost_response', [False, True])
def test_directory_session_preserves_unrelated_reads_after_write(endpoint, lost_response):
    from ima_manager.tree import DirectorySession
    client = Mock()
    entries = {'source': [{'media_id': 'own', 'title': 'a.txt'}],
               'destination': [], 'unrelated': []}
    client.call.return_value = {'move_results': {'own': {'ret_code': 0}}}
    client.listing.side_effect = lambda kb, folder: list(entries[folder])
    session = DirectorySession(client)
    for folder in entries:
        session.listing('kb', folder)
    body = ({'src_knowledge_base_id': 'kb', 'dst_knowledge_base_id': 'kb',
             'dst_folder_id': 'destination', 'infos': [{'media_id': 'own'}]}
            if endpoint == 'move_knowledge' else
            {'knowledge_base_id': 'kb', 'media_id': 'own', 'name': 'new.txt'})
    if lost_response:
        client.call.side_effect = ImaError('response lost')
        with pytest.raises(ImaError, match='response lost'):
            session.call(endpoint, body, experimental=True)
    else:
        session.call(endpoint, body, experimental=True)
    entries['source'] = []
    entries['destination'] = [{'media_id': 'own', 'title': 'a.txt'}]
    assert session.listing('kb', 'source') == (
        [{'media_id': 'own', 'title': 'new.txt'}] if endpoint == 'rename_knowledge' and not lost_response else [])
    session.listing('kb', 'destination')
    session.listing('kb', 'unrelated')
    assert client.listing.call_count == ((5 if endpoint == 'move_knowledge' else 4) if lost_response else 3)


def test_directory_session_unknown_batch_source_keeps_unrelated_directories():
    from ima_manager.tree import DirectorySession
    client = Mock()
    client.listing.return_value = [{'media_id': 'own', 'title': 'a.txt'}]
    client.call.return_value = {'move_results': {'own': {'ret_code': 0}, 'unknown': {'ret_code': 0}}}
    session = DirectorySession(client)
    session.listing('kb', 'source')
    session.listing('other-kb', '')
    session.call('move_knowledge', {'src_knowledge_base_id': 'kb', 'dst_knowledge_base_id': 'kb',
                                  'infos': [{'media_id': 'own'}, {'media_id': 'unknown'}]})
    session.listing('kb', 'source')
    session.listing('other-kb', '')
    assert client.listing.call_count == 2


@pytest.mark.parametrize('endpoint', ['create_folder', 'rename_knowledge', 'move_knowledge'])
def test_directory_write_reuses_unaffected_list_and_rows(endpoint):
    from ima_manager.tree import DirectorySession
    client = Mock()
    client.call.return_value = {'media_id': 'new-folder', 'move_results': {'own': {'ret_code': 0}}}
    session = DirectorySession(client)
    unrelated = [{'media_id': 'unrelated', 'title': 'untouched'}]
    session.cache = {('kb', 'source'): [{'media_id': 'own', 'title': 'a.txt'}],
                     ('kb', 'destination'): [], ('kb', 'unrelated'): unrelated}
    body = ({'knowledge_base_id': 'kb', 'folder_id': 'source', 'name': 'new'} if endpoint == 'create_folder' else
            {'knowledge_base_id': 'kb', 'media_id': 'own', 'name': 'new'} if endpoint == 'rename_knowledge' else
            {'src_knowledge_base_id': 'kb', 'dst_knowledge_base_id': 'kb', 'dst_folder_id': 'destination', 'infos': [{'media_id': 'own'}]})
    session.call(endpoint, body)
    assert session.cache['kb', 'unrelated'] is unrelated
    assert session.cache['kb', 'unrelated'][0] is unrelated[0]


def test_directory_session_applies_move_response_without_extra_reads():
    from ima_manager.tree import DirectorySession
    client = Mock()
    own = {'media_id': 'own', 'title': 'a.txt'}
    client.listing.side_effect = [[], [own], [], [own], [], [], [own]]
    client.call.return_value = {'move_results': {'own': {'ret_code': 0}}}
    session = DirectorySession(client)
    session.listing('kb', 'unrelated')
    session.listing('kb', 'source')
    session.listing('kb', 'destination')
    move_file(session, 'kb', 'source', 'own', 'destination', expected_title='a.txt')
    session.listing('kb', 'unrelated')
    assert client.listing.call_count == 3
    assert session.listing('kb', 'source') == []
    assert session.listing('kb', 'destination') == [own]


def test_directory_session_does_not_hide_new_destination_conflict():
    from ima_manager.tree import DirectorySession
    client = Mock()
    own = {'media_id': 'own', 'title': 'a.txt'}
    client.listing.side_effect = [[{'media_id': 'manual', 'title': 'A.txt'}], [own]]
    session = DirectorySession(client)
    session.listing('kb', 'destination')
    with pytest.raises(ImaError, match='同名'):
        move_file(session, 'kb', 'source', 'own', 'destination')
    client.call.assert_not_called()


@pytest.mark.parametrize('results', [None, {}, {'a': {'ret_code': 0}},
    {'a': {'ret_code': 0}, 'b': {'ret_code': 1}},
    {'a': {'ret_code': 0}, 'b': {'ret_code': False}}])
def test_batch_move_requires_explicit_success_for_every_file(results):
    from ima_manager.scope import move_files
    client = Mock()
    items = [{'media_id': 'a', 'title': 'a.txt'}, {'media_id': 'b', 'title': 'b.txt'}]
    client.listing.side_effect = [items, []]
    client.call.return_value = {'move_results': results}
    with pytest.raises(ImaError, match='未确认本批全部'):
        move_files(client, 'kb', 'source', items, 'destination')
    assert len(client.call.call_args.args[1]['infos']) == 2


def test_batch_move_rejects_eleven_items_before_any_api_request():
    from ima_manager.scope import move_files
    client = Mock()
    items = [{'media_id': str(index), 'title': f'{index}.txt'} for index in range(11)]
    with pytest.raises(ImaError, match='1～10'):
        move_files(client, 'kb', 'source', items, 'destination')
    client.listing.assert_not_called()
    client.call.assert_not_called()


@pytest.mark.parametrize('source_after,destination_after', [
    ([{'media_id': 'a', 'title': 'a.txt'}], [{'media_id': 'a', 'title': 'a.txt'}, {'media_id': 'b', 'title': 'b.txt'}]),
    ([], [{'media_id': 'a', 'title': 'a.txt'}]),
    ([], [{'media_id': 'a', 'title': 'a.txt'}, {'media_id': 'b', 'title': 'wrong.txt'}]),
])
def test_batch_move_checks_all_source_and_destination_identities(source_after, destination_after):
    from ima_manager.scope import move_files
    client = Mock()
    items = [{'media_id': 'a', 'title': 'a.txt'}, {'media_id': 'b', 'title': 'b.txt'}]
    client.listing.side_effect = [items, [], source_after, destination_after]
    client.call.return_value = {'move_results': {'a': {'ret_code': 0}, 'b': {'ret_code': 0}}}
    with pytest.raises(ImaError, match='位置未能确认'):
        move_files(client, 'kb', 'source', items, 'destination')


@pytest.mark.parametrize('problem', ['missing', 'renamed', 'folder', 'duplicate', 'destination', 'stop'])
def test_batch_move_preflight_blocks_entire_batch(problem):
    import threading
    from ima_manager.scope import move_files
    client = Mock()
    items = [{'media_id': 'a', 'title': 'a.txt'}, {'media_id': 'b', 'title': 'b.txt'}]
    source = [dict(item) for item in items]
    destination = []
    stop = threading.Event()
    if problem == 'missing':
        source.pop()
    elif problem == 'renamed':
        source[1]['title'] = 'changed.txt'
    elif problem == 'folder':
        source[1]['media_type'] = 99
    elif problem == 'duplicate':
        source.append(dict(source[1]))
    elif problem == 'destination':
        destination = [{'media_id': 'manual', 'title': 'A.txt'}]
    else:
        stop.set()
    client.listing.side_effect = [source, destination]
    with pytest.raises(ImaError):
        move_files(client, 'kb', 'source', items, 'destination', stop=stop)
    client.call.assert_not_called()
