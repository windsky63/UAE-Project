"""Layout, discovery and full archives use disposable project trees only."""
from pathlib import Path
import json
import sys
import zipfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_layout import tool_directories, tool_path
from project_integrity import MANIFEST, make_manifest, verify
from install_dependencies import installation_plan
from build_deployment import build
from build_update import verify_archive


def project(tmp_path):
    root = tmp_path / '中文项目 带空格'
    admin = root / '管理中心'
    admin.mkdir(parents=True)
    (admin / 'tools').mkdir()
    (admin / 'tools/requirements.txt').write_text('requests\n', encoding='utf-8')
    (admin / 'server.py').write_text('# management\n', encoding='utf-8')
    for name in ('打开管理中心.cmd', '安装依赖.cmd', '验证程序完整性.cmd'):
        (root / name).write_bytes(b'@echo off\r\n')
    for name in ('同步工具', 'future-tool'):
        base = root / 'tools' / name
        (base / 'assets/deep').mkdir(parents=True)
        (base / 'requirements.txt').write_text('requests\n', encoding='utf-8')
        (base / 'main.py').write_text('print("tool")\n', encoding='utf-8')
        (base / 'assets/deep/unlisted.custom').write_bytes(b'important resource')
        (base / 'assets/config.json').write_bytes(b'{"asset":true}')
        (base / 'assets/history').mkdir()
        (base / 'assets/history/reference.txt').write_bytes(b'static history documentation')
        (base / 'tests').mkdir()
        (base / 'tests/test_tool.py').write_text('def test_ok(): assert True\n', encoding='utf-8')
    parked = root / '其他'
    parked.mkdir()
    (parked / 'old.py').write_bytes(b'do not restore or include')
    return root


def test_discovery_and_installation_cover_new_tools_without_registration(tmp_path):
    root = project(tmp_path)
    assert {p.name for p in tool_directories(root)} == {'同步工具', 'future-tool'}
    plan = installation_plan(root)
    assert len(plan['requirements']) == 3
    assert all(path.is_relative_to(root / 'tools') or path.parent == root / '管理中心/tools' for path in plan['requirements'])
    assert tool_path('future-tool', root) == root / 'tools/future-tool'
    with pytest.raises(ValueError):
        tool_path('../其他', root)


def test_complete_package_includes_unlisted_assets_and_all_tools_and_keeps_parked_files(tmp_path):
    root = project(tmp_path)
    before = (root / '其他/old.py').read_bytes()
    result = build(root=root, build_web=False)
    assert Path(result['archive']).parent == root / 'releases'
    assert Path(result['backup']).parent == root / 'backups'
    with zipfile.ZipFile(result['archive']) as archive:
        names = set(archive.namelist())
        for tool in ('同步工具', 'future-tool'):
            assert f'tools/{tool}/main.py' in names
            assert f'tools/{tool}/assets/deep/unlisted.custom' in names
            assert f'tools/{tool}/assets/config.json' in names
            assert f'tools/{tool}/assets/history/reference.txt' in names
            assert f'tools/{tool}/tests/test_tool.py' in names
        assert not any(name.startswith('其他/') for name in names)
        assert MANIFEST in names
    assert verify_archive(result['archive'])['verified']
    assert verify(root, strict=True)['ok']
    assert (root / '其他/old.py').read_bytes() == before
    assert not (root / 'old.py').exists()


def test_runtime_data_and_local_settings_are_not_overwritten_by_package(tmp_path):
    root = project(tmp_path)
    tool = root / 'tools/future-tool'
    (tool / 'runtime').mkdir()
    (tool / 'runtime/receipts.sqlite3').write_bytes(b'keep database')
    (tool / 'config.json').write_bytes(b'keep server config')
    (root/'管理中心/config').mkdir()
    (root/'管理中心/config/logging.json').write_bytes(b'keep local logging policy')
    (root/'管理中心/config/logging.example.json').write_bytes(b'{}')
    result = build(root=root, build_web=False)
    with zipfile.ZipFile(result['archive']) as archive:
        assert 'tools/future-tool/runtime/receipts.sqlite3' not in archive.namelist()
        assert 'tools/future-tool/config.json' not in archive.namelist()
        assert '管理中心/config/logging.json' not in archive.namelist()
        assert '管理中心/config/logging.example.json' in archive.namelist()
    assert (tool / 'runtime/receipts.sqlite3').read_bytes() == b'keep database'


def test_integrity_detects_missing_modified_and_new_program_files(tmp_path):
    root = project(tmp_path)
    (root / MANIFEST).write_text(json.dumps(make_manifest(root)), encoding='utf-8')
    assert verify(root, strict=True)['ok']
    target = root / 'tools/future-tool/main.py'
    target.write_bytes(b'changed')
    assert not verify(root)['ok']
    target.unlink()
    assert any('缺失' in message for message in verify(root)['errors'])
    (root / '管理中心/new.py').write_bytes(b'new')
    assert any('清单未覆盖' in message for message in verify(root, strict=True)['errors'])


def test_sync_credentials_are_included_in_deployment_and_integrity_manifest(tmp_path, monkeypatch):
    root = project(tmp_path)
    app = root / 'tools/FusionLive同步工具/app'
    app.mkdir(parents=True)
    credentials = b'def get_credentials(): return ("test-user", "test-password")\n'
    (app / 'local_credentials.py').write_bytes(credentials)
    (app.parent / 'release_manifest.json').write_text(
        json.dumps({'format': 1, 'preserved_local_files': ['app/local_credentials.py']}),
        encoding='utf-8')
    # This fixture tests packaging; real FusionLive startup uses the full app.
    monkeypatch.setattr('build_deployment.verify_sync_startup', lambda entries: None)
    result = build(root=root, build_web=False)
    name = 'tools/FusionLive同步工具/app/local_credentials.py'
    with zipfile.ZipFile(result['archive']) as archive:
        assert archive.read(name) == credentials
        assert name in json.loads(archive.read(MANIFEST))['files']
        component = json.loads(archive.read('tools/FusionLive同步工具/release_manifest.json'))
        assert 'app/local_credentials.py' in component['files']
        assert not component.get('preserved_local_files')
    assert verify_archive(result['archive'])['verified']


def test_repeated_builds_are_full_and_keep_separate_source_backups(tmp_path):
    root = project(tmp_path)
    first = build(root=root, build_web=False)
    (root / 'tools/future-tool/next.py').write_bytes(b'new module')
    second = build(root=root, build_web=False)
    assert first['backup'] != second['backup']
    assert first['archive'] != second['archive']
    with zipfile.ZipFile(second['archive']) as archive:
        assert 'tools/同步工具/main.py' in archive.namelist()
        assert 'tools/future-tool/next.py' in archive.namelist()
