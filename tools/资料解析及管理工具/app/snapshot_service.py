"""Batch SQLite report aggregation, with one-time import of legacy JSON."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from paths import check_output_directory
from analysis_worker import candidate_key, parser_version
from local_analysis import analyze, ledger_sources, export_weld_table
from result_store import ensure_schema, encode_report, import_reports, validate_report


def cached_snapshot(database, *, output, base_dir=None, cache=None, prefab_rule='none', progress=None,
                    export_welds=True):
    started = previous = time.monotonic()
    def note(message):
        nonlocal previous
        at = time.monotonic()
        if progress:
            progress(f'缓存汇总：{message}；本阶段 {at-previous:.3f}秒；累计 {at-started:.3f}秒')
        previous = at
    database = Path(database).resolve()
    base = Path(base_dir).resolve() if base_dir else database.parent.parent
    output = check_output_directory(output, database=database, base_dir=base)
    output.mkdir(parents=True, exist_ok=True)
    cache = check_output_directory(cache or base/'runtime/file_analysis', database=database, base_dir=base)
    note('开始核对源文件状态；不读取源文件内容')
    sources, inventory = ledger_sources(database, base)
    version = parser_version()
    keys = [candidate_key(source, source['item'], version) for source in sources]
    note(f'源文件状态核对完成；可读取 {len(sources)}；台账候选 {len(inventory)}')
    jobs = {}
    queue = cache/'queue.sqlite3'
    if queue.exists():
        with closing(sqlite3.connect(queue.as_uri()+'?mode=ro', uri=True)) as db:
            schema_exists = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_reports'").fetchone()
        if not schema_exists:
            with closing(sqlite3.connect(queue, timeout=5)) as db, db:
                ensure_schema(db)
        with closing(sqlite3.connect(queue.as_uri()+'?mode=ro', uri=True)) as db:
            db.row_factory = sqlite3.Row
            jobs = {row['key']:dict(row) for row in db.execute("""SELECT j.key,j.state,j.result_path,j.error,j.updated,
                r.payload_sha256 FROM jobs j LEFT JOIN analysis_reports r
                ON r.job_key=j.key AND r.result_path=j.result_path AND r.job_updated=j.updated
                WHERE j.key IN (SELECT value FROM json_each(?))""", (json.dumps(keys),))}
    entries = []
    for source, key in zip(sources, keys):
        job = jobs.get(key)
        path = None
        if job and job.get('result_path'):
            candidate = (cache/job['result_path']).resolve()
            if candidate.is_relative_to(cache):
                path = candidate
        entries.append((key,source,job,path))
    counts=dict(pending=0,failed=0,completed=0,imported=0)
    def reports():
        batch_size=32
        for offset in range(0,len(entries),batch_size):
            batch=entries[offset:offset+batch_size]
            payloads={}
            if queue.exists():
                with closing(sqlite3.connect(queue.as_uri()+'?mode=ro',uri=True)) as db:
                    payloads={row[0]:(row[1],row[2],row[3]) for row in db.execute("""SELECT r.job_key,r.payload,r.payload_sha256,r.job_updated
                        FROM analysis_reports r JOIN jobs j ON j.key=r.job_key
                        AND j.result_path=r.result_path AND j.updated=r.job_updated
                        WHERE r.job_key IN (SELECT value FROM json_each(?))""",(json.dumps([x[0] for x in batch]),))}
            imports=[]
            for key,source,job,path in batch:
                item=source['item']
                if not job or job['state'] in ('pending','running'):
                    counts['pending']+=1
                    item['analysis_state']='处理中' if job and job['state']=='running' else '待分析'
                    continue
                item['analysis_state']=job['state']
                counts['failed']+=job['state']=='failed'
                try:
                    stored=payloads.get(key)
                    if stored:
                        payload,sha,updated=stored
                        if updated!=job['updated']:
                            raise ValueError('读取期间任务结果发生变化，等待下一次汇总')
                        if hashlib.sha256(payload.encode('utf-8')).hexdigest()!=sha:
                            raise ValueError('数据库结果校验失败')
                        report=json.loads(payload)
                    else:
                        if path is None:
                            raise FileNotFoundError('legacy cache missing')
                        report=json.loads(path.read_text(encoding='utf-8'))
                        payload,sha=encode_report(report)
                        imports.append((key,job['result_path'],job['updated'],payload,sha))
                        job['payload_sha256']=sha
                        counts['imported']+=1
                    validate_report(report)
                    counts['completed']+=job['state']=='done'
                except (OSError,ValueError):
                    counts['failed']+=job['state']!='failed'
                    item['analysis_state']='数据库结果或旧缓存不可读，待重试'
                else:
                    yield report
            if imports:
                with closing(sqlite3.connect(queue,timeout=5)) as db:
                    import_reports(db,imports)
            if offset==0 or offset+batch_size>=len(entries) or time.monotonic()-previous>=5:
                note(f'数据库批量读取 {min(offset+batch_size,len(entries))}/{len(entries)}；本次导入旧JSON {counts["imported"]}')
    groups,report,target=analyze(sources_override=[],cached_reports=reports(),inventory_override=inventory,
        output=output,prefab_rule=prefab_rule,
        progress=lambda message: note(message) if not message.startswith('生成/读取焊口') else None)
    unresolved=len(report.get('incomplete',[]))
    pending,failed=counts['pending'],counts['failed']
    report.update(completed_analysis_files=counts['completed'],pending_analysis_files=pending,
        failed_analysis_files=failed,unresolved_analysis_groups=unresolved,
        analysis_complete=not(pending or failed or unresolved or report['unavailable_local_files']),from_cache=True)
    if pending or failed:
        for record in groups.values():
            record['状态']=f'已完成部分；待处理{pending}，失败{failed}'
            record['依据']+='；后台尚未全部处理完成'
        report['groups']=[{'project':p,'unit':u,'source':s,**record} for (p,u,s),record in sorted(groups.items())]
    if export_welds:
        export_weld_table(report,base)
    data=json.dumps(report,ensure_ascii=False,indent=2).encode('utf-8')
    target.write_bytes(data)
    note(f'汇总结果已保存；导入旧JSON {counts["imported"]}；后续从数据库读取')
    return groups,report,target
