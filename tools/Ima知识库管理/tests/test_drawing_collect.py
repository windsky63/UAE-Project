"""PDF drawing collection through the same read-only, incremental pipeline as PP."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
from unittest.mock import patch
import sqlite3
import subprocess
import sys
import zipfile

from openpyxl import Workbook
import pytest

from ima_manager.collect import collect_documents
from ima_manager.collection_rules import is_drawing, is_equipment_list, relative_destination, selected
from ima_manager.database_source import sync_module


ISO = '4-262-52-0001-1C1-001-01'
PID = '30201_50150_LT_HE-H4-262-00-30-001'
STEEL = '30201_50150_LT_HE-H4-262-18-11-001'
INSTRUMENT = '30201_50150_LT_HE-H2-057-15-38-001'
TIE_IN = '4-262-53-0001-1C1-001-01'


def drawing(name, **values):
    result = sync_module('business_type').classify(filename=name)
    return {'文件名': name, '业务类型': result['business_type'], '业务专业': result['discipline'],
            '目录': 'Project/Drawings', '本地路径': 'downloads/' + name, **values}


@pytest.fixture
def case(tmp_path):
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    return downloads, tmp_path / '待同步', tmp_path / 'ledger.xlsx', tmp_path / 'runtime'


def collect(case, rows, **kwargs):
    downloads, source, ledger, runtime = case
    book = Workbook()
    sheet = book.active
    sheet.title = '完整字段'
    keys = list(dict.fromkeys(key for row in rows for key in row))
    sheet.append(keys)
    for row in rows:
        sheet.append([row.get(key) for key in keys])
    # Complete-field identity columns are required even for legacy records without IDs.
    for key in ('工作区', '内容ID'):
        if key not in keys:
            sheet.cell(1, len(keys) + 1, key)
            keys.append(key)
    book.save(ledger)
    book.close()
    return collect_documents(ledger, downloads, source, runtime=runtime, **kwargs)


@pytest.mark.parametrize(('number', 'category', 'plant', 'unit'), [
    (ISO, '地上新建管道单线图', 'H4_Habshan 4', '单元262'),
    (PID, '工艺管道及仪表流程图', 'H4_Habshan 4', '单元262'),
    (STEEL, '主体钢结构标准图', 'H4_Habshan 4', '单元262'),
    (INSTRUMENT, '仪表安装详图', 'H2_Habshan 2', '单元057'),
    (TIE_IN, '地上_地下碰口管道单线图', 'H4_Habshan 4', '单元262')])
def test_pdf_categories_use_plant_and_unit_and_are_incremental(case, number, category, plant, unit):
    downloads, source, ledger, runtime = case
    origin = downloads / (number + '.PDF')
    origin.write_bytes(b'%PDF-drawing')
    before = origin.read_bytes(), origin.stat().st_mtime_ns
    rows = [drawing(origin.name), drawing(origin.name)]
    preview = collect(case, rows, dry_run=True)
    assert preview['counts'] == {'would_copy': 1, 'duplicate': 1}
    assert not source.exists()
    report = collect(case, rows)
    assert report['counts'] == {'copied': 1, 'duplicate': 1}
    discipline = sync_module('business_type').parse_number(origin.name)['discipline']
    destination = source / '图纸（其他）' / discipline / category / plant / unit / origin.name
    assert destination.read_bytes() == before[0]
    assert (origin.read_bytes(), origin.stat().st_mtime_ns) == before
    assert report['files'][0]['collection_root'] == '图纸（其他）'
    assert collect(case, rows)['counts'] == {'unchanged': 1, 'duplicate': 1}


@pytest.mark.parametrize('name', [
    ISO + '.dwg', ISO + '.dxf', ISO + '.pcf', ISO + '.sha', ISO + '.png',
    ISO + '.zip', ISO + '.xlsx', ISO + '_ATR.pdf', ISO + '_CC.pdf',
    'ADNOCGP-WTRAN-123456.pdf', 'unclassified.pdf', 'drawing.pdf',
    'PP-' + PID + '.pdf', 'RPT-' + PID + '.pdf'])
def test_excludes_non_pdf_non_drawings_and_inherited_or_review_attachments(name):
    row = drawing(name)
    if name in ('ADNOCGP-WTRAN-123456.pdf', 'drawing.pdf'):
        row.update(业务类型='工艺管道及仪表流程图', 业务类型识别状态='inherited')
    assert not is_drawing(row)


def test_mixed_collection_preserves_procedures_and_updates_drawings_without_history(case):
    downloads, source, ledger, runtime = case
    name = PID + '.pdf'
    origin = downloads / name
    origin.write_bytes(b'%PDF-old')
    procedure = downloads / 'PP-rules.pdf'
    procedure.write_bytes(b'%PDF-procedure')
    rows = [drawing(name), {'文件名': procedure.name, '业务类型': '项目程序文件',
            '业务专业': '项目管理', '目录': 'Project', '本地路径': procedure.name}]
    assert collect(case, rows)['counts'] == {'copied': 2}
    assert (source / '项目管理规定/01_项目管理' / procedure.name).read_bytes() == procedure.read_bytes()
    destination = source / '图纸（其他）/工艺及通用系统/工艺管道及仪表流程图/H4_Habshan 4/单元262' / name
    origin.write_bytes(b'%PDF-new-version')
    report = collect(case, rows)
    assert report['counts'] == {'updated': 1, 'unchanged': 1}
    assert report['files'][0]['old_sha256'] == hashlib.sha256(b'%PDF-old').hexdigest()
    assert not (source / '删除/更新旧版').exists()
    assert destination.read_bytes() == origin.read_bytes()
    destination.write_bytes(b'user-edit')
    origin.write_bytes(b'%PDF-third-version')
    result = collect(case, rows)
    assert result['counts'] == {'copied': 1, 'unchanged': 1}
    assert Path(result['files'][0]['destination']).name == origin.stem + '-1.pdf'
    assert destination.read_bytes() == b'user-edit'


@pytest.mark.parametrize('nested', [False, True])
def test_zip_pdf_members_without_ready_path_are_not_extracted(case, nested):
    downloads, source, ledger, runtime = case
    name = INSTRUMENT + '.pdf'
    origin = downloads / 'package-2.zip'
    with zipfile.ZipFile(origin, 'w') as outer:
        if nested:
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, 'w') as zipped:
                zipped.writestr('drawings/' + name, b'%PDF-member')
            outer.writestr('docs/instrument.zip', inner.getvalue())
            member = 'docs/instrument.zip!/drawings/' + name
        else:
            member = 'drawings/' + name
            outer.writestr(member, b'%PDF-member')
    before = origin.read_bytes(), origin.stat().st_mtime_ns
    rows = [drawing(name, 本地路径=origin.name, 所属压缩包文件名=origin.name, 包内完整路径=member)]
    report = collect(case, rows)
    assert report['counts'] == {'error': 1}
    assert 'ima 不读取或解压 ZIP' in report['files'][0]['reason']
    assert not list(source.rglob('*.pdf'))
    assert (origin.read_bytes(), origin.stat().st_mtime_ns) == before


@pytest.mark.parametrize('prefix', ['../inner.zip!/', '/inner.zip!/', 'C:/inner.zip!/'])
def test_nested_zip_unsafe_paths_are_reported_and_other_drawings_continue(case, prefix):
    downloads, source, ledger, runtime = case
    origin = downloads / 'bundle.zip'
    with zipfile.ZipFile(origin, 'w') as archive:
        archive.writestr('readme', 'unused')
    valid = downloads / (PID + '.pdf')
    valid.write_bytes(b'%PDF-valid')
    rows = [drawing(INSTRUMENT + '.pdf', 本地路径=origin.name, 所属压缩包文件名=origin.name,
                    包内完整路径=prefix + INSTRUMENT + '.pdf'), drawing(valid.name)]
    report = collect(case, rows)
    assert report['counts'] == {'error': 1, 'copied': 1}
    assert report['has_issues']
    assert not (source / '图纸（其他）/仪表、控制及通信/仪表安装详图').exists()


@pytest.mark.parametrize(('number', 'extension', 'directory'), [
    (PID, '.pdf', '图纸（其他）/工艺及通用系统/工艺管道及仪表流程图/H4_Habshan 4/单元262'),
    ('NM-30201_50150_LT_HE-H4-213-13-00-001', '.xlsx', '设备、材料或碰口清单/管道/H4_Habshan 4/单元213'),
    ('NM-30201_50150_LT_HE-H4-262-13-00-001', '.pdf', '设备、材料或碰口清单（其他）/管道/H4_Habshan 4/单元262')])
def test_database_real_cli_collects_exact_identity_without_credentials_or_excel(case, number, extension, directory):
    downloads, source, ledger, runtime = case
    runtime.mkdir()
    database = runtime / 'ledger.sqlite3'
    name = number + extension
    actual = downloads / (number + '-2' + extension)
    actual.write_bytes(b'%PDF-identity-two')
    with closing(sqlite3.connect(database)) as db, db:
        db.executescript('''CREATE TABLE files (project TEXT,id TEXT,filename TEXT,path TEXT,
            target_path TEXT,active INTEGER,status TEXT,bytes INTEGER,sha256 TEXT);
            CREATE TABLE local_paths (project TEXT,file_id TEXT,local_path TEXT);''')
        db.execute('INSERT INTO files VALUES (?,?,?,?,?,1,?,?,?)',
            ('p', 'two', name, 'downloads/missing.pdf', None, 'done', actual.stat().st_size,
             hashlib.sha256(actual.read_bytes()).hexdigest()))
        db.execute('INSERT INTO local_paths VALUES (?,?,?)', ('p', 'two', 'downloads/' + actual.name))
    before = database.read_bytes()
    env = {key: value for key, value in os.environ.items() if not key.startswith('IMA_')}
    env['PYTHONIOENCODING'] = 'utf-8'
    script = ('from pathlib import Path; from ima_manager import config; '
              f'config.RUNTIME=Path({str(runtime)!r}); '
              'from ima_manager.__main__ import main; raise SystemExit(main())')
    process = subprocess.run([sys.executable, '-c', script, 'collect', '--database', str(database),
        '--downloads', str(downloads), '--source', str(source)],
        cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
        text=True, encoding='utf-8', timeout=30)
    assert process.returncode == 0, process.stderr
    report = json.loads(process.stdout)
    assert report['counts'] == {'copied': 1}
    assert report['files'][0]['file_id'] == 'two'
    assert (source / directory / actual.name).read_bytes() == actual.read_bytes()
    assert database.read_bytes() == before
    assert not ledger.exists()


@pytest.mark.parametrize(('name', 'folders'), [
    ('2-233-52-0187-AGX-AAA.pdf', ('H2_Habshan 2', '单元233')),
    ('30201_50150_LT_HE-GG-000-18-11-003.pdf', ('GG_跨厂区通用', '单元000')),
    ('1-43-52-0187-AGX-AAA.pdf', ('H1_Habshan 1', '单元43')),
    ('B-233-52-0187-AGX-AAA.pdf', ()),
    ('DWG-30201_50150_LT_HE-LEGACY.pdf', ()),
    ('30201_50150_LT_HE-ZZ-233-00-30-001.pdf', ()),
    (PID + ' 30201_50150_LT_HE-H2-233-00-30-001.pdf', ()),
    (PID + ' 30201_50150_LT_HE-H4-233-00-30-001.pdf', ())])
def test_location_uses_own_number_or_falls_back_directly_to_category(case, name, folders):
    downloads, source, ledger, runtime = case
    origin = downloads / name
    origin.write_bytes(b'%PDF-location')
    row = drawing(name, 目录='HABSHANDBN/H5/999',
                  文档编号='30201_50150_LT_HE-H5-999-00-30-001')
    report = collect(case, [row])
    assert report['counts'] == {'copied': 1}
    item = report['files'][0]
    relative = Path(item['destination']).relative_to(source)
    discipline = sync_module('business_type').parse_number(name)['discipline']
    assert relative.parts == ('图纸（其他）', discipline, item['category'], *folders, name)
    assert not list(source.rglob('*待核对*'))


def test_same_type_and_basename_in_different_units_do_not_conflict(case):
    downloads, source, ledger, runtime = case
    actual_name = 'drawing.pdf'
    first = downloads / 'unit113' / actual_name
    second = downloads / 'unit200' / actual_name
    for origin, payload in ((first, b'%PDF-113'), (second, b'%PDF-200')):
        origin.parent.mkdir()
        origin.write_bytes(payload)
    rows = [drawing('30201_50150_LT_HE-H4-113-00-30-001.pdf',
                    本地路径=first.relative_to(downloads).as_posix()),
            drawing('30201_50150_LT_HE-H4-200-00-30-001.pdf',
                    本地路径=second.relative_to(downloads).as_posix())]
    assert collect(case, rows)['counts'] == {'copied': 2}
    root = source / '图纸（其他）/工艺及通用系统/工艺管道及仪表流程图/H4_Habshan 4'
    assert (root / '单元113' / actual_name).read_bytes() == b'%PDF-113'
    assert (root / '单元200' / actual_name).read_bytes() == b'%PDF-200'


def test_same_type_with_multiple_numbers_in_same_unit_has_one_location():
    name = PID + ' 30201_50150_LT_HE-H4-262-00-30-002.pdf'
    row = drawing(name)
    path = relative_destination(row, '工艺管道及仪表流程图', name)
    assert path.parts == ('图纸（其他）', '工艺及通用系统', '工艺管道及仪表流程图', 'H4_Habshan 4', '单元262', name)


@pytest.mark.parametrize('unit', ['213', '027', '037', '038', '039', '265', '273'])
@pytest.mark.parametrize(('code', 'discipline'), [('13', '管道'), ('18', '钢结构'), ('19', '安全、消防及火气设备')])
def test_primary_drawings_require_both_unit_and_discipline(unit, code, discipline):
    name = f'DWG-30201_50150_LT_HE-H4-{unit}-{code}-11-001.pdf'
    row = drawing(name, 业务专业='错误的台账专业')
    path = relative_destination(row, row['业务类型'], name)
    assert path.parts == ('图纸', discipline, row['业务类型'], 'H4_Habshan 4', f'单元{unit}', name)


@pytest.mark.parametrize(('unit', 'code'), [('262', '13'), ('213', '15'), ('262', '15'), ('000', '18')])
def test_other_drawings_keep_identical_hierarchy(unit, code):
    name = f'DWG-30201_50150_LT_HE-H4-{unit}-{code}-11-001.pdf'
    row = drawing(name)
    path = relative_destination(row, row['业务类型'], name)
    assert path.parts == ('图纸（其他）', row['业务专业'], row['业务类型'], 'H4_Habshan 4', f'单元{unit}', name)


def test_responsibility_type_switch_controls_primary_drawing_folder():
    name='DWG-30201_50150_LT_HE-H4-027-13-11-001.pdf'
    row=drawing(name)
    scope=dict(disciplines=['管道'],units=['027'],drawing_types=[])
    with patch('ima_manager.collection_rules.responsibility_scope',return_value=scope):
        assert relative_destination(row,row['业务类型'],name).parts[0]=='图纸（其他）'
        scope['drawing_types']=[row['业务类型']]
        assert relative_destination(row,row['业务类型'],name).parts[0]=='图纸'


def test_primary_and_other_drawings_are_both_collected(case):
    downloads, source, ledger, runtime = case
    names = ['4-213-52-0001-1C1-001-01.pdf',
             '30201_50150_LT_HE-H4-273-18-11-001.pdf',
             'DWG-30201_50150_LT_HE-H4-027-19-11-001.pdf', PID + '.pdf']
    rows = [drawing(name) for name in names]
    for name in names:
        (downloads / name).write_bytes(b'%PDF-' + name.encode())
    report = collect(case, rows)
    assert report['counts'] == {'copied': 4}
    assert [r['collection_root'] for r in report['files']] == ['图纸'] * 3 + ['图纸（其他）']
    assert report['collection_roots'] == ['项目管理规定', '图纸', '图纸（其他）',
                                           '设备、材料或碰口清单', '设备、材料或碰口清单（其他）']
    assert collect(case, rows)['counts'] == {'unchanged': 4}


@pytest.mark.parametrize('edited', [False, True])
def test_existing_layout_migrates_before_copy_and_preserves_edited_bytes(case, edited):
    downloads, source, ledger, runtime = case
    name = ISO + '.pdf'
    origin = downloads / name
    origin.write_bytes(b'%PDF-original')
    row = drawing(name)
    old_relative = Path('图纸') / row['业务类型'] / 'H4_Habshan 4' / '单元262' / name
    with patch('ima_manager.collection_rules.relative_destination', return_value=old_relative):
        assert collect(case, [row])['counts'] == {'copied': 1}
    old = source / old_relative
    if edited:
        old.write_bytes(b'%PDF-user-edit')
    preview = collect(case, [row], dry_run=True)
    assert preview['counts'] == {'would_copy': 1}
    assert old.exists()
    assert not (runtime / 'drawing-layout-backups').exists()
    report = collect(case, [row])
    result = report['files'][0]
    assert Path(result['destination']).read_bytes() == origin.read_bytes()
    if edited:
        assert Path(report['migrations'][0]['new']).read_bytes() == b'%PDF-user-edit'
        assert result['destination'] != report['migrations'][0]['new']
    else:
        assert report['counts'] == {'unchanged': 1}
        assert Path(report['migrations'][0]['new']).read_bytes() == origin.read_bytes()
    assert not old.exists()
    assert not (source / '图纸').exists()
    assert origin.read_bytes() == b'%PDF-original'
    assert collect(case, [row])['counts'] == {'unchanged': 1}


@pytest.mark.parametrize('extension', ['.PDF', '.xls', '.xlsx', '.csv'])
@pytest.mark.parametrize(('unit', 'code', 'discipline', 'root'), [
    ('213', '13', '管道', '设备、材料或碰口清单'),
    ('027', '18', '钢结构', '设备、材料或碰口清单'),
    ('273', '19', '安全、消防及火气设备', '设备、材料或碰口清单'),
    ('262', '13', '管道', '设备、材料或碰口清单（其他）'),
    ('213', '00', '工艺及通用系统', '设备、材料或碰口清单（其他）')])
def test_equipment_lists_copy_in_supported_formats_without_business_type_level(case, extension, unit, code, discipline, root):
    downloads, source, ledger, runtime = case
    name = f'NM-30201_50150_LT_HE-H4-{unit}-{code}-00-001{extension}'
    origin = downloads / name
    origin.write_bytes(b'list payload')
    row = drawing(name, 业务专业='错误的台账专业')
    assert is_equipment_list(row)
    assert not is_drawing(row)
    report = collect(case, [row])
    assert report['counts'] == {'copied': 1}
    destination = source / root / discipline / 'H4_Habshan 4' / f'单元{unit}' / name
    assert destination.read_bytes() == origin.read_bytes()
    assert report['files'][0]['collection_root'] == root
    assert collect(case, [row])['counts'] == {'unchanged': 1}


@pytest.mark.parametrize('name', [
    'NM-30201_50150_LT_HE-H4-213-13-00-001.zip',
    'NM-30201_50150_LT_HE-H4-213-13-00-001.dwg',
    'NM-30201_50150_LT_HE-H4-213-13-00-001_CRS.xlsx',
    'NM-30201_50150_LT_HE-H4-213-13-00-001_CRSR.xlsx',
    'NM-30201_50150_LT_HE-H4-213-13-00-001_ATR.pdf',
    'NM-30201_50150_LT_HE-H4-213-13-00-001_CC.pdf',
    'WOODPLC-WTRAN-012345.pdf', 'attachment.xlsx'])
def test_lists_reject_unsupported_formats_and_inherited_review_attachments(name):
    row = drawing(name, 业务类型='设备、材料或碰口清单')
    assert not is_equipment_list(row)
    assert not selected(row)


def test_nested_zip_list_without_ready_path_is_not_extracted(case):
    downloads, source, ledger, runtime = case
    name = 'NM-30201_50150_LT_HE-H4-037-13-00-001.xlsx'
    archive = downloads / 'lists.zip'
    inner = io.BytesIO()
    with zipfile.ZipFile(inner, 'w') as stream:
        stream.writestr('tables/' + name, b'list member')
    with zipfile.ZipFile(archive, 'w') as stream:
        stream.writestr('nested.zip', inner.getvalue())
    before = archive.read_bytes()
    row = drawing(name, 本地路径=archive.name, 所属压缩包文件名=archive.name,
                  包内完整路径='nested.zip!/tables/' + name)
    report = collect(case, [row])
    assert report['counts'] == {'error': 1}
    assert 'ima 不读取或解压 ZIP' in report['files'][0]['reason']
    assert not list(source.rglob('*.xlsx'))
    assert archive.read_bytes() == before


def test_old_other_drawings_name_is_migrated_before_collection(case):
    downloads, source, ledger, runtime = case
    name = PID + '.pdf'
    (downloads / name).write_bytes(b'%PDF-other')
    row = drawing(name)
    old_relative = Path('其他图纸/工艺及通用系统/工艺管道及仪表流程图/H4_Habshan 4/单元262') / name
    with patch('ima_manager.collection_rules.relative_destination', return_value=old_relative):
        assert collect(case, [row])['counts'] == {'copied': 1}
    report = collect(case, [row])
    result = report['files'][0]
    assert result['collection_root'] == '图纸（其他）'
    assert Path(result['destination']).read_bytes() == b'%PDF-other'
    assert report['counts'] == {'unchanged': 1}
    receipt = json.loads((source / '.ima-state/layout-migrations.json').read_text(encoding='utf-8'))[0]
    assert receipt['old'] == str(source / old_relative)
    assert receipt['status'] == 'done'
    assert not (source / old_relative).exists()
    assert not (source / '其他图纸').exists()


def test_list_with_unconfirmed_location_goes_to_other_without_guessing():
    name = 'NM-30201_50150_LT_HE-LEGACY.xlsx'
    row = drawing(name, 目录='H4/213', 业务专业='管道')
    assert is_equipment_list(row)
    assert relative_destination(row, row['业务类型'], name).parts == (
        '设备、材料或碰口清单（其他）', '未识别专业', name)


def test_all_local_migrations_complete_before_opening_any_new_source(case, monkeypatch):
    downloads, source, ledger, runtime = case
    old = source / '其他图纸/旧分类' / (PID + '.pdf')
    old.parent.mkdir(parents=True)
    old.write_bytes(b'%PDF-existing')
    origin = downloads / (ISO + '.pdf')
    origin.write_bytes(b'%PDF-new')
    from ima_manager import collect as module
    original = module.open_source
    def after_migration(path, stop, notify):
        assert not old.exists()
        assert not (source / '其他图纸').exists()
        assert len(list(source.rglob('*.pdf'))) == 1
        return original(path, stop, notify)
    monkeypatch.setattr(module, 'open_source', after_migration)
    report = collect(case, [drawing(origin.name)])
    assert report['counts'] == {'copied': 1}
    assert len(report['migrations']) == 1
