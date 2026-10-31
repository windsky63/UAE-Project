"""Build the cumulative fix for repeated document scans during selection."""
from build_analysis_speed_patch import main, PAYLOAD
from build_download_window_patch import GUIDE as WINDOW_GUIDE, PATCH as WINDOW_PATCH

PATCH = '2026.10.31-download-selection.1'
GUIDE = WINDOW_GUIDE.replace(WINDOW_PATCH, PATCH).replace(
    'FusionLive 下载时段禁止解析更新', 'FusionLive 下载任务选择性能修复（累计更新）').replace(
    '这是累计补丁，包含上一包解析查询索引修复及已有HTTP 90秒超时修复。',
    '这是累计补丁，包含下载时段禁止解析、解析查询索引及已有HTTP 90秒超时修复。').replace(
    '备份 app/analysis_worker.py、app/analysis_cache.json、app/fusion_client.py、',
    '备份 app/sync.py、app/analysis_worker.py、app/analysis_cache.json、app/fusion_client.py、').replace(
    '备份的四个程序文件', '备份的五个程序文件')
GUIDE += '''
本包新增修复：
- 改写“选择下一个待下载文件”查询，先按内容ID查关联，再按文档完整键确认
  盘点状态，避免为每个待下载文件扫描大量已核对文档。
- 保留当前周期、有效记录、失败重试及优先单元/新增/更新文件排序规则。
- 每个成功文件新增“下载阶段耗时”日志，分别记录选择任务、请求接收及校验、
  登记及迁移三个阶段。可在 scheduler.log/run.log 中核对实际瓶颈。
  其中请求接收阶段也包含文件落盘校验，不是单独的网络带宽测试。
- 本包不修改解析缓存版本，不替换服务器数据库，不增加新依赖。

验证：117项相关测试通过，包括大清单查询工作量上限、排序、盘点状态和重试过滤。
本地较旧台账仅在内存副本中补齐旧版缺失字段后对比：2040个候选的结果及顺序完全一致；
旧查询约2.28秒，新查询约0.015秒。服务器日志中两次文件选择空档各约49秒，
本地验证不是服务器实测，部署后请以新增分阶段日志确认实际改善。
下载接收及路径维护仍可能有独立耗时，本修复不能保证消除所有网络或磁盘等待。
'''


if __name__ == '__main__':
    main(patch=PATCH, guide_override=GUIDE, payload=(*PAYLOAD, 'app/sync.py'),
         description='Avoid per-candidate document scans in download selection; add per-stage timing; cumulative download-window, history-index and HTTP-timeout fixes.')
