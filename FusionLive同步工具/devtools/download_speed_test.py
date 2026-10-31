"""Measure one real download without changing the synchronization ledger."""
import json
import sqlite3
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from fusion_client import RUNTIME, now
from local_credentials import get_credentials
from login_test import TestClient
from sync import RunLock


def main():
    destination = RUNTIME / ('speed-test-' + now().strftime('%Y%m%d-%H%M%S'))
    destination.mkdir(parents=True, exist_ok=False)
    report = {'time_beijing': now().isoformat(), 'http': [], 'samples': []}

    def record(**values):
        report.update(values)
        (destination / 'result.json').write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(values, ensure_ascii=False), flush=True)

    client = TestClient(*get_credentials())
    original = client.http.request
    timings = {}

    def request(method, url, **kwargs):
        started = time.monotonic()
        response = original(method, url, **kwargs)
        elapsed = time.monotonic() - started
        report['http'].append({'path': urlsplit(url).path, 'status': response.status_code,
                               'seconds': round(elapsed, 4)})
        if urlsplit(url).path == '/servlet/HtmlDownloadServlet':
            timings['headers'] = time.monotonic()
            record(response_wait_seconds=round(elapsed, 4))
        return response

    client.http.request = request
    try:
        with RunLock(), client:
            db = sqlite3.connect((RUNTIME / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True)
            try:
                row = db.execute("""SELECT metadata FROM files WHERE project=? AND active=1
                    AND size BETWEEN 8000000 AND 20000000
                    AND json_extract(metadata,'$.readAccessStatus')='Available'
                    ORDER BY abs(size-10000000) LIMIT 1""", (client.project_id,)).fetchone()
            finally:
                db.close()
            if row is None:
                raise RuntimeError('No readable 8-20 MB file in the local catalogue')
            item = json.loads(row[0])
            record(filename=item['filename'], content_id=item['id'], expected_bytes=item['contentSize'])
            started = last_sample = time.monotonic()

            def progress(received, expected):
                nonlocal last_sample
                at = time.monotonic()
                timings.setdefault('first_chunk', at)
                timings['last_chunk'] = at
                if at - last_sample >= 5 or received == int(expected):
                    sample = {'bytes': received, 'seconds': round(at-started, 3),
                              'MB_per_second': round(received/(at-started)/1e6, 4)}
                    report['samples'].append(sample)
                    record(progress=sample)
                    last_sample = at
                if at - started > 180:
                    raise TimeoutError('Speed test exceeded 180 seconds; partial file will be removed')

            result = client.download(item, destination / Path(item['filename']).name, progress=progress)
            elapsed = time.monotonic() - started
            body_seconds = timings['last_chunk'] - timings['headers']
            record(bytes=result['bytes'], sha256=result['sha256'], path=result['path'],
                   total_seconds=round(elapsed, 4),
                   overall_MB_per_second=round(result['bytes']/elapsed/1e6, 4),
                   body_seconds=round(body_seconds, 4),
                   body_MB_per_second=round(result['bytes']/body_seconds/1e6, 4),
                   first_chunk_wait_seconds=round(timings['first_chunk']-started, 4))
    except Exception as exc:
        record(error_type=type(exc).__name__, error=str(exc))
        raise
    finally:
        record(logout_confirmed=bool(client.logout_result and client.logout_result.get('confirmed')))


if __name__ == '__main__':
    main()
