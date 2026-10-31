"""Package the cumulative download-window analysis exclusion patch."""
from build_analysis_speed_patch import main

PATCH = '2026.10.31-download-window.1'
GUIDE = f'''FusionLive 下载时段禁止解析更新
补丁版本：{PATCH}
适用基础版本：2026.10.30-extraction-cache.1；本包不是完整安装包。
这是累计补丁，包含上一包解析查询索引修复及已有HTTP 90秒超时修复。

部署步骤：
1. 服务器安全退出同步、定时调度及后台解析，等待保存完成、进程全部退出。
2. 备份 app/analysis_worker.py、app/analysis_cache.json、app/fusion_client.py、
   release_manifest.json，以及 runtime/file_analysis/queue.sqlite3
   和同名 -wal、-shm 文件（如存在）。
3. 解压本包，将 app 文件夹及 release_manifest.json 合并覆盖至服务器的
   FusionLive同步工具目录，即 main.py、app、runtime 所在层级。
4. 运行 maintenance/检查程序完整性.cmd，确认通过后按原入口启动。
5. 北京时间02:00—10:30，解析应提示“下载时段，禁止启动解压解析”。
   常驻调度开启时，10:30后约一分钟内自动重试启动解析；也可以手动启动。

本次行为：
- 每天北京时间02:00（含）至10:30（不含）禁止启动后台解压解析。
  不受服务器系统时区影响；时段内即使当天下载提前完成，也不启动解析。
- 下载完成回调、简报调度的自动启动以及管理中心/命令行手动启动均受限制；
  --resume 不能绕过时间限制。
- 已运行的解析跨入02:00时，完成并保存当前文件后退出，不领取下一任务。
  不强制杀进程，因此较大的当前文件仍可能需要一定收尾时间。
- 跨入下载时段时延后退出时的汇总及Excel导出，避免为退出再做全量导出。
- 时间限制不写永久停止标记；非下载时段保留原有手动停止、退出和迁移限制。
  手动停止状态仍需按原方式恢复，10:30不会清除用户的手动停止请求。
- 继续保留已有解析缓存标识和历史查询索引；不触发本次修改导致的全量重解析。

部署时请保留服务器原有 runtime、downloads、history、配置和解析结果。
本包不含账号、数据库、下载样本及本地诊断数据；无需新增依赖。
不要将本机复制来的 queue.sqlite3 或诊断数据库传回服务器。
基础版本不同请先核对，不要混用完整性清单。

验证包括北京时间边界及UTC换算、下载回调不启动解析、手动恢复不能绕过、
跨时段完成当前任务后退出且不汇总导出；另验证缓存、调度及简报相关测试。
打包后校验ZIP及全部65个程序清单文件，确认解析缓存标识保持不变。

部署后观察 scheduler.log 和 runtime/file_analysis/progress.log。
后续文件更新应不再反复启动解析并等待其退出；网络读取超时仍可能独立存在。
本补丁不改独立手工离线分析程序，只限制同步工具的解压解析队列入口。

回退：安全退出全部任务后恢复步骤2备份的四个程序文件。
新增查询索引可以保留，一般不需要回滚数据库。
'''


if __name__ == '__main__':
    main(patch=PATCH, guide_override=GUIDE,
         description='Disable queue analysis during Beijing 02:00-10:30; safely finish current job and defer shutdown exports; cumulative index and HTTP timeout fixes.')
