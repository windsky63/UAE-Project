"""Apply the configuration and integrity metadata for the adjacent ima update."""
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil


SOURCE = r'D:\阿联酋项目文件管理\Ima知识库管理\待同步'
IMA = 'Ima知识库管理'
SYNC = 'FusionLive同步工具'


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.ima-update.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def apply(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'ima-update-manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        relative = PurePosixPath(name)
        path = root / name
        if (relative.is_absolute() or '..' in relative.parts or ':' in name or '\\' in name
                or path.is_symlink() or not path.resolve().is_relative_to(root)):
            raise ValueError('Invalid update path: ' + name)
        data = path.read_bytes()
        if len(data) != expected['bytes'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise ValueError('Update checksum mismatch: ' + name)
    release_path = root / SYNC / 'release_manifest.json'
    release = json.loads(release_path.read_text(encoding='utf-8'))
    if release.get('format') != 1 or release.get('release') != manifest['base_sync_release']:
        raise ValueError('同步工具基础版本不匹配，请使用与本更新包匹配的最新部署版本。')
    settings_path = root / IMA / 'runtime/settings.json'
    settings = json.loads(settings_path.read_text(encoding='utf-8')) if settings_path.exists() else {}
    if not isinstance(settings, dict):
        raise ValueError('ima 设置必须是 JSON 对象，未修改配置。')
    for path in (release_path, settings_path):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError('配置文件位置异常，未修改。')
    backup = root / IMA / 'runtime/backups' / ('ima-update-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
    backup.mkdir(parents=True)
    shutil.copy2(release_path, backup / 'sync-release_manifest.json')
    if settings_path.exists():
        shutil.copy2(settings_path, backup / 'settings.json')
    for name in ('app/business_type.py', 'app/archive_index.py'):
        release['files'][name] = manifest['files'][SYNC + '/' + name]
    release.update(patch_release=manifest['release'],
                   patch_description='ima collects local downloads including retained versions; excludes history and staging.')
    settings['source'] = SOURCE
    write_json(release_path, release)
    write_json(settings_path, settings)
    return dict(updated=True, source=SOURCE, backup=str(backup), release=manifest['release'])


if __name__ == '__main__':
    print(json.dumps(apply(Path(__file__).resolve().parent), ensure_ascii=False, indent=2))
