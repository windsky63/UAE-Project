"""Build the two-module layout-conflict download patch without business data."""
import hashlib
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
import zipfile

HOME = Path(__file__).resolve().parents[1]
PATCH = '2026.11.05-layout-skip.1'
PAYLOAD = ('app/sync.py', 'app/file_layout.py')
DESCRIPTION = 'Isolate pre-download target conflicts by dependency group; retain files and continue independent downloads.'
GUIDE = '''FusionLive 下载路径冲突跳过补丁
适用基础版本：2026.11.04-owner-updates.1。

1. 在管理中心安全停止同步、常驻调度和后台解析，等待进程退出。
2. 备份服务器 app/sync.py、app/file_layout.py 和 release_manifest.json。
3. 将本包 app 目录及 release_manifest.json 合并覆盖到服务器
   D:\\阿联酋项目\\FusionLive同步工具（main.py、app、runtime 所在层级）。
4. 使用原有入口重新启动下载，沿用台账和已保存清单。

下载前整批路径规划遇到“迁移目标已有文件”时，改为按文件依赖组准备。
冲突项保留原文件，记入失败台账并跳过，继续其他独立文件。
此类失败项不自动反复重试；排除路径冲突后使用原有重试失败项入口。
已完成但路径待整理的文件保留完成状态及原路径，不重新下载。
发布冲突时保留已校验暂存和旧文件，等待后续处理，不冒充发布成功。
未完成迁移的恢复、校验失败、认证限制和磁盘不足仍保留原有保护。

本包只含两个程序文件、发布校验清单及本说明。
保留服务器配置、凭证、runtime、downloads、history 和 logs。
无需删除冲突PDF、迁移标记或修改台账，无需重新安装依赖。
针对性回归：5项通过，另有3个子测试通过；未执行完整回归或真实平台下载。
'''


def main():
    path = HOME / 'release_manifest.json'
    manifest = json.loads(path.read_text(encoding='utf-8'))
    if manifest['release'] != '2026.11.04-owner-updates.1':
        raise RuntimeError('Unexpected base release')
    for name, entry in manifest['files'].items():
        if name not in PAYLOAD:
            data = (HOME / name).read_bytes()
            if len(data) != entry['bytes'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
                raise RuntimeError('Unrelated baseline mismatch: ' + name)
    for name in PAYLOAD:
        data = (HOME / name).read_bytes()
        compile(data, name, 'exec')
        manifest['files'][name] = dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    manifest.update(patch_release=PATCH,
                    patch_generated_at_beijing=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                    patch_description=DESCRIPTION)
    encoded = (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    output = HOME / 'releases' / ('FusionLive_Patch_' + PATCH + '.zip')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in PAYLOAD:
            archive.write(HOME / name, name)
        archive.writestr('release_manifest.json', encoded)
        archive.writestr('部署说明.txt', GUIDE.encode('utf-8-sig'))
    with zipfile.ZipFile(output) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == set(PAYLOAD) | {'release_manifest.json', '部署说明.txt'}
        for name in PAYLOAD:
            assert hashlib.sha256(archive.read(name)).hexdigest() == manifest['files'][name]['sha256']
    path.write_bytes(encoded)
    output.with_suffix('.zip.sha256').write_text(hashlib.sha256(output.read_bytes()).hexdigest() + '  ' + output.name + '\n', encoding='utf-8')
    output.with_suffix('.README.txt').write_text(GUIDE, encoding='utf-8-sig')
    print(json.dumps(dict(archive=str(output), bytes=output.stat().st_size, verified=True), ensure_ascii=True))


if __name__ == '__main__':
    main()
