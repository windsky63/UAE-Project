import copy
import hashlib
import json
from pathlib import Path
import threading

import pytest

from ima_manager.api import ImaError
from ima_manager.layout import destination, migrate_local, read_moves
from ima_manager.sync import Engine, Store, scan


NAME = '30201_50150_LT_HE-H4-213-13-60-001.pdf'


def unreceipted_case(tmp_path, count=1):
    source = tmp_path / 'relocated-source'
    new = destination(source, Path(NAME))
    new.parent.mkdir(parents=True)
    new.write_bytes(b'baseline')
    cloud = Cloud()
    for index in range(2, count + 1):
        name = NAME.replace('001.pdf', f'{index:03}.pdf')
        new.with_name(name).write_bytes(name.encode())
        cloud.entries['old-folder'].append({'media_id': f'media-{index}', 'title': name, 'media_type': 1})
    engine = Engine(cloud, Store(tmp_path / 'fresh-runtime/state.sqlite3'))
    return source, new, cloud, engine


@pytest.mark.parametrize('selected', ['', 'root'])
def test_no_state_preview_finds_old_cloud_then_moves_without_upload(tmp_path, selected):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    before = engine.store.path.read_bytes()
    rows = engine.plan(source, 'kb', selected)
    assert rows[0]['status'] == 'ready_migrate'
    assert rows[0]['migration_unreceipted']
    assert rows[0]['migration_old_folder'] == 'old-folder'
    assert rows[0]['migration_media_id'] == 'media'
    assert engine.store.path.read_bytes() == before
    assert not (source / '.ima-state').exists()
    assert cloud.events == []
    assert len(cloud.reads) == len(set(cloud.reads))
    engine.execute(rows, 'kb', selected)
    assert rows[0]['status'] == 'unchanged'
    assert cloud.entries['old-folder'] == []
    assert engine.store.get(engine.account, 'kb', rows[0]['target_folder_id'], rows[0]) == ('done', 'media')
    assert 'create_media' not in cloud.events and 'add_knowledge' not in cloud.events
    engine = Engine(cloud, Store(engine.store.path))
    assert engine.plan(source, 'kb', selected)[0]['status'] == 'unchanged'


def test_unreceipted_move_lost_response_recovers_from_durable_intent(tmp_path):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    cloud.lose_move = True
    with pytest.raises(ImaError, match='response lost'):
        engine.execute(engine.plan(source, 'kb'), 'kb')
    assert cloud.events.count('move_knowledge') == 1
    restarted = Engine(cloud, Store(engine.store.path))
    rows = restarted.plan(source, 'kb')
    assert rows[0]['status'] == 'ready_migrate'
    restarted.execute(rows, 'kb')
    assert rows[0]['status'] == 'unchanged'
    assert cloud.events.count('move_knowledge') == 1


def test_resume_old_oversized_rejected_migration_in_ten_item_batches(tmp_path, monkeypatch):
    from ima_manager import cloud_layout, scope
    source, new, cloud, engine = unreceipted_case(tmp_path, count=21)
    rows = engine.plan(source, 'kb')
    # Reproduce the previously deployed 50-item setting against the real limit.
    with monkeypatch.context() as guard:
        guard.setattr(cloud_layout, 'MOVE_BATCH_SIZE', 50)
        guard.setattr(scope, 'MOVE_BATCH_SIZE', 50)
        with pytest.raises(ImaError, match='no more than 10') as error:
            engine.execute(rows, 'kb')
    assert error.value.code == 51
    assert cloud.move_batches == []
    assert len(cloud.entries['old-folder']) == 21
    with engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM layout_moves WHERE phase='pending'").fetchone()[0] == 21
    restarted = Engine(cloud, Store(engine.store.path))
    rows = restarted.plan(source, 'kb')
    assert all(row['status'] == 'ready_migrate' for row in rows)
    restarted.execute(rows, 'kb')
    assert [len(batch) for batch in cloud.move_batches] == [10, 10, 1]
    assert len({media for batch in cloud.move_batches for media in batch}) == 21
    assert all(row['status'] == 'unchanged' for row in rows)
    assert not any(event in {'create_media', 'add_knowledge'} for event in cloud.events)
    assert all(row['status'] == 'unchanged' for row in restarted.plan(source, 'kb'))


def test_failed_migration_preview_reads_one_old_parent_not_twenty_empty_targets(tmp_path):
    from ima_manager.cloud_layout import prepare_unreceipted
    source, new, cloud, engine = unreceipted_case(tmp_path, count=20)
    for index, path in enumerate(sorted(new.parent.glob('*.pdf'))):
        destination_path = path.parent / f'leaf-{index}' / path.name
        destination_path.parent.mkdir()
        path.rename(destination_path)
    rows = engine.plan(source, 'kb')
    assert all(row['status'] == 'ready_migrate' for row in rows)
    prepare_unreceipted(engine, source, 'kb', '', rows, threading.Event())
    with engine.store.connect() as db:
        assert db.execute('SELECT COUNT(DISTINCT new_folder) FROM layout_moves').fetchone()[0] == 20
    cloud.reads.clear()
    rows = engine.plan(source, 'kb')
    assert all(row['status'] == 'ready_migrate' for row in rows)
    assert cloud.reads == ['old-folder']


@pytest.mark.parametrize('change', ['cloud-duplicate', 'local-duplicate', 'busy'])
def test_unreceipted_ambiguity_never_falls_back_to_new_upload(tmp_path, change):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    if change == 'cloud-duplicate':
        cloud.entries['old-folder'].append({'media_id': 'second', 'title': NAME, 'media_type': 1})
    elif change == 'local-duplicate':
        other = source / '图纸/another' / NAME
        other.parent.mkdir(parents=True)
        other.write_bytes(b'different')
    else:
        engine.store.put(engine.account, 'kb', 'old-folder', scan(source)[0], 'uncertain', 'media')
    rows = engine.plan(source, 'kb')
    assert all(row['status'] == ('uncertain' if change == 'busy' else 'conflict') for row in rows)
    engine.execute(rows, 'kb')
    assert cloud.events == []


@pytest.mark.parametrize('change', ['content', 'cloud-identity'])
def test_unreceipted_migration_rechecks_reviewed_local_and_cloud_identity(tmp_path, change):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    rows = engine.plan(source, 'kb')
    if change == 'content':
        new.write_bytes(b'changed')
    else:
        cloud.entries['old-folder'][0]['media_id'] = 'replacement'
        engine.directory_snapshot = {}  # Exercise execution without a saved snapshot.
    with pytest.raises(ImaError, match='变化'):
        engine.execute(rows, 'kb')
    assert cloud.events == []


def test_unreceipted_batch_reuses_each_directory_read_and_moves_before_upload(tmp_path, monkeypatch):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    names = [NAME.replace('001.pdf', f'{i:03}.pdf') for i in range(1, 21)]
    for name in names[1:]:
        new.with_name(name).write_bytes(name.encode())
        cloud.entries['old-folder'].append({'media_id': name, 'title': name, 'media_type': 1})
    # A genuinely new local file remains an upload after the cloud inventory.
    new.with_name(NAME.replace('001.pdf', '999.pdf')).write_bytes(b'new')
    def upload(row, kb, folder):
        cloud.events.append('upload:' + row['name'])
        row.update(status='done')
    monkeypatch.setattr(engine, '_one', upload)
    rows = engine.plan(source, 'kb')
    assert sum(row['status'] == 'ready_migrate' for row in rows) == 20
    assert sum(row['status'] == 'ready' for row in rows) == 1
    assert len(cloud.reads) == len(set(cloud.reads))
    engine.execute(rows, 'kb')
    assert [len(batch) for batch in cloud.move_batches] == [10, 10]
    assert cloud.events.count('move_knowledge') == 2
    assert sum(event.startswith('upload:') for event in cloud.events) == 1
    assert cloud.events.index('move_knowledge') < next(i for i, e in enumerate(cloud.events) if e.startswith('upload:'))
    assert sum(row['status'] == 'unchanged' for row in rows) == 20


def test_unreceipted_execution_preserves_unselected_old_cloud_files(tmp_path):
    source, new, cloud, engine = unreceipted_case(tmp_path)
    second = NAME.replace('001.pdf', '002.pdf')
    new.with_name(second).write_bytes(b'second')
    cloud.entries['old-folder'].append({'media_id': 'second', 'title': second, 'media_type': 1})
    rows = engine.plan(source, 'kb')
    selected = [row for row in rows if row['name'] == NAME]
    engine.execute(selected, 'kb')
    assert selected[0]['status'] == 'unchanged'
    assert cloud.entries['old-folder'] == [{'media_id': 'second', 'title': second, 'media_type': 1}]


def test_preview_separates_correct_cloud_files_old_cloud_files_and_new_uploads(tmp_path):
    from collections import Counter
    from ima_manager.tree import CloudTree
    source, new, cloud, engine = unreceipted_case(tmp_path)
    # Three old cloud files, two at the correct destination, and one new file.
    for i in range(2, 7):
        new.with_name(NAME.replace('001.pdf', f'{i:03}.pdf')).write_bytes(str(i).encode())
    for i in (2, 3):
        cloud.entries['old-folder'].append({'media_id': f'old-{i}',
            'title': NAME.replace('001.pdf', f'{i:03}.pdf'), 'media_type': 1})
    target = CloudTree(engine.client, 'kb', create=engine._directory).parent(new.relative_to(source).parent.parts)
    for i in (4, 5):
        cloud.entries[target].append({'media_id': f'correct-{i}',
            'title': NAME.replace('001.pdf', f'{i:03}.pdf'), 'media_type': 1})
    (new.parent / 'blocked.zip').write_bytes(b'PK')
    cloud.events.clear()
    rows = engine.plan(source, 'kb')
    assert Counter(row['status'] for row in rows) == {
        'ready_migrate': 3, 'cloud_existing': 2, 'ready': 1, 'blocked': 1}
    assert cloud.events == []


def old_copy(tmp_path, payload=b'old'):
    source = tmp_path / 'source'
    old = source / '图纸/旧分类' / NAME
    old.parent.mkdir(parents=True)
    old.write_bytes(payload)
    return source, old, tmp_path / 'runtime'


def test_local_moves_existing_files_without_reading_downloads_and_removes_empty_old_folders(tmp_path):
    source, old, runtime = old_copy(tmp_path)
    sha = hashlib.sha256(old.read_bytes()).hexdigest()
    states = {'identity': {'destination': str(old), 'sha256': sha}}
    new = destination(source, old)
    preview = migrate_local(source, runtime, states, dry_run=True)
    assert preview[0]['status'] == 'would_migrate' and old.exists() and not new.exists()
    assert not (source / '.ima-state').exists()
    result = migrate_local(source, runtime, states)
    assert result[0]['status'] == 'done' and new.read_bytes() == b'old'
    assert not old.exists() and not old.parent.exists()
    assert states['identity']['destination'] == str(new)
    assert migrate_local(source, runtime, states) == []


def test_local_collision_preserves_all_distinct_contents_and_backs_up_duplicate(tmp_path):
    source, old, runtime = old_copy(tmp_path, b'one')
    other = source / '图纸/另一分类' / NAME
    other.parent.mkdir(parents=True)
    other.write_bytes(b'two')
    same = source / '其他图纸/旧分类' / NAME
    same.parent.mkdir(parents=True)
    same.write_bytes(b'one')
    migrate_local(source, runtime, {})
    files = list(source.rglob('*.pdf'))
    assert sorted(p.read_bytes() for p in files) == [b'one', b'two']
    assert len(list(runtime.rglob('*.pdf'))) == 1
    assert all(e['status'] == 'done' for e in read_moves(source))


def test_local_rename_committed_before_response_recovers_intent(tmp_path, monkeypatch):
    source, old, runtime = old_copy(tmp_path)
    original = Path.rename
    def interrupted(path, target):
        result = original(path, target)
        if path == old:
            raise OSError('lost response after rename')
        return result
    with monkeypatch.context() as guard:
        guard.setattr(Path, 'rename', interrupted)
        with pytest.raises(OSError, match='lost response'):
            migrate_local(source, runtime, {})
    assert read_moves(source)[0]['status'] == 'pending'
    migrate_local(source, runtime, {})
    assert read_moves(source)[0]['status'] == 'done'
    assert destination(source, old).read_bytes() == b'old'


def test_migration_hash_cache_is_invalidated_when_source_changes_after_planning(tmp_path, monkeypatch):
    from ima_manager import layout
    source, old, runtime = old_copy(tmp_path)
    save = layout.atomic_json
    def change_after_intent(path, value):
        save(path, value)
        old.write_bytes(b'changed-after-planning')
    monkeypatch.setattr(layout, 'atomic_json', change_after_intent)
    with pytest.raises(ImaError, match='源文件已变化'):
        migrate_local(source, runtime, {})
    assert old.read_bytes() == b'changed-after-planning'
    assert not destination(source, old).exists()


class Cloud:
    client_id = 'layout-test'

    def __init__(self):
        self.entries = {'': [{'media_id': 'root', 'title': '图纸', 'media_type': 99}],
                        'root': [{'media_id': 'old-folder', 'title': '旧分类', 'media_type': 99}],
                        'old-folder': [{'media_id': 'media', 'title': NAME, 'media_type': 1}]}
        self.events = []
        self.reads = []
        self.move_batches = []
        self.lose_move = False

    def listing(self, kb, folder=''):
        self.reads.append(folder)
        return copy.deepcopy(self.entries[folder])

    def repeated_many(self, kb, folder, params):
        return {p['name']: any(e['title'] == p['name'] for e in self.entries[folder]) for p in params}

    def redact(self, text):
        return text

    def call(self, endpoint, body, **kwargs):
        self.events.append(endpoint)
        if endpoint == 'create_folder':
            identity = 'folder-' + str(len(self.entries))
            self.entries[body.get('folder_id', '')].append(
                {'media_id': identity, 'title': body['name'], 'media_type': 99})
            self.entries[identity] = []
            return {'media_id': identity}
        if endpoint == 'move_knowledge':
            if len(body['infos']) > 10:
                raise ImaError('ima 51: invalid MoveKnowledgeReq.Infos: value must contain no more than 10 item(s)',
                               code=51, endpoint=endpoint)
            media_ids = [info['media_id'] for info in body['infos']]
            self.move_batches.append(media_ids)
            for media in media_ids:
                old = next(items for items in self.entries.values() if any(e['media_id'] == media for e in items))
                own = next(e for e in old if e['media_id'] == media)
                old.remove(own)
                self.entries[body.get('dst_folder_id', '')].append(own)
            if self.lose_move:
                self.lose_move = False
                raise ImaError('move committed but response lost')
            return {'move_results': {media: {'ret_code': 0} for media in media_ids}}
        if endpoint == 'rename_knowledge':
            for items in self.entries.values():
                for item in items:
                    if item['media_id'] == body['media_id']:
                        item['title'] = body['name']
            return {}
        raise AssertionError(endpoint)


def cloud_case(tmp_path, monkeypatch):
    source, old, runtime = old_copy(tmp_path)
    cloud = Cloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    receipt = next(r for r in scan(source) if r['path'] == str(old))
    engine.store.put(engine.account, 'kb', 'old-folder', receipt, 'done', 'media')
    migrate_local(source, runtime, {})
    (source / 'new.txt').write_bytes(b'new')
    def upload(row, kb, folder):
        cloud.events.append('upload:' + row['name'])
        row.update(status='done')
    monkeypatch.setattr(engine, '_one', upload)
    return source, old, cloud, engine, receipt


def test_cloud_moves_receipted_identity_before_new_upload_and_rekeys_receipt(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    manual = {'media_id': 'manual', 'title': 'manual.pdf', 'media_type': 1}
    cloud.entries['old-folder'].append(manual)
    rows = engine.plan(source, 'kb')
    assert cloud.events == []
    migrated_plan = next(r for r in rows if r['name'] == NAME)
    assert migrated_plan['status'] == 'ready_migrate'
    assert migrated_plan['migration_old_path'] == str(old)
    assert '不重复上传' in migrated_plan['reason']
    assert next(r for r in rows if r['name'] == 'new.txt')['status'] == 'ready'
    assert engine.store.get(engine.account, 'kb', 'old-folder', receipt) == ('done', 'media')
    with engine.store.connect() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE name='layout_moves'").fetchone() is None
    engine.execute(rows, 'kb')
    assert cloud.events.index('move_knowledge') < cloud.events.index('upload:new.txt')
    assert not any(e == 'upload:' + NAME for e in cloud.events)
    moved = next(r for r in rows if r['name'] == NAME)
    assert moved['status'] == 'unchanged'
    assert engine.store.get(engine.account, 'kb', moved['target_folder_id'], moved) == ('done', 'media')
    assert engine.store.get(engine.account, 'kb', 'old-folder', receipt)[0] == 'migrated'
    assert cloud.entries['old-folder'] == [manual]
    before = cloud.events.count('move_knowledge')
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert cloud.events.count('move_knowledge') == before


def test_cloud_lost_move_response_blocks_new_upload_then_recovers_without_reupload(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    cloud.lose_move = True
    with pytest.raises(ImaError, match='response lost'):
        engine.execute(engine.plan(source, 'kb'), 'kb')
    assert not any(e.startswith('upload:') for e in cloud.events)
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert cloud.events.count('move_knowledge') == 1
    assert 'upload:new.txt' in cloud.events and 'upload:' + NAME not in cloud.events


def test_cloud_destination_collision_blocks_all_new_uploads(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    from ima_manager.tree import CloudTree
    tree = CloudTree(engine.client, 'kb', create=engine._directory)
    new = destination(source, old)
    folder = tree.parent(new.relative_to(source).parent.parts)
    cloud.entries[folder].append({'media_id': 'manual', 'title': NAME, 'media_type': 1})
    with pytest.raises(ImaError, match='同名'):
        engine.execute(engine.plan(source, 'kb'), 'kb')
    assert not any(e.startswith('upload:') for e in cloud.events)
    assert cloud.entries['old-folder'][0]['media_id'] == 'media'


def test_cancel_during_local_migration_leaves_remaining_files_unmoved(tmp_path):
    source, old, runtime = old_copy(tmp_path)
    second = old.with_name(NAME.replace('001.pdf', '002.pdf'))
    second.write_bytes(b'second')
    stop = threading.Event()
    with pytest.raises(ImaError, match='安全停止'):
        migrate_local(source, runtime, {}, stop=stop, notify=lambda _: stop.set())
    assert sum(p.exists() for p in [old, second]) == 1
    migrate_local(source, runtime, {})
    assert not old.exists() and not second.exists()


def test_previous_release_backup_mapping_preserves_numbered_cloud_destination(tmp_path):
    source, old, runtime = old_copy(tmp_path)
    new = destination(source, old).with_name(Path(NAME).stem + '-2.pdf')
    new.parent.mkdir(parents=True, exist_ok=True)
    old.rename(new)
    record = runtime / 'drawing-layout-backups/identity/origin.json'
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({'original_path': str(old), 'destination': str(new),
                                 'sha256': hashlib.sha256(b'old').hexdigest()}), encoding='utf-8')
    migrate_local(source, runtime, {})
    assert read_moves(source)[0]['old'] == str(old)
    assert read_moves(source)[0]['new'] == str(new)
    assert read_moves(source)[0]['status'] == 'done'


@pytest.mark.parametrize('changed_before_migration', [False, True])
def test_cloud_moves_old_version_before_running_normal_update(tmp_path, monkeypatch, changed_before_migration):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    destination(source, old).write_bytes(b'updated version')
    if changed_before_migration:
        # The local version was newer already when the previous release moved it.
        from ima_manager.config import atomic_json
        from ima_manager.layout import manifest_path
        moves = read_moves(source)
        moves[0]['sha256'] = hashlib.sha256(b'updated version').hexdigest()
        atomic_json(manifest_path(source), moves)
    def update(row, kb, folder):
        assert row['old_media_id'] == 'media'
        cloud.events.append('update')
        row['status'] = 'done'
    monkeypatch.setattr(engine, '_update', update)
    rows = engine.plan(source, 'kb')
    planned = next(r for r in rows if r['name'] == NAME)
    assert planned['status'] == 'ready_migrate'
    assert '版本更新' in planned['reason']
    engine.execute(rows, 'kb')
    assert cloud.events.index('move_knowledge') < cloud.events.index('update')


def test_cloud_duplicate_with_matching_receipts_is_archived_without_upload(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    # An earlier upload already created an identical file in the new layout.
    from ima_manager.tree import CloudTree
    tree = CloudTree(engine.client, 'kb', create=engine._directory)
    new = destination(source, old)
    target = tree.parent(new.relative_to(source).parent.parts)
    cloud.entries[target].append({'media_id': 'already-new', 'title': NAME, 'media_type': 1})
    engine.store.put(engine.account, 'kb', target, dict(receipt, path=str(new)), 'done', 'already-new')
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert not any(e == 'upload:' + NAME for e in cloud.events)
    assert cloud.entries[target] == [{'media_id': 'already-new', 'title': NAME, 'media_type': 1}]
    with engine.store.connect() as db:
        state, archived = db.execute('SELECT status,note FROM receipts WHERE path=?', (str(old),)).fetchone()
    assert state == 'migrated_duplicate'
    assert cloud.entries[archived][0]['media_id'] == 'media'


def test_cloud_numbered_collision_is_renamed_before_move(tmp_path, monkeypatch):
    source, old, runtime = old_copy(tmp_path)
    cloud = Cloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    receipt = next(r for r in scan(source) if r['path'] == str(old))
    engine.store.put(engine.account, 'kb', 'old-folder', receipt, 'done', 'media')
    base = destination(source, old)
    base.parent.mkdir(parents=True, exist_ok=True)
    base.write_bytes(b'different-content')
    state = {'identity': {'destination': str(old), 'base_destination': str(old), 'sha256': receipt['sha256']}}
    migrate_local(source, runtime, state)
    assert state['identity']['base_destination'] == str(base)
    assert state['identity']['identity_tag'] == '-1'
    from ima_manager.tree import CloudTree
    folder = CloudTree(engine.client, 'kb', create=engine._directory).parent(base.relative_to(source).parent.parts)
    cloud.entries[folder].append({'media_id': 'manual', 'title': NAME, 'media_type': 1})
    # Select only the migrated file; an unrelated conflicting file is untouched.
    rows = [r for r in engine.plan(source, 'kb') if r['name'] == base.stem + '-1.pdf']
    engine.execute(rows, 'kb')
    assert rows[0]['status'] == 'unchanged'
    assert cloud.events.index('rename_knowledge') < cloud.events.index('move_knowledge')
    assert {r['title'] for r in cloud.entries[folder]} == {NAME, base.stem + '-1.pdf'}


def test_old_flat_cloud_upload_moves_using_its_exact_receipt(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    cloud.entries[''] = cloud.entries['old-folder']
    cloud.entries['old-folder'] = []
    with engine.store.connect() as db:
        db.execute("UPDATE receipts SET folder='' WHERE path=?", (str(old),))
    rows = engine.plan(source, 'kb')
    engine.execute(rows, 'kb')
    assert any(e == 'move_knowledge' for e in cloud.events)
    assert not any(e == 'upload:' + NAME for e in cloud.events)
    assert not any(e.get('media_id') == 'media' for e in cloud.entries[''])


def test_migration_preview_missing_media_is_blocked_instead_of_new_upload(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    cloud.entries['old-folder'] = []
    rows = engine.plan(source, 'kb')
    item = next(r for r in rows if r['name'] == NAME)
    assert item['status'] == 'blocked'
    assert '媒体未找到' in item['reason']
    assert cloud.events == []


def test_migration_preview_reads_only_old_parent_not_new_or_old_directory_trees(tmp_path, monkeypatch):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    (source / 'new.txt').unlink()
    original = cloud.listing
    reads = []
    def listing(kb, folder=''):
        reads.append(folder)
        assert folder == 'old-folder', 'known migration must not crawl the old or new directory tree'
        return original(kb, folder)
    monkeypatch.setattr(cloud, 'listing', listing)
    progress = []
    engine.plan_notify = progress.append
    rows = engine.plan(source, 'kb')
    assert reads == ['old-folder']
    assert rows[0]['status'] == 'ready_migrate'
    assert cloud.events == []
    assert any('本地扫描完成' in item for item in progress)
    assert any('迁移预览核对完成' in item for item in progress)
    assert '预览计划完成' in progress[-1]


@pytest.mark.parametrize('change', ['account', 'kb'])
def test_migration_preview_can_discover_current_cloud_without_using_other_scope_receipts(tmp_path, monkeypatch, change):
    source, old, cloud, engine, receipt = cloud_case(tmp_path, monkeypatch)
    with engine.store.connect() as db:
        db.execute(f"UPDATE receipts SET {change}=?", ('different',))
    rows = engine.plan(source, 'kb')
    item = next(r for r in rows if r['name'] == NAME)
    assert item['status'] == 'ready_migrate'
    assert item['migration_unreceipted']
    assert item['migration_old_path'] == item['path']
    assert cloud.events == []


def batch_cloud_case(tmp_path, monkeypatch, count=8):
    source, old, runtime = old_copy(tmp_path)
    for index in range(2, count + 1):
        # The confirmed drawing subtype only accepts serials 001-099; use
        # valid filename suffixes to exercise more than two 10-item batches.
        old.with_name(Path(NAME).stem + f'-{index}.pdf').write_bytes(b'old')
    cloud = Cloud()
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    cloud.entries['old-folder'] = []
    for index, row in enumerate(scan(source)):
        media = f'media-{index}'
        cloud.entries['old-folder'].append({'media_id': media, 'title': row['name'], 'media_type': 1})
        engine.store.put(engine.account, 'kb', 'old-folder', row, 'done', media)
    migrate_local(source, runtime, {})
    (source / 'new.txt').write_bytes(b'new')
    def upload(row, kb, folder):
        assert len(cloud.entries['old-folder']) == 0
        cloud.events.append('upload:' + row['name'])
        row['status'] = 'done'
    monkeypatch.setattr(engine, '_one', upload)
    return source, cloud, engine


@pytest.mark.parametrize('count, sizes', [(8, [8]), (10, [10]), (11, [10, 1]),
                                         (20, [10, 10]), (101, [10] * 10 + [1])])
def test_cloud_migration_batches_same_directory_and_bounds_requests(tmp_path, monkeypatch, count, sizes):
    source, cloud, engine = batch_cloud_case(tmp_path, monkeypatch, count)
    rows = engine.plan(source, 'kb')
    cloud.reads.clear()
    engine.execute(rows, 'kb')
    assert [len(batch) for batch in cloud.move_batches] == sizes
    assert sum(row['status'] == 'unchanged' for row in rows) == count
    assert cloud.events[-1] == 'upload:new.txt'
    # Batch count and file count do not increase source-directory reads.
    assert cloud.reads.count('old-folder') == 0
    with engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM layout_moves WHERE phase='done'").fetchone()[0] == count
    previous = len(cloud.move_batches)
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert len(cloud.move_batches) == previous


def test_cloud_batch_lost_response_recovers_all_without_replaying(tmp_path, monkeypatch):
    source, cloud, engine = batch_cloud_case(tmp_path, monkeypatch)
    cloud.lose_move = True
    with pytest.raises(ImaError, match='response lost'):
        engine.execute(engine.plan(source, 'kb'), 'kb')
    assert len(cloud.move_batches) == 1 and len(cloud.move_batches[0]) == 8
    assert not any(event.startswith('upload:') for event in cloud.events)
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert len(cloud.move_batches) == 1
    assert cloud.events[-1] == 'upload:new.txt'


def test_cloud_partial_batch_only_retries_remaining_identities(tmp_path, monkeypatch):
    source, cloud, engine = batch_cloud_case(tmp_path, monkeypatch)
    call = cloud.call
    def partial(endpoint, body, **kwargs):
        if endpoint != 'move_knowledge':
            return call(endpoint, body, **kwargs)
        actual = dict(body, infos=body['infos'][:3])
        result = call(endpoint, actual, **kwargs)
        result['move_results'].update({info['media_id']: {'ret_code': 1} for info in body['infos'][3:]})
        return result
    with monkeypatch.context() as guard:
        guard.setattr(cloud, 'call', partial)
        with pytest.raises(ImaError, match='未确认本批全部'):
            engine.execute(engine.plan(source, 'kb'), 'kb')
    assert not any(event.startswith('upload:') for event in cloud.events)
    completed = set(cloud.move_batches[0])
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert len(cloud.move_batches) == 2
    assert len(cloud.move_batches[1]) == 5
    assert completed.isdisjoint(cloud.move_batches[1])


def test_cloud_stop_after_batch_preserves_intents_and_skips_next_batch(tmp_path, monkeypatch):
    source, cloud, engine = batch_cloud_case(tmp_path, monkeypatch, 101)
    stop = threading.Event()
    def notify(message):
        if message.startswith('云端批量迁移接口已确认'):
            stop.set()
    with pytest.raises(ImaError, match='安全停止'):
        engine.execute(engine.plan(source, 'kb'), 'kb', stop=stop, notify=notify)
    assert [len(batch) for batch in cloud.move_batches] == [10]
    assert not any(event.startswith('upload:') for event in cloud.events)
    completed = set(cloud.move_batches[0])
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert all(completed.isdisjoint(batch) for batch in cloud.move_batches[1:])
    assert sum(map(len, cloud.move_batches)) == 101


def test_cloud_batches_keep_merged_duplicates_in_receipt_order(tmp_path, monkeypatch):
    source, old, runtime = old_copy(tmp_path)
    other = source / '图纸/第二旧分类' / NAME
    other.parent.mkdir(parents=True)
    other.write_bytes(b'old')
    second_name = Path(NAME).stem + '-2.pdf'
    old.with_name(second_name).write_bytes(b'old')
    other.with_name(second_name).write_bytes(b'old')
    cloud = Cloud()
    cloud.entries['root'].append({'media_id': 'old-second', 'title': '第二旧分类', 'media_type': 99})
    cloud.entries['old-folder'] = []
    cloud.entries['old-second'] = []
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    rows = {row['path']: row for row in scan(source)}
    # Interleave receipts so a duplicate precedes another group's successful
    # identity. Batching those duplicate names would delay its target receipt.
    for index, (path, folder) in enumerate([
            (old.with_name(second_name), 'old-folder'), (other, 'old-second'),
            (old, 'old-folder'), (other.with_name(second_name), 'old-second')]):
        media = f'media-{index}'
        cloud.entries[folder].append({'media_id': media, 'title': path.name, 'media_type': 1})
        engine.store.put(engine.account, 'kb', folder, rows[str(path)], 'done', media)
    migrate_local(source, runtime, {})
    engine.execute(engine.plan(source, 'kb'), 'kb')
    with engine.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM layout_moves WHERE phase='done'").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM layout_moves WHERE phase='done_duplicate'").fetchone()[0] == 2
    assert all(len(batch) == 1 for batch in cloud.move_batches)
    assert cloud.entries['old-folder'] == [] and cloud.entries['old-second'] == []


def test_cloud_batches_never_mix_target_directories(tmp_path, monkeypatch):
    source, old, runtime = old_copy(tmp_path)
    for name in [Path(NAME).stem + '-2.pdf', NAME.replace('H4-', 'H2-'),
                 (Path(NAME).stem + '-2.pdf').replace('H4-', 'H2-')]:
        old.with_name(name).write_bytes(b'old')
    cloud = Cloud()
    cloud.entries['old-folder'] = []
    engine = Engine(cloud, Store(tmp_path / 'state.sqlite3'))
    titles = {}
    for index, row in enumerate(scan(source)):
        media = f'media-{index}'
        titles[media] = row['name']
        cloud.entries['old-folder'].append({'media_id': media, 'title': row['name'], 'media_type': 1})
        engine.store.put(engine.account, 'kb', 'old-folder', row, 'done', media)
    migrate_local(source, runtime, {})
    engine.execute(engine.plan(source, 'kb'), 'kb')
    assert [len(batch) for batch in cloud.move_batches] == [2, 2]
    assert all(len({'H4' if 'H4-' in titles[media] else 'H2' for media in batch}) == 1
               for batch in cloud.move_batches)


def test_execution_does_not_rescan_or_migrate_files_outside_selected_plan(tmp_path, monkeypatch):
    from ima_manager import sync
    source, cloud, engine = batch_cloud_case(tmp_path, monkeypatch)
    rows = [row for row in engine.plan(source, 'kb') if row['status'] == 'ready_migrate'][:2]
    def no_scan(*args, **kwargs):
        raise AssertionError('migration must not trigger another full local scan')
    monkeypatch.setattr(sync, 'scan', no_scan)
    engine.execute(rows, 'kb')
    assert [len(batch) for batch in cloud.move_batches] == [2]
    assert len(cloud.entries['old-folder']) == 6
    assert all(row['status'] == 'unchanged' for row in rows)
    assert not any(event.startswith('upload:') for event in cloud.events)
