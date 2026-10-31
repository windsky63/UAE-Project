"""Build and verify the server analysis replacement-index patch."""
import hashlib
import json
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / 'app'))
from analysis_worker import parser_version
from fusion_client import now
from preflight import run_checks

PATCH = '2026.10.31-analysis-speed.1'
BASE = '2026.10.30-extraction-cache.1'
PAYLOAD = ('app/analysis_worker.py', 'app/analysis_cache.json', 'app/fusion_client.py')


def main(*, patch=PATCH, guide_override=None, description=None, payload=PAYLOAD):
    manifest_path = HOME / 'release_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['release'] != BASE:
        raise RuntimeError('Unexpected base release')
    expected_key = json.loads((HOME / 'runtime/analysis-speed-diagnosis.json').read_text())['parser_key_before']
    if parser_version() != expected_key:
        raise RuntimeError('Patch would invalidate existing parser caches')
    for name in payload:
        data = (HOME / name).read_bytes()
        manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    manifest['patch_release'] = patch
    manifest['patch_generated_at_beijing'] = now().isoformat()
    manifest['patch_description'] = description or 'Add completed-replacement lookup index; preserve effective parser cache key; include existing HTTP 90-second timeouts.'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report = run_checks(runtime=False)
    if not report['ok']:
        raise RuntimeError(report['errors'])
    guide = f'''FusionLive 解析任务性能修复补丁
版本：{PATCH}
基础版本：{BASE}；本包不是完整安装包。

部署步骤：
1. 在服务器使用原入口安全退出同步工具及后台解析，等待当前文件处理、保存完成，确认相关进程已退出。
2. 备份服务器以下文件：
   app/analysis_worker.py
   app/analysis_cache.json
   app/fusion_client.py
   release_manifest.json
   同时备份 runtime/file_analysis/queue.sqlite3 及同名 -wal、-shm 文件（如存在）。
3. 解压本包，将 app 文件夹与 release_manifest.json 合并覆盖到服务器
   FusionLive同步工具目录，即包含 main.py、app、runtime 的那一层。
   不要覆盖到项目总目录，也不要多套一层目录。
4. 运行 maintenance/检查程序完整性.cmd，确认检查通过后，用原来的入口启动工具和解析。
5. 后台解析初始化时会自动为服务器现有解析数据库添加 jobs_replacement 索引，
   无需手动执行SQL。索引会额外占少量磁盘空间；首次初始化可能稍有等待。

本次修改：
- 为历史缓存清理的替代任务查询添加 (project,file_id,active,state) 索引。
- 保持更新前本地有效解析缓存标识，避免本次性能修改触发全部重新解析。
- 随包带上当前已有的HTTP连接、读取90秒超时版本，避免与更新后的完整性清单混用。
  这是累计包含的既有超时修复，不是下载限速设置。
- 同步更新程序完整性清单。

数据说明：
本包不含账号、数据库、下载文件、服务器配置、测速文件或测试用数据库。
保留服务器原有 runtime、downloads、history、解析结果和配置，无需安装新依赖。
如果服务器不是上述基础版本，请先核对版本，不要混用完整性清单。
无需把本机复制来的 queue.sqlite3 或诊断用数据库传回服务器。

验证：
- 解析与缓存相关33项测试通过，新增测试能检测缺失索引引起的性能退化。
- 服务器解析库副本共85890条任务：旧查询超过8秒未结束；新增索引后同一查询约0.11秒。
  该数据仅为查询性能，不代表整体解析提速倍数，也不能证明服务器下载慢完全由解析造成。
- 原始数据库未修改；打包后解压检查全部清单文件、ZIP校验及缓存标识。

部署后观察：
查看 runtime/file_analysis/progress.log 中连续文件的开始、结束时间。
如下载仍为0.05 MB/s，可安全暂停解析后下载同一个文件对比，进一步区分资源竞争和网络问题。

回退：
安全退出后，恢复步骤2备份的四个程序文件，再按原入口启动。
新增索引可保留，它不修改业务记录；通常不需要回滚数据库。
'''
    if guide_override is not None:
        guide = guide_override
    output = HOME / 'releases' / f'FusionLive_Patch_{patch}.zip'
    names = (*payload, 'release_manifest.json')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(HOME / name, name)
        archive.writestr('部署说明.txt', guide.encode('utf-8-sig'))
    with tempfile.TemporaryDirectory(prefix='fusion-analysis-patch-') as temporary:
        target = Path(temporary)
        for name in manifest['files']:
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(HOME / name, path)
        with zipfile.ZipFile(output) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == set(names) | {'部署说明.txt'}
            archive.extractall(target)
        verification = run_checks(target, runtime=False)
        assert verification['ok'], verification['errors']
        baseline = json.loads((target / 'app/analysis_cache.json').read_text(encoding='utf-8'))
        for name, digest in baseline['source_hashes'].items():
            path = target / (name if name in ('document_coding_rules.json', 'material_split_rules.json') else 'app/' + name)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
        assert baseline['cache_key'] == expected_key
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    output.with_suffix('.README.txt').write_text(guide, encoding='utf-8-sig')
    print(json.dumps({'archive': str(output), 'bytes': output.stat().st_size,
                      'sha256': digest, 'files_checked': report['files_checked'],
                      'verified': True}, ensure_ascii=True))


if __name__ == '__main__':
    main()
