"""Read-only ledger analysis; optional bounded live probes, never full downloads."""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import requests
from download_diagnostics import download_parameters
from fusion_client import RUNTIME, now


def inspect(database):
    with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        rows = [dict(row) for row in db.execute("""SELECT project,id,filename,status,size,folder_id,metadata,error
            FROM files WHERE active=1 AND lower(filename) LIKE '%.zip' ORDER BY size,id""")]
    failed = [row for row in rows if row['status'] != 'done']
    successful = [row for row in rows if row['status'] == 'done']
    folders = {row['folder_id'] for row in failed}
    controls = [row for row in successful if row['folder_id'] in folders]
    samples = ([max(controls, key=lambda row: row['size'])] if controls else [])
    if failed:
        samples += [min(failed, key=lambda row: row['size']), max(failed, key=lambda row: row['size'])]
    samples = list({(row['project'], row['id']): row for row in samples}.values())
    def summary(row):
        data = json.loads(row['metadata'])
        return {**{key: row[key] for key in ('project', 'id', 'filename', 'status', 'size', 'folder_id')},
                'read_access': data.get('readAccessStatus'), 'virtual': data.get('isVirtualLink'),
                'renditions': data.get('hasRenditions'),
                'last_http_status': str(row.get('error') or '')[:3]}
    report = dict(database=str(database.resolve()), generated_at=now().isoformat(),
                  mode='offline', failed_archives=[summary(row) for row in failed],
                  successful_archives=len(successful),
                  maximum_successful_size=max((row['size'] for row in successful), default=None),
                  minimum_incomplete_size=min((row['size'] for row in failed), default=None),
                  probe_samples=[summary(row) for row in samples], probes=[])
    return report, samples


def probe_one(client, row):
    item = json.loads(row['metadata'])
    result = dict(id=row['id'], filename=row['filename'], size=row['size'], checked_at=now().isoformat())
    if item.get('readAccessStatus') != 'Available':
        return {**result, 'skipped': '平台未声明文件可读取'}
    if row['project'] != client.project_id:
        raise RuntimeError('登录工作区与诊断台账不一致，停止')
    params = {**download_parameters(client, item), 'guid': str(uuid.uuid4())}
    try:
        # Use the production request exactly. Close after headers on success;
        # do not save file bytes, change the ledger, or try alternative privileges.
        with client.request('GET', '/servlet/HtmlDownloadServlet', params=params,
                            stream=True, headers={'Accept-Encoding': 'identity'}) as response:
            result.update(status=response.status_code,
                          headers={key: response.headers[key] for key in ('Content-Type', 'Content-Length', 'Content-Range')
                                   if key in response.headers}, body_saved=False)
    except requests.HTTPError as exc:
        result['error'] = getattr(exc, 'download_evidence', {'status': exc.response.status_code})
        if exc.response.status_code != 403:
            exc.probe_result = result
            raise
        # A download-only denial is different from an expired global session.
        try:
            client.folders(client.cabinet_id)
            result['protected_catalogue_access'] = 'ok'
        except Exception as access_error:
            result['protected_catalogue_access'] = type(access_error).__name__
            access_error.probe_result = result
            raise
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=RUNTIME / 'ledger.sqlite3')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--probe', action='store_true', help='登录后探测最多三个样本；正常响应只读响应头，不下载完整文件')
    parser.add_argument('--manual-override', action='store_true', help='仅在操作人员明确批准临时诊断时跳过时段限制；仍遵守暂停及停止标记')
    args = parser.parse_args(argv)
    if args.manual_override and not args.probe:
        parser.error('--manual-override 仅用于 --probe')
    report, samples = inspect(args.database)
    output = args.output or RUNTIME / ('download-diagnostic-' + now().strftime('%Y%m%d-%H%M%S') + '.json')
    if output.resolve() == args.database.resolve() or output.suffix.lower() != '.json':
        raise ValueError('诊断输出必须为独立 JSON 文件，不可覆盖台账')
    def save():
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    if args.probe:
        from local_credentials import get_credentials
        from login_test import TestClient, check_test_window
        from sync import RunLock
        check_test_window(manual_override=args.manual_override)
        client = TestClient(*get_credentials(), manual_override=args.manual_override)
        report['mode'] = 'live-probe'
        try:
            with RunLock(), client:
                report['rendition_security'] = client.rendition_security
                report['native_user'] = client.native_user
                if client.rendition_security is None or (client.rendition_security and not client.native_user):
                    raise RuntimeError('未确认原件下载权限，停止诊断')
                client.folders(client.cabinet_id)
                report['protected_catalogue_before'] = 'ok'
                for row in samples:
                    try:
                        report['probes'].append(probe_one(client, row))
                    finally:
                        save()
        except Exception as exc:
            if hasattr(exc, 'probe_result'):
                report['probes'].append(exc.probe_result)
            report['stopped'] = type(exc).__name__
            raise
        finally:
            report['logout_confirmed'] = bool(client.logout_result and client.logout_result.get('confirmed'))
            client.http.close()
            save()
    print(f'诊断结果：{output.resolve()}；未完成 ZIP：{len(report["failed_archives"])} 个', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
