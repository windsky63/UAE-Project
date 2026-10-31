# Windows 部署任务

当前完整包：`2026.10.30-windows-python312-full.5`。优先阅读根目录“先读我-本次服务器更新.md”；本包增加待核对操作记录清除、微信问答统一归档回收站及本地会话彻底删除，保留日志进度合并、目录整理CMD修复、单元优先下载、下载中断恢复和NAS历史统一。会话删除不清除微信发送审计或服务端执行线程。必须保留服务器 `FusionLive同步工具/history`；旧NAS存量迁移须单独预览，不随安装自动运行。

此前功能继续保留：微信与网页 Codex 使用同一共享后端实现，网页保持持久会话、微信保留按群预建轮换；两套发送器共用桌面互斥与输入保护。紧急快捷键 Ctrl+Shift+F12 会解除输入保护并暂停后续发送，“恢复微信发送.cmd”负责手动恢复。共享代理和两份 desktop_guard.py 都必须完整部署，并安全重启管理中心及微信监听。详细更新行为见管理中心 SERVER_UPDATE_2026_10_28.md。

本版本附带 codex-wechat/wechat.codex.json 默认文件，默认开启Codex回复与发送，数据库目录为D:/xwechat_files、账号为，会话列表为空时保持待配置模式。服务器已有同名配置时保留原文件；缺失时安装入口可从示例生成。微信页面对缺失/不完整配置会填充默认字段，保存后创建或更新真实配置。

Codex 对话已适配 CLI 0.164 的命名权限配置，保持限定项目读写及禁止自动提权。缺失旧 rollout 时保留本地记录并新建执行线程，以有限的最近对话提供背景，不重发历史请求。

目标电脑已有下载文件和上传工具。先将安装包解压到独立暂存目录，校验 deployment-manifest.json 的文件哈希，再检查已有程序位置及进程。不要直接覆盖运行中的项目。

## 数据保留

先安全停止旧下载、上传、解析和推送进程，等待收尾后备份。完整保留服务器 FusionLive同步工具/downloads、runtime、logs、exports，绿联云上传工具/config.json、runtime、logs，以及自定义计划、规则、模板、账号配置和同步 app/local_credentials.py。已有管理中心 runtime、codex-wechat 配置和 data 也需保留。不使用开发机台账替换服务器台账，不删除现有资料，不强杀数据库写入进程。部署包写入各工具的releases，不能把exports当作安装包目录清空。

四个程序目录保持同级，打开管理中心.cmd 与 admin_launcher.py 放在共同上级目录。上传配置首次创建才使用 config.example.json；微信配置首次创建才使用 wechat.codex.example.json。包内按用户授权包含同步工具内置登录凭据，不要输出到报告或公开发布。

## Python 环境

如果包内有“先读我-含环境安装.md”，优先按该文件操作：已包含基础 Python 和共享环境，不创建新环境、不执行 pip install，只运行“安装便携环境.cmd”校正最终路径和离线检查。不得把旧虚拟环境和随包环境混合覆盖。

本包使用服务器 Python 3.12，全部工具统一使用根目录 .venv-analysis。运行 install_python312.cmd，或使用该环境的 python.exe -m pip install -r requirements.txt 一次安装根目录汇总的全部依赖。管理中心和上传工具后端只用标准库，web/dist 已提供，运行不需要 npm 构建前端。

## 外部软件与配置

检查服务器 Codex CLI 是否可用，必要时参考 https://learn.chatgpt.com/docs/cli 安装。codex login status 检查登录，用户亲自完成必要的 codex login；不复制开发机认证令牌。

微信客户端和运行程序使用同一 Windows 用户。按服务器实际信息配置 database_root、database_account、account_name、chats 和 sync_sources。保持 codex_enabled=false、allow_send=false，未经用户明确启动不采集真实消息、不生成或发送。未知账号和聊天信息集中向用户询问，不能猜测。

核对上传 source_root、download_ledger 和 NAS target_root，保留原配置和权限。没有原下载台账时先寻找原 runtime，不创建空台账冒充迁移完成。

## 验证与交付

用随包环境的完整 python.exe 路径运行同步 main.py preflight --gui --wechat、上传 main.py integrity；进入 codex-wechat 后运行上一级 .venv-analysis/Scripts/python.exe -m wechat_local --check。这些是离线检查，不等于真实业务链路验证。不要用 --once 代替 --check。

双击打开管理中心.cmd，默认只启动 127.0.0.1:8765 上的本地页面。需要局域网时关闭旧服务后运行打开管理中心-局域网.cmd；其他电脑直接访问 http://服务器IP:8765/，无需会话链接，不再生成访问地址.txt。network_config.example.json 可复制为 network_config.json 自定义监听地址和端口；现有配置保留。

核对原台账数量、抽样预览和上传进度。不启动正式下载、批量上传或真实微信收发。最后报告备份位置、最终路径、环境及检查结果、需用户补充的登录和账号信息。不要在报告打印凭据或会话 token。
