"""Complete per-file results in the existing analysis SQLite database."""
import hashlib
import json

SCHEMA = """CREATE TABLE IF NOT EXISTS analysis_reports(
    job_key TEXT PRIMARY KEY, result_path TEXT, job_updated REAL NOT NULL,
    payload TEXT NOT NULL, payload_sha256 TEXT NOT NULL)"""


def ensure_schema(db):
    db.execute(SCHEMA)


def validate_report(report):
    if not isinstance(report,dict):
        raise ValueError('解析结果须为对象')
    for name in ('files','pipeline_results'):
        rows=report.get(name)
        if not isinstance(rows,list) or any(not isinstance(row,dict) for row in rows):
            raise ValueError('解析结果列表无效：'+name)
    for row in report['pipeline_results']:
        if any(key not in row for key in ('project','unit','source','pipeline','sha256','origin','conflict')):
            raise ValueError('管线结果字段不完整')
    return report


def encode_report(report):
    if report is None:
        return None
    validate_report(report)
    payload = json.dumps(report, ensure_ascii=False, separators=(',', ':'))
    return payload, hashlib.sha256(payload.encode('utf-8')).hexdigest()


def store_report(db, key, encoded):
    if encoded is None:
        db.execute('DELETE FROM analysis_reports WHERE job_key=?', (key,))
        return
    db.execute("""INSERT OR REPLACE INTO analysis_reports
        SELECT key,result_path,updated,?,? FROM jobs WHERE key=?""", (*encoded,key))


def import_reports(db, rows):
    # Never overwrite a newer worker result with an older JSON import.
    with db:
        for key,path,updated,payload,sha in rows:
            db.execute("""INSERT INTO analysis_reports
                SELECT key,result_path,updated,?,? FROM jobs
                WHERE key=? AND result_path=? AND updated=?
                ON CONFLICT(job_key) DO UPDATE SET result_path=excluded.result_path,
                    job_updated=excluded.job_updated,payload=excluded.payload,payload_sha256=excluded.payload_sha256""", (payload,sha,key,path,updated))
