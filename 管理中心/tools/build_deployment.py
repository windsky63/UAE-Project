"""Build a complete deployment of the management centre and every tools folder."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_layout import PROJECT_ROOT as ROOT, tool_directories
from project_integrity import MANIFEST, make_manifest, program_files, verify

RELEASE = 'layout-v1'


def refresh_component_manifests(root):
    files = program_files(root)
    for tool in tool_directories(root):
        if tool.name not in {'FusionLive同步工具', '绿联云上传工具'}:
            continue
        path = tool / 'release_manifest.json'
        if not path.is_file():
            continue
        data = json.loads(path.read_text(encoding='utf-8'))
        if tool.name == 'FusionLive同步工具':
            data.pop('preserved_local_files', None)
        runtime_files = []
        for item in files:
            if not item.is_relative_to(tool) or item == path:
                continue
            parts = item.relative_to(tool).parts
            # Component startup checkers intentionally accept only their own
            # runtime paths. The project manifest still covers every source,
            # test, document and nested resource in the complete archive.
            if len(parts) == 1 or (tool.name == 'FusionLive同步工具' and len(parts) == 2
                                  and parts[0] in {'app', 'maintenance', 'wechat_push'}):
                runtime_files.append(item)
        data['files'] = {p.relative_to(tool).as_posix(): {
            'sha256': hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes': p.stat().st_size}
            for p in runtime_files}
        data['deployment_snapshot'] = datetime.now().strftime('%Y.%m.%d') + '-' + RELEASE
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def verify_sync_startup(entries):
    prefix = 'tools/FusionLive同步工具/'
    if not any(name.startswith(prefix) for name in entries):
        return  # A generic deployment test may contain other tools only.
    with tempfile.TemporaryDirectory(prefix='deployment-startup-') as temporary:
        base = Path(temporary)
        for name, data in entries.items():
            if name.startswith(prefix):
                path = base / name[len(prefix):]
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
        command = [sys.executable, '-X', 'utf8', '-I', '-c',
            'import sys; sys.path.insert(0,sys.argv[1]); from preflight import run_checks; '
            'r=run_checks(sys.argv[2],runtime=False,startup=True); print(repr(r["errors"])); '
            'sys.exit(0 if r["ok"] else 1)', str(base / 'app'), str(base)]
        result = subprocess.run(command, capture_output=True, timeout=120)
        if result.returncode:
            raise RuntimeError('同步工具启动完整性检查失败：' + result.stdout.decode('utf-8', errors='replace')
                               + result.stderr.decode('utf-8', errors='replace'))


def source_backup(root=ROOT):
    root = Path(root).resolve()
    output = root / 'backups' / ('Source_' + datetime.now().strftime('%Y%m%d_%H%M%S_%f') + '.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in program_files(root):
            archive.write(path, path.relative_to(root).as_posix())
    with zipfile.ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError('Source backup CRC failure')
    return output


def build(*, overlay=True, include_ima_credentials=None, include_local_config=False, root=ROOT, build_web=True):
    root = Path(root).resolve()
    if not overlay or include_local_config:
        raise ValueError('仅生成完整程序部署包；服务器业务数据和真实配置独立保留。')
    backup = source_backup(root)
    refresh_component_manifests(root)
    web = root / '管理中心/web'
    if build_web and (web / 'package.json').is_file():
        npm = shutil.which('npm.cmd') or shutil.which('npm')
        if not npm:
            raise RuntimeError('打包需要 Node.js/npm，以构建最新管理中心网页。')
        subprocess.run([npm, 'run', 'build'], cwd=web, check=True)
    if web.is_dir() and not (web / 'dist/index.html').is_file():
        raise RuntimeError('管理中心网页构建产物缺失。')
    tools = [p.name for p in tool_directories(root)]
    if not tools:
        raise RuntimeError('tools 中未发现工具，拒绝生成不完整部署包。')
    required = {'打开管理中心.cmd', '安装依赖.cmd', '验证程序完整性.cmd'}
    if {p.name for p in root.glob('*.cmd')} != required:
        raise RuntimeError('根目录必须且只能保留启动、安装依赖、验证完整性三个 CMD。')
    manifest = make_manifest(root)
    manifest.update(release=datetime.now().strftime('%Y.%m.%d-%H%M%S_%f') + '-' + RELEASE,
                    full_deployment=True, tools=tools, runtime_data_included=False, existing_server_overlay=True)
    entries = {p.relative_to(root).as_posix(): p.read_bytes() for p in program_files(root)}
    if not required.issubset(entries):
        raise RuntimeError('根目录 CMD 缺失或使用了受保护的文件链接。')
    if include_ima_credentials is None:
        preference = root / 'tools/Ima知识库管理/runtime/deployment.json'
        include_ima_credentials = preference.is_file() and json.loads(
            preference.read_text(encoding='utf-8')).get('include_credentials') is True
    if include_ima_credentials:
        import sys
        sys.path.insert(0, str(root / 'tools/Ima知识库管理'))
        from ima_manager.config import load_credentials
        client_id, api_key = load_credentials()
        name = 'tools/Ima知识库管理/credentials.bootstrap.json'
        data = json.dumps(dict(client_id=client_id, api_key=api_key, replace_existing=True)).encode()
        entries[name] = data
        manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    manifest['contains_portable_ima_credentials'] = bool(include_ima_credentials)
    verify_sync_startup(entries)
    entries[MANIFEST] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output = root / 'releases' / ('Project_Tools_' + manifest['release'] + '.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(output) as archive, tempfile.TemporaryDirectory(prefix='complete-deployment-') as temp:
        if len(archive.namelist()) != len(set(archive.namelist())) or archive.testzip() is not None:
            raise RuntimeError('部署包 ZIP 完整性检查失败。')
        if set(archive.namelist()) != set(manifest['files']) | {MANIFEST}:
            raise RuntimeError('部署包与完整程序清单不一致。')
        archive.extractall(temp)
        result = verify(temp, strict=True)
        if not result['ok']:
            raise RuntimeError(str(result['errors']))
    (root / MANIFEST).write_bytes(entries[MANIFEST])
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(checksum + '  ' + output.name + '\n', encoding='utf-8')
    result = dict(archive=str(output), backup=str(backup), files=len(entries), bytes=output.stat().st_size,
                  sha256=checksum, verified=True, full_deployment=True, tools=tools)
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--overlay', action='store_true', help='兼容参数：始终打包全部程序')
    parser.add_argument('--include-ima-credentials', action=argparse.BooleanOptionalAction, default=None,
                        help='携带私有服务器一次性凭证；默认沿用已保存偏好')
    args = parser.parse_args()
    build(include_ima_credentials=args.include_ima_credentials)
