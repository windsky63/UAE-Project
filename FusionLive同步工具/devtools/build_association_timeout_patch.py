"""Build the narrowly scoped association timeout server patch."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / 'app'))
from preflight import run_checks
from fusion_client import now

PATCH = '2026.10.31-association-timeout.1'
BASE = '2026.10.30-extraction-cache.1'


def main():
    manifest_path = HOME / 'release_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['release'] != BASE:
        raise RuntimeError('Unexpected base release')
    data = (HOME / 'app/fusion_client.py').read_bytes()
    manifest['files']['app/fusion_client.py'] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    manifest['patch_release'] = PATCH
    manifest['patch_generated_at_beijing'] = now().isoformat()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report = run_checks(runtime=False)
    if not report['ok']:
        raise RuntimeError(report['errors'])
    guide = f'''FusionLive 关联接口超时修复
补丁版本：{PATCH}
适用基础版本：{BASE}（本包不是完整安装包）

部署步骤：
1. 在服务器安全退出 FusionLive 同步任务，等待同步进程完全退出。
2. 备份服务器 FusionLive同步工具/app/fusion_client.py 和 release_manifest.json。
3. 解压本包，把 app 文件夹和 release_manifest.json 合并覆盖到服务器的
   FusionLive同步工具目录，也就是包含 main.py、app、runtime 的那一层。
   不要解压到项目总目录，也不要多套一层文件夹。
4. 运行 maintenance/检查程序完整性.cmd（如存在），确认检查通过后，
   用原来的入口启动正式下载，继续已保存的关联核对及下载进度。

本次修改：仅 getRelatedContentFSAction 关联接口读取超时由25秒改为90秒；
连接超时仍为8秒，其他接口仍为原来的(8,25)秒。现有重试和完成判定保持不变。
完整性清单同步更新，避免只覆盖代码引发SHA-256校验失败。
本包不含账号、数据库、下载文件、服务器配置或本地诊断数据；无需安装新依赖。
若服务器基础版本不是上述版本，请勿覆盖配套清单，以免混用版本。

验证：本地实登，日志六条记录全部查询成功，小文件228字节下载成功并通过校验；
大型包返回4228项，耗时15.6—23秒。已确认注销。相关25项单元测试通过。
本地未复现服务器超时；部署后仍需观察原失败项，90秒不能保证解决所有网络问题。
若仍超时，请保留新run.log中接口异常、版本ID和实际超时秒数继续排查。

回退：安全退出任务后，恢复步骤2备份的两个文件，再按原入口启动。
'''
    output = HOME / 'releases' / f'FusionLive_Patch_{PATCH}.zip'
    names = ['app/fusion_client.py', 'release_manifest.json']
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(HOME / name, name)
        archive.writestr('部署说明.txt', guide.encode('utf-8-sig'))
    with tempfile.TemporaryDirectory(prefix='fusion-timeout-patch-') as temporary:
        target = Path(temporary)
        for name in manifest['files']:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOME / name, destination)
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == set(names) | {'部署说明.txt'}
            archive.extractall(target)
        verification = run_checks(target, runtime=False)
        assert verification['ok'], verification['errors']
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    output.with_suffix('.README.txt').write_text(guide, encoding='utf-8-sig')
    print(json.dumps({'archive': str(output), 'bytes': output.stat().st_size,
                      'sha256': digest, 'files_checked': report['files_checked'], 'verified': True}, ensure_ascii=True))


if __name__ == '__main__':
    main()
