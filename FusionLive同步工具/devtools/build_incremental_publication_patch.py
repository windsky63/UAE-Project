"""Build the cumulative scoped publication server update."""
from build_analysis_speed_patch import main, PAYLOAD

PATCH = '2026.10.31-incremental-publication.1'
GUIDE = f'''FusionLive 登记及迁移性能修复（累计更新）
补丁版本：{PATCH}
适用基础版本：2026.10.30-extraction-cache.1，本包不是完整安装包。
包含此前下载任务选择优化、02:00—10:30禁止解析、解析查询索引及HTTP超时修复。

部署步骤：
1. 安全退出服务器同步、常驻调度及后台解析，等待当前任务保存、进程全部退出。
2. 备份以下六个程序文件：
   app/file_layout.py
   app/sync.py
   app/analysis_worker.py
   app/analysis_cache.json
   app/fusion_client.py
   release_manifest.json
   同时备份 runtime/ledger.sqlite3、runtime/file_analysis/queue.sqlite3
   及各自同名 -wal、-shm 文件（如存在），以服务器原文件为准。
3. 解压本包，将 app 文件夹与 release_manifest.json 合并覆盖到服务器
   FusionLive同步工具目录，也就是 main.py、app、runtime 所在的层级。
   不要覆盖到项目总目录或多套一层文件夹。
4. 运行 maintenance/检查程序完整性.cmd，确认通过后按原入口启动。
5. 查看 scheduler.log/run.log 的“下载阶段耗时”，重点比较“登记及迁移”。

本次修复：
- 启动时建立本轮下载稳定的路径映射及版本依赖组。
- 每个更新文件只读取、校验、归档和发布本组文件；多个新版共用旧版时，
  保持原规则：全部相关新版完成前保留旧版文件。
- 逐文件发布不再备份整个数据库、重建全项目路径映射、遍历整个下载目录、
  重放全部历史迁移来更新解析队列；只更新本次涉及的队列路径引用。
- 保留逐文件SHA256校验、不覆盖冲突目标、新旧版本归档和迁移日志。
  逐文件迁移在文件移动前写入SQLite恢复记录；异常中断后走完整恢复流程。
- 启动准备、未完成迁移恢复及手工全量整理继续使用完整检查流程。
  因此启动/恢复阶段仍可能等待；本次主要消除每个文件重复进行的全量维护。
- 保留三段耗时日志：选择任务、请求接收及校验、登记及迁移。
- 北京时间02:00—10:30禁止解压解析，10:30后常驻调度自动重试，
  手动停止标记仍按原规则处理；已有解析缓存版本标识保持不变。

验证：
148项相关测试通过。
覆盖增量计划与全量计划结果一致、新旧版本保留、不触碰无关文件、
禁止全量扫描/目录遍历/重建映射、无新增逐文件整库备份、
发布后提交前中断恢复、校验失败保留现场、多个新版共用旧版等情况。
并运行下载、任务选择、调度、解析、缓存、导出及简报相关测试。
打包后解压验证ZIP、全部65个程序清单文件及解析缓存标识。
服务器此前测得登记及迁移41.562秒；本包尚未在服务器实测，
部署后应根据新日志确认实际改善，网络和磁盘性能仍可能独立影响耗时。

数据与依赖：
本包不含账号、数据库、下载文件、配置、测试样本或本地诊断数据；无需新增依赖。
保留服务器 runtime、downloads、history、exports 和已有进度。
不要把本机复制来的数据库或诊断数据库传回服务器。

回退：
安全退出全部任务后恢复步骤2备份的六个程序文件。
如存在 runtime/layout.pending，请保留迁移现场并先完成恢复，不要删除暂存文件。
新增的查询索引可以保留；一般无需回滚数据库或下载文件。
'''


if __name__ == '__main__':
    main(patch=PATCH, guide_override=GUIDE,
         payload=(*PAYLOAD, 'app/sync.py', 'app/file_layout.py'),
         description='Scope per-file publication to related versions; avoid repeated full catalogue, backup, mapping and directory maintenance; preserve journals and cumulative prior fixes.')
