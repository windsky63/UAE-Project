"""Bounded live association probe; does not change the synchronization ledger."""
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
    records = []
    destination = RUNTIME / ('association-probe-' + now().strftime('%Y%m%d-%H%M%S') + '.json')

    def record(**data):
        records.append(dict(time=now().isoformat(), **data))
        destination.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(data, ensure_ascii=False), flush=True)

    client = TestClient(*get_credentials())
    original_request = client.http.request

    def timed_request(method, url, **kwargs):
        if urlsplit(url).path == '/pol/getRelatedContentFSAction':
            kwargs['timeout'] = (90, 90)
        started = time.monotonic()
        try:
            response = original_request(method, url, **kwargs)
        except Exception as exc:
            record(kind='http', method=method, path=urlsplit(url).path,
                   seconds=round(time.monotonic() - started, 3), error=str(exc), error_type=type(exc).__name__)
            raise
        record(kind='http', method=method, path=urlsplit(url).path,
               seconds=round(time.monotonic() - started, 3), status=response.status_code)
        if urlsplit(url).path == '/pol/getRelatedContentFSAction' and b'isActionSucessful="false"' in response.content:
            record(kind='platform_failure', response=response.text[:2500])
        return response

    client.http.request = timed_request
    record(kind='start', proxy_environment_present=bool(__import__('requests').utils.get_environ_proxies('https://us.fusion.live')))
    with RunLock():
        with client:
            record(kind='login', project=client.project_id)
            versions = ('30280588', '29284703', '29648709', '29946510', '21508129', '29748802')
            virtual_links = {}
            if '--folder-check' in sys.argv:
                versions = []
                for start in range(0, 1000, 100):
                    total, documents = client.document_page('30277655', start, 100)
                    record(kind='folder_page', total=total, start=start, count=len(documents))
                    for document in documents:
                        if any(marker in document.get('reference', '') for marker in ('63-00-004', '63-00-002', '17-23-001')):
                            version = document['id']
                            versions.append(version)
                            virtual_links[version] = document.get('virtualLinkId') if document.get('isVirtualLink') == 'true' else None
                            record(kind='current_document', version=version, reference=document.get('reference'),
                                   isVirtualLink=document.get('isVirtualLink'), virtualLinkId=document.get('virtualLinkId'))
                    if start + len(documents) >= total or not documents:
                        break
            for version in versions:
                started = time.monotonic()
                try:
                    items = client.related(version, virtual_links.get(version))
                    record(kind='association', version=version, seconds=round(time.monotonic()-started, 3), count=len(items))
                except Exception as exc:
                    record(kind='association', version=version, seconds=round(time.monotonic()-started, 3), error_type=type(exc).__name__, error=str(exc))
                    if type(exc).__name__ not in ('ReadTimeout', 'ConnectTimeout', 'ConnectionError', 'RuntimeError'):
                        raise
            db = sqlite3.connect((RUNTIME / 'ledger.sqlite3').as_uri() + '?mode=ro', uri=True)
            try:
                row = db.execute("SELECT metadata FROM files WHERE project=? AND active=1 AND json_extract(metadata,'$.readAccessStatus')='Available' ORDER BY COALESCE(size,0),id LIMIT 1", (client.project_id,)).fetchone()
            finally:
                db.close()
            if row:
                metadata = json.loads(row[0])
                size = int(metadata.get('contentSize') or 0)
                if 0 < size <= 1024 * 1024:
                    target = RUNTIME / 'association_probe_download' / str(metadata['id']) / Path(metadata['filename']).name
                    result = client.download(metadata, target)
                    record(kind='download', content_id=metadata['id'], bytes=result['bytes'], sha256=result['sha256'])
    record(kind='complete', logout_confirmed=bool(client.logout_result and client.logout_result.get('confirmed')))


if __name__ == '__main__':
    main()
