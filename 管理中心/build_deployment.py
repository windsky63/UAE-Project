"""Build a compact runtime overlay for copy, extract and replace deployment."""
import argparse
import ast
import hashlib
import json
import sys
import subprocess
import tempfile
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
RELEASE = '2026.11.08-cache-cleanup.7'
EXCLUDED = {'.venv', '.venv-analysis', 'node_modules', '__pycache__', '.pytest_cache', '.git',
            'releases', 'exports', 'runtime', 'downloads', 'history', 'logs'}
LOCAL_CONFIG_FILES = (
    '绿联云上传工具/config.json', 'codex-wechat/wechat.codex.json',
    '管理中心/network_config.json', 'Ima知识库管理/runtime/settings.json',
    'FusionLive同步工具/runtime/wechat_push.json',
    'FusionLive同步工具/runtime/login_test_settings.json',
    'FusionLive同步工具/runtime/analysis_settings.json',
    'codex-wechat/data/wechat-uia-profile.json',
)


def verify_sync_startup(entries):
    """Run the shipped checker against extracted bytes, without logging in."""
    prefix = 'FusionLive同步工具/'
    with tempfile.TemporaryDirectory(prefix='deployment-startup-') as temporary:
        root = Path(temporary)
        for name, data in entries.items():
            if name.startswith(prefix):
                path = root / name[len(prefix):]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        # Emulate an existing server's independent credentials; never import or
        # export the developer's real login file for an overlay verification.
        manifest = json.loads((root / 'release_manifest.json').read_bytes())
        if 'app/local_credentials.py' in manifest.get('preserved_local_files', []):
            (root / 'app/local_credentials.py').write_bytes(b'# Existing server credentials\n')
        command = [sys.executable, '-I', '-c',
                   "import sys; sys.path.insert(0, sys.argv[1]); "
                   "from preflight import run_checks; "
                   "r = run_checks(sys.argv[2], runtime=False, startup=True); "
                   "print(repr(r['errors'])); sys.exit(0 if r['ok'] else 1)",
                   str(root / 'app'), str(root)]
        result = subprocess.run(command, capture_output=True, timeout=120)
        if result.returncode:
            raise ValueError('Packaged sync startup check failed: ' +
                             result.stdout.decode('utf-8', errors='replace') +
                             result.stderr.decode('utf-8', errors='replace'))


def build(*, overlay=True, include_ima_credentials=None, include_local_config=False):
    if overlay and include_local_config:
        raise ValueError('Actual local configuration is only allowed in a fresh private installation')
    sys.path.insert(0, str(ROOT / 'Ima知识库管理'))
    from ima_manager.config import deployment_credentials_enabled
    if include_ima_credentials is None:
        include_ima_credentials = deployment_credentials_enabled()
    entries = {}

    def add(path):
        name = path.relative_to(ROOT).as_posix()
        if not path.is_file() or EXCLUDED.intersection(path.relative_to(ROOT).parts):
            return
        if path.suffix in {'.pyc', '.pyo'}:
            return
        if {'tests', 'devtools'}.intersection(path.relative_to(ROOT).parts) or path.name.startswith(('test_', 'build_')):
            return
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError('Deployment source must stay within the project: ' + name)
        entries[name] = path.read_bytes()

    def tree(folder):
        for path in (ROOT / folder).rglob('*'):
            add(path)

    for folder in ('FusionLive同步工具', '绿联云上传工具'):
        base = ROOT / folder
        manifest = json.loads((base / 'release_manifest.json').read_text(encoding='utf-8'))
        for name, metadata in manifest['files'].items():
            source = base / name
            assert hashlib.sha256(source.read_bytes()).hexdigest() == metadata['sha256'], source
            assert source.stat().st_size == metadata['bytes'], source
            add(source)
        add(base / 'release_manifest.json')
    add(ROOT / 'FusionLive同步工具/app/local_credentials.example.py')
    for folder in ('FusionLive同步工具/templates',):
        tree(folder)

    for path in (ROOT / '管理中心').iterdir():
        if path.suffix == '.py' and not path.name.startswith('build_'):
            add(path)
    for folder in ('管理中心/web/dist',
                   'codex-wechat/wechat_local', 'codex-wechat/.agents',
                   'codex-wechat/scripts'):
        tree(folder)
    add(ROOT / 'tools/cleanup_deployment.py')
    tree('Ima知识库管理/ima_manager')
    if include_ima_credentials:
        # Export only to this explicitly requested private archive, never to source files.
        sys.path.insert(0, str(ROOT / 'Ima知识库管理'))
        from ima_manager.config import load_credentials
        client_id, api_key = load_credentials()
        entries['Ima知识库管理/credentials.bootstrap.json'] = json.dumps(
            {'client_id': client_id, 'api_key': api_key, 'replace_existing': True}, ensure_ascii=False).encode('utf-8')
    for name in ('requirements.txt', '打开ima管理工具.cmd'):
        add(ROOT / 'Ima知识库管理' / name)
    add(ROOT / '清理多余文件.cmd')
    for path in (ROOT / 'codex-wechat').iterdir():
        if path.suffix in {'.toml', '.md', '.txt', '.cmd'} or path.name == 'wechat.codex.example.json':
            add(path)
    for name in ('打开管理中心.cmd', 'admin_launcher.py', 'setup_python312.py', 'install_python312.cmd', 'requirements.txt', '恢复微信发送.cmd'):
        add(ROOT / name)
    add(ROOT / '打开管理中心-局域网.cmd')
    add(ROOT / '管理中心/network_config.example.json')
    # NAS uses a template; WeChat includes a safe initial file, not this PC's config.
    entries['绿联云上传工具/config.example.json'] = (ROOT / '绿联云上传工具/config.example.json').read_bytes()
    entries['codex-wechat/wechat.codex.json'] = (ROOT / 'codex-wechat/wechat.codex.example.json').read_bytes()
    initial_wechat = json.loads(entries['codex-wechat/wechat.codex.json'])
    assert initial_wechat['allow_send'] is True and initial_wechat['codex_enabled'] is True and initial_wechat['chats'] == []
    assert initial_wechat['database_root'] == 'D:/xwechat_files'
    assert initial_wechat['database_account'] == ''
    if overlay:
        # Keep the server's selected chats and live account configuration.
        del entries['codex-wechat/wechat.codex.json']
    template = ROOT / 'FusionLive同步工具/data/管道简报模板.xlsx'
    if template.is_file():
        add(template)
    assert '管理中心/web/dist/index.html' in entries
    assert 'codex-wechat/.agents/skills/wechat-chat/SKILL.md' in entries
    assert 'FusionLive同步工具/app/local_credentials.py' in entries
    assert '管理中心/diagnose_shutdown.py' in entries
    for name in ('管理中心/ima_control.py',
                 'Ima知识库管理/ima_manager/__main__.py', 'Ima知识库管理/ima_manager/control.py',
                 'Ima知识库管理/ima_manager/daily.py', 'Ima知识库管理/ima_manager/watch.py',
                 'Ima知识库管理/ima_manager/collect.py', 'Ima知识库管理/ima_manager/document_rules.py',
                 'Ima知识库管理/ima_manager/sync.py', 'Ima知识库管理/ima_manager/scope.py',
                 'Ima知识库管理/requirements.txt'):
        assert name in entries, 'Missing ima integration module: ' + name
    assert any(name.startswith('管理中心/web/dist/assets/ImaPanel-') and name.endswith('.js') for name in entries)
    assert 'Ima知识库管理/ima_manager/gui.py' not in entries
    for name in ('FusionLive同步工具/app/archive_cache.py',
                 'codex-wechat/wechat_local/file_transfer.py'):
        assert name in entries, 'Missing current feature module: ' + name
    assert b'PCF-EXPORT-01' in entries['FusionLive同步工具/app/business_type.py']
    assert b'authorization_quote' in entries['codex-wechat/wechat_local/file_transfer.py']
    for name in ('codex-wechat/wechat_local/shared_backend.py',
                 'codex-wechat/wechat_local/desktop_guard.py', 'FusionLive同步工具/wechat_push/desktop_guard.py',
                 '恢复微信发送.cmd', 'FusionLive同步工具/恢复微信发送.cmd'):
        assert name in entries, 'Missing current shared runtime module: ' + name
    assert entries['codex-wechat/wechat_local/desktop_guard.py'] == entries['FusionLive同步工具/wechat_push/desktop_guard.py']
    if overlay:
        # Overlay archives must not replace the server's existing login file.
        del entries['FusionLive同步工具/app/local_credentials.py']
    for name in ('恢复微信发送.cmd', 'FusionLive同步工具/恢复微信发送.cmd'):
        entries[name].decode('ascii')
        assert b'\n' not in entries[name].replace(b'\r\n', b''), name + ' requires CRLF'
    def documentation(name):
        path = Path(name)
        if '.agents' in path.parts or path.name.casefold() in {'license', 'notice', 'copying'}:
            return False
        if path.suffix.casefold() in {'.md', '.rst'}:
            return True
        return path.suffix.casefold() == '.txt' and not path.name.startswith('requirements')

    omitted = sorted(name for name in entries if documentation(name))
    for name in omitted:
        del entries[name]
    configured = []
    if include_local_config:
        for name in LOCAL_CONFIG_FILES:
            source = ROOT/name
            if not source.is_file():
                continue
            if source.is_symlink() or not source.resolve().is_relative_to(ROOT):
                raise ValueError('Configuration must stay within project: '+name)
            value = source.read_bytes()
            if not isinstance(json.loads(value.decode('utf-8-sig')), dict):
                raise ValueError('Configuration must be a JSON object: '+name)
            entries[name] = value
            configured.append(name)
        for name in ('绿联云上传工具/config.json','codex-wechat/wechat.codex.json',
                     'Ima知识库管理/runtime/settings.json'):
            if name not in configured:
                raise ValueError('Requested actual configuration missing: '+name)
    if not overlay:
        entries['prepare_deployment.py'] = (ROOT/'tools/prepare_deployment.py').read_bytes()
        entries['部署迁移说明.md'] = (ROOT/'tools/deployment_guide.md').read_bytes()
        initialization = '''@echo off
setlocal
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0"
if errorlevel 1 exit /b 1
if exist "%~dp0.venv-analysis\\Scripts\\python.exe" (
  "%~dp0.venv-analysis\\Scripts\\python.exe" -X utf8 "%~dp0prepare_deployment.py"
) else (
  py -3.12 -X utf8 "%~dp0prepare_deployment.py"
)
set "PREPARE_RESULT=%errorlevel%"
popd
pause
exit /b %PREPARE_RESULT%
'''
        entries['初始化新目录.cmd'] = initialization.replace('\n','\r\n').encode('ascii')
    for folder in ('FusionLive同步工具', '绿联云上传工具'):
        key = folder + '/release_manifest.json'
        component = json.loads(entries[key])
        component['files'] = {name: metadata for name, metadata in component['files'].items()
                              if folder + '/' + name in entries}
        if folder == 'FusionLive同步工具':
            if overlay:
                component['preserved_local_files'] = ['app/local_credentials.py']
            else:
                component.pop('preserved_local_files', None)
        entries[key] = (json.dumps(component, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    verify_sync_startup(entries)
    for name in entries:
        parts = Path(name).parts
        assert not {'tests', 'devtools', 'node_modules', '__pycache__'}.intersection(parts), name
        assert not name.startswith('管理中心/web/src/'), name
        assert not Path(name).name.startswith(('test_', 'build_')), name
        assert Path(name).name not in {'AGENTS.md', 'pytest.ini', 'requirements-test.txt'}, name
        assert ('runtime' not in parts or name in LOCAL_CONFIG_FILES) and 'downloads' not in parts, name
        assert 'history' not in parts, name
        assert not any(part.endswith(('.sqlite3','.sqlite3-wal','.sqlite3-shm','.sqlite3.###')) for part in parts), name
        assert name != '绿联云上传工具/config.json' or include_local_config
        assert not name.startswith('codex-wechat/data/') or name in configured, name
        assert 'releases' not in parts and 'exports' not in parts and 'logs' not in parts, name
        if name.endswith('.py'):
            ast.parse(entries[name].decode('utf-8-sig'), filename=name, feature_version=(3, 12))
    if overlay:
        assert not {'codex-wechat/wechat.codex.json', '绿联云上传工具/config.json',
                    '管理中心/network_config.json', 'FusionLive同步工具/app/local_credentials.py'}.intersection(entries)
    manifest = {
        'release': RELEASE,
        'contains_fusionlive_login_credentials': not overlay,
        'contains_portable_ima_credentials': include_ima_credentials,
        'contains_actual_local_configuration': include_local_config,
        'included_local_configuration': configured,
        'runtime_data_included': False,
        'existing_server_overlay': overlay,
        'deployment_method': ('copy archive to server, extract and replace program files, then restart' if overlay
                              else 'extract into a new directory, migrate server data, install environment and initialize configuration'),
        'development_files_included': False,
        'retired_program_files': ['FusionLive同步工具/app/app_paths.py',
                                  'FusionLive同步工具/app/output_paths.py',
                                  'codex-wechat/wechat_local/desktop_lock.py',
                                  'Ima知识库管理/ima_manager/advanced.py',
                                  '管理中心/download_stats.py'],
        'documentation_included': not overlay,
        'runtime_skill_documents_included': True,
        'cleanup_command': '清理多余文件.cmd',
        'cleanup_apply_command': '清理多余文件.cmd --apply',
        'cleanup_unknown_business_files': 'preserved',
        'python': '3.12',
        'python_runtime_included': False,
        'dependency_installation_requires_network': True,
        'python312_syntax_checked': True,
        'component_releases': {folder: json.loads(entries[folder+'/release_manifest.json'])['release']
                               for folder in ('FusionLive同步工具','绿联云上传工具')},
        'preserve_existing': ['*/runtime', '*/downloads', '*/history', '*/logs', '*/exports', 'codex-wechat/data',
                              'Ima知识库管理/待同步', 'Ima知识库管理/待同步/每日更新记录.txt',
                              'codex-wechat/wechat.codex.json', '绿联云上传工具/config.json',
                              '管理中心/network_config.json', 'FusionLive同步工具/app/local_credentials.py'],
        'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                  for name, data in sorted(entries.items())},
    }
    entries['deployment-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    variant = '-fresh-configured' if not overlay and include_local_config else ''
    output = ROOT / '管理中心/releases' / f'Project_Tools_{RELEASE}{variant}.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(output) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())), 'Duplicate archive entries'
        assert set(archive.namelist()) == set(entries), 'Archive contents differ from manifest'
        assert archive.testzip() is None
        for name, metadata in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == metadata['sha256'], name
            assert len(archive.read(name)) == metadata['bytes'], name
        for folder in ('FusionLive同步工具', '绿联云上传工具'):
            release = json.loads(archive.read(folder + '/release_manifest.json'))
            for name, metadata in release['files'].items():
                assert hashlib.sha256(archive.read(folder + '/' + name)).hexdigest() == metadata['sha256'], name
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    result = {'archive': str(output), 'files': len(entries), 'bytes': output.stat().st_size,
              'sha256': digest, 'verified': True, 'sync_startup_integrity_verified': True}
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--overlay', dest='overlay', action='store_true', help='Default: preserve existing server configuration and login credentials')
    mode.add_argument('--fresh-install', dest='overlay', action='store_false', help='Include initial configuration for a new installation')
    parser.set_defaults(overlay=True)
    parser.add_argument('--include-ima-credentials', action=argparse.BooleanOptionalAction, default=None,
                        help='Include one-time ima credentials; default follows the local deployment preference')
    parser.add_argument('--include-local-config', action='store_true',
                        help='Fresh private installation: retain actual ima, NAS and WeChat configuration')
    args = parser.parse_args()
    build(overlay=args.overlay, include_ima_credentials=args.include_ima_credentials,
          include_local_config=args.include_local_config)
