"""Build a complete private Windows deployment, retaining authorized credentials."""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent.parent
RELEASE = '2026.10.31-all-http-timeout90-full.1'
EXCLUDED = {'.venv', '.venv-analysis', 'node_modules', '__pycache__', '.pytest_cache', '.git',
            'releases', 'exports', 'runtime', 'downloads', 'history', 'logs'}


def build(*, overlay=False):
    entries = {}

    def add(path):
        name = path.relative_to(ROOT).as_posix()
        if not path.is_file() or EXCLUDED.intersection(path.relative_to(ROOT).parts):
            return
        if path.suffix in {'.pyc', '.pyo'}:
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
    for folder in ('FusionLive同步工具/docs', 'FusionLive同步工具/devtools',
                   'FusionLive同步工具/tests', 'FusionLive同步工具/templates'):
        tree(folder)

    for path in (ROOT / '管理中心').iterdir():
        if path.suffix in {'.py', '.md'}:
            add(path)
    for folder in ('管理中心/tests', '管理中心/web/src', '管理中心/web/dist',
                   'codex-wechat/wechat_local', 'codex-wechat/.agents',
                   'codex-wechat/scripts', 'codex-wechat/tests'):
        tree(folder)
    for path in (ROOT / '管理中心/web').iterdir():
        if path.suffix in {'.json', '.js', '.cjs', '.html'}:
            add(path)
    for path in (ROOT / 'codex-wechat').iterdir():
        if path.suffix in {'.toml', '.md', '.txt', '.cmd'} or path.name == 'wechat.codex.example.json':
            add(path)
    for name in ('打开管理中心.cmd', 'admin_launcher.py', 'setup_python312.py', 'install_python312.cmd', 'requirements.txt', '恢复微信发送.cmd'):
        add(ROOT / name)
    entries['先读我-Python312部署.md'] = (ROOT / '管理中心/PYTHON312_DEPLOYMENT.md').read_bytes()
    entries['先读我-本次服务器更新.md'] = (ROOT / '管理中心/SERVER_DEPLOY_2026_10_30.md').read_bytes()
    if overlay:
        entries['先读我-本次服务器更新.md'] = (ROOT / '管理中心/SERVER_DEPLOY_2026_10_31.md').read_bytes()
    guide = ROOT / '部署说明-给Codex.md'
    if not guide.is_file():
        guide = ROOT / '管理中心/DEPLOYMENT_CODEX.md'
    entries['部署说明-给Codex.md'] = ('> 本包使用服务器 Python 3.12，不含解释器或虚拟环境。先阅读“先读我-Python312部署.md”，在最终目录运行 install_python312.cmd 安装依赖。\n\n'.encode('utf-8') + guide.read_bytes())
    add(ROOT / '打开管理中心-局域网.cmd')
    add(ROOT / '管理中心/network_config.example.json')
    # NAS uses a template; WeChat includes a safe initial file, not this PC's config.
    entries['绿联云上传工具/config.example.json'] = (ROOT / '绿联云上传工具/config.json').read_bytes()
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
    # Reference-only deliverables; never overwrite a server's active ledger.
    for path in ROOT.glob('PP-30201_50150_LT_HE-*.pdf'):
        entries['部署参考/编码规则/' + path.name] = path.read_bytes()
    audit = ROOT / 'PCF台账复核.xlsx'
    if audit.is_file():
        entries['部署参考/' + audit.name] = audit.read_bytes()

    assert '管理中心/web/dist/index.html' in entries
    assert 'codex-wechat/.agents/skills/wechat-chat/SKILL.md' in entries
    assert 'FusionLive同步工具/app/local_credentials.py' in entries
    assert '管理中心/diagnose_shutdown.py' in entries
    assert '管理中心/web/src/LogsPanel.vue' in entries
    for name in ('FusionLive同步工具/app/archive_cache.py',
                 'FusionLive同步工具/devtools/audit_pcf_ledger.py',
                 'codex-wechat/wechat_local/file_transfer.py'):
        assert name in entries, 'Missing current feature module: ' + name
    assert b'PCF-EXPORT-01' in entries['FusionLive同步工具/app/business_type.py']
    assert b'authorization_quote' in entries['codex-wechat/wechat_local/file_transfer.py']
    for name in ('codex-wechat/wechat_local/shared_backend.py',
                 'codex-wechat/wechat_local/desktop_guard.py', 'FusionLive同步工具/wechat_push/desktop_guard.py',
                 '恢复微信发送.cmd', 'FusionLive同步工具/恢复微信发送.cmd'):
        assert name in entries, 'Missing current shared runtime module: ' + name
    assert entries['codex-wechat/wechat_local/desktop_guard.py'] == entries['FusionLive同步工具/wechat_push/desktop_guard.py']
    for name in ('恢复微信发送.cmd', 'FusionLive同步工具/恢复微信发送.cmd'):
        entries[name].decode('ascii')
        assert b'\n' not in entries[name].replace(b'\r\n', b''), name + ' requires CRLF'
    for name in entries:
        parts = Path(name).parts
        assert 'runtime' not in parts and 'downloads' not in parts, name
        assert 'history' not in parts, name
        assert not any(part.endswith(('.sqlite3','.sqlite3-wal','.sqlite3-shm','.sqlite3.###')) for part in parts), name
        assert name != '绿联云上传工具/config.json'
        assert not name.startswith('codex-wechat/data/'), name
        assert 'releases' not in parts and 'exports' not in parts and 'logs' not in parts, name
        if name.endswith('.py'):
            ast.parse(entries[name].decode('utf-8-sig'), filename=name, feature_version=(3, 12))
    manifest = {
        'release': RELEASE,
        'contains_fusionlive_login_credentials': True,
        'runtime_data_included': False,
        'existing_server_overlay': overlay,
        'instructions': '部署说明-给Codex.md',
        'python': '3.12',
        'python_runtime_included': False,
        'dependency_installation_requires_network': True,
        'python312_syntax_checked': True,
        'component_releases': {folder: json.loads(entries[folder+'/release_manifest.json'])['release']
                               for folder in ('FusionLive同步工具','绿联云上传工具')},
        'preserve_existing': ['*/runtime', '*/downloads', '*/history', '*/logs', '*/exports', 'codex-wechat/data',
                              'codex-wechat/wechat.codex.json', '绿联云上传工具/config.json',
                              '管理中心/network_config.json', 'FusionLive同步工具/app/local_credentials.py'],
        'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                  for name, data in sorted(entries.items())},
    }
    entries['deployment-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    output = ROOT / '管理中心/releases' / f'Project_Tools_{RELEASE}.zip'
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
    print(json.dumps({'archive': str(output), 'files': len(entries), 'bytes': output.stat().st_size,
                      'sha256': digest, 'verified': True}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--overlay', action='store_true', help='Preserve existing server WeChat configuration')
    build(overlay=parser.parse_args().overlay)
