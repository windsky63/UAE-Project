"""Build and verify a code-only update for an existing project-tools server."""
import argparse
import ast
import hashlib
import json
from pathlib import Path, PurePosixPath
import zipfile

HOME = Path(__file__).resolve().parent
ROOT = HOME.parent
RELEASE = '2026.10.29-excel-ledger.1'
MANIFEST = 'admin-update-manifest.json'
GUIDE = '先读我-服务器更新.md'
EXCLUDED = {'runtime', 'data', 'downloads', 'logs', 'exports', 'releases', 'node_modules',
            '__pycache__', '.pytest_cache', '.git', '.venv', '.venv-analysis'}
SYNC_FILES = {
    'main.py', 'app/path_maintenance.py', 'app/path_maintenance_launcher.py', 'app/file_layout.py', 'app/history_repair.py',
    'app/inventory.py', 'app/workflow.py', '整理文件目录.cmd', 'docs/文件目录整理.md',
    'app/ledger_workbook.py', 'app/台账.py',
    'app/archive_index.py', 'app/sync.py', 'app/fusion_client.py', 'app/ledger.py',
    'app/runtime_logging.py', 'app/reporting.py', 'app/analysis_settings.py', 'document_coding_rules.json',
    'app/business_type.py',
    'app/analysis_worker.py', 'app/local_analysis.py', 'app/analysis_cache.json', 'app/weld_classification.py',
    'app/briefing.py', 'app/briefing_schedule.py', 'app/preflight.py', 'app/scheduler.py', 'app/wechat_delivery.py',
    'wechat_push/uia.py', 'wechat_push/desktop_guard.py',
    '恢复微信发送.cmd',
    'release_manifest.json', 'devtools/build_update_package.py',
    'devtools/create_test_briefing.py', 'docs/部署新版.txt',
}
SYNC_ENTRIES = {'FusionLive同步工具/' + name for name in SYNC_FILES}
REQUIRED = {
    '管理中心/server.py', '管理中心/manager.py', '管理中心/codex_chat.py',
    '管理中心/daily_publications.py', '管理中心/publication_runner.py',
    '管理中心/codex_project_tools.py', '管理中心/briefing_runner.py',
    '管理中心/web/src/PublicationsPanel.vue',
    '管理中心/codex_runtime.py', '管理中心/log_tools.py', '管理中心/diagnose_shutdown.py', '管理中心/web/dist/index.html',
    '管理中心/web/src/LogsPanel.vue',
    '管理中心/web/src/ConfigPanel.vue', '管理中心/web/src/App.vue',
    'codex-wechat/wechat_local/app_server.py', 'codex-wechat/wechat_local/memory.py',
    'codex-wechat/wechat_local/codex_runner.py', 'codex-wechat/wechat_local/listener.py',
    'codex-wechat/wechat_local/config.py', 'codex-wechat/wechat_local/state.py',
    'codex-wechat/wechat_local/spool.py', 'codex-wechat/wechat_local/wechat_uia.py',
    'codex-wechat/wechat_local/desktop_guard.py',
    'codex-wechat/wechat_local/shared_backend.py',
    'codex-wechat/wechat_local/delivery.py', 'codex-wechat/wechat_local/wechat_capture.py',
    'codex-wechat/wechat_local/__main__.py',
    'codex-wechat/.agents/skills/wechat-chat/SKILL.md',
    'codex-wechat/.agents/skills/wechat-chat/project-qa.md',
    'codex-wechat/wechat.codex.example.json', GUIDE,
    '恢复微信发送.cmd',
} | SYNC_ENTRIES


def digest(data):
    return hashlib.sha256(data).hexdigest()


def allowed_name(name):
    path = PurePosixPath(name)
    return (not path.is_absolute() and '..' not in path.parts and '\\' not in name and ':' not in name
            and not EXCLUDED.intersection(path.parts)
            and name not in {'codex-wechat/wechat.codex.json', '管理中心/network_config.json'}
            and not name.startswith('绿联云上传工具/')
            and (not name.startswith('FusionLive同步工具/') or name in SYNC_ENTRIES))


def collect_entries(root=ROOT):
    entries = {}
    root = Path(root).resolve()
    def add(path):
        if not path.is_file() or path.suffix in {'.pyc', '.pyo'}:
            return
        name = path.relative_to(root).as_posix()
        if not allowed_name(name):
            return
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Update sources must be regular files within the project: ' + name)
        entries[name] = path.read_bytes()

    admin = root / '管理中心'
    for path in admin.glob('*.py'):
        add(path)
    for name in ('README.md', 'SERVER_UPDATE_2026_10_28.md'):
        add(admin / name)
    for folder in ('管理中心/web/src', '管理中心/web/dist', '管理中心/tests',
                   'codex-wechat/wechat_local', 'codex-wechat/.agents/skills/wechat-chat',
                   'codex-wechat/scripts', 'codex-wechat/tests'):
        for path in (root / folder).rglob('*'):
            if folder.startswith('管理中心/web/') or path.suffix in {'.py', '.md', '.txt'} or path.name in {'LICENSE', 'NOTICE'}:
                add(path)
    for name in ('package.json', 'index.html', 'vite.config.js'):
        add(admin / 'web' / name)
    for name in ('README.md', 'requirements.txt', 'pyproject.toml', 'wechat.codex.example.json',
                 '启动单机监听.cmd', '安装单机版.cmd'):
        add(root / 'codex-wechat' / name)
    for name in ('打开管理中心.cmd', '打开管理中心-局域网.cmd', 'admin_launcher.py',
                 'setup_python312.py', 'install_python312.cmd', 'requirements.txt', '恢复微信发送.cmd'):
        add(root / name)
    for name in SYNC_ENTRIES:
        add(root / name)
    entries[GUIDE] = (admin / 'SERVER_UPDATE_2026_10_28.md').read_bytes()
    return entries


def validate_entries(entries):
    missing = REQUIRED - entries.keys()
    if missing:
        raise ValueError('Update is missing required files: ' + ', '.join(sorted(missing)))
    for name, data in entries.items():
        if not allowed_name(name):
            raise ValueError('Unexpected update entry: ' + name)
        if name.endswith('.py'):
            ast.parse(data.decode('utf-8-sig'), filename=name, feature_version=(3, 12))
    assert entries['codex-wechat/wechat_local/desktop_guard.py'] == entries['FusionLive同步工具/wechat_push/desktop_guard.py'], 'Desktop protection copies differ'
    for name in ('恢复微信发送.cmd', 'FusionLive同步工具/恢复微信发送.cmd'):
        entries[name].decode('ascii')
        assert b'\n' not in entries[name].replace(b'\r\n', b''), name + ' requires CRLF'
    example = json.loads(entries['codex-wechat/wechat.codex.example.json'])
    assert example['chats'] == [] and example['allow_send'] and example['codex_enabled']
    assert example['database_root'] == 'D:/xwechat_files'
    assert example['database_account'] == ''
    assert example['codex_backend'] == 'app-server'
    assert example['codex_session_rotate_percent'] == 70 and example['codex_session_prewarm_percent'] == 55
    launcher = entries['打开管理中心.cmd']
    launcher.decode('ascii')
    assert b'\n' not in launcher.replace(b'\r\n', b''), 'Windows launcher requires CRLF'
    sync_manifest = json.loads(entries['FusionLive同步工具/release_manifest.json'])
    preflight_source = ast.parse(entries['FusionLive同步工具/app/preflight.py'].decode('utf-8-sig'))
    version = next(ast.literal_eval(node.value) for node in preflight_source.body
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == 'RELEASE_ID'
                                               for target in node.targets))
    assert version == sync_manifest['release'], 'Sync checker/manifest version mismatch'
    for name, metadata in sync_manifest['files'].items():
        packaged = entries.get('FusionLive同步工具/' + name)
        if packaged is not None:
            assert digest(packaged) == metadata['sha256'] and len(packaged) == metadata['bytes'], name


def verify_archive(path, target_root=None):
    path = Path(path)
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), 'Duplicate ZIP entries'
        assert archive.testzip() is None, 'ZIP CRC validation failed'
        manifest = json.loads(archive.read(MANIFEST))
        assert manifest['release'] == RELEASE and manifest['base_installation_required']
        assert not manifest['runtime_included'] and not manifest['credentials_included']
        assert set(names) == set(manifest['files']) | {MANIFEST}, 'Unexpected files in ZIP'
        entries = {name: archive.read(name) for name in manifest['files']}
        validate_entries(entries)
        for name, data in entries.items():
            assert digest(data) == manifest['files'][name]['sha256'], name
            assert len(data) == manifest['files'][name]['bytes'], name
    checksum = digest(path.read_bytes())
    sidecar = path.with_suffix('.zip.sha256')
    if sidecar.is_file():
        assert sidecar.read_text(encoding='utf-8').split()[0] == checksum, 'ZIP SHA256 mismatch'
    result = dict(archive=str(path.resolve()), release=RELEASE, files=len(entries), bytes=path.stat().st_size,
                  sha256=checksum, python312_syntax_checked=True, verified=True)
    if target_root is not None:
        sync = Path(target_root).resolve() / 'FusionLive同步工具'
        sync_manifest = json.loads(entries['FusionLive同步工具/release_manifest.json'])
        for name, metadata in sync_manifest['files'].items():
            if 'FusionLive同步工具/' + name in entries:
                continue
            source = sync / name
            if source.is_symlink() or not source.resolve().is_relative_to(sync):
                raise ValueError('Invalid existing sync file: ' + name)
            data = source.read_bytes()
            if digest(data) != metadata['sha256'] or len(data) != metadata['bytes']:
                raise ValueError('Existing sync file differs from the required base; no files changed: ' + name)
        result['base_installation_verified'] = True
    return result


def build():
    entries = collect_entries()
    validate_entries(entries)
    manifest = dict(release=RELEASE, format_version=2, base_installation_required=True,
        update_scope=['管理中心', 'codex-wechat shared backend and replies', 'FusionLive briefing and desktop protection patch', 'root launch/setup/resume scripts'],
        sync_base_release='2026.10.27-prefab-rules.1',
        runtime_included=False, credentials_included=False, python='3.12', instructions=GUIDE,
        preserve=['codex-wechat/wechat.codex.json', 'codex-wechat/data', '管理中心/runtime',
                  '管理中心/network_config.json', 'FusionLive同步工具/runtime', 'FusionLive同步工具/exports',
                  'FusionLive同步工具/downloads', 'FusionLive同步工具/logs',
                  'FusionLive同步工具/app/local_credentials.py', '绿联云上传工具', '.venv-analysis'],
        files={name: dict(sha256=digest(data), bytes=len(data)) for name, data in sorted(entries.items())})
    output = HOME / 'releases' / f'Project_Tools_Server_Update_{RELEASE}.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
        archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
    output.with_suffix('.zip.sha256').write_text(digest(output.read_bytes()) + '  ' + output.name + '\n', encoding='utf-8')
    return verify_archive(output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify', type=Path, help='Verify an existing ZIP without rebuilding or extracting it')
    parser.add_argument('--target-root', type=Path, help='Read-only validation of unchanged sync program files on the existing server')
    args = parser.parse_args()
    if args.target_root and not args.verify:
        parser.error('--target-root requires --verify')
    print(json.dumps(verify_archive(args.verify, args.target_root) if args.verify else build(), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
