"""Prepare a relocated private installation without starting network tasks."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path, PureWindowsPath, PurePosixPath
import shutil
import sys


CONFIG_PATH_FIELDS = {
    'Ima知识库管理/runtime/settings.json': ('source', 'ledger_path', 'downloads_path', 'database_path'),
    '绿联云上传工具/config.json': ('source_root', 'download_ledger'),
    'codex-wechat/wechat.codex.json': ('database_cache', 'uia_profile', 'state_path', 'workspaces'),
}
INTERNAL_FOLDERS = {
    'FusionLive同步工具': {'downloads', 'history', 'runtime', 'exports'},
    'Ima知识库管理': {'待同步', 'runtime'},
    'codex-wechat': {'data'},
    '绿联云上传工具': {'runtime', 'exports'},
}


def relocated_path(value, root):
    if not isinstance(value, str) or not PureWindowsPath(value).is_absolute():
        return value
    if value.startswith(('\\\\', '//')):
        return value  # Keep NAS shares and external dependencies unchanged.
    parts = PureWindowsPath(value).parts
    for index in range(len(parts)-1):
        if parts[index] in INTERNAL_FOLDERS and parts[index+1] in INTERNAL_FOLDERS[parts[index]]:
            suffix = parts[index:]
            if '..' in suffix:
                raise ValueError('Project path contains a parent-directory escape')
            return str(root.joinpath(*suffix).resolve())
    return value


def prepare(root, *, check_only=False, import_credentials=True):
    root = Path(root).resolve()
    if not (root/'deployment-manifest.json').is_file():
        raise ValueError('Run this tool inside the extracted complete deployment package')
    updates = []
    for name, fields in CONFIG_PATH_FIELDS.items():
        path = root/name
        if not path.is_file():
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Configuration path is outside the installation')
        data = json.loads(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('Configuration must be a JSON object')
        changed = []
        for field in fields:
            value = data.get(field)
            replacement = relocated_path(value, root)
            if value != replacement:
                data[field] = replacement
                changed.append(field)
        if changed:
            updates.append({'file':name, 'fields':changed})
            if not check_only:
                temporary = path.with_suffix(path.suffix+'.deploy.tmp')
                temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
                temporary.replace(path)
    imported = False
    bootstrap = root/'Ima知识库管理/credentials.bootstrap.json'
    if not check_only and import_credentials and bootstrap.is_file():
        sys.path.insert(0, str(root/'Ima知识库管理'))
        from ima_manager.config import load_credentials
        load_credentials()  # Encrypt for this Windows user and consume the bootstrap.
        imported = not bootstrap.exists()
    return {'check_only':check_only, 'updated_config_paths':updates,
            'ima_bootstrap_consumed':imported, 'network_accessed':False}


def apply_server_update(root, *, check_only=False, import_credentials=True):
    """Verify an overlay and apply only this release's authorized settings."""
    root = Path(root).resolve()
    manifest = json.loads((root / 'deployment-manifest.json').read_text(encoding='utf-8'))
    if not manifest.get('existing_server_overlay') or not isinstance(manifest.get('files'), dict):
        raise ValueError('This command requires a complete server overlay package')
    bootstrap = root / 'Ima知识库管理/credentials.bootstrap.json'
    credentials = root / 'Ima知识库管理/runtime/credentials.dat'
    for name, expected in manifest['files'].items():
        relative = PurePosixPath(name)
        path = root / name
        if relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name:
            raise ValueError('Invalid update path')
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Update file is outside the installation')
        if path == bootstrap and not path.exists() and credentials.is_file():
            continue  # A successful earlier application consumed the bootstrap.
        data = path.read_bytes()
        if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise ValueError('Update checksum mismatch: ' + name)
    config = root / 'codex-wechat/wechat.codex.json'
    for path in (config, bootstrap, credentials):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('Configuration path is outside the installation')
    data = json.loads(config.read_text(encoding='utf-8-sig')) if config.is_file() else None
    if data is not None and not isinstance(data, dict):
        raise ValueError('WeChat configuration must be a JSON object')
    desired = {'codex_timeout_seconds': 0, 'codex_idle_timeout_seconds': 0}
    if data is not None:
        desired['codex_connect_timeout_seconds'] = data.get('codex_connect_timeout_seconds', 90)
    changed = [key for key, value in desired.items() if data is not None and data.get(key) != value]
    backup = None
    pending_credentials = import_credentials and bootstrap.is_file()
    if not check_only and (changed or pending_credentials):
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
        backup = root / '管理中心/runtime/server-update-backups' / stamp
        if not backup.resolve().is_relative_to(root):
            raise ValueError('Backup directory is outside the installation')
        backup.mkdir(parents=True)
        if changed:
            shutil.copy2(config, backup / 'wechat.codex.json')
        if pending_credentials and credentials.is_file():
            shutil.copy2(credentials, backup / 'ima-credentials.dat')
    if not check_only and changed:
        data.update(desired)
        temporary = config.with_suffix('.json.server-update.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(config)
    imported = False
    if not check_only and pending_credentials:
        sys.path.insert(0, str(root / 'Ima知识库管理'))
        from ima_manager import config as ima_config
        if ima_config.ROOT.resolve() != (root / 'Ima知识库管理').resolve():
            raise ValueError('ima credential importer belongs to another installation')
        ima_config.load_credentials()
        imported = not bootstrap.exists()
        if not imported:
            raise ValueError('ima credentials were not imported; check environment credential overrides')
    return dict(check_only=check_only, release=manifest.get('release'), verified_files=len(manifest['files']),
                updated_wechat_fields=changed, ima_bootstrap_consumed=imported,
                backup=str(backup) if backup else None, network_accessed=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--check-only', action='store_true')
    parser.add_argument('--server-update', action='store_true')
    args = parser.parse_args()
    try:
        operation = apply_server_update if args.server_update else prepare
        print(json.dumps(operation(args.root, check_only=args.check_only), ensure_ascii=False))
    except Exception as exc:
        print('Deployment preparation failed: '+type(exc).__name__, file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
