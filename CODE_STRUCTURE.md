# 工具代码结构

各工具保持独立入口和部署边界；职责相同的小模块归入现有业务模块。公共代码优先由所属工具提供，其他工具复用，避免维护多套统计口径。

| 工具 | 主要模块与职责 |
| --- | --- |
| FusionLive 同步 | `fusion_client` 访问平台；`sync`、`inventory`、`workflow` 管理同步；`ledger`、`file_layout` 管理台账与归档；`paths` 统一程序位置、长路径、路径迁移和输出目录保护 |
| FusionLive 分析与发布 | `analysis_worker` 管理队列；`local_analysis`、`archive_cache`、`archive_index` 管理解析输入；PCF/IDF 解析器保留独立边界；`reporting`、`briefing` 生成文字和表格；`briefing_schedule`、`wechat_delivery` 管理定时发布与发送 |
| NAS 上传 | `main` 管理入口与调度；`nas_sync` 管理上传、续传及校验；`nas_history` 管理历史归档。三者职责不同，保留独立模块 |
| ima 知识库 | `api` 管理协议；`sync` 管理同步引擎；`scope` 集中目录映射、操作范围校验及云端建目录、改名、移动；`collect` 管理资料收集；`daily`、`watch` 管理每日记录与监控 |
| 微信桥接 | `listener` 管理监听；`state` 集中消息、任务持久化及桥接进程锁；`spool` 管理发送队列；`file_transfer` 管理附件；`desktop_guard`、`wechat_uia` 管理桌面互斥与界面操作；后端通信保留在 `app_server`、`shared_backend` |
| 管理中心 | `manager` 管理工具生命周期；`server` 提供 HTTP 接口；`daily_publications` 集中日报快照读取、工作台数量适配及发布校验；`file_catalog` 管理文件检索；`codex_chat`、`ima_control` 对接对应工具 |

## 本轮合并

| 原文件 | 现位置 |
| --- | --- |
| `FusionLive同步工具/app/app_paths.py`、`output_paths.py` | `FusionLive同步工具/app/paths.py` |
| `codex-wechat/wechat_local/desktop_lock.py` | `codex-wechat/wechat_local/state.py` |
| `Ima知识库管理/ima_manager/advanced.py` | `Ima知识库管理/ima_manager/scope.py` |
| `管理中心/download_stats.py` | `管理中心/daily_publications.py` |

调用方直接导入新位置，不再新增兼容转发文件。路径模块仅依赖标准库，避免路径配置引入网络客户端。管理中心继续读取既有每日快照，不在工作台刷新时扫描文件目录。

跨工具的桌面保护协议保留各自随包副本，以支持独立部署；已有一致性测试校验两份实现。历史补丁构建脚本保留版本用途，不并入运行模块。

## 日志约定

管理中心捕获任务输出时补齐北京时间，记录工具、操作、任务ID、PID和退出码，并限制单文件大小、保留轮转文件；已有源时间戳保留。接口500错误记录不含查询参数的路径、关联request_id和脱敏堆栈；正常HTTP轮询不逐条刷屏。

阶段计数只表示该阶段已经确认的工作，不把读取、扫描、请求发出或进程存活写成上传/发送完成。阶段切换清除上一阶段的当前文件、速度和关联计数，增量进度事件仍可省略phase。NAS预检计数和上传计数分开；未知总数明确标记待扫描，不给百分比。

同步、解析、NAS、微信和ima继续使用各自已有日志入口。成功进度可按时间窗口汇总，汇总保留次数及最近上下文，在阶段边界输出。失败、冲突、待核对与停止立即保留；零失败计数不当作错误。微信台账分布是累计状态，不表示本轮完成；ima结果事件数不冒充文件完成数。

管理中心合并同一源、同一阶段及阶段序号的进度，并保留跨轮次边界；跨文件完全镜像的副本合并展示，源文件不重写。Python堆栈在搜索及错误级别筛选前与原因合成同一事件，保留代码位置。日志不得输出密码、API密钥、Authorization值或请求Cookie。协议JSON的stdout保持可解析，观察性日志使用stderr。

各工具仍可独立部署，不引入对管理中心日志模块的运行依赖。不新增操作入口；日志变化不修改调度窗口、业务超时、重试次数或安全停止规则。

## 验证与维护

统一使用根目录 `tools/run_tests.py`，测试范围见 [TESTING.md](TESTING.md)。修改文件名时同步检查导入、隔离测试复制清单、完整性检查和发布清单；源代码清单哈希必须与文件字节一致。

本轮修改前的源码及 SHA256 校验表保存在 `runtime/code-consolidation/`，包括原有未提交改动。该目录属于本地备份，不参与运行或发布。
