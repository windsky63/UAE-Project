"""Current extracted-member inventory, separate from platform download records."""
from contextlib import closing
import hashlib
import json
from pathlib import Path, PurePosixPath
import sqlite3


def readonly(path):
    db = sqlite3.connect('file:' + str(Path(path).resolve()) + '?mode=ro', uri=True, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def report_members(report, result_path):
    """Includes every extracted file format and nested archive, even if not parsed."""
    files = {r['origin']: (index, r) for index, r in enumerate(report.get('files', []), 1)}
    for member in report.get('archive_members', []):
        if member.get('state') == '目录已合并':
            continue
        archive = member['archive']
        output = member.get('duplicate_of') or member['output']
        path = member.get('extracted_path')
        if not path:
            # Read previous committed caches without re-extracting their sources.
            info = files.get(archive)
            if not info or not info[1].get('sha256'):
                continue
            index, record = info
            path = str(Path(result_path).parent / 'extracted' / f'{index:06d}-{record["sha256"][:12]}' / output)
        origin = archive + '!/' + member['output']
        parsed = files.get(origin, (None, {}))[1]
        yield dict(project=str(member['project']), archive=archive, member=member['member'],
                   entry=member['entry'], origin=origin, path=path, bytes=member['size'],
                   sha256=member.get('sha256'), filename=PurePosixPath(member['member'].replace('\\', '/')).name,
                   extraction_state=member['state'], duplicate_of=member.get('duplicate_of', ''),
                   analysis_state=parsed.get('state', '已解压，内容未解析'))


def current_members(base, *, ledger_path=None, include_inactive=False, downloads=None):
    """Committed caches matching downloaded parents; opt in to local old versions."""
    base = Path(base).resolve()
    downloads = Path(downloads).resolve() if downloads is not None else base / 'downloads'
    cache = base / 'runtime/file_analysis'
    queue_path = cache / 'queue.sqlite3'
    ledger = Path(ledger_path) if ledger_path is not None else base / 'runtime/ledger.sqlite3'
    if not ledger.is_file() or not queue_path.is_file():
        return []
    with closing(readonly(ledger)) as db:
        parents = {(str(r['project']), str(r['id'])): dict(r) for r in db.execute(
            "SELECT project,id,filename,path,bytes,sha256 FROM files WHERE status='done'"
            + ("" if include_inactive else " AND active=1"))}
    results, seen = [], set()
    with closing(readonly(queue_path)) as queue:
        tables = {r[0] for r in queue.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'jobs' not in tables:
            return []
        for raw in queue.execute("SELECT * FROM jobs WHERE state IN ('done','failed') AND result_path IS NOT NULL"
                                 + ("" if include_inactive else " AND active=1") + " ORDER BY updated DESC"):
            job = dict(raw)
            key = str(job['project']), str(job['file_id'])
            parent = parents.get(key)
            if key in seen or not parent or not parent['sha256'] or parent['sha256'] != job['expected_sha']:
                continue
            parent_path = (base / str(parent['path']).replace('\\', '/')).resolve()
            if (not parent_path.is_relative_to(downloads) or parent_path.is_symlink() or not parent_path.is_file() or
                    parent_path.stat().st_size != parent['bytes']):
                continue
            result_path = (cache / job['result_path']).resolve()
            job_root = (cache / 'jobs' / job['key']).resolve()
            if (not job_root.is_relative_to(cache / 'jobs') or not result_path.is_relative_to(job_root)
                    or result_path.name != 'analysis.json' or not result_path.is_file()):
                continue
            seen.add(key)
            rows = []
            if 'archive_inventory' in tables:
                rows = [json.loads(r[0]) for r in queue.execute(
                    'SELECT payload FROM archive_inventory WHERE job_key=? ORDER BY archive,entry', (job['key'],))]
            if not rows:
                if result_path.stat().st_size > 64 * 1024 * 1024:
                    continue
                report = json.loads(result_path.read_text(encoding='utf-8'))
                rows = list(report_members(report, result_path))
            for row in rows:
                path = Path(row['path']).resolve()
                if row['project'] != key[0] or not path.is_relative_to(result_path.parent / 'extracted'):
                    continue
                row = dict(row)
                row.update(parent_file_id=key[1], parent_filename=parent['filename'], parent_path=parent['path'],
                           parent_sha256=parent['sha256'], job_key=job['key'], result_path=job['result_path'],
                           path=str(path), member_path=row['origin'].split('!/', 1)[-1])
                row['availability'] = ('ready' if path.is_file() and path.stat().st_size == row['bytes']
                                       else 'missing_or_size_mismatch')
                row['id'] = hashlib.sha256(json.dumps([key, job['key'], row['archive'], row['entry'], row['member']],
                                                     ensure_ascii=False).encode()).hexdigest()[:24]
                results.append(row)
    return results


def verified_member(base, identity):
    """Revalidate ancestry and child bytes on every read or download."""
    item = next((r for r in current_members(base) if r['id'] == identity), None)
    if item is None or item['availability'] != 'ready' or not item['sha256']:
        raise ValueError('压缩包版本已变化或解压文件缺失，请刷新台账并重新解析')
    path = Path(item['path'])
    parent = Path(base).resolve() / item['parent_path']
    parent_digest = hashlib.sha256()
    with parent.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            parent_digest.update(block)
    if parent_digest.hexdigest() != item['parent_sha256']:
        raise ValueError('所属压缩包SHA256校验失败，请重新下载或解析')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != item['sha256']:
        raise ValueError('解压文件SHA256校验失败')
    return item, path
