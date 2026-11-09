# 新目录完整部署与迁移

本包包含全部运行程序、编译后的管理中心网页、FusionLive 登录配置、当前微信配置、NAS 实际配置、ima 知识库设置及一次性凭据迁移文件。包含已验证的大文件分段下载功能。包内含私人账号配置，请直接传到自己的服务器。

Python 解释器、虚拟环境、下载资料、业务数据库和运行历史不在程序包内。服务器已有 Windows Python 3.12，可在新目录运行 `install_python312.cmd` 创建独立环境并安装依赖，安装需要网络。不要复制旧 `.venv-analysis`，虚拟环境含旧路径。微信客户端、Codex 登录环境和 NAS 的 Windows/SMB 登录权限沿用服务器原有环境。

## 部署顺序

1. 在旧管理中心安全停止全部工具，等待下载、解析、微信发送及注销结束，再退出旧管理中心。迁移期间不要让两套程序同时运行。
2. 把完整 ZIP 解压到新的项目根目录，直接看到 `打开管理中心.cmd`、各工具文件夹及 `deployment-manifest.json`。
3. 从旧项目按下表复制数据到新目录相同位置。复制整个目录，保留其内部结构。数据库与下载资料使用同一停机时点，SQLite 的 `-wal`、`-shm` 如仍存在也一起复制，不手动删 WAL。
4. 运行 `install_python312.cmd`。若已在新目录建立好 Python 3.12 环境，可跳过安装。
5. 运行 `初始化新目录.cmd`。它仅修正项目内部的旧绝对路径，并把 ima 一次性凭据加密到当前 Windows 用户；不会启动下载、云端同步或发送。可重复运行。
6. 用 CMD 在新目录执行 `.venv-analysis\Scripts\python.exe FusionLive同步工具\main.py preflight --startup`。应退出码 0；具体结果在 `FusionLive同步工具/runtime/startup_check.json`。完整包中包含 `app/local_credentials.py` 及其清单条目。
7. 打开管理中心，核对 ima 知识库和待同步目录、NAS 路径及微信会话，再按需要启动工具。保留旧目录作为回退，停止使用旧快捷方式。

## 从旧项目迁移到新项目的同名目录

| 目录 | 内容与作用 |
| --- | --- |
| `FusionLive同步工具/downloads/` | 所有已下载原文件，整棵目录复制 |
| `FusionLive同步工具/history/` | 已归档旧版本，存在时复制 |
| `FusionLive同步工具/runtime/` | 下载台账、恢复记录、解析缓存、日报状态及微信推送设置；与下载文件一起迁移 |
| `FusionLive同步工具/logs/` | 日志、简报和发送记录，保留追溯与去重信息 |
| `FusionLive同步工具/exports/` | 导出台账和目录清单，存在时复制 |
| `绿联云上传工具/runtime/` | 上传台账、续传和历史迁移状态，避免重复上传与核验 |
| `绿联云上传工具/logs/`、`exports/` | 上传日志与导出记录，存在时复制 |
| `Ima知识库管理/待同步/` | 待同步资料及每日更新记录，存在时复制；自定义资料目录单独迁移 |
| `Ima知识库管理/runtime/` | ima 同步状态、计划、收集记录和原服务器设置，存在时复制 |
| `codex-wechat/data/` | 微信监听进度、发送队列、去重记录、会话和工作目录 |
| `codex-wechat/logs/` | 微信运行记录，存在时复制 |
| `管理中心/runtime/`、`logs/` | 管理中心任务与日志，存在时复制 |

若旧目录中 SQLite 台账仍在项目根目录，而非表中标准位置，先核对旧配置：下载台账应放到 `FusionLive同步工具/runtime/ledger.sqlite3`，上传台账放到 `绿联云上传工具/runtime/upload.sqlite3`。不要把旧诊断快照覆盖更新的正式数据库。

包内配置来自当前开发项目。若服务器配置更新，应保留服务器自己的 `绿联云上传工具/config.json`、`codex-wechat/wechat.codex.json`、`管理中心/network_config.json`、`FusionLive同步工具/runtime/wechat_push.json` 及 `Ima知识库管理/runtime/settings.json`，再运行初始化修正内部路径。NAS 的 UNC 共享路径和项目之外的目录不会被自动修改。

ima 的 `runtime/credentials.dat` 只能在原 Windows 用户/机器环境解密。同一服务器、同一 Windows 用户可随 runtime 迁移；若换用户或机器，不复制旧 `credentials.dat`，让初始化使用包内 `credentials.bootstrap.json` 重新加密。迁移文件带 `replace_existing: true` 时，会按用户授权替换已保存的旧凭证；不带该标志时保留原账号。该明文迁移文件成功导入后会自动删除，ZIP本身仍含凭证。不要把本机加密文件当成跨机器凭据。本项目本机已授权向个人服务器 `D:\阿联酋项目文件管理` 的私有部署包携带当前凭证，打包偏好保存在 `Ima知识库管理/runtime/deployment.json`，值为 `include_credentials: true`；显式使用 `--no-include-ima-credentials` 可排除。

通常沿用包内 FusionLive 凭据。若需换成旧服务器自己的 `app/local_credentials.py`，必须同时更新 `release_manifest.json` 中该文件的 SHA-256 和字节数，避免再次触发完整性检查；也可保留包内文件，通过环境变量覆盖账号。

运行前核对是否有旧停止标记：`stop.request`、`scheduler.exit.request`、`watch-stop.request`、`login.paused` 等。它们可能来自主动停机或账号暂停；不要未经核对直接全部删除。管理中心的正常启动/恢复入口会处理适用的停止状态。

无需迁移 `.git`、开发测试、`node_modules`、本机虚拟环境、旧部署 ZIP、临时诊断快照或本机根目录备份。根目录备份属于本地清理保护，不作为服务器正式数据源。
