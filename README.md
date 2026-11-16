# 阿联酋项目文件管理

集中管理 FusionLive 下载、本地资料解析、NAS 上传、ima 知识库同步与微信 Codex 问答。推荐通过管理中心配置和启停工具，打开网页本身不会启动业务任务。

## 安装与启动

1. 在 Windows 安装 Python 3.12 或更新版本，并准备 Node.js/npm（安装器检测到前端项目时会安装依赖并构建网页）。
2. 双击根目录 **安装依赖.cmd**，创建或复用共享环境 `.venv-analysis`，安装管理中心与各工具声明的依赖。
3. 双击 **打开管理中心.cmd**。当前入口默认传入 `--lan`，监听所有 IPv4 网卡，默认端口 8765；本机访问 `http://127.0.0.1:8765/`，其他电脑使用服务器 IP。
4. 在网页核对各工具的账号、数据路径和目标，保存后启动需要的工具。配置变更通常需安全停止后重启工具。
5. 退出时先安全停止业务工具，等待收尾，再关闭管理中心服务窗口。关闭浏览器不停止后台任务。

仅在本机使用时，从项目根目录直接执行：

```powershell
& '.\.venv-analysis\Scripts\python.exe' '管理中心/launcher.py' --host 127.0.0.1 --port 8765
```

此方式不经过默认带 `--lan` 的 CMD。本机模式使用启动窗口给出的会话链接；局域网模式可直接访问管理页面，应在受信任网络使用。

## 组件导航

| 组件 | 职责 | 说明 |
| --- | --- | --- |
| 管理中心 | 网页配置、启停、任务、文件查询、日志、Codex 对话和共享服务 | [README](管理中心/README.md) |
| FusionLive 同步工具 | 平台盘点、下载、目录维护、下载台账及收尾推送文 | [README](tools/FusionLive同步工具/README.md) |
| 资料解析及管理工具 | 本地解析、管道日报、图纸日报、微信发布及定时发送图纸 | [README](tools/资料解析及管理工具/README.md) |
| 绿联云上传工具 | 按下载台账镜像正式及历史资料到 NAS | [README](tools/绿联云上传工具/README.md) |
| ima 知识库管理 | 提取副本、预览上传、知识库同步、版本更新及归档 | [README](tools/Ima知识库管理/README.md) |
| codex-wechat | 微信采集、Codex 问答及受控文件发送 | [README](tools/codex-wechat/README.md) |

下载工具先生成资料和台账；解析、NAS、ima 按各自配置读取下载结果。同步工具不再承担解析及微信发送；微信问答只使用当前授权范围内的资料。

## 目录与业务数据

```text
项目根目录/
├─ 安装依赖.cmd
├─ 打开管理中心.cmd
├─ 验证程序完整性.cmd
├─ 管理中心/                 服务端、web、共享服务、测试和维护脚本
│  ├─ runtime/               管理状态、网页会话与测试报告
│  └─ tools/                 安装、校验、测试及构建脚本
├─ tools/                    五个业务工具，新增工具也放在这里
├─ .venv-analysis/           共享 Python 环境
├─ logs/                     集中操作日志（运行后生成）
├─ runtime/                  项目级运行记录
├─ releases/                 部署 ZIP 和 SHA256
└─ backups/                  打包前源码备份
```

各工具的数据仍保存在自己的 `runtime`、`downloads`、`history`、`data`、`待同步` 和导出目录。解析工具通过 `--data-home` 可读取同步工具原有数据目录，无需搬迁文件。自定义服务器绝对路径应在部署后核对。

下载台账、上传回执、发送去重记录、解析缓存及运行锁用于恢复与判断状态，不能作为普通日志清理。优先使用管理中心的日志清理及安全停止功能。

## 校验与部署

双击 **验证程序完整性.cmd**，按 `管理中心/tools/integrity-manifest.json` 检查缺失或修改的程序文件；不会登录平台、上传或发送微信。校验通过不代表账号、网络及业务执行已验证。

从根目录生成完整部署包：

```powershell
& '.\.venv-analysis\Scripts\python.exe' '管理中心/tools/build_deployment.py'
```

构建先创建 `backups` 源码备份，再构建网页，收集管理中心及 tools 下全部程序、测试、文档和资源，校验同步工具启动完整性，输出 `releases` 中带时间戳的 ZIP 与 `.sha256`，最后解压复核。`管理中心/tools/build_update.py` 使用同一全量流程。

部署包排除虚拟环境、node_modules、缓存、数据库、下载资料、微信运行数据、ima 待同步副本和普通本地配置，不能代替业务备份。同步登录文件 `tools/FusionLive同步工具/app/local_credentials.py` 按现有规则携带并覆盖；ima 凭证携带读取本机部署偏好，可用 `--no-include-ima-credentials` 排除。含凭证 ZIP 应作为私有部署资料保存。

服务器部署顺序：安全停止工具及管理中心 → 备份配置与业务数据 → 完整解压合并覆盖 → 安装依赖 → 校验完整性 → 启动管理中心核对路径 → 启动业务工具。修改源码或文档后，旧完整性清单可能报告变化，正式构建时重新生成清单。

## 开发与测试

新增工具在 `tools/<工具名>/` 提供 README 及依赖声明。安装器检测 `requirements.txt`，没有该文件的 Python 工具可使用 `pyproject.toml`；前端使用 `package.json`。接入网页需另行提供启停和状态适配器。

从项目根目录执行：

```powershell
# 查看完整测试计划，不执行
& '.\.venv-analysis\Scripts\python.exe' '管理中心/tools/run_tests.py' --full --plan
# 默认 quick 仅包含少量快速检查
& '.\.venv-analysis\Scripts\python.exe' '管理中心/tools/run_tests.py'
# 全部组件；网页验证另加 --ui
& '.\.venv-analysis\Scripts\python.exe' '管理中心/tools/run_tests.py' --full
```

范围定义在 `管理中心/tools/test_suites.json`，报告位于 `管理中心/runtime/test-reports`，见 [测试约定](管理中心/TESTING.md)。

## 常见问题

- 缺依赖或网页文件：重新运行安装器，启动异常查看 `管理中心/runtime/launcher.log`。
- 局域网无法访问：核对服务器 IP、监听地址、端口和防火墙，修改后重启服务。
- 台账存在但文件不可读：核对实体文件和服务器路径，仅复制数据库不能补齐资料。
- 安全停止较慢：查看当前文件、网络请求或发送阶段，等待收尾，不通过删锁或反复启动处理。

迁移背景见 [重构说明](管理中心/重构说明.md)。
