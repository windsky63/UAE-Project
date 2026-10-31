# Windows 启动入口修复

旧 CMD 使用 LF 换行并直接包含 UTF-8 中文路径，用户报告命令被拆成 `p`、`TF8`、`OENCODING` 等片段。此前测试只启动 Python 服务，未覆盖真实 CMD 入口，属于验证遗漏。

修复为纯 ASCII、CRLF 的 CMD，并将中文目录定位交给相邻 `admin_launcher.py`。支持本地虚拟环境、系统 py/python，缺少 Python/后端/前端时显示诊断；保留错误退出码，Python 异常写入 launcher.log。CMD 参数传递给服务，可用 `--no-browser` 测试启动。

新增测试直接用真实 Windows cmd.exe，从中文且含空格的项目目录运行入口，检查参数、UTF-8 环境、退出码及错误日志。打包时验证 ASCII、CRLF 和所有文件摘要，包含 admin_launcher.py，不包含运行数据或凭据。

管理中心的工具配置、文件检索、离线流程、日志和响应式布局沿用前期验证。Codex 界面通过模拟协议进行流式交互验证，真实 CLI 仅验证初始化握手；服务器上的登录、网络、NAS、微信客户端及正式任务仍依赖该机环境，不能把本地模拟通过视作这些外部连接已通过。
