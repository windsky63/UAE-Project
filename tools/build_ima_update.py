"""Build the ima overlay, following the authorized private server packaging preference."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path, PurePosixPath
import tempfile
import sys
import zipfile


ROOT = Path(__file__).resolve().parents[1]
RELEASE = '2026.11.06-ima-credentials-retry.1'
IMA = 'Ima知识库管理'
SYNC = 'FusionLive同步工具'
SOURCE = r'D:\阿联酋项目文件管理\Ima知识库管理\待同步'
GUIDE = f'''ima 完整工具更新：{RELEASE}

本包用于已有最新项目管理工具的服务器，不是独立首次安装包。
包含 ima 全部运行模块、使用说明和入口，以及提取依赖的两个同步工具共享模块。
不含数据库、下载文件、待同步资料、同步回执或凭证，不覆盖服务器已有账号及知识库配置。

部署：
1. 在管理中心安全停止 ima、下载调度及解析，等待任务退出，再退出管理中心。
2. 备份服务器 Ima知识库管理/ima_manager、FusionLive同步工具/app/business_type.py、
   FusionLive同步工具/app/archive_index.py 及 FusionLive同步工具/release_manifest.json。
3. 将 ZIP 全部内容合并覆盖到服务器项目根目录，即能直接看到
   Ima知识库管理、FusionLive同步工具、管理中心的那一层。不要删除原有工具目录。
4. 在项目根目录双击“应用ima更新.cmd”。脚本先校验所有包内文件，再只更新两个共享模块
   的启动校验哈希和 ima 的 source 字段；保留服务器其他配置，自动备份原配置与校验清单。
   待同步目录固定设为：{SOURCE}
   本步骤不移动旧待同步资料、不清理业务文件、不启动云端同步。
5. 重新打开管理中心，确认待同步目录，然后点击“提取本地资料”。
   首次正式提取会清理新待同步目录中已有的“删除/更新旧版”专用目录。

行为：
- 只提取下载原目录中实际存在并符合分类规则的文件，平台已失效的本地旧版也可提取。
- 未下载新版直接跳过；不提取下载原目录外的 history 和下载暂存资料。
- 本地副本更新时校验后原子替换，不再保留“待同步/删除/更新旧版”。
- 手动放入“删除”其他目录的文件保留；原下载文件的替换、移动及归档由同步工具管理。
- 云端旧版处理沿用已有逻辑。

配置迁移：
只改配置路径，不自动搬迁旧待同步目录。若旧路径仍有需要沿用的待同步资料，停止工具后
由你确认并搬到上述新路径；保留 ima 原 runtime 和凭证文件，不清空同步回执。

回退：停止相关工具，恢复步骤2备份的程序，再恢复
Ima知识库管理/runtime/backups/ima-update-时间/ 内的原 settings.json 和同步工具校验清单。
正常提取清理的本地历史旧副本不在程序回退范围内。
'''


def build(*, include_ima_credentials=None):
    sys.path.insert(0, str(ROOT / IMA))
    from ima_manager.config import deployment_credentials_enabled, load_credentials
    if include_ima_credentials is None:
        include_ima_credentials = deployment_credentials_enabled()
    entries = {}
    for path in sorted((ROOT / IMA / 'ima_manager').glob('*.py')):
        entries[path.relative_to(ROOT).as_posix()] = path.read_bytes()
    for name in ('requirements.txt', '打开ima管理工具.cmd', 'README.md'):
        entries[IMA + '/' + name] = (ROOT / IMA / name).read_bytes()
    for name in ('app/business_type.py', 'app/archive_index.py'):
        entries[SYNC + '/' + name] = (ROOT / SYNC / name).read_bytes()
    entries['应用ima更新.py'] = (ROOT / 'tools/ima_update_apply.py').read_bytes()
    entries['应用ima更新.cmd'] = ('''@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv-analysis\\Scripts\\python.exe" (
  echo 未找到项目 Python 环境，请在现有项目根目录解压此更新包。
  pause
  exit /b 1
)
"%~dp0.venv-analysis\\Scripts\\python.exe" "%~dp0应用ima更新.py"
if errorlevel 1 (
  echo 更新未完成，请保留上方错误信息。
  pause
  exit /b 1
)
echo 配置和程序清单更新完成，请重新打开管理中心。
pause
''').replace('\n', '\r\n').encode('utf-8')
    guide = GUIDE
    if include_ima_credentials:
        client_id, api_key = load_credentials()
        entries[IMA + '/credentials.bootstrap.json'] = json.dumps(
            dict(client_id=client_id, api_key=api_key, replace_existing=True), ensure_ascii=False).encode('utf-8')
        guide = guide.replace('不含数据库、下载文件、待同步资料、同步回执或凭证，不覆盖服务器已有账号及知识库配置。',
            '不含业务数据库、下载资料或同步回执。按用户授权携带新ima凭证，仅用于个人服务器D:\\阿联酋项目文件管理；不修改知识库配置。')
        guide += '\n凭证迁移：下次启动ima并加载凭证时，将包内一次性凭证加密保存到服务器当前Windows用户，替换旧凭证并删除迁移文件。保留原runtime回执。ZIP本身仍含凭证。\n'
    guide += '\n本包同时包含220021连续三次中止、200001有限退避重试与接口名日志。\n'
    guide += '目录查询优化：每个计划/执行阶段按目录缓存；无同名新增文件不读取整份目标目录，普通根目录新增上传无需目录列表。移动、归档前后的必要身份与结果核验保留。\n'
    entries['ima更新部署说明.txt'] = guide.encode('utf-8-sig')
    release = json.loads((ROOT / SYNC / 'release_manifest.json').read_text(encoding='utf-8'))
    manifest = dict(release=RELEASE, base_sync_release=release['release'], source=SOURCE,
                    generated_at_beijing=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    runtime_data_included=False, credentials_included=include_ima_credentials,
                    files={name: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
                           for name, data in entries.items()})
    entries['ima-update-manifest.json'] = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output = ROOT / 'releases' / ('ima-full-update-' + RELEASE + '.zip')
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    with tempfile.TemporaryDirectory(prefix='ima-package-check-') as temporary:
        target = Path(temporary)
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            assert len(archive.namelist()) == len(set(archive.namelist()))
            assert set(archive.namelist()) == set(entries)
            for name in archive.namelist():
                relative = PurePosixPath(name)
                assert not relative.is_absolute() and '..' not in relative.parts and ':' not in name
                assert not {'runtime', 'downloads', 'history', 'tests', '__pycache__'}.intersection(relative.parts)
                data = archive.read(name)
                assert data == entries[name]
                if name.endswith('.py'):
                    compile(data, name, 'exec')
                if name in manifest['files']:
                    assert hashlib.sha256(data).hexdigest() == manifest['files'][name]['sha256']
            archive.extractall(target)
        # Verify the updater against a fake existing installation, preserving settings.
        (target / SYNC / 'release_manifest.json').write_text(json.dumps(release), encoding='utf-8')
        settings_path = target / IMA / 'runtime/settings.json'
        settings_path.parent.mkdir()
        prior = dict(source='D:/old/source', kb='server-kb', folder='server-folder', interval_seconds=123,
                     auto_sync_enabled=False, database_path='D:/server/ledger.sqlite3')
        settings_path.write_text(json.dumps(prior), encoding='utf-8')
        spec = importlib.util.spec_from_file_location('ima_update_check', target / '应用ima更新.py')
        updater = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(updater)
        updater.apply(target)
        actual = json.loads(settings_path.read_text(encoding='utf-8'))
        assert actual == dict(prior, source=SOURCE)
        updated = json.loads((target / SYNC / 'release_manifest.json').read_text(encoding='utf-8'))
        for name in ('app/business_type.py', 'app/archive_index.py'):
            assert updated['files'][name] == manifest['files'][SYNC + '/' + name]
        updater.apply(target)  # Safe to rerun after an interrupted deployment.
        assert json.loads(settings_path.read_text(encoding='utf-8')) == actual
        payload = target / IMA / 'ima_manager/collect.py'
        payload.write_bytes(payload.read_bytes() + b'\n# changed\n')
        before = settings_path.read_bytes()
        try:
            updater.apply(target)
        except ValueError:
            pass
        else:
            raise AssertionError('Modified payload was accepted')
        assert settings_path.read_bytes() == before
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    result = dict(archive=str(output), bytes=output.stat().st_size, files=len(entries), sha256=digest,
                  zip_verified=True, updater_verified=True)
    output.with_suffix('.verification.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--include-ima-credentials', action=argparse.BooleanOptionalAction, default=None)
    args = parser.parse_args()
    build(include_ima_credentials=args.include_ima_credentials)
