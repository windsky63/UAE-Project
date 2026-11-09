# 回归测试入口

日常小改动只选对应的小范围：文件数量用 `--area file-counts`，文字日报及发送用 `--area daily-text`；也可用 `--changed` 指定本轮源码，现已精确映射这两类改动。不要无差别叠加 briefing、directory、download、overview。只有实际改到对应流程或出现尚未排除的风险时才扩大；代码没再改、检查已经通过时不重复运行。仅文档、发布清单或打包变化不重跑业务测试。

从项目根目录使用共享解释器运行。默认是快速基础检查，输出会明确标注局部覆盖；完整回归使用 `--full`。测试按组件分进程执行，避免多个同名 tests 包互相污染。默认最多并发两个组件，不在同一组件内并行修改全局补丁、环境变量或桌面锁。

本机共享环境已具备测试依赖。新开发环境先安装项目运行依赖，再执行 `python -m pip install -r requirements-test.txt`；测试入口不会自动安装依赖。前端检查使用 web/package.json 声明的 Playwright 和本地浏览器。

```powershell
# 快速基础检查
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py

# 仅验证这次改动涉及的功能，可组合多个功能
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py --area overview logging

# 显式传入本轮改动路径；未配置映射的代码会回退到所属组件完整测试
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py --changed '管理中心/daily_publications.py'

# 先查看精确的文件和测试方法，不运行
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py --area logging --plan

# 只有前端改动时，额外构建一次并运行选中的隔离浏览器检查
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py --area overview --ui

# 单个组件或全项目完整回归
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py --full --component manager
& '.\.venv-analysis\Scripts\python.exe' tools/run_tests.py --full
```

## 按功能组织

完整的文件清单、节点和源码映射集中在 `tools/test_suites.json`。`--list` 可以查看分类。

| 功能 | 覆盖范围 |
| --- | --- |
| quick | 日志、下载优先级、解析设置、版本统计、引用消息及测试入口的快速基础检查 |
| file-counts | 共享实体统计、正式/历史范围、删除后更新和只读预览；不运行Excel渲染或NAS上传 |
| daily-text | 文字日报、发送前备份、发送去重、独立时间及进程恢复；不运行Excel渲染或NAS上传 |
| overview | 工作台卡片、版本和待发布数量；`--ui` 验证三个屏幕尺寸 |
| logging | 日志目录、编码、汇总、跨文件去重及管理中心捕获标记 |
| download | 下载窗口、调度、网络重试、文件选择、续传、分批核对 |
| directory | 目录映射、前后版本保留、历史归档、迁移恢复及跨工具引用 |
| analysis | 解析队列、PCF/IDF算法、压缩包缓存和包内索引 |
| briefing | 日报、简报渲染、定时生成、发送保护和业务工具 |
| messages | 微信引用、@、监听、消息去重、停止和重试 |
| codex | Codex会话、共享后端、IPC、上下文、进程生命周期和超时集成 |
| catalogue | 授权检索、文件分类、包内成员和文件发送；`--ui` 检查隔离的包内资料页面 |
| upload | NAS上传、续传、旧副本归档、相关管理中心日志 |
| harness | 测试选择、重叠去重、未知代码回退和失败退出码 |
| ima | ima 管理中心页面、每日更新 TXT、复制与版本更新、异常恢复、凭证和安全停止；`--ui` 检查 ima 页面与工作台；新增清单时同时运行 harness |
| ima-collect | 项目管理规定与 PDF 图纸分类提取、只读身份定位、嵌套 ZIP、增量与旧版归档 |
| ima-ui | ima 页面流程、完整记录分页、异步任务状态与日志；`--ui` 运行 ima 隔离浏览器检查 |

修改分类清单时，harness 检查会核对每个测试文件及指定方法是否存在。整文件与其中单个方法同时入选时，只运行整文件一次。直接修改测试文件时只选该文件；删除测试文件时回退到该组件完整回归。源码影响映射需要维护，未知代码不会静默跳过。

`--changed` 只接收当前这轮实际修改的路径，不自动把整个未提交工作区算作本轮改动。公共源码、跨组件行为或改动范围不确定时，应选择相应多个功能或完整回归。不能因快速检查通过就宣称完整回归通过。

## 文件位置和耗时报告

- 同步工具：`FusionLive同步工具/tests/test_*.py`。
- 管理中心：`管理中心/tests/test_*.py`。
- 微信：`codex-wechat/tests/test_*.py`，模拟服务器留在同目录供集成用例使用。
- NAS：`绿联云上传工具/test_upload.py`、`test_nas_history.py`，继续兼容其独立部署清单。
- 测试入口本身：`tools/tests/test_test_runner.py`。
- 前端既有检查脚本保留在 `管理中心/web/check-*.cjs`；统一入口仅自动调用已确认使用模拟API的两项，不将需要真实服务的脚本混进快速检查。

每个组件有明确的 pytest 收集范围，不扫描 runtime、下载目录或历史目录，不创建 `.pytest_cache`。直接进入组件运行 `python -m pytest` 仍运行该组件完整测试。根目录使用统一入口，避免同名 tests 包冲突。

每次执行记录保存在 `runtime/test-reports/日期时间-唯一ID/`：

- `summary.json`：范围、实际命令、退出码、每组件耗时及最慢用例。
- `组件.log`：完整UTF-8测试输出和失败原因。
- `组件.xml`：逐测试JUnit耗时，便于追查慢项。

入口始终真正执行选中的测试，不复用上一次“已通过”来跳过检查。默认不构建前端或启动浏览器；只有显式 `--ui` 才执行，且一次构建供本轮所选检查复用。

旧入口 `管理中心/log_tools.py test --tool manager` 现在运行该组件快速检查；加 `--full` 可运行完整回归。统一入口不提供真实下载、上传、模型请求或微信发送测试命令。

## 本次实测

2026-11-02，在当前开发机测得；耗时随负载变化，各行覆盖范围不同。

| 入口 | 实际覆盖 | 总耗时 |
| --- | --- | --- |
| 默认 quick | 75项通过，另有29个子测试 | 4.15秒 |
| logging overview harness | 65项通过、1项跳过，另有6个子测试 | 2.90秒 |
| overview --ui | 6项后端检查、一次前端构建和三个屏幕尺寸的浏览器检查 | 9.03秒 |

整理前的一次完整测量中，同步工具526项耗时104.48秒，微信299项耗时73.91秒，管理中心112项通过、1项跳过耗时45.25秒，NAS70项耗时7.73秒；当时四个组件并行，不能将这些时间简单相加作为全项目墙钟耗时。慢项主要是实际子进程/IPC、配置校验和解析集成检查。

完整用例收集仍为同步526、管理中心113、微信299、NAS70，新增测试入口自身11项，共1019项。日常加速来自准确选择本轮范围、消除重复选择和避免重复构建；完整集成检查仍保留。HTTP测试服务器的收尾轮询从默认0.5秒缩短为0.01秒，相关5项接口与SSE检查已通过。
