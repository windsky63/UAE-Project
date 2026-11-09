"""Build and verify the narrowly scoped network read retry patch."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / 'app'))
from fusion_client import now
from preflight import run_checks

PATCH = '2026.11.02-network-retry.1'
BASE = '2026.10.30-extraction-cache.1'
PREVIOUS = '2026.11.01-history-alias.1'
GUIDE = f'''FusionLive 网络读取重试更新
补丁版本：{PATCH}
适用：基础版本 {BASE}，已安装 {PREVIOUS} 的服务器。
这是本次增量包，不包含之前补丁；服务器版本不符时不要覆盖整份清单。

部署步骤：
1. 在管理中心安全停止同步及常驻调度，等待进程完成注销并退出。
2. 核对服务器 release_manifest.json 中 release 和 patch_release 与上述适用版本一致。
3. 备份服务器 app/fusion_client.py 和 release_manifest.json。
4. 将本包 app 文件夹及 release_manifest.json 合并覆盖到服务器
   D:\\阿联酋项目\\FusionLive同步工具（main.py、app、runtime 所在层）。
   不要解压到项目总目录，不要多套一层目录。
5. 运行 maintenance/检查程序完整性.cmd，通过后使用原有入口重新启动下载。
   当天已结束的旧任务不会自动重启；重新启动后沿用保存的进度。

本次更新：
- GET 请求超时、连接中断及 HTTP 502/503/504，等待5、15、30秒重试，
  首次请求加3次重试，共最多4次；原连接/读取超时仍为90/90秒。
- 等待期间每秒检查停止请求和访问时段，窗口结束或人工停止即退出等待。
- 记录接口路径、异常类型、等待时间及重试次数；不输出参数、密码或Cookie。
- 不重放POST认证，不重试证书错误、401/403/418/429。
- 单文件流传输失败仍使用原有下载队列最多3次尝试机制；
  请求重试耗尽后仍按原流程保存进度并注销，不无限重启任务。

日志排查：
查看 logs/2026-11-02/run.log 中02:04:23前约100行及失败后的收尾部分。
同时查看当日 run-*-summary.json 中 ended 对应02:04的那份，贴出 run 对象。
旧版顶层错误只保存异常文字，没有完整调用堆栈或失败接口，因此不保证
旧日志能定位具体接口；新版本的“网络请求暂时失败”会记录接口路径。
若 run.log 已轮转，查看同目录 run.1.log、run.2.log 等文件。

本包仅包含 app/fusion_client.py、release_manifest.json、本说明。
不含账号、配置、数据库、日志或下载文件，无需新增依赖。
验证：61项离线网络重试、同步、登录、退出和调度测试通过；未登录真实平台。
打包时校验ZIP并在临时程序目录覆盖解压后执行完整程序清单校验。
回退：安全停止后恢复第3步备份的两个文件。
'''


def main():
    manifest_path = HOME / 'release_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['release'] != BASE or manifest.get('patch_release') not in (PREVIOUS, PATCH):
        raise RuntimeError('Unexpected base release/patch')
    data = (HOME / 'app/fusion_client.py').read_bytes()
    manifest['files']['app/fusion_client.py'] = {
        'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    manifest.update(patch_release=PATCH, patch_generated_at_beijing=now().isoformat(),
                    patch_description='Bounded GET network retries with cancellable backoff and endpoint diagnostics.',
                    patch_previous_release=PREVIOUS)
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output = HOME / 'releases' / f'FusionLive_Patch_{PATCH}.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('app/fusion_client.py', data)
        archive.writestr('release_manifest.json', encoded)
        archive.writestr('部署说明.txt', GUIDE.encode('utf-8-sig'))
    with tempfile.TemporaryDirectory(prefix='fusion-network-retry-') as temporary:
        target = Path(temporary)
        for name in manifest['files']:
            destination = target / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOME / name, destination)
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == {'app/fusion_client.py', 'release_manifest.json', '部署说明.txt'}
            archive.extractall(target)
        report = run_checks(target, runtime=False)
        if not report['ok']:
            raise RuntimeError(report['errors'])
    manifest_path.write_bytes(encoded)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    output.with_suffix('.README.txt').write_text(GUIDE, encoding='utf-8-sig')
    print(json.dumps({'archive': str(output), 'bytes': output.stat().st_size,
                      'sha256': digest, 'files_checked': report['files_checked'],
                      'verified': True}, ensure_ascii=True))


if __name__ == '__main__':
    main()
