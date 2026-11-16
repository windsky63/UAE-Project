"""Complete program inventory. Runtime data and local configuration stay local."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_layout import PROJECT_ROOT

MANIFEST = '管理中心/tools/integrity-manifest.json'
EXCLUDED_DIRS = {'.git', '.venv', '.venv-analysis', 'node_modules', '__pycache__', '.pytest_cache',
                 'runtime', 'downloads', 'history', 'logs', 'exports', 'releases', 'backups', '待同步'}
GLOBAL_EXCLUDES = {'.git', '.venv', '.venv-analysis', 'node_modules', '__pycache__', '.pytest_cache'}
LOCAL_FILES = {'config.json', 'wechat.codex.json', 'network_config.json', 'local_credentials.py',
               'credentials.dat', 'credentials.bootstrap.json', '.env'}


def program_files(root=PROJECT_ROOT):
    root = Path(root).resolve()
    files = [p for p in root.iterdir() if p.is_file() and not p.is_symlink() and p.suffix.lower() == '.cmd']
    if (root / 'README.md').is_file():
        files.append(root / 'README.md')
    for base in [root / '管理中心', root / 'tools']:
        if not base.is_dir():
            continue
        if base.is_symlink() or base.is_junction():
            raise ValueError('有效程序目录不能使用链接：' + str(base))
        for current, directories, names in os.walk(base, followlinks=False):
            current = Path(current)
            component_root = current == root / '管理中心' or current.parent == root / 'tools'
            directories[:] = [name for name in directories if name not in GLOBAL_EXCLUDES and
                not (component_root and name in EXCLUDED_DIRS) and
                not (current / name).is_symlink() and not (current / name).is_junction() and
                not (current == root / 'tools/codex-wechat' and name == 'data')]
            for name in names:
                path = current / name
                relative = path.relative_to(root).as_posix()
                local = (name in LOCAL_FILES and (component_root or name in {
                    'local_credentials.py', 'credentials.dat', 'credentials.bootstrap.json', '.env'}))
                if relative == 'tools/FusionLive同步工具/app/local_credentials.py':
                    local = False
                if (relative in {MANIFEST, '管理中心/config/logging.json'} or local or path.suffix in {'.pyc', '.pyo'}
                        or path.is_symlink() or path.is_junction()):
                    continue
                if not path.resolve().is_relative_to(root):
                    raise ValueError('程序文件超出项目目录：' + str(path))
                files.append(path)
    return sorted(files)


def make_manifest(root=PROJECT_ROOT):
    root = Path(root).resolve()
    return {'format': 1, 'layout': 'management-tools-v1', 'files': {
        path.relative_to(root).as_posix(): {'bytes': path.stat().st_size,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()} for path in program_files(root)}}


def verify(root=PROJECT_ROOT, *, strict=False):
    root = Path(root).resolve()
    path = root / MANIFEST
    if not path.is_file():
        return {'ok': False, 'errors': ['缺少管理中心完整性清单，请使用完整部署包。'], 'checked': 0}
    manifest = json.loads(path.read_text(encoding='utf-8'))
    errors = []
    for name, expected in manifest['files'].items():
        target = root / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or '\\' in name or ':' in name
                or target.is_symlink() or not target.resolve().is_relative_to(root)):
            errors.append('清单路径无效：' + name)
        elif not target.is_file():
            errors.append('缺失：' + name)
        elif target.stat().st_size != expected['bytes'] or hashlib.sha256(target.read_bytes()).hexdigest() != expected['sha256']:
            errors.append('内容不一致：' + name)
    if strict:
        extra = {p.relative_to(root).as_posix() for p in program_files(root)} - set(manifest['files'])
        errors.extend('清单未覆盖：' + name for name in sorted(extra))
    return {'ok': not errors, 'checked': len(manifest['files']), 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=PROJECT_ROOT)
    parser.add_argument('--write', action='store_true', help='生成当前有效程序的完整性清单')
    parser.add_argument('--strict', action='store_true')
    args = parser.parse_args()
    if args.write:
        path = args.root / MANIFEST
        path.write_text(json.dumps(make_manifest(args.root), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    result = verify(args.root, strict=args.strict)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
