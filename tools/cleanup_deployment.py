"""Preview and quarantine obsolete deployment files; never delete user files."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid


MANIFEST = 'deployment-manifest.json'
QUARANTINE = '_待检查清理文件'
PROTECTED_DIRS = {
    'runtime', 'logs', 'log', 'downloads', 'history', 'exports', 'data', 'releases',
    '待同步', '删除', 'templates', 'backup', 'backups', '备份', 'config', 'configs',
    '.git', '.agents', '.codex', '.aws', 'node_modules', 'python-runtime',
    'venv', 'env', '__pycache__', '.pytest_cache', QUARANTINE.casefold(),
}
DATA_SUFFIXES = {
    '.log', '.json', '.jsonl', '.db', '.sqlite', '.sqlite3', '.wal', '.shm',
    '.csv', '.xlsx', '.xls', '.xlsm', '.pdf', '.pcf', '.idf', '.sha', '.dwg',
    '.zip', '.7z', '.rar', '.bak', '.tmp', '.key', '.pem', '.pfx', '.p12',
    '.ini', '.toml', '.yaml', '.yml', '.env', '.png', '.jpg', '.jpeg',
}


def linked(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def confined(root, relative):
    parts = PurePosixPath(relative).parts
    if not parts or PurePosixPath(relative).is_absolute() or any(
            p in ('..', '.') or ':' in p or '\\' in p for p in parts):
        raise ValueError('清单包含非法相对路径：' + relative)
    path = root
    for part in parts:
        path = path / part
        if linked(path):
            raise ValueError('不处理链接路径：' + relative)
    if not path.resolve().is_relative_to(root):
        raise ValueError('路径超出项目目录：' + relative)
    return path


def protected(relative):
    path = PurePosixPath(relative)
    if any(part.casefold() in PROTECTED_DIRS or part.casefold().startswith('.venv')
           for part in path.parts[:-1]):
        return True
    name = path.name.casefold()
    # Unknown business files stay put. Only obsolete code/static resources and
    # recognisable documentation are eligible outside the deployment manifest.
    eligible = path.suffix.casefold() in {'.py', '.cmd', '.bat', '.ps1', '.js', '.cjs',
        '.mjs', '.vue', '.css', '.html', '.map', '.md', '.rst'}
    eligible = eligible or (path.suffix.casefold() == '.txt' and
        any(word in name for word in ('说明', '先读我', 'readme', '部署', '报告', '审计')))
    return (path.suffix.casefold() in DATA_SUFFIXES or name.startswith('.') or
            not eligible or
            '.sqlite' in name or '.db-' in name or
            any(word in name for word in ('credential', 'secret', 'token', 'config')) or
            name in {'license', 'notice', 'copying', 'local_credentials.py'})


def expected_files(root):
    manifest_path = confined(root, MANIFEST)
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    files = manifest['files']
    required = {'admin_launcher.py', 'FusionLive同步工具/main.py',
                '管理中心/manager.py', '绿联云上传工具/main.py',
                'Ima知识库管理/ima_manager/sync.py', 'codex-wechat/wechat_local/state.py'}
    if not isinstance(files, dict) or not required.issubset(files):
        raise ValueError('不是完整项目部署清单，停止清理。')
    for name, metadata in files.items():
        confined(root, name)
        if not isinstance(metadata, dict) or not isinstance(metadata.get('sha256'), str):
            raise ValueError('部署清单格式无效。')
    # Refuse to clean an incomplete installation or the wrong directory.
    if any(not confined(root, name).is_file() for name in required):
        raise ValueError('项目核心程序缺失，请先完整部署，停止清理。')
    return {name.casefold() for name in files} | {MANIFEST.casefold()}


def snapshot(path):
    info = path.stat()
    return dict(bytes=info.st_size, mtime_ns=info.st_mtime_ns,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest())


def plan(root):
    root = Path(root).resolve()
    expected = expected_files(root)
    candidates, kept, links = [], [], []
    for current, directories, files in os.walk(root, followlinks=False):
        parent = Path(current)
        retained = []
        for name in directories:
            path = parent / name
            if linked(path):
                links.append(path.relative_to(root).as_posix())
            elif name.casefold() not in PROTECTED_DIRS and not name.casefold().startswith('.venv'):
                retained.append(name)
        directories[:] = retained
        for name in files:
            path = parent / name
            relative = path.relative_to(root).as_posix()
            if linked(path):
                links.append(relative)
            elif relative.casefold() in expected:
                continue
            elif protected(relative):
                kept.append(relative)
            elif stat.S_ISREG(path.stat().st_mode):
                candidates.append(dict(path=relative, **snapshot(path)))
    return dict(root=str(root), candidates=sorted(candidates, key=lambda r:r['path']),
                protected_unlisted=sorted(kept), skipped_links=sorted(links))


def quarantine(root, proposal):
    root = Path(root).resolve()
    if proposal['root'] != str(root):
        raise ValueError('预览与执行目录不一致。')
    expected = expected_files(root)
    # Validate every source and absolute destination before moving anything.
    relative = QUARANTINE + '/' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8]
    destination = confined(root, relative)
    for item in proposal['candidates']:
        name = item['path']
        source = confined(root, name)
        confined(root, relative + '/' + name)
        if name.casefold() in expected or protected(name) or snapshot(source) != {
                k:item[k] for k in ('bytes', 'mtime_ns', 'sha256')}:
            raise ValueError('预览后文件已变化或受保护，停止移动：' + name)
    destination.mkdir(parents=True, exist_ok=False)
    journal = destination / '清理记录.json'
    record = dict(root=str(root), moved=[], errors=[], candidates=proposal['candidates'])

    def save():
        temporary = journal.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, journal)

    save()
    for item in proposal['candidates']:
        try:
            source = confined(root, item['path'])
            target = confined(root, relative + '/' + item['path'])
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or snapshot(source) != {k:item[k] for k in ('bytes','mtime_ns','sha256')}:
                raise ValueError('文件发生变化，未移动')
            source.rename(target)
            record['moved'].append(item['path'])
        except (OSError, ValueError) as exc:
            record['errors'].append(dict(path=item['path'], error=str(exc)))
        save()
    return destination, record


def main():
    parser = argparse.ArgumentParser(description='仅预览；--apply 确认后移入待检查文件夹，不删除文件')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    proposal = plan(root)
    folder = confined(root, 'runtime/cleanup-reports')
    folder.mkdir(parents=True, exist_ok=True)
    report = folder / ('preview-' + datetime.now().strftime('%Y%m%d-%H%M%S-') + uuid.uuid4().hex[:8] + '.json')
    report.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding='utf-8')
    print('项目目录：', root)
    print('预览报告：', report)
    print('候选文件数：', len(proposal['candidates']))
    for item in proposal['candidates']:
        print('待移动：', item['path'])
    print('清单外数据/配置已保留：', len(proposal['protected_unlisted']))
    if not args.apply or not proposal['candidates']:
        print('未移动任何文件。执行命令加 --apply 后输入 MOVE 才会移动。')
        return
    if input('请先停止全部工具并检查上述清单。输入 MOVE 将候选文件移入待检查文件夹：').strip() != 'MOVE':
        print('已取消。')
        return
    destination, record = quarantine(root, proposal)
    print('待检查目录：', destination)
    print('已移动：', len(record['moved']), '失败：', len(record['errors']))
    print('请检查后自行删除此目录；需要恢复时按原相对路径移回。程序不会删除文件。')
    if record['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
