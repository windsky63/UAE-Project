# 统一回归入口

从项目根目录使用共享 Python 执行 `管理中心/tools/run_tests.py`。范围与组件定义集中在同目录 `test_suites.json`；不要恢复“其他”中的旧测试入口。

```powershell
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py --area layout
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py --component manager --plan
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py --changed 管理中心/tests/test_project_layout.py
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py --full --component ima
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py --full --ui
```

无参数运行 quick 基础检查；只有 `--full` 才标为完整组件回归。按本轮实际改动选择相关功能或明确传入 `--changed`，不要把长期工作区全部算成本轮改动。源路径基础改动需覆盖涉及的多个组件。新增或重命名测试时同步维护 `test_suites.json`。

组件分别启动 pytest 进程，保留真实子进程、超时、停止和恢复检查。报告、日志、逐用例 XML 耗时统一写到 `管理中心/runtime/test-reports/时间-标识/`。通过后没有后续代码修改、失败或未排除风险时，不重复扩大测试。

测试不调用真实平台、NAS、模型或微信发送。网页验证使用隔离接口和现有 Playwright；只有相关前端改动才加 `--ui`。打包本身进行 ZIP、完整文件哈希、解压复核及同步工具启动检查，不代替业务回归。


## 测试归属与精简规则

业务分类、下载推送文、通用台账工作簿、日志限流及轮转由同步工具覆盖；资料解析及管理工具只测试解析进度日志等差异，管理中心通过 tests/test_shared_implementations.py 检查随包封装的通用实现与同步工具在语法树层面一致。两份实现以后出现差异时，该契约测试会失败，必须统一实现或为差异明确补充测试。所有跨工具检查放在管理中心测试目录；工具内部测试不得依赖管理中心或兄弟工具。

- 不因移动功能复制整套测试；功能迁移时同步迁移用例归属。
- 相同断言针对不同实现或部署边界时仍有价值，不能仅因测试正文相同就删除。
- 发送防重、结果不确定、平台版本变化、缓存一致性、停止恢复等边界检查保留。
- 同一文件不重复定义同名测试；后定义会覆盖前定义，并不会增加执行覆盖。
- 日常按改动选择 daily-text、analysis、drawing-delivery 或 shared-core；发布前再跑完整回归。

```powershell
.\.venv-analysis\Scripts\python.exe 管理中心\tools\run_tests.py --area shared-core
```

本次精简移除80项重复测试正文、6个被覆盖的同名定义；删除3个冗余测试文件，新增1个通用实现一致性文件（4项参数化检查），净减少2个文件。关键业务覆盖仍保留在归属组件。

新增“independence”范围：把工具复制到没有中心和兄弟工具的临时目录验证资源自给，并验证受管理模式明确使用中心实现、中心缺失时报错。跨工具的图纸范围契约归管理中心测试所有。
