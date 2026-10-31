"""Build a push-only update for the diagnosed incremental-publication server."""
from build_analysis_speed_patch import main

PATCH = '2026.10.31-migration-independent-push.1'
GUIDE = f'''FusionLive 定时发布与文件迁移解耦修复
补丁版本：{PATCH}
适用：基础版本 2026.10.30-extraction-cache.1，已部署
2026.10.31-incremental-publication.1 累计更新的服务器。
已对照本次服务器诊断包核对，除本次修改文件外，全部程序与服务器清单一致。
本包是同步工具更新包，无需更新管理中心、微信工具或安装依赖。

部署步骤：
1. 在管理中心安全停止同步工具，等待常驻调度及相关任务退出。
2. 备份服务器 FusionLive同步工具 下的两个文件：
   app/briefing_schedule.py
   release_manifest.json
3. 将本包中的 app 文件夹与 release_manifest.json 合并覆盖到服务器
   FusionLive同步工具目录，即 main.py、app、runtime 所在的层级。
   不要覆盖到项目总目录；部署说明.txt 无需复制。
4. 运行 maintenance/检查程序完整性.cmd，确认通过后按原入口启动。
5. 如果存在 runtime/layout.pending，日志应出现：
   文件路径整理未完成：暂停启动原始文件解析；推送文和缓存简报继续定时生成、发送。
   北京时间10:40准备简报、11:00按原接收会话与发布开关发送。

修复行为：
- 文件迁移标记不再阻止下载日报推送文、缓存简报的定时准备与发送。
- 仍暂停启动读取原始文件的后台解析；迁移标记解除后恢复解析调度。
- 简报按现有缓存生成；结果不完整时保持原有部分结果或待分析通知说明。
- 文件迁移期间仍会轮询并保存已启动的发布任务结果。
- 保留接收会话、发布开关、发送时间窗口、防重复与不确定结果保护。

当天部署：
本补丁不会自动补发已过11:00—11:05窗口的任务。
本次诊断记录显示10月31日11:03已手动提交过推送文，请先核对接收会话，
避免再次手动发送相同内容。正常定时行为从下一个有效窗口执行。

数据说明：
不要删除 runtime/layout.pending 或迁移暂存目录；迁移恢复独立处理。
本包不含数据库、下载文件、账号、运行配置、日志、测试或诊断副本。
无需把本机根目录的日志或数据库复制回服务器。
既有下载、解析缓存、迁移恢复记录和微信接收配置均保留。

验证：
123项相关离线测试通过。
离线回归覆盖迁移标记存在时图文发布、纯文字发布、无简报时生成待分析通知、
重启后不重复提交、迁移解除后恢复解析，以及迁移期间保存异步发送结果。
测试使用模拟发送器，不操作真实微信。
更新包已进行ZIP校验及部署后完整性清单校验。

回退：
安全停止同步工具，恢复步骤2备份的两个程序文件，按原入口启动。
'''


if __name__ == '__main__':
    main(patch=PATCH, guide_override=GUIDE,
         payload=('app/briefing_schedule.py',),
         description='Allow scheduled daily text and cached briefing publication during path migration; keep original-file analysis paused and log the independent publication policy.')
