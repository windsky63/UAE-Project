"""Cumulative push independence and automatic interrupted-batch recovery."""
from build_analysis_speed_patch import main

PATCH = '2026.10.31-auto-migration-recovery.1'
PAYLOAD = ('app/briefing_schedule.py', 'app/file_layout.py',
           'app/publication_worker.py', 'app/migration_recovery.py')
GUIDE = f'''FusionLive 中断迁移自动恢复与独立定时发布更新
版本：{PATCH}
适用：2026.10.30-extraction-cache.1基础版本，已安装
2026.10.31-incremental-publication.1累计更新的服务器。
也可覆盖2026.10.31-migration-independent-push.1；无需先安装该旧推送补丁。

部署步骤：
1. 在管理中心安全停止同步工具，等待同步、常驻调度、解析和投递任务退出。
2. 备份服务器 FusionLive同步工具 下以下文件：
   app/briefing_schedule.py
   app/file_layout.py
   app/publication_worker.py
   release_manifest.json
   app/migration_recovery.py（如存在）
   同时备份 runtime/ledger.sqlite3、runtime/file_analysis/queue.sqlite3，
   以及各自同名-wal、-shm文件（如存在）。
3. 解压本包，将 app 文件夹和 release_manifest.json 合并覆盖到服务器
   FusionLive同步工具目录，即main.py、app、runtime所在层级。
   保留服务器原有数据库、下载、历史文件、迁移暂存、标记与微信配置。
4. 运行 maintenance/检查程序完整性.cmd，通过后用原入口启动同步工具。
5. 北京时间02:00—10:30仍为下载时段；窗口外自动恢复已登记的中断迁移。
   服务器当前两条pending记录也会进入恢复流程，无需先执行手工目录整理。

修复行为：
- 文件迁移不再阻止推送文与缓存简报的定时生成、发送。
- 下载退出、文件锁释放后，在独立子进程恢复已登记的迁移批次。
  仅完成恢复记录及路径/队列引用更新，不额外生成整库文件移动方案，不登录下载平台。
- 恢复前备份台账；失败后保留现场和恢复记录，60秒后自动重试，重用同一恢复备份。
- 恢复完成后自动解除迁移自身创建的解析暂停，后续调度恢复解析。
  用户手动停止和整体退出请求继续生效。
- 进程在文件发布后、台账提交前或路径映射收尾阶段中断，重启后可继续恢复。
- 安全退出不会强制终止迁移恢复子进程；恢复任务检查退出请求并保留可接续记录。
- 已提交的发送任务继续保存结果；恢复过程不阻塞定时发布线程。

查看状态（服务器项目根目录CMD）：
powershell -NoProfile -Command "Get-Content '.\\FusionLive同步工具\\runtime\\path_migration.log' -Encoding UTF8 -Tail 30"
powershell -NoProfile -Command "Get-Content '.\\FusionLive同步工具\\runtime\\migration_recovery.json' -Encoding UTF8"
这些文件会在新版自动恢复首次运行后生成。
scheduler.log也会记录恢复启动、结果或下次重试原因。

限制与保留规则：
校验不通过、文件丢失、权限/磁盘错误或另有历史整理恢复记录时，不覆盖冲突内容；
保留现场并记录原因，此类实际数据问题仍需处理，单纯重试不保证解决。
未安装并重启前，旧程序不会获得自动恢复能力。
定时发布仍遵守原会话配置、发送窗口和防重复规则，不自动补发当天过期内容。
既有解析缓存版本不变；本包不含账号、数据库、配置、日志、诊断或测试文件。

验证：
194项相关离线测试通过，包括真实独立恢复子进程、两条pending版本替换、
文件发布后中断、路径映射收尾失败后重试、备份重用、人工停止保留、
下载时段与锁冲突、恢复期间11:00继续投递，以及解析任务安全退出。
打包后校验ZIP、66项程序清单及既有解析缓存版本。

回退：安全停止后恢复步骤2的程序备份与旧清单；不要删除迁移暂存和恢复记录。
如回退后手动整理完成，使用原有明确恢复/启动入口恢复解析。
'''


if __name__ == '__main__':
    main(patch=PATCH, guide_override=GUIDE, payload=PAYLOAD,
         description='Recover journaled file moves asynchronously outside the download window; preserve manual stop ownership, retry with visible state, and keep daily publication independent.')
