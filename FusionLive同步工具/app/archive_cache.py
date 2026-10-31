"""Content-addressed extraction cache, independent of business parser versions.

Run directories keep their own paths for existing audit readers. Hard links share
the extracted bytes; a different volume falls back to copying, never to unpacking.
"""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import uuid

SCHEMA = 1  # Increment when extraction/path validation semantics change.


def _hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def _path(root, value):
    from local_analysis import _parts
    path = root.joinpath(*_parts(value))
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('解压缓存包含越界路径或链接')
    return path


def _validate(root, records):
    """Do not trust size/mtime alone, including for same-size modifications."""
    checked = set()
    for record in records:
        _path(root, record['output'])
        if record['state'] == '目录已合并':
            continue
        output = record.get('duplicate_of') or record['output']
        path = _path(root, output)
        identity = output, record['size'], record['sha256']
        if identity in checked:
            continue
        if (not path.is_file() or path.stat().st_size != record['size']
                or _hash(path) != record['sha256']):
            raise ValueError('解压缓存文件缺失或SHA256不符')
        checked.add(identity)


def _link(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def _materialize(root, records, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        raise ValueError('解压目标必须为新目录，不能覆盖已有目录。')
    outputs = []
    with tempfile.TemporaryDirectory(prefix='.reuse-', dir=destination.parent) as temp:
        staging = Path(temp)
        for record in records:
            if record['state'] == '目录已合并':
                _path(staging, record['output']).mkdir(parents=True, exist_ok=True)
            elif not record.get('duplicate_of'):
                _link(_path(root, record['output']), _path(staging, record['output']))
                outputs.append(destination / record['output'])
        staging.rename(destination)
    return outputs


def _consume(records, budget):
    from local_analysis import MAX_ARCHIVE_FILE_BYTES
    sizes = [r['size'] for r in records if r['state'] != '目录已合并']
    if any(size < 0 or size > MAX_ARCHIVE_FILE_BYTES for size in sizes):
        raise ValueError('解压缓存内单文件超过当前大小限制')
    if sum(sizes) > budget['bytes'] or len(records) > budget['files']:
        raise ValueError('压缩包展开总大小或文件数超过本次分析上限。')
    budget['bytes'] -= sum(sizes)
    budget['files'] -= len(records)


def _legacy(cache, sha):
    """Adopt committed pre-cache extractions without changing historical files."""
    queue = cache.parent / 'queue.sqlite3'
    if not queue.is_file():
        return
    from archive_index import readonly
    with closing(readonly(queue)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        nested = (" OR key IN (SELECT job_key FROM archive_inventory WHERE json_extract(payload,'$.sha256')=?)"
                  if 'archive_inventory' in tables else '')
        paths = [r[0] for r in db.execute(
            'SELECT result_path FROM jobs WHERE (expected_sha=?' + nested +
            ') AND result_path IS NOT NULL ORDER BY updated DESC', (sha, sha) if nested else (sha,))]
    for value in paths:
        report_path = (cache.parent / value).resolve()
        if (not report_path.is_relative_to(cache.parent / 'jobs') or not report_path.is_file()
                or report_path.stat().st_size > 64 * 1024**2):
            continue
        try:
            report = json.loads(report_path.read_text(encoding='utf-8'))
            if 'archive_members' not in report:
                continue
            for index, item in enumerate(report.get('files', []), 1):
                if item.get('sha256') != sha or item.get('state') != '已解压':
                    continue
                records = [{k: m[k] for k in ('entry', 'member', 'size', 'output', 'state', 'sha256', 'duplicate_of') if k in m}
                           for m in report.get('archive_members', []) if m['archive'] == item['origin']]
                root = report_path.parent / 'extracted' / f'{index:06d}-{sha[:12]}'
                if root.is_dir():
                    _validate(root, records)
                    yield root, records
        except (OSError, ValueError, KeyError, TypeError):
            continue


def extract_cached(archive, destination, *, cache, sha, extractor, budget, audit, progress=None):
    from analysis_worker import worker_lock
    from local_analysis import MAX_ARCHIVE_EXPANDED_BYTES, MAX_ARCHIVE_ENTRIES
    from output_paths import check_output_directory
    cache = check_output_directory(cache, source_root=archive)
    destination = check_output_directory(destination, source_root=archive)
    budget = budget if budget is not None else dict(bytes=MAX_ARCHIVE_EXPANDED_BYTES, files=MAX_ARCHIVE_ENTRIES)
    key = f'v{SCHEMA}-{Path(archive).suffix.lower()[1:]}-{sha}'
    slot = cache / key
    slot.mkdir(parents=True, exist_ok=True)
    with worker_lock(cache, key + '.lock') as acquired:
        if not acquired:
            raise OSError('另一个任务正在生成此压缩包的解压缓存，请稍后重试')
        manifest = slot / 'manifest.json'
        reused = False
        try:
            data = json.loads(manifest.read_text(encoding='utf-8'))
            if data['schema'] != SCHEMA or data['sha256'] != sha:
                raise ValueError('解压缓存版本不符')
            root = _path(slot, data['generation']) / 'files'
            records = data['records']
            _validate(root, records)
            reused = True
        except (OSError, ValueError, KeyError, TypeError):
            root, records = None, None
        if root is None:
            try:
                legacy = next(_legacy(cache, sha), None)
            except (sqlite3.Error, OSError, ValueError):
                legacy = None
            generation = uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix='.build-', dir=slot) as temp:
                staging = Path(temp)
                if legacy is not None:
                    root, records = legacy
                    _materialize(root, records, staging / 'files')
                    reused = True
                else:
                    records = []
                    if progress:
                        progress(f'缓存未命中，开始解压 {Path(archive).suffix[1:].upper()}：{archive}')
                    # Reserve the caller's budget only once, below. A private
                    # copy still enforces all original extraction limits here.
                    extractor(archive, staging / 'files', budget=dict(budget), audit=records)
                _validate(staging / 'files', records)
                staging.rename(slot / generation)
            root = slot / generation / 'files'
            data = dict(schema=SCHEMA, sha256=sha, generation=generation, records=records)
            pending = slot / (generation + '.json.tmp')
            pending.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
            os.replace(pending, manifest)
        _consume(records, budget)
        files = _materialize(root, records, destination)
        audit.extend(records)
        if progress:
            progress(('复用已校验解压缓存' if reused else '已保存解压缓存') + f'：{archive}；SHA256 {sha[:12]}')
        return files


def _remove_tree(path, allowed):
    """Never follow a cleanup target outside the explicitly owned cache tree."""
    allowed = allowed.resolve()
    resolved = path.resolve()
    if path.is_symlink() or resolved == allowed or not resolved.is_relative_to(allowed):
        raise ValueError('拒绝清理缓存目录之外的路径')
    if path.is_dir():
        shutil.rmtree(path)
        return 1
    return 0


def cleanup_history(queue):
    """Remove obsolete extraction bytes only after a replacement is committed.

    Original downloads, JSON analysis reports and inventory rows are retained.
    All current jobs must finish before collecting shared content caches, since
    pending archives may still need their historical nested archive caches.
    """
    from analysis_worker import worker_lock
    base = queue.cache.resolve()
    jobs_root = base / 'jobs'
    removed = 0
    for row in queue.db.execute('''SELECT old.key FROM jobs old
        WHERE old.active=0 AND EXISTS
        (SELECT 1 FROM jobs current WHERE current.project=old.project
         AND current.file_id=old.file_id AND current.active=1 AND current.state='done')'''):
        job_root = (jobs_root / row['key']).resolve()
        if not re.fullmatch(r'[0-9a-f]{64}', row['key']) or not job_root.is_relative_to(jobs_root):
            continue
        # A failed/interrupted attempt may have no committed result_path. Clean
        # all superseded attempts, retaining each report for the audit trail.
        for run in job_root.glob('analysis-*'):
            if run.is_dir() and not run.is_symlink() and run.resolve().is_relative_to(job_root):
                if (run / 'extracted').exists() or (run / 'split_idf').exists():
                    (run / 'extraction.cleaned').write_text('Rebuild extracted files before reactivating this historical result.\n', encoding='utf-8')
                cleaned = _remove_tree(run / 'extracted', jobs_root)
                cleaned += _remove_tree(run / 'split_idf', jobs_root)
                removed += cleaned
    active = list(queue.db.execute('SELECT expected_sha,state,result_path FROM jobs WHERE active=1'))
    if any(r['state'] != 'done' for r in active):
        return removed
    live = {r['expected_sha'] for r in active if r['expected_sha']}
    for row in active:
        if not row['result_path']:
            return removed
        result = (base / row['result_path']).resolve()
        if not result.is_relative_to(jobs_root):
            return removed
        try:
            report = json.loads(result.read_text(encoding='utf-8'))
            live.update(r['sha256'] for r in report['files']
                        if r.get('sha256') and Path(r['path']).suffix.lower() in ('.zip', '.7z'))
        except (OSError, ValueError, KeyError, TypeError):
            return removed  # Incomplete references must never trigger collection.
    cache = base / 'extraction_cache'
    if not cache.is_dir():
        return removed
    for slot in cache.iterdir():
        match = re.fullmatch(r'v\d+-(?:zip|7z)-([0-9a-f]{64})', slot.name)
        if not match or not slot.is_dir():
            continue
        with worker_lock(cache, slot.name + '.lock') as acquired:
            if not acquired:
                continue
            if match[1] not in live:
                removed += _remove_tree(slot, cache)
                continue
            try:
                data = json.loads((slot / 'manifest.json').read_text(encoding='utf-8'))
                current = data['generation']
                for generation in slot.iterdir():
                    if (generation.is_dir() and re.fullmatch(r'[0-9a-f]{32}', generation.name)
                            and generation.name != current):
                        removed += _remove_tree(generation, cache)
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return removed


def main():
    import argparse
    from types import SimpleNamespace
    from app_paths import HOME
    from analysis_worker import worker_lock
    from archive_index import readonly
    parser = argparse.ArgumentParser(description='清理已有成功替代版本的旧解压文件，保留原始压缩包和解析报告')
    parser.add_argument('--cleanup', action='store_true', required=True)
    parser.add_argument('--base-dir', type=Path, default=HOME)
    args = parser.parse_args()
    cache = args.base_dir.resolve() / 'runtime/file_analysis'
    database = cache / 'queue.sqlite3'
    if not database.is_file():
        print('未找到本地解析队列，没有可清理的历史解压文件。')
        return
    with worker_lock(cache) as acquired:
        if not acquired:
            parser.exit(1, '后台解析正在运行，请安全停止后再单独清理。\n')
        with closing(readonly(database)) as db:
            count = cleanup_history(SimpleNamespace(cache=cache, db=db))
        print(f'已清理旧版解压目录/缓存 {count} 个；原始压缩包、版本记录和解析报告保留。')


if __name__ == '__main__':
    main()
