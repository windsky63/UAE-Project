# 绿联云上传工具

读取 FusionLive 下载台账及本地资料，通过 SMB 将正式文件、历史版本与已导出的 Excel 台账同步到 NAS。支持常驻调度、手动单轮、断点恢复、目录镜像及 NAS 本机 SHA256 校验。

[项目说明](../../README.md) · [管理中心](../../管理中心/README.md) · [详细规则与历史说明](使用说明.txt)。日常建议使用管理中心的上传工具页面。

## 首次配置

1. 运行项目根目录 `安装依赖.cmd`，安装共享 Python 环境与依赖。
2. 在管理中心上传设置中配置下载原目录、下载台账及 NAS 目标。独立使用时参考 `config.example.json` 创建 `config.json`，保留已有配置，不直接用示例覆盖。
3. 确认运行工具的 Windows 用户能访问 NAS SMB 共享，并具有目标及其同级归档目录的写入权限。
4. 默认使用 SSH 本机校验。在实际运行工具的服务器安全停止上传后，运行 `配置NAS本机校验.cmd`，完成指纹确认、专用密钥和目录映射验证。NAS 需提供 SSH、Python3，服务器需有 OpenSSH 客户端。
5. 运行“检查连接与清单”，核对目标和候选资料，再启动上传。此检查会访问 NAS，不是纯离线检查。

### 配置字段

| 字段 | 用途 |
| --- | --- |
| `source_root` | 本地下载原目录 |
| `download_ledger` | 下载工具的 SQLite 台账路径 |
| `target_root` | NAS 专用镜像目录，通常为 SMB UNC 路径 |
| `verification_method` | `ssh`（默认）或显式选择 `smb` 完整回读 |
| `remote_hash` | SSH 主机、端口、用户、NAS 内部目录、私钥等，见示例配置 |
| `ledger_update_interval_minutes` | Excel 台账最短更新间隔，默认 60 分钟，可设 60～10080 |

SSH 与 SMB 必须对应同一目标目录，不能仅凭共享名猜 NAS 内部路径。向导通过随机探针验证映射；SSH 故障不会自动回退 SMB。配置变更后安全停止并重启上传。

## 启动、时段与安全退出

- `启动每日上传.cmd`：常驻调度，北京时间每日 12:00—次日 00:00 执行。
- 管理中心“上传一次”：手动完整单轮，时段外需页面确认，结束后退出；有其他轮次时排队执行。
- `安全退出.cmd`：请求安全停止，等待当前 SMB 读写返回并保存进度。
- `检查程序完整性.cmd`：按发布清单检查程序，不测试 NAS 连接。
- `检查连接与清单.cmd`：连接 SMB 并校验本地候选，不执行正式上传。
- `单文件上传测试.cmd`：会实际写入 NAS，仅验证所选文件，不代表整树镜像成功。

## 命令行

以下命令从本工具目录执行：

```powershell
# 离线程序校验
& '..\..\.venv-analysis\Scripts\python.exe' main.py integrity
# 联网检查 SMB 与候选清单
& '..\..\.venv-analysis\Scripts\python.exe' main.py check
# 检查 SSH 和映射：创建并清理 32 字节探针，不改业务文件
& '..\..\.venv-analysis\Scripts\python.exe' main.py remote-hash-check
# 正式常驻调度
& '..\..\.venv-analysis\Scripts\python.exe' main.py schedule
# 手动单轮，允许时段外执行，会实际写入 NAS
& '..\..\.venv-analysis\Scripts\python.exe' main.py manual-once
# 请求安全退出
& '..\..\.venv-analysis\Scripts\python.exe' main.py stop
```

普通 `once` 仍遵守自动上传时段。可用 `--config` 选择配置、`--max-files` 限量处理，或用 `--verify-all` 强制重新校验。限量及单文件模式不执行整树镜像。

## 镜像与恢复规则

完整轮次执行：本地预检 → 已知旧路径迁移 → 多余文件归档与空目录整理 → 缺失或变化文件上传 → 内容及结构核验。目标根目录是本工具专用范围，不能混放需要独立保留的人工资料。

相同 SHA256 的文件接管或跳过；目标内容不同会先备份再更新。多余文件及未知旧副本移入目标旁的 `<目标目录名>.nas-archive`，只对多余空目录执行 rmdir，不递归删除文件。归档是可选恢复来源，缺失后仍可从有效本地文件重新上传。

本地文件缺失、路径映射不明、SHA256 不符、清单不完整或目录整理未完成时，整轮上传和镜像暂停，不先上传其他文件。日志及该轮报告列出原因。单文件或限量模式完成仅表示本次所选候选完成。

完整轮次还会同步下载工具 exports 中已发布的 Excel 台账到 NAS 根目录；不会生成 Excel，也不上传 SQLite。更新间隔从上次成功同步持久计算，尚无 Excel 时保留 NAS 既有台账。

## 数据、日志与部署

- `config.json`：服务器本地设置。
- `runtime/upload.sqlite3`：上传确认、目录迁移、归档及恢复依据。
- `runtime/ssh`：服务器专用 SSH 密钥，不能用开发电脑密钥覆盖。
- `logs/YYYY-MM-DD/upload-*-summary.json`：轮次报告，包含候选诊断、处理结果及耗时；日志会给出具体路径。
- `logs/YYYY-MM-DD/nas/run.log`：独立运行日志；管理模式主进程输出由中心捕获。

升级前安全停止，保留配置、runtime 和业务资料，使用根目录全量部署流程覆盖程序及匹配清单。清理普通日志不应删除上传回执、续传状态或 SSH 密钥。

## 常见问题与开发验证

- SSH 失败：核对当前 Windows 用户的密钥、主机指纹、NAS Python3 和目录映射；不会自动回退 SMB。
- 清单未通过：查看报告中的 `candidate_diagnostics.records` 及路径示例，先修复本地资料或台账。
- 长时间没有文件完成：查看阶段心跳；NAS 扫描、哈希和 SMB 调用可能耗时，心跳不代表文件已上传。
- 完整性失败：使用完整部署包及匹配的 release_manifest.json，不单独替换某个模块。

从项目根目录查看离线回归计划：

```powershell
& '.\.venv-analysis\Scripts\python.exe' '管理中心/tools/run_tests.py' --full --component nas --plan
```

去掉 `--plan` 才执行测试。真实 NAS 写入使用上方明确标注的业务入口。
