"""One-time JSON-to-SQLite migration; no source parsing or message sending."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from paths import HOME
from result_store import ensure_schema, encode_report, import_reports


def migrate(base, progress=print):
    cache=Path(base).resolve()/'runtime/file_analysis'
    queue=cache/'queue.sqlite3'
    if not queue.is_file():
        progress('没有旧解析队列，无需导入。')
        return dict(imported=0,failed=0)
    with closing(sqlite3.connect(queue,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        with db:
            ensure_schema(db)
        jobs=list(db.execute("""SELECT j.key,j.result_path,j.updated FROM jobs j
            LEFT JOIN analysis_reports r ON r.job_key=j.key
            AND r.result_path=j.result_path AND r.job_updated=j.updated
            WHERE j.active=1 AND j.state IN ('done','failed')
            AND j.result_path IS NOT NULL AND r.job_key IS NULL"""))
        imported=failed=0
        for start in range(0,len(jobs),250):
            rows=[]
            for job in jobs[start:start+250]:
                try:
                    path=(cache/job['result_path']).resolve()
                    if not path.is_relative_to(cache):
                        raise ValueError('结果路径超出缓存目录')
                    report=json.loads(path.read_text(encoding='utf-8'))
                    if not isinstance(report,dict) or not all(k in report for k in ('files','pipeline_results')):
                        raise ValueError('解析结果格式无效')
                    payload,sha=encode_report(report)
                    rows.append((job['key'],job['result_path'],job['updated'],payload,sha))
                except (OSError,ValueError,TypeError) as exc:
                    failed+=1
                    progress(f'旧结果无法导入：{job["key"][:12]}；{type(exc).__name__}')
            import_reports(db,rows)
            imported+=len(rows)
            progress(f'旧解析结果导入：{min(start+250,len(jobs))}/{len(jobs)}；读取有效 {imported}；失败 {failed}')
    return dict(imported=imported,failed=failed)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-dir',type=Path,default=HOME)
    args=parser.parse_args()
    result=migrate(args.base_dir)
    print(json.dumps(result,ensure_ascii=False))
    return int(bool(result['failed']))

if __name__=='__main__':
    raise SystemExit(main())
