import json

from ima_manager.empty_folders import report_empty_folders
from ima_manager.sync import Engine, Store


class Client:
    client_id = 'empty-folder-test'

    def listing(self, *args):
        raise AssertionError('cleanup report must not add network requests')

    def call(self, *args, **kwargs):
        raise AssertionError('unsupported folder writes must not be attempted')


def test_cleanup_report_keeps_active_and_nonempty_folders_and_adds_no_requests(tmp_path):
    engine = Engine(Client(), Store(tmp_path / 'state.sqlite3'))
    with engine.store.connect() as db:
        db.execute('CREATE TABLE layout_moves(account,kb,old_folder,new_folder,phase)')
        db.executemany('INSERT INTO layout_moves VALUES (?,?,?,?,?)',
                       [(engine.account, 'kb', identity, 'new', phase) for identity, phase in
                        [('empty', 'done'), ('unknown', 'done'), ('active', 'done'),
                         ('nonempty', 'done'), ('pending', 'pending')]])
    messages = []
    with engine._directory_session():
        engine.client.cache[('kb', 'empty')] = []
        engine.client.cache[('kb', 'nonempty')] = [{'media_id': 'file'}]
        report_empty_folders(engine, [{'target_folder_id': 'active'}], 'kb', '', None, messages.append)
    report = json.loads((tmp_path / 'empty-folder-cleanup.json').read_text(encoding='utf-8'))
    assert report['state'] == 'manual_cleanup_required'
    assert report['network_requests'] == 0
    assert report['folders'] == [{'folder_id': 'empty', 'state': 'empty'},
                                 {'folder_id': 'unknown', 'state': 'needs_check'}]
    assert '未自动清理' in messages[0]


def test_no_migrations_needs_no_cleanup_report(tmp_path):
    engine = Engine(Client(), Store(tmp_path / 'state.sqlite3'))
    with engine._directory_session():
        assert report_empty_folders(engine, [], 'kb', '', None, lambda _: None) == 0
    assert not (tmp_path / 'empty-folder-cleanup.json').exists()
