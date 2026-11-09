"""Package stable document/member matching and quiet scheduler wait logs."""
import build_layout_skip_patch as builder

builder.PATCH = '2026.11.05-version-identity.1'
builder.PAYLOAD = ('app/sync.py', 'app/file_layout.py', 'app/scheduler.py')
builder.DESCRIPTION = ('Separate own-document identity from package attachment roles; '
    'relocate retained predecessors occupying another member target; keep routine '
    'thread waits concise and retain opt-in diagnostics; includes layout conflict isolation.')
builder.GUIDE = '''FusionLive 主文件与同名附件版本关联修复及线程日志优化
版本：2026.11.05-version-identity.1
适用：2026.11.04-owner-updates.1；可从原版或今天的layout-skip.1补丁升级。
本包包含此前的下载前路径冲突跳过修复，不需要重复安装上一包。

部署：
1. 在管理中心安全停止同步、常驻调度和后台解析，等待进程退出。
2. 备份 app/sync.py、app/file_layout.py、app/scheduler.py 和 release_manifest.json。
3. 将本包 app 目录及 release_manifest.json 合并覆盖到服务器
   D:\\阿联酋项目\\FusionLive同步工具（main.py、app、runtime 所在层级）。
4. 从原入口重新启动常驻调度和下载。调度日志修改需要重启调度进程。

本次修复：
- 版本匹配用文件自身的文档关联识别主文件，不再由资料包中的附件角色覆盖其身份。
  同名主文件与WTR附件不能互相作为前后版本。
- 正式目录中的保留旧副本若占用了另一个成员的目标位置，下载准备阶段会将它
  安全迁到自己的当前成员位置，再恢复另一个成员的旧副本。
  使用原有备份、SHA256校验、全批暂存及迁移恢复机制，不覆盖未知文件。
  新版仍为待下载，不把保留旧副本计为新版完成。普通旧副本无需改名或移动。
- 升级旧迁移规划策略；尚未执行的不安全旧归档计划沿用备份重规划保护。
  已部分执行的可疑旧计划仍保留现场并报错，不盲目跳过恢复。
- 安全退出等待线程时，普通日志只显示等待对象和时长。
  FUSIONLIVE_LOG_DETAIL=1时，线程堆栈单独保存到
  runtime/scheduler-thread-stacks.txt；真实异常仍保留错误及调用堆栈。
- 新完成迁移只清理该任务自身的空暂存目录；不删除已有暂存内容，
  不在下载启动时遍历清理几万个历史目录。

无需删除冲突PDF、修改台账或事先执行目录整理。真实未知占位仍保留并跳过。
本包仅包含三个程序文件、匹配的发布校验清单和本说明。
不含数据库、下载内容、配置或凭证；保留服务器所有业务数据，无新增依赖。

验证：真实用户台账只读计算确认旧主文件只匹配新主文件，旧WTR只匹配新WTR。
针对性回归155项通过、1项跳过，另有3个子测试通过。
范围：版本保留、迁移恢复、下载替换/回执/跳过、手动维护及调度日志。
未执行全项目完整回归或真实平台下载。
'''

if __name__ == '__main__':
    builder.main()
