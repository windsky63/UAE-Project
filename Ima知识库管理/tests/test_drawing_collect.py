"""PDF drawing collection through the same read-only, incremental pipeline as PP."""
from contextlib import closing
import hashlib
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import zipfile

from openpyxl import Workbook
import pytest

from ima_manager.collect import collect_documents
from ima_manager.collection_rules import is_drawing, relative_destination
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
    destination = source / '图纸' / category / plant / unit / origin.name
    assert destination.read_bytes() == before[0]
    assert (origin.read_bytes(), origin.stat().st_mtime_ns) == before
    assert report['files'][0]['collection_root'] == '图纸'
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
    destination = source / '图纸/工艺管道及仪表流程图/H4_Habshan 4/单元262' / name
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
def test_zip_pdf_members_are_read_without_modifying_archive(case, nested):
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
    assert report['counts'] == {'copied': 1}
    assert (source / '图纸/仪表安装详图/H2_Habshan 2/单元057' / name).read_bytes() == b'%PDF-member'
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
    assert not (source / '图纸/仪表安装详图').exists()


def test_database_real_cli_collects_exact_identity_without_credentials_or_excel(case):
    downloads, source, ledger, runtime = case
    runtime.mkdir()
    database = runtime / 'ledger.sqlite3'
    name = PID + '.pdf'
    actual = downloads / (PID + '-2.pdf')
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
    assert (source / '图纸/工艺管道及仪表流程图/H4_Habshan 4/单元262' / actual.name).read_bytes() == actual.read_bytes()
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
    assert relative.parts == ('图纸', item['category'], *folders, name)
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
    root = source / '图纸/工艺管道及仪表流程图/H4_Habshan 4'
    assert (root / '单元113' / actual_name).read_bytes() == b'%PDF-113'
    assert (root / '单元200' / actual_name).read_bytes() == b'%PDF-200'


def test_same_type_with_multiple_numbers_in_same_unit_has_one_location():
    name = PID + ' 30201_50150_LT_HE-H4-262-00-30-002.pdf'
    row = drawing(name)
    path = relative_destination(row, '工艺管道及仪表流程图', name)
    assert path.parts == ('图纸', '工艺管道及仪表流程图', 'H4_Habshan 4', '单元262', name)
