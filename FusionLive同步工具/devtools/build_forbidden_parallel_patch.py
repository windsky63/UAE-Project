"""Package the cumulative per-file 403 and concurrent-analysis server fix."""
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

PATCH = '2026.11.01-history-alias.1'
BASE = '2026.10.30-extraction-cache.1'
PAYLOAD = ('app/sync.py', 'app/analysis_worker.py', 'app/analysis_cache.json',
           'app/fusion_client.py', 'app/file_layout.py', 'app/briefing_schedule.py',
           'app/publication_worker.py', 'app/migration_recovery.py')
GUIDE = f'''FusionLive 历史与当前版本同路径修复（累计更新）
版本：{PATCH}
适用基础版本：{BASE}。本包为累计程序补丁，不是首次安装包。
累计包含此前的下载选择优化、增量文件发布、迁移自动恢复及HTTP超时修复。

部署步骤：
1. 安全停止服务器管理中心内的同步任务、常驻调度和后台解析，等待进程退出。
2. 备份本包 app 目录中的八个同名文件及 release_manifest.json；
   同时备份服务器 runtime/ledger.sqlite3、runtime/file_analysis/queue.sqlite3
   及同名 -wal、-shm 文件（如存在）。
3. 将本包 app 文件夹和 release_manifest.json 合并覆盖到服务器的
   FusionLive同步工具目录，也就是 main.py、app、runtime 所在层级。
   不要解压到项目总目录，也不要多套一层目录。
4. 运行 maintenance/检查程序完整性.cmd，检查通过后从原入口重新启动。
   当天恢复下载可在管理中心使用原有启动下载入口；已结束的旧任务不会自动复活。
5. 如之前手动停止了解析，请使用原有恢复解析入口；本包保留人工停止状态。

本次行为：
- 历史记录和当前记录引用同一文件时，仅当两者已记录的大小、SHA256一致且当前状态为done，
  才允许生成独立历史副本；复制前及发布前核验实际内容，不移动或删除当前文件。
  历史副本写入history/version-历史记录ID目录，保存迁移恢复记录，可在中断后继续。
  同一文件有多条历史记录时分别保留副本；历史复制不会重定向当前解析任务路径。
  新旧内容不同、缺失校验身份或物理文件校验失败时，保留原文件并报错，不猜测版本归属。
  本修复在原有下载准备/迁移流程中自动执行，不需要手动删除文件或改台账。
- 文件下载返回HTTP 403时，记录失败、文件名及内容ID，跳过本轮并继续下一个文件。
  同一文件每轮只尝试一次，不会立即对同一403连续重试。
  失败项保留为可重试，后续轮次仍遵守原有最多3次尝试限制；
  已耗尽次数的文件需使用原有重试失败项入口重新加入队列。
- 恢复解析在北京时间02:00（含）—10:30（不含）的时段门禁。
  下载回调、常驻调度及手动恢复入口均不能在此时段启动解析。
  已运行的解析进入02:00后，完成并保存当前文件后退出，不领取下一任务。
  10:30后常驻调度可再次启动解析；人工停止状态仍需明确恢复。
- 下载后的移动替换遇到解析/投递任务占用时，先保留已校验的新文件，继续后续下载。
  提交解析安全停止请求，解析保存当前文件后释放占用，不强行中断。
  后续下载循环会重试移动；传输队列结束后继续等待待替换文件完成，仍响应安全停止和时段结束。
  Windows共享冲突（错误32/33）也会保留迁移日志并延后重试。
  普通权限不足、校验失败等错误仍明确报错，不伪装成占用后无限重试。
  未替换的已校验文件在runtime/download_ready中登记，重启后可恢复，无需重新传输。
- 登录异常401、访问限制418/429、磁盘不足和人工停止仍按原有方式退出。
  文件迁移期间的解析保护、迁移恢复的下载时段避让及文件锁仍保留。
- 保持现有解析缓存标识，避免本次调度改动导致全量重解析；不新增依赖。

部署后观察：
出现403应看到“下载被拒绝（HTTP 403），已记录失败并跳过本轮”，随后继续其他文件。
解析日志见 runtime/file_analysis/progress.log。
所有403项仍标记失败，不会伪装为下载完成；本包不会解决平台本身的权限问题。

数据保留：
本包不含账号密码、真实配置、数据库、下载内容、日志或测试数据。
保留服务器原有 local_credentials.py、所有配置、runtime、downloads、history及解析结果。
保持现有基础版本及配置；若完整性检查出现本包之外文件不匹配，请先核对服务器版本。

验证：相关离线回归覆盖下载时段门禁、自动/手动入口、跨时段退出、403跳过、延后替换及安全退出。
新增6项历史同路径回归：独立副本、改名并归档、多历史引用、身份冲突拒绝、物理校验及发布后中断恢复。
新增真实文件锁测试：占用期间继续下载下一文件，释放后替换；安全停止并重开台账后无需重下。
打包后解压校验ZIP、全部程序清单及解析缓存版本；未执行真实平台下载。

回退：安全停止后恢复步骤2备份的程序及旧清单，保留现有业务数据和迁移记录。
'''


def main():
    manifest_path = HOME / 'release_manifest.json'
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if manifest['release'] != BASE:
        raise RuntimeError('Unexpected base release')
    cache_path = HOME / 'app/analysis_cache.json'
    baseline = json.loads(cache_path.read_text(encoding='utf-8'))
    # Only orchestration changed; the parser and its business rules are unchanged.
    for name, digest in baseline['source_hashes'].items():
        path = HOME / (name if name.endswith('_rules.json') else 'app/' + name)
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if name == 'analysis_worker.py':
            baseline['source_hashes'][name] = actual
        elif actual != digest:
            raise RuntimeError('Unrelated parser change: ' + name)
    cache_path.write_text(json.dumps(baseline, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    assert parser_version() == baseline['cache_key']
    for name in PAYLOAD:
        data = (HOME / name).read_bytes()
        manifest['files'][name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
    manifest.update(patch_release=PATCH, patch_generated_at_beijing=now().isoformat(),
                    patch_description='Journal independent history copies for verified identical current/history path aliases; preserve current file and analysis references; retain download-window analysis exclusion, 403 skip and deferred publication.')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    report = run_checks(runtime=False)
    if not report['ok']:
        raise RuntimeError(report['errors'])
    output = HOME / 'releases' / f'FusionLive_Patch_{PATCH}.zip'
    output.parent.mkdir(parents=True, exist_ok=True)
    names = (*PAYLOAD, 'release_manifest.json')
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in names:
            archive.write(HOME / name, name)
        archive.writestr('部署说明.txt', GUIDE.encode('utf-8-sig'))
    with tempfile.TemporaryDirectory(prefix='fusion-forbidden-parallel-') as temporary:
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
        copied = json.loads((target / 'app/analysis_cache.json').read_text(encoding='utf-8'))
        assert copied['cache_key'] == baseline['cache_key']
        for name, digest in copied['source_hashes'].items():
            path = target / (name if name.endswith('_rules.json') else 'app/' + name)
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest, name
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix('.zip.sha256').write_text(digest + '  ' + output.name + '\n', encoding='utf-8')
    output.with_suffix('.README.txt').write_text(GUIDE, encoding='utf-8-sig')
    print(json.dumps({'archive': str(output), 'bytes': output.stat().st_size,
                      'sha256': digest, 'files_checked': report['files_checked'],
                      'parser_cache_preserved': True, 'verified': True}, ensure_ascii=True))


if __name__ == '__main__':
    main()
