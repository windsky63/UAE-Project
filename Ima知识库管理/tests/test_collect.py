import hashlib
from contextlib import closing
import json
import os
from pathlib import Path
import subprocess
import sqlite3
import sys
import zipfile
from unittest.mock import Mock

from openpyxl import Workbook
import pytest

from ima_manager.api import ImaError
from ima_manager.collect import collect_documents, prepare_documents
from ima_manager.sync import move_local_to_deleted

HEADERS = ['文件名', '业务类型', '业务专业', '目录', '本地路径', '所属压缩包文件名', '包内完整路径',
           '工作区', '内容ID', '所属压缩包内容ID']


def write_ledger(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = '文件台账'
    sheet.append(['标题'])
    sheet.append(HEADERS)
    for row in rows:
        sheet.append([row.get(k) for k in HEADERS])
    book.save(path)
    book.close()


@pytest.fixture
def case(tmp_path):
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    source = tmp_path / '待同步'
    ledger = tmp_path / '文件台账.xlsx'
    origin = downloads / 'PP-测试规定.pdf'
    origin.write_bytes(b'%PDF-original-unchanged')
    row = {'文件名': origin.name, '业务类型': '项目程序文件', '业务专业': '项目管理',
           '目录': '项目', '本地路径': 'downloads/' + origin.name}
    write_ledger(ledger, [row])
    runtime = tmp_path / 'runtime'
    return downloads, source, ledger, origin, row, runtime


def run(case, **kwargs):
    downloads, source, ledger, origin, row, runtime = case
    return collect_documents(ledger, downloads, source, runtime=runtime, **kwargs)


def test_copy_preserves_source_bytes_and_mtime_and_is_incremental(case):
    downloads, source, ledger, origin, row, runtime = case
    before = origin.read_bytes(), origin.stat().st_mtime_ns
    report = run(case)
    assert report['counts'] == {'copied': 1}
    destination = Path(report['files'][0]['destination'])
    assert destination.read_bytes() == before[0]
    assert (origin.read_bytes(), origin.stat().st_mtime_ns) == before
    assert run(case)['counts'] == {'unchanged': 1}


@pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing')
@pytest.mark.parametrize(('exclusive', 'release'), [(False, True), (True, True), (True, False)])
def test_collection_coexists_with_nas_reader_and_waits_for_exclusive_handle(case, exclusive, release):
    from concurrent.futures import ThreadPoolExecutor
    downloads, source, ledger, origin, row, runtime = case
    first = run(case)
    destination = Path(first['files'][0]['destination'])
    old_bytes = destination.read_bytes()
    origin.write_bytes(b'%PDF-updated-for-concurrent-reader')
    script = '''
import ctypes, sys
from ctypes import wintypes
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
kernel.CreateFileW.restype = wintypes.HANDLE
kernel.CloseHandle.argtypes = [wintypes.HANDLE]
handle = kernel.CreateFileW(sys.argv[1], 0x80000000, int(sys.argv[2]), None, 3, 0, None)
if handle == wintypes.HANDLE(-1).value: raise ctypes.WinError(ctypes.get_last_error())
print('ready', flush=True)
sys.stdin.readline()
kernel.CloseHandle(handle)
'''
    process = subprocess.Popen([sys.executable, '-c', script, str(origin), '0' if exclusive else '1'],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True)
    messages = []
    try:
        assert process.stdout.readline().strip() == 'ready'
        def notify(message):
            messages.append(message)
            if release and '占用' in message and process.poll() is None:
                process.stdin.write('\n')
                process.stdin.flush()
        with ThreadPoolExecutor(max_workers=1) as pool:
            report = pool.submit(run, case, notify=notify).result(timeout=15)
        assert report['counts'] == ({'updated': 1} if release else {'error': 1})
        assert destination.read_bytes() == (b'%PDF-updated-for-concurrent-reader' if release else old_bytes)
        assert list(destination.parent.glob('*.pdf')) == [destination]
        assert not (source / '删除/更新旧版').exists()
        assert any('占用' in message for message in messages) == exclusive
    finally:
        if process.poll() is None:
            process.communicate('\n', timeout=10)
        else:
            process.communicate(timeout=10)
    if not release:
        assert run(case)['counts'] == {'updated': 1}
        assert destination.read_bytes() == origin.read_bytes()


def test_dry_run_does_not_create_copy_or_success_manifest(case):
    downloads, source, ledger, origin, row, runtime = case
    report = run(case, dry_run=True)
    assert report['counts'] == {'would_copy': 1}
    assert not source.exists()
    assert not (runtime / 'collection-state.json').exists()


def test_default_filter_matches_packager_and_deduplicates(case):
    downloads, source, ledger, origin, row, runtime = case
    write_ledger(ledger, [row, row, dict(row, 文件名='ADNOCGP-WTRAN-1.pdf'),
                          dict(row, 文件名='ordinary.pdf', 业务类型='其他'),
                          dict(row, 文件名='未命名')])
    report = run(case)
    assert report['selected_rows'] == 3
    assert report['counts'] == {'copied': 1, 'duplicate': 1, 'placeholder': 1}


def test_all_disciplines_are_always_selected_despite_legacy_settings(case, monkeypatch):
    import ima_manager.collect as collection
    downloads, source, ledger, origin, row, runtime = case
    write_ledger(ledger, [dict(row, 业务专业='质量管理')])
    monkeypatch.setattr(collection, 'RUNTIME', runtime)
    report = prepare_documents({'ledger_path': str(ledger), '_legacy_ledger_mode': True, 'downloads_path': str(downloads),
                                'collect_scope': 'project', 'collect_attachments': True,
                                'collect_guides': True}, source)
    assert report['counts'] == {'copied': 1}
    assert report['scope'] == 'all'
    assert not report['include_attachments'] and not report['include_guides']


def test_zip_member_is_copied_without_changing_archive(case):
    downloads, source, ledger, origin, row, runtime = case
    archive = downloads / 'bundle.zip'
    with zipfile.ZipFile(archive, 'w') as original:
        original.writestr('资料/PP-包内.pdf', b'%PDF-member')
    before = hashlib.sha256(archive.read_bytes()).hexdigest()
    write_ledger(ledger, [dict(row, 文件名='PP-包内.pdf', 本地路径='downloads/bundle.zip',
                              所属压缩包文件名='bundle.zip', 包内完整路径='资料/PP-包内.pdf')])
    report = run(case)
    assert report['counts'] == {'copied': 1}
    assert Path(report['files'][0]['destination']).read_bytes() == b'%PDF-member'
    assert Path(report['files'][0]['destination']).relative_to(source).parts == (
        '项目管理规定', '01_项目管理', 'PP-包内.pdf')
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == before


@pytest.mark.parametrize(('professional', 'category'), [
    ('项目管理', '01_项目管理'), ('质量管理', '02_质量管理'), ('施工管理', '03_施工管理'),
    ('安全环保', '04_安全环保'), ('采购管理', '05_采购与物资管理'),
    ('调试', '06_调试与试运行'), ('管道', '07_专业技术程序')])
def test_existing_seven_categories_use_exact_flat_file_hierarchy(case, professional, category):
    downloads, source, ledger, origin, row, runtime = case
    nested = downloads / 'HABSHANDBN/608200/60 Subcontractor/SBC-006-SINOPEC' / origin.name
    nested.parent.mkdir(parents=True)
    origin.rename(nested)
    write_ledger(ledger, [dict(row, 业务专业=professional, 本地路径=nested.relative_to(downloads).as_posix())])
    report = run(case)
    assert report['counts'] == {'copied': 1}
    destination = source / '项目管理规定' / category / origin.name
    assert Path(report['files'][0]['destination']) == destination
    assert destination.read_bytes() == nested.read_bytes()
    assert list((source / '项目管理规定').rglob('*.pdf')) == [destination]


def test_colliding_sources_get_numbered_names_and_traceable_state(case):
    downloads, source, ledger, origin, row, runtime = case
    other = downloads / 'other' / origin.name
    other.parent.mkdir()
    other.write_bytes(b'different-source')
    write_ledger(ledger, [row, dict(row, 本地路径=other.relative_to(downloads).as_posix())])
    report = run(case)
    assert report['counts'] == {'copied': 2}
    item = report['files'][1]
    assert Path(item['destination']).name == origin.stem + '-1.pdf'
    assert item['identity_tag'] == '-1'
    states = json.loads((runtime / 'collection-state.json').read_text(encoding='utf-8'))
    assert states[item['source_identity']]['origin'] == str(other)
    assert states[item['source_identity']]['sha256'] == hashlib.sha256(other.read_bytes()).hexdigest()
    assert run(case)['counts'] == {'unchanged': 2}
    assert (source / '项目管理规定/01_项目管理' / origin.name).read_bytes() == origin.read_bytes()
    assert other.read_bytes() == b'different-source'


def test_download_revision_suffixes_are_preserved_despite_same_ledger_title(case):
    downloads, source, ledger, origin, row, runtime = case
    first = downloads / (origin.stem + '-1.pdf')
    second = downloads / (origin.stem + '-2.pdf')
    first.write_bytes(b'revision-one')
    second.write_bytes(b'revision-two')
    write_ledger(ledger, [dict(row, 本地路径=first.name), dict(row, 本地路径=second.name)])
    assert run(case)['counts'] == {'copied': 2}
    folder = source / '项目管理规定/01_项目管理'
    assert (folder / first.name).read_bytes() == b'revision-one'
    assert (folder / second.name).read_bytes() == b'revision-two'
    assert not (folder / origin.name).exists()


@pytest.mark.parametrize('dry_run', [False, True])
def test_numbering_reuses_identical_content_and_skips_occupied_names(case, dry_run):
    downloads, source, ledger, origin, row, runtime = case
    rows = [row]
    for index, payload in enumerate((b'second', b'second', b'third'), 1):
        other = downloads / str(index) / origin.name
        other.parent.mkdir()
        other.write_bytes(payload)
        rows.append(dict(row, 本地路径=other.relative_to(downloads).as_posix()))
    folder = source / '项目管理规定/01_项目管理'
    folder.mkdir(parents=True)
    occupied = folder / (origin.stem + '-1.pdf')
    occupied.write_bytes(b'unrelated-user-copy')
    write_ledger(ledger, rows)
    report = run(case, dry_run=dry_run)
    assert report['counts'] == {('would_copy' if dry_run else 'copied'): 3, 'unchanged': 1}
    assert [Path(item['destination']).name for item in report['files']] == [
        origin.name, origin.stem + '-2.pdf', origin.stem + '-2.pdf', origin.stem + '-3.pdf']
    assert occupied.read_bytes() == b'unrelated-user-copy'
    if dry_run:
        assert list(folder.iterdir()) == [occupied]
        assert not (runtime / 'collection-state.json').exists()
    else:
        states = json.loads((runtime / 'collection-state.json').read_text(encoding='utf-8'))
        assert len(states) == 4
        assert run(case)['counts'] == {'unchanged': 4}


def test_identical_sources_keep_independent_provenance_when_one_changes(case):
    downloads, source, ledger, origin, row, runtime = case
    other = downloads / 'other' / origin.name
    other.parent.mkdir()
    other.write_bytes(origin.read_bytes())
    write_ledger(ledger, [row, dict(row, 本地路径=other.relative_to(downloads).as_posix())])
    first = run(case)
    assert first['counts'] == {'copied': 1, 'unchanged': 1}
    assert len({item['destination'] for item in first['files']}) == 1
    origin.write_bytes(b'new-source-version')
    changed = run(case)
    assert changed['counts'] == {'copied': 1, 'unchanged': 1}
    assert Path(changed['files'][0]['destination']).name == origin.stem + '-1.pdf'
    assert Path(changed['files'][1]['destination']).read_bytes() == other.read_bytes()
    # A numbered copy still supports safe updates without retaining old copies.
    origin.write_bytes(b'next-source-version')
    updated = run(case)
    assert updated['counts'] == {'updated': 1, 'unchanged': 1}
    assert updated['files'][0]['destination'] == changed['files'][0]['destination']
    assert Path(updated['files'][0]['destination']).read_bytes() == b'next-source-version'
    assert 'archive' not in updated['files'][0]
    assert not (source / '删除/更新旧版').exists()


def test_process_exit_recovers_durable_numbered_provenance_without_checkpoint(case):
    downloads, source, ledger, origin, row, runtime = case
    other = downloads / 'other' / origin.name
    other.parent.mkdir()
    other.write_bytes(b'second')
    write_ledger(ledger, [row, dict(row, 本地路径=other.relative_to(downloads).as_posix())])
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    script = ("import os\nfrom ima_manager.collect import collect_documents\n"
              "count = 0\ndef notify(message):\n global count\n count += 1\n"
              " if count == 2: os._exit(23)\n"
              f"collect_documents({str(ledger)!r}, {str(downloads)!r}, {str(source)!r}, "
              f"runtime={str(runtime)!r}, notify=notify)\n")
    result = subprocess.run([sys.executable, '-c', script], cwd=Path(__file__).resolve().parents[1],
                            env=env, capture_output=True, timeout=30)
    assert result.returncode == 23, result.stderr
    assert not (runtime / 'collection-state.json').exists()
    with closing(sqlite3.connect(runtime / 'collection-pending.sqlite3')) as db:
        assert db.execute('SELECT count(*) FROM entries').fetchone()[0] == 2
    report = run(case)
    assert report['counts'] == {'unchanged': 2}
    assert report['files'][1]['identity_tag'] == '-1'
    assert Path(report['files'][1]['destination']).read_bytes() == b'second'
    states = json.loads((runtime / 'collection-state.json').read_text(encoding='utf-8'))
    assert states[report['files'][1]['source_identity']]['origin'] == str(other)
    with closing(sqlite3.connect(runtime / 'collection-pending.sqlite3')) as db:
        assert db.execute('SELECT count(*) FROM entries').fetchone()[0] == 0


def test_copy_hashes_in_stream_and_checkpoints_state_once(case, monkeypatch):
    import ima_manager.collect as collection
    downloads, source, ledger, origin, row, runtime = case
    rows = [row]
    for index in range(8):
        other = downloads / f'PP-{index}.pdf'
        other.write_bytes(b'payload' * 1000)
        rows.append(dict(row, 文件名=other.name, 本地路径=other.name))
    write_ledger(ledger, rows)
    writes = []
    original = collection.atomic_json
    def checkpoint(path, value):
        writes.append(Path(path).name)
        original(path, value)
    def no_second_hash(stream):
        raise AssertionError('New copies must be hashed while staging, without rereading the temporary file')
    monkeypatch.setattr(collection, 'atomic_json', checkpoint)
    monkeypatch.setattr(collection, 'fingerprint', no_second_hash)
    report = run(case)
    assert report['counts'] == {'copied': 9}
    assert writes.count('collection-state.json') == 1
    assert all(item['sha256'] == hashlib.sha256(Path(item['destination']).read_bytes()).hexdigest()
               for item in report['files'])


def registry(case, files, paths=()):
    downloads, source, ledger, origin, row, runtime = case
    path = downloads.parent / 'runtime/ledger.sqlite3'
    path.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('CREATE TABLE files (project TEXT,id TEXT,path TEXT,active INTEGER,PRIMARY KEY(project,id))')
        db.execute('CREATE TABLE local_paths (project TEXT,file_id TEXT,local_path TEXT,PRIMARY KEY(project,file_id))')
        db.executemany('INSERT INTO files VALUES (?,?,?,?)', files)
        db.executemany('INSERT INTO local_paths VALUES (?,?,?)', paths)
    return path


def test_complete_sheet_restores_ids_and_registry_resolves_distinct_numbered_files(case):
    downloads, source, ledger, origin, row, runtime = case
    origin.unlink()
    second = downloads / (origin.stem + '-2.pdf')
    third = downloads / (origin.stem + '-3.pdf')
    second.write_bytes(b'identity-two')
    third.write_bytes(b'identity-three')
    write_ledger(ledger, [dict(row, 本地路径=None), dict(row, 本地路径=None)])
    from openpyxl import load_workbook
    book = load_workbook(ledger)
    complete = book.create_sheet('完整字段')
    complete.append(HEADERS)
    for identifier in ('file-two', 'file-three'):
        record = dict(row, 工作区='project', 内容ID=identifier, 本地路径='downloads/old/missing.pdf')
        complete.append([record.get(key) for key in HEADERS])
    book.save(ledger)
    book.close()
    db = registry(case, [('project', 'file-two', 'downloads/old/missing.pdf', 1),
                         ('project', 'file-three', None, 1)],
                        [('project', 'file-two', 'downloads/' + second.name),
                         ('project', 'file-three', 'downloads/' + third.name)])
    before = db.read_bytes()
    report = run(case)
    assert report['counts'] == {'copied': 2} and not report['has_issues']
    folder = source / '项目管理规定/01_项目管理'
    assert (folder / second.name).read_bytes() == b'identity-two'
    assert (folder / third.name).read_bytes() == b'identity-three'
    assert [item['file_id'] for item in report['files']] == ['file-two', 'file-three']
    assert db.read_bytes() == before  # Read-only registry access must not modify downloader state.


def test_identity_uses_registered_current_path_without_layout_table_entry(case):
    downloads, source, ledger, origin, row, runtime = case
    actual = downloads / (origin.stem + '-2.pdf')
    origin.rename(actual)
    write_ledger(ledger, [dict(row, 工作区='project', 内容ID='two')])
    registry(case, [('project', 'two', 'downloads/' + actual.name, 1)])
    report = run(case)
    assert report['counts'] == {'copied': 1}
    assert Path(report['files'][0]['destination']).name == actual.name


@pytest.mark.parametrize('scenario', ['missing', 'inactive', 'other-project', 'outside'])
def test_identity_never_substitutes_other_file_even_when_ledger_path_exists(case, scenario):
    downloads, source, ledger, origin, row, runtime = case
    write_ledger(ledger, [dict(row, 工作区='project', 内容ID='wanted')])
    entries = [('other' if scenario == 'other-project' else 'project', 'wanted',
                '../outside.pdf' if scenario == 'outside' else 'downloads/missing.pdf',
                0 if scenario == 'inactive' else 1)]
    registry(case, entries)
    (downloads.parent / 'outside.pdf').write_bytes(b'outside')
    report = run(case)
    assert report['counts'] == {'error': 1} and report['has_issues']
    assert '内容ID' in report['files'][0]['reason']
    assert not (source / '项目管理规定').exists()
    assert origin.read_bytes() == b'%PDF-original-unchanged'


def test_exported_identity_path_works_without_database_but_no_suffix_guessing(case):
    downloads, source, ledger, origin, row, runtime = case
    actual = downloads / (origin.stem + '-3.pdf')
    origin.rename(actual)
    write_ledger(ledger, [dict(row, 工作区='project', 内容ID='three', 本地路径=actual.name)])
    assert run(case)['counts'] == {'copied': 1}
    write_ledger(ledger, [dict(row, 工作区='project', 内容ID='missing', 本地路径=None)])
    report = run(case)
    assert report['counts'] == {'error': 1}
    assert '猜选' in report['files'][0]['reason']


def test_archive_identity_resolves_numbered_parent_archive(case):
    downloads, source, ledger, origin, row, runtime = case
    archive = downloads / 'bundle-2.zip'
    with zipfile.ZipFile(archive, 'w') as original:
        original.writestr('资料/' + origin.name, b'%PDF-parent-two')
    write_ledger(ledger, [dict(row, 工作区='project', 内容ID='member-id', 所属压缩包内容ID='archive-two',
                              所属压缩包文件名='bundle.zip', 包内完整路径='资料/' + origin.name)])
    registry(case, [('project', 'archive-two', 'downloads/bundle-2.zip', 1)])
    report = run(case)
    assert report['counts'] == {'copied': 1}
    assert Path(report['files'][0]['destination']).read_bytes() == b'%PDF-parent-two'
    assert 'bundle-2.zip!/' in report['files'][0]['origin']
    assert report['files'][0]['archive_file_id'] == 'archive-two'


def test_existing_matching_business_copy_is_reused_and_later_updated(case):
    downloads, source, ledger, origin, row, runtime = case
    destination = source / '项目管理规定/01_项目管理' / origin.name
    destination.parent.mkdir(parents=True)
    destination.write_bytes(origin.read_bytes())
    assert run(case)['counts'] == {'unchanged': 1}
    origin.write_bytes(b'new-source')
    report = run(case)
    assert report['counts'] == {'updated': 1}
    assert destination.read_bytes() == b'new-source'
    assert report['files'][0]['old_sha256'] == hashlib.sha256(b'%PDF-original-unchanged').hexdigest()
    assert not (source / '删除/更新旧版').exists()


def test_unknown_category_is_reported_without_creating_an_eighth_folder(case):
    downloads, source, ledger, origin, row, runtime = case
    write_ledger(ledger, [dict(row, 业务专业='未识别')])
    report = run(case)
    assert report['counts'] == {'error': 1}
    assert '01～07' in report['files'][0]['reason']
    assert not (source / '项目管理规定').exists()


def test_conflicting_destination_and_changed_source_are_not_overwritten(case):
    downloads, source, ledger, origin, row, runtime = case
    report = run(case)
    destination = Path(report['files'][0]['destination'])
    destination.write_bytes(b'user-edited-copy')
    result = run(case)
    assert result['counts'] == {'copied': 1}
    assert Path(result['files'][0]['destination']).name == origin.stem + '-1.pdf'
    assert destination.read_bytes() == b'user-edited-copy'
    assert origin.read_bytes() == b'%PDF-original-unchanged'


def test_managed_copy_updates_without_local_history_and_preserves_source(case):
    downloads, source, ledger, origin, row, runtime = case
    old = origin.read_bytes()
    first = run(case)
    destination = Path(first['files'][0]['destination'])
    origin.write_bytes(b'%PDF-new-source-version')
    before = origin.read_bytes(), origin.stat().st_mtime_ns
    preview = run(case, dry_run=True)
    assert preview['counts'] == {'would_update': 1}
    assert destination.read_bytes() == old
    report = run(case)
    assert report['counts'] == {'updated': 1}
    assert destination.read_bytes() == before[0]
    assert report['files'][0]['old_sha256'] == hashlib.sha256(old).hexdigest()
    assert 'archive' not in report['files'][0]
    assert not (source / '删除/更新旧版').exists()
    assert (origin.read_bytes(), origin.stat().st_mtime_ns) == before
    assert run(case)['counts'] == {'unchanged': 1}


def test_collection_removes_only_generated_history_and_preserves_manual_deletions(case):
    downloads, source, ledger, origin, row, runtime = case
    history = source / '删除/更新旧版/项目管理规定/oldhash/old.pdf'
    history.parent.mkdir(parents=True)
    history.write_bytes(b'old-generated-copy')
    manual = source / '删除/项目管理规定/manual.pdf'
    manual.parent.mkdir(parents=True)
    manual.write_bytes(b'manually-deleted-file')
    before = origin.read_bytes(), origin.stat().st_mtime_ns
    assert run(case, dry_run=True)['removed_local_history_files'] == 0
    assert history.read_bytes() == b'old-generated-copy'
    report = run(case)
    assert report['counts'] == {'copied': 1}
    assert report['removed_local_history_files'] == 1
    assert not (source / '删除/更新旧版').exists()
    assert manual.read_bytes() == b'manually-deleted-file'
    assert (origin.read_bytes(), origin.stat().st_mtime_ns) == before


def test_failed_update_keeps_previous_copy_without_local_history(case, monkeypatch):
    import ima_manager.collect as collection
    downloads, source, ledger, origin, row, runtime = case
    first = run(case)
    destination = Path(first['files'][0]['destination'])
    previous = destination.read_bytes()
    origin.write_bytes(b'new-local-content')
    replace = collection.os.replace
    def fail_publish(src, dst):
        if Path(dst) == destination:
            raise OSError('simulated publish failure')
        return replace(src, dst)
    with monkeypatch.context() as patch:
        patch.setattr(collection.os, 'replace', fail_publish)
        report = run(case)
    assert report['counts'] == {'error': 1}
    assert destination.read_bytes() == previous
    assert not (source / '删除/更新旧版').exists()
    assert run(case)['counts'] == {'updated': 1}
    assert destination.read_bytes() == origin.read_bytes()


@pytest.mark.parametrize('legacy_deleted_state', [False, True])
def test_missing_copy_is_recreated_even_when_archive_or_legacy_deleted_state_exists(case, legacy_deleted_state):
    downloads, source, ledger, origin, row, runtime = case
    report = run(case)
    destination = Path(report['files'][0]['destination'])
    archived = move_local_to_deleted(source, destination)
    archived_bytes = archived.read_bytes()
    if legacy_deleted_state:
        state_path = runtime / 'collection-state.json'
        states = json.loads(state_path.read_text(encoding='utf-8'))
        for previous in states.values():
            previous['status'] = 'deleted'
        state_path.write_text(json.dumps(states), encoding='utf-8')
    preview = run(case, dry_run=True)
    assert preview['counts'] == {'would_copy': 1}
    assert not destination.exists()
    assert run(case)['counts'] == {'copied': 1}
    assert destination.read_bytes() == archived_bytes == origin.read_bytes()
    assert archived.read_bytes() == archived_bytes
    destination.unlink()
    archived.unlink()
    assert run(case)['counts'] == {'copied': 1}
    assert destination.read_bytes() == origin.read_bytes()
    assert origin.exists()


def test_missing_and_ambiguous_sources_are_reported_without_guessing(case):
    downloads, source, ledger, origin, row, runtime = case
    origin.unlink()
    write_ledger(ledger, [dict(row, 本地路径='missing')])
    assert run(case)['counts'] == {'error': 1}
    for name in ('a', 'b'):
        folder = downloads / name
        folder.mkdir()
        (folder / origin.name).write_bytes(b'candidate')
    report = run(case)
    assert report['counts'] == {'error': 1}
    assert '多个同名' in report['files'][0]['reason']


def test_collection_error_notification_includes_reason_and_continues(case):
    downloads, source, ledger, origin, row, runtime = case
    write_ledger(ledger, [dict(row, 文件名='PP-missing.pdf', 本地路径='missing'), row])
    messages = []
    report = run(case, notify=messages.append)
    assert report['counts'] == {'error': 1, 'copied': 1}
    assert 'PP-missing.pdf — error：文件缺失' in messages[0]
    assert ' — copied' in messages[1]


def test_zip_traversal_is_rejected(case):
    downloads, source, ledger, origin, row, runtime = case
    archive = downloads / 'bundle.zip'
    with zipfile.ZipFile(archive, 'w') as original:
        original.writestr('../PP-outside.pdf', b'bad')
    write_ledger(ledger, [dict(row, 文件名='PP-outside.pdf', 本地路径='downloads/bundle.zip',
                              所属压缩包文件名='bundle.zip', 包内完整路径='../PP-outside.pdf')])
    assert run(case)['counts'] == {'error': 1}
    assert not (source.parent / 'PP-outside.pdf').exists()


def test_source_and_destination_cannot_overlap(case):
    downloads, source, ledger, origin, row, runtime = case
    with pytest.raises(ImaError, match='不能相互包含'):
        collect_documents(ledger, downloads, downloads / 'output', runtime=runtime)


def test_invalid_workbook_is_reported_without_creating_copies(case):
    downloads, source, ledger, origin, row, runtime = case
    ledger.write_bytes(b'not-an-xlsx')
    with pytest.raises(ImaError, match='有效的 XLSX'):
        run(case)
    assert not source.exists()


def test_valid_ledger_paths_do_not_walk_entire_download_tree(case, monkeypatch):
    def no_walk(*args, **kwargs):
        raise AssertionError('valid ledger path must not need a full tree scan')
    monkeypatch.setattr('ima_manager.collect.os.walk', no_walk)
    assert run(case)['counts'] == {'copied': 1}


def test_disabled_automatic_collection_never_touches_inputs():
    assert prepare_documents({'collect_enabled': False}, 'not-a-path') is None


def test_sync_copies_ledger_files_before_planning_uploads(case, monkeypatch):
    from ima_manager import __main__ as cli
    downloads, source, ledger, origin, row, runtime = case
    settings = {'source': str(source), 'kb': 'fake-kb', 'folder': '', 'recursive': True,
                'collect_enabled': True, 'ledger_path': str(ledger), 'downloads_path': str(downloads)}
    monkeypatch.setattr(cli, 'load_settings', lambda: settings)
    monkeypatch.setattr(cli, 'load_credentials', lambda: ('fake-client', 'fake-key'))
    monkeypatch.setattr(cli, 'ImaClient', Mock())
    monkeypatch.setattr(cli, 'RUNTIME', runtime)
    monkeypatch.setattr('ima_manager.collect.RUNTIME', runtime)
    monkeypatch.setattr(cli.signal, 'signal', lambda *args: None)
    engine = Mock()
    def plan(*args):
        copies = list(source.rglob(origin.name))
        assert len(copies) == 1
        assert copies[0].read_bytes() == origin.read_bytes()
        return []
    engine.plan.side_effect = plan
    engine.execute.return_value = []
    monkeypatch.setattr(cli, 'Engine', Mock(return_value=engine))
    assert cli.main(['sync', '--execute', '--ledger', str(ledger)]) == 0
    assert origin.exists()


def test_collect_cli_real_process_needs_no_ima_credentials(case):
    downloads, source, ledger, origin, row, runtime = case
    env = {key: value for key, value in os.environ.items() if not key.startswith('IMA_')}
    env['PYTHONIOENCODING'] = 'utf-8'
    script = ('from pathlib import Path; from ima_manager import config; '
              f'config.RUNTIME=Path({str(runtime)!r}); '
              'from ima_manager.__main__ import main; raise SystemExit(main())')
    process = subprocess.run([sys.executable, '-c', script, 'collect', '--ledger', str(ledger),
                             '--downloads', str(downloads), '--source', str(source), '--dry-run'],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout)['counts'] == {'would_copy': 1}
    assert not source.exists()


def test_collect_cli_partial_failure_is_a_completed_task_with_visible_issues(case):
    downloads, source, ledger, origin, row, runtime = case
    write_ledger(ledger, [dict(row, 文件名='PP-missing.pdf', 本地路径='missing'), row])
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    script = ('from pathlib import Path; from ima_manager import config; '
              f'config.RUNTIME=Path({str(runtime)!r}); '
              'from ima_manager.__main__ import main; raise SystemExit(main())')
    process = subprocess.run([sys.executable, '-c', script, 'collect', '--ledger', str(ledger),
                              '--downloads', str(downloads), '--source', str(source)],
                             cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                             text=True, encoding='utf-8', timeout=30)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report['has_issues'] and report['counts'] == {'error': 1, 'copied': 1}
    assert Path(report['files'][1]['destination']).read_bytes() == origin.read_bytes()
    assert '文件缺失' in process.stderr and '可继续预览和同步' in process.stderr


def live_database(case, records):
    downloads, source, ledger, origin, row, runtime = case
    database = downloads.parent / 'runtime/ledger.sqlite3'
    database.parent.mkdir(exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript('''
            CREATE TABLE files (project TEXT,id TEXT,filename TEXT,path TEXT,target_path TEXT,
              active INTEGER,status TEXT,bytes INTEGER,sha256 TEXT,PRIMARY KEY(project,id));
            CREATE TABLE folders (project TEXT,id TEXT,path TEXT,active INTEGER);
            CREATE TABLE documents (project TEXT,id TEXT,folder_id TEXT,reference TEXT,active INTEGER);
            CREATE TABLE document_files (project TEXT,document_id TEXT,folder_id TEXT,file_id TEXT,active INTEGER);
            CREATE TABLE local_paths (project TEXT,file_id TEXT,local_path TEXT);
            INSERT INTO folders VALUES ('p','folder','HABSHANDBN/Project',1);
            INSERT INTO documents VALUES ('p','doc','folder','PP-30201_50150_LT_HE-GG-999-50-00-251',1);
        ''')
        for identifier, name, path in records:
            physical = downloads / path if path else None
            db.execute('INSERT INTO files VALUES (?,?,?,?,?,1,?,?,?)', ('p', identifier, name,
                'downloads/' + path if path else None, 'downloads/' + path if path else None,
                'done' if physical and physical.is_file() else 'pending',
                physical.stat().st_size if physical and physical.is_file() else 0,
                hashlib.sha256(physical.read_bytes()).hexdigest() if physical and physical.is_file() else None))
            db.execute('INSERT INTO document_files VALUES (?,?,?,?,1)', ('p', 'doc', 'folder', identifier))
    return database


def test_default_collection_reads_database_ids_without_opening_excel(case, monkeypatch):
    import ima_manager.collect as collection
    downloads, source, ledger, origin, row, runtime = case
    original = 'PP-30201_50150_LT_HE-GG-999-51-00-736.pdf'
    second, third = original.replace('.pdf', '-2.pdf'), original.replace('.pdf', '-3.pdf')
    (downloads / second).write_bytes(b'identity-two')
    (downloads / third).write_bytes(b'identity-three')
    database = live_database(case, [('two', original, second), ('three', original, third),
                                     ('missing', original, None)])
    before = database.read_bytes()
    monkeypatch.setattr(collection, 'RUNTIME', runtime)
    monkeypatch.setattr(collection, 'read_rows', Mock(side_effect=AssertionError('Excel must not be opened')))
    report = prepare_documents({'database_path': str(database), 'downloads_path': str(downloads),
                                'ledger_path': str(ledger.with_name('outdated.xlsx'))}, source)
    assert report['source_type'] == 'database' and report['database'] == str(database.resolve())
    assert report['counts'] == {'copied': 2} and not report['has_issues']
    assert {item['file_id'] for item in report['files']} == {'two', 'three'}
    assert all(item['record_source'] == 'database' for item in report['files'])
    folder = source / '项目管理规定/02_质量管理'
    assert (folder / second).read_bytes() == b'identity-two'
    assert (folder / third).read_bytes() == b'identity-three'
    assert database.read_bytes() == before


def test_database_snapshot_tracks_path_updates_and_keeps_local_inactive_parents(case, monkeypatch):
    import ima_manager.collect as collection
    downloads, source, ledger, origin, row, runtime = case
    database = live_database(case, [('file', origin.name, origin.name)])
    monkeypatch.setattr(collection, 'RUNTIME', runtime)
    settings = {'database_path': str(database), 'downloads_path': str(downloads)}
    assert prepare_documents(settings, source)['counts'] == {'copied': 1}
    actual = downloads / (origin.stem + '-3.pdf')
    origin.rename(actual)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('UPDATE files SET path=?,target_path=?', ('downloads/' + actual.name, 'downloads/' + actual.name))
    result = prepare_documents(settings, source)
    assert Path(result['files'][0]['destination']).name == actual.name
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('UPDATE documents SET active=0')
    report = prepare_documents(settings, source)
    assert report['selected_rows'] == 1 and report['counts'] == {'unchanged': 1}


def test_database_cli_real_process_needs_neither_excel_nor_credentials(case):
    downloads, source, ledger, origin, row, runtime = case
    database = live_database(case, [('file', origin.name, origin.name)])
    ledger.unlink()
    env = {key: value for key, value in os.environ.items() if not key.startswith('IMA_')}
    env['PYTHONIOENCODING'] = 'utf-8'
    script = ('from pathlib import Path; from ima_manager import config; '
              f'config.RUNTIME=Path({str(runtime)!r}); '
              'from ima_manager.__main__ import main; raise SystemExit(main())')
    result = subprocess.run([sys.executable, '-c', script, 'collect', '--database', str(database),
                             '--downloads', str(downloads), '--source', str(source)],
                            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                            text=True, encoding='utf-8', timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report['source_type'] == 'database' and report['counts'] == {'copied': 1}
    assert not ledger.exists()


def test_missing_or_wrong_database_never_creates_or_changes_it(case, monkeypatch):
    import ima_manager.collect as collection
    downloads, source, ledger, origin, row, runtime = case
    database = runtime / 'missing.sqlite3'
    monkeypatch.setattr(collection, 'RUNTIME', runtime)
    settings = {'database_path': str(database), 'downloads_path': str(downloads)}
    with pytest.raises(ImaError, match='下载数据库不存在'):
        prepare_documents(settings, source)
    assert not database.exists()
    runtime.mkdir(exist_ok=True)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('CREATE TABLE wrong (id INTEGER)')
    before = database.read_bytes()
    with pytest.raises(ImaError, match='缺少 files'):
        prepare_documents(settings, source)
    assert database.read_bytes() == before


def test_live_database_reader_sees_only_committed_wal_paths(case):
    from ima_manager.database_source import read_database
    from ima_manager.document_rules import selected
    downloads, source, ledger, origin, row, runtime = case
    database = live_database(case, [('file', origin.name, origin.name)])
    changed = 'downloads/' + origin.stem + '-2.pdf'
    (downloads / Path(changed).name).write_bytes(origin.read_bytes())
    with closing(sqlite3.connect(database)) as writer:
        writer.execute('PRAGMA journal_mode=WAL')
        writer.execute('BEGIN IMMEDIATE')
        writer.execute('UPDATE files SET path=?,target_path=?', (changed, changed))
        before_commit = read_database(database, selected)
        assert before_commit[0][1]['_registered_paths'] == ['downloads/' + origin.name]
        writer.commit()
        after_commit = read_database(database, selected)
        assert after_commit[0][1]['_registered_paths'] == [changed]


@pytest.mark.parametrize('inactive', [False, True])
def test_database_archive_members_use_committed_matching_parent_version(case, monkeypatch, inactive):
    import ima_manager.collect as collection
    from ima_manager.database_source import read_database, sync_module
    from ima_manager.document_rules import selected
    downloads, source, ledger, origin, row, runtime = case
    member = 'PP-30201_50150_LT_HE-GG-999-50-00-251.pdf'
    archive = downloads / 'bundle-2.zip'
    payload = b'%PDF-member'
    with zipfile.ZipFile(archive, 'w') as zipped:
        zipped.writestr('docs/' + member, payload)
    database = live_database(case, [('zip', 'bundle.zip', archive.name)])
    cache = runtime / 'file_analysis'
    job = cache / 'jobs/job'
    extracted = job / 'extracted/docs' / member
    extracted.parent.mkdir(parents=True)
    extracted.write_bytes(payload)
    (job / 'analysis.json').write_text('{}', encoding='utf-8')
    with closing(sqlite3.connect(cache / 'queue.sqlite3')) as db, db:
        db.executescript('''CREATE TABLE jobs (key TEXT,project TEXT,file_id TEXT,expected_sha TEXT,active INTEGER,state TEXT,result_path TEXT,updated TEXT);
          CREATE TABLE archive_inventory (job_key TEXT,archive TEXT,entry INTEGER,payload TEXT);''')
        db.execute('INSERT INTO jobs VALUES (?,?,?,?,1,?,?,?)', ('job', 'p', 'zip',
            hashlib.sha256(archive.read_bytes()).hexdigest(), 'done', 'jobs/job/analysis.json', '2026-11-04'))
        item = dict(project='p', filename=member, path=str(extracted), bytes=len(payload),
                    origin=str(archive) + '!/docs/' + member, archive=str(archive), entry=1, member='docs/' + member)
        db.execute('INSERT INTO archive_inventory VALUES (?,?,?,?)', ('job', str(archive), 1, json.dumps(item)))
        if inactive:
            db.execute('UPDATE jobs SET active=0')
    if inactive:
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE files SET active=0')
            db.execute('UPDATE documents SET active=0')
            db.execute('UPDATE document_files SET active=0')
    monkeypatch.setattr(collection, 'RUNTIME', runtime)
    report = prepare_documents({'database_path': str(database), 'downloads_path': str(downloads)}, source)
    assert report['counts'] == {'copied': 1}
    assert Path(report['files'][0]['destination']).read_bytes() == payload
    indexed = sync_module('archive_index')
    original = indexed.current_members
    def changed_after_snapshot(*args, **kwargs):
        members = original(*args, **kwargs)
        for item in members:
            item['parent_sha256'] = 'changed-after-the-ledger-snapshot'
        return members
    with monkeypatch.context() as patch:
        patch.setattr(indexed, 'current_members', changed_after_snapshot)
        assert read_database(database, selected) == []
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE files SET sha256='different-version'")
    assert read_database(database, selected) == []


def test_database_collects_retained_old_version_then_new_local_version(case):
    downloads, source, ledger, origin, row, runtime = case
    name = 'PP-30201_50150_LT_HE-GG-999-51-00-950.pdf'
    old = downloads / name.replace('.pdf', '-1.pdf')
    old.write_bytes(b'%PDF-old-local-version')
    database = live_database(case, [('old', name, old.name), ('new', name, None)])
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE files SET active=0 WHERE id='old'")
        db.execute("UPDATE document_files SET active=0 WHERE file_id='old'")
    before = database.read_bytes()
    def collect():
        return collect_documents(database, downloads, source, runtime=runtime, from_database=True)
    result = collect()
    assert result['counts'] == {'copied': 1} and not result['has_issues']
    assert result['files'][0]['file_id'] == 'old'
    assert Path(result['files'][0]['destination']).name == old.name
    assert Path(result['files'][0]['destination']).read_bytes() == old.read_bytes()
    assert database.read_bytes() == before
    assert collect()['counts'] == {'unchanged': 1}
    new = downloads / name
    new.write_bytes(b'%PDF-new-local-version')
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE files SET status='done',path=?,target_path=? WHERE id='new'",
                   ('downloads/' + new.name, 'downloads/' + new.name))
    result = collect()
    assert result['counts'] == {'unchanged': 1, 'copied': 1}
    assert {item['file_id'] for item in result['files']} == {'old', 'new'}
    # Only the downloader moves sources into history. Collection does not.
    history = downloads.parent / 'history'
    history.mkdir()
    archived = history / old.name
    old.rename(archived)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE files SET path=?,target_path=? WHERE id='old'",
                   ('history/' + old.name, str(archived)))
    result = collect()
    assert result['counts'] == {'unchanged': 1}
    assert result['files'][0]['file_id'] == 'new'
    assert archived.read_bytes() == b'%PDF-old-local-version'


@pytest.mark.parametrize('location', ['history', 'runtime/download_ready', 'outside'])
def test_database_collection_excludes_files_outside_configured_downloads(case, location):
    downloads, source, ledger, origin, row, runtime = case
    database = live_database(case, [('file', origin.name, origin.name)])
    excluded = downloads.parent / location / origin.name
    excluded.parent.mkdir(parents=True, exist_ok=True)
    origin.rename(excluded)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute('UPDATE files SET path=?,target_path=?',
                   (str(excluded), excluded.relative_to(downloads.parent).as_posix()))
        db.execute('INSERT INTO local_paths VALUES (?,?,?)', ('p', 'file', str(excluded)))
    before = database.read_bytes(), excluded.read_bytes(), excluded.stat().st_mtime_ns
    report = collect_documents(database, downloads, source, runtime=runtime, from_database=True)
    assert report['selected_rows'] == 0 and not report['has_issues']
    assert not list(source.rglob('*.pdf'))
    assert (database.read_bytes(), excluded.read_bytes(), excluded.stat().st_mtime_ns) == before


def test_database_collection_uses_configured_download_root_and_registry(case):
    downloads, source, ledger, origin, row, runtime = case
    database = live_database(case, [('file', origin.name, origin.name)])
    configured = downloads.parent / 'server-downloads'
    configured.mkdir()
    actual = configured / (origin.stem + '-1.pdf')
    origin.rename(actual)
    with closing(sqlite3.connect(database)) as db, db:
        db.execute("UPDATE files SET active=0,path=NULL,target_path=NULL")
        db.execute('INSERT INTO local_paths VALUES (?,?,?)', ('p', 'file', str(actual)))
    report = collect_documents(database, configured, source, runtime=runtime, from_database=True)
    assert report['counts'] == {'copied': 1}
    assert Path(report['files'][0]['destination']).read_bytes() == actual.read_bytes()
