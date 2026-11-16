"""Request budgets use simulated transports, including saved-plan execution."""
from collections import Counter
import threading

import pytest

from ima_manager import plans
from ima_manager.api import ImaError
from ima_manager.sync import Engine, Store, scan, move_local_to_deleted
from test_updates import Cloud
from test_layout import batch_cloud_case, unreceipted_case


class CountingCloud(Cloud):
    def __init__(self):
        super().__init__()
        self.entries = {'': []}
        self.reads = Counter()
        self.name_requests = 0
        self.partial = False
        self.lose = False

    def listing(self, kb, folder=''):
        self.reads[folder] += 1
        return super().listing(kb, folder)

    def repeated_many(self, *args):
        self.name_requests += 1
        return super().repeated_many(*args)

    def call(self, endpoint, body, **kwargs):
        if endpoint != 'move_knowledge':
            return super().call(endpoint, body, **kwargs)
        self.calls.append((endpoint, body))
        assert 1 <= len(body['infos']) <= 10
        results = {}
        for index, info in enumerate(body['infos']):
            mid = info['media_id']
            if self.partial and index >= 3:
                results[mid] = {'ret_code': 1}
                continue
            src = next(items for items in self.entries.values() if any(r['media_id'] == mid for r in items))
            item = next(r for r in src if r['media_id'] == mid)
            src.remove(item)
            self.entries[body.get('dst_folder_id', '')].append(item)
            results[mid] = {'ret_code': 0}
        self.partial = False
        if self.lose:
            self.lose = False
            raise ImaError('response lost')
        return {'move_results': results}


def test_hundred_uploads_use_one_name_batch_and_two_writes_each(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    for index in range(100):
        (source / f'{index}.txt').write_bytes(b'content')
    cloud = CountingCloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert all(row['status'] == 'done' for row in rows)
    assert cloud.name_requests == 1
    assert cloud.reads == {}
    assert Counter(endpoint for endpoint, _ in cloud.calls) == {'create_media': 100, 'add_knowledge': 100}
    assert len(cloud.uploaded) == 100


def test_upload_success_keeps_complete_directory_snapshot(tmp_path):
    from ima_manager.tree import DirectorySession
    cloud = CountingCloud()
    client = DirectorySession(cloud)
    client.listing('kb')
    for index in range(100):
        client.call('add_knowledge', dict(knowledge_base_id='kb', media_id=str(index), title=f'{index}.txt', media_type=13))
        assert client.repeated('kb', '', f'{index}.txt', 13)
        assert len(client.listing('kb')) == index + 1
    assert cloud.reads == {'': 1}


def test_saved_plan_reuses_old_directory_after_new_engine_load(tmp_path, monkeypatch):
    source, cloud, engine = batch_cloud_case(tmp_path, monkeypatch, 101)
    (source / 'new.txt').unlink()
    runtime = tmp_path / 'runtime'
    settings = {}
    before = plans.receipt_revision(engine, 'kb'), plans.layout_revision(source)
    rows = engine.plan(source, 'kb')
    identity = plans.save(runtime, engine, settings, source, 'kb', '', True, {'files': rows}, before)
    replacement = Engine(cloud, engine.store)
    artifact = plans.load(runtime, replacement, settings, source, 'kb', '', True, expected_id=identity)
    cloud.reads.clear()
    replacement.execute(artifact['files'], 'kb')
    assert cloud.reads.count('old-folder') == 0
    assert [len(batch) for batch in cloud.move_batches] == [10] * 10 + [1]


def test_discovery_does_not_read_unrelated_categories(tmp_path):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    cloud.entries[''].append(dict(media_id='unrelated', title='项目管理规定', media_type=99))
    cloud.entries['unrelated'] = []
    engine.plan(source, 'kb')
    assert 'unrelated' not in cloud.reads


def test_subfolder_mapping_is_reused_by_next_engine(tmp_path):
    source = tmp_path / 'source'
    (source / 'project').mkdir(parents=True)
    (source / 'project/a.txt').write_bytes(b'content')
    cloud = CountingCloud()
    cloud.entries = {'': [dict(media_id='project', title='project', media_type=99)], 'project': []}
    store = Store(tmp_path / 'state.sqlite3')
    Engine(cloud, store).plan(source, 'kb', 'project')
    assert cloud.reads[''] == 1
    cloud.reads.clear()
    Engine(cloud, store).plan(source, 'kb', 'project')
    assert cloud.reads[''] == 0


def deletion_case(tmp_path, count=21):
    source = tmp_path / 'source'
    source.mkdir()
    cloud = CountingCloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    for index in range(count):
        (source / f'{index:03}.txt').write_bytes(b'content')
    for index, row in enumerate(scan(source)):
        mid = f'media-{index}'
        cloud.entries[''].append(dict(media_id=mid, title=row['name'], media_type=13))
        engine.store.put(engine.account, 'kb', '', row, 'done', mid)
        move_local_to_deleted(source, row['path'])
    return source, cloud, engine


def test_twenty_one_deletes_use_three_batches_and_commit_receipts(tmp_path):
    source, cloud, engine = deletion_case(tmp_path)
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert all(row['status'] == 'deleted' for row in rows)
    assert [len(body['infos']) for endpoint, body in cloud.calls if endpoint == 'move_knowledge'] == [10, 10, 1]
    with engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM receipts WHERE status='archived'").fetchone()[0] == 21
    assert all(count == 1 for count in cloud.reads.values())


@pytest.mark.parametrize('failure', ['partial', 'lost', 'stop'])
def test_delete_resume_does_not_move_confirmed_files_again(tmp_path, failure):
    source, cloud, engine = deletion_case(tmp_path)
    stop = threading.Event()
    cloud.partial = failure == 'partial'
    cloud.lose = failure == 'lost'
    def notify(message):
        if failure == 'stop' and message.startswith('云端批量逻辑删除接口已确认'):
            stop.set()
    rows = engine.plan(source, 'kb')
    if failure == 'stop':
        engine.execute(rows, 'kb', stop=stop, notify=notify)
        with engine.store.connect() as db:
            assert db.execute("SELECT COUNT(*) FROM receipts WHERE status='archived'").fetchone()[0] == 10
    else:
        with pytest.raises(ImaError):
            engine.execute(rows, 'kb')
    moved = {item['media_id'] for folder, items in cloud.entries.items() if folder for item in items}
    previous = len(cloud.calls)
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert all(row['status'] == 'deleted' for row in rows)
    later = {info['media_id'] for endpoint, body in cloud.calls[previous:] if endpoint == 'move_knowledge' for info in body['infos']}
    assert not moved.intersection(later)
