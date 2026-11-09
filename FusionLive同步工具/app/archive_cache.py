"""Content-addressed extraction cache, independent of business parser versions.

Run directories keep their own paths for existing audit readers. Hard links share
the extracted bytes; a different volume falls back to copying, never to unpacking.
"""
from collections import defaultdict
from contextlib import ExitStack, closing
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
    from paths import check_output_directory
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


def _cleanup_lineages(queue):
    """Use explicit platform lineage; filenames alone never prove succession."""
    from archive_index import readonly
    database = getattr(queue, 'database', queue.cache.parent / 'ledger.sqlite3')
    with closing(readonly(database)) as db:
        files = {(r['project'], r['id']): dict(r) for r in db.execute('SELECT * FROM files')}
        own = defaultdict(set)
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='documents'").fetchone():
            columns = {r[1] for r in db.execute('PRAGMA table_info(documents)')}
            if {'project', 'id', 'first_id'} <= columns:
                for row in db.execute('SELECT project,id,first_id FROM documents'):
                    own[(row['project'], row['id'])].add(str(row['first_id'] or row['id']))
    families = {}
    for key, row in files.items():
        family = (key[0], 'file', key[1])
        if len(own[key]) == 1:
            family = (key[0], 'document', next(iter(own[key])))
        elif not own[key]:
            data = json.loads(row.get('metadata') or '{}')
            if data.get('firstVersionId'):
                # Package attachment members must remain separate families.
                family = (key[0], 'attachment', str(data['firstVersionId']),
                          row.get('relation'), row['filename'].casefold())
        families[key] = family
    # Older catalogue migrations may have removed a file row while retaining
    # its own-document version evidence. That explicit evidence is sufficient.
    for key, first_ids in own.items():
        if key not in families and len(first_ids) == 1:
            families[key] = (key[0], 'document', next(iter(first_ids)))
    return families, files


def cleanup_history(queue, *, progress=None):
    """Shared automatic/manual maintenance, excluding active file delivery."""
    from analysis_worker import worker_lock
    with worker_lock(queue.cache.parent / 'wechat_delivery') as acquired:
        if not acquired:
            raise ValueError('文件投递正在使用缓存，请稍后再清理。')
        return _cleanup_history(queue, progress=progress)


def _cleanup_history(queue, *, progress=None):
    """Remove obsolete extraction bytes only after a replacement is committed.

    Original downloads, JSON analysis reports and inventory rows are retained.
    All current jobs must finish before collecting shared content caches, since
    pending archives may still need their historical nested archive caches.
    """
    from analysis_worker import worker_lock
    base = queue.cache.resolve()
    jobs_root = base / 'jobs'
    log = progress or (lambda message: None)
    log('核对下载台账、文档版本关联和解析任务')
    families, files = _cleanup_lineages(queue)
    active = list(queue.db.execute('SELECT * FROM jobs WHERE active=1'))
    reports = {}

    def report_for(row):
        if row['key'] in reports:
            return reports[row['key']]
        report = None
        if row['result_path'] and re.fullmatch(r'[0-9a-f]{64}', row['key']):
            result = (base / row['result_path']).resolve()
            if (result.is_relative_to(jobs_root / row['key'])
                    and not (result.parent / 'extraction.cleaned').exists()):
                try:
                    report = json.loads(result.read_text(encoding='utf-8'))
                    entries = report.get('files') if isinstance(report, dict) else None
                    if (not isinstance(entries, list) or not entries or not isinstance(entries[0], dict)
                            or (row['expected_sha'] and entries[0].get('sha256') != row['expected_sha'])):
                        report = None
                except (OSError, ValueError, TypeError):
                    pass
        reports[row['key']] = report
        return report

    completed = defaultdict(list)
    current_ids = defaultdict(set)
    for key, source in files.items():
        if source['active']:
            current_ids[families.get(key, (key[0], 'file', key[1]))].add(key[1])
    by_id = set()
    for row in active:
        key = row['project'], row['file_id']
        source = files.get(key)
        if (row['state'] != 'done' or not source or not source['active'] or source['status'] != 'done'
                or (source.get('sha256') and source['sha256'] != row['expected_sha'])
                or report_for(row) is None):
            continue
        by_id.add(key)
        completed[families.get(key, (key[0], 'file', key[1]))].append(row)
    # Multiple current file IDs in one family are ambiguous. Preserve historical
    # task directories until the platform association is unambiguous.
    unique_families = {family for family, rows in completed.items()
                       if len(current_ids[family]) == 1 and len({r['file_id'] for r in rows}) == 1}
    removed = 0
    checked = retained = 0
    for row in queue.db.execute('SELECT key,project,file_id FROM jobs WHERE active=0'):
        checked += 1
        key = row['project'], row['file_id']
        family = families.get(key, (key[0], 'file', key[1]))
        if key not in by_id and family not in unique_families:
            retained += 1
            continue
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
                if cleaned:
                    log(f'已清理旧任务解压目录：{row["key"][:12]}；{cleaned} 个目录')
    log(f'旧任务核对：检查 {checked} 个；成功替代关系明确 {checked-retained} 个；关联不明确或新版未就绪，保留 {retained} 个')
    archive_ids = {key for key, source in files.items() if source['active']
                   and Path(source['filename']).suffix.lower() in ('.zip', '.7z')}
    if (not archive_ids <= by_id or
            any(r['state'] != 'done' or (r['project'], r['file_id']) not in by_id for r in active)):
        log('共享解压缓存暂时保留：仍有任务未完成、源版本已变化或结果无法核验')
        return removed
    live = {r['expected_sha'] for r in active if r['expected_sha']}
    for row in active:
        try:
            report = report_for(row)
            if report is None:
                return removed
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
                log(f'已清理无引用共享解压缓存：{slot.name}')
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
    from paths import HOME
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
    try:
        with ExitStack() as stack:
            for folder, name, label in ((cache, 'worker.lock', '后台解析'),
                                        (cache.parent, 'sync.lock', '同步下载')):
                if not stack.enter_context(worker_lock(folder, name)):
                    raise ValueError(label+'正在运行，请安全停止后再单独清理。')
            if (cache.parent / 'layout.pending').exists():
                raise ValueError('文件迁移尚未完成，请先恢复迁移。')
            with closing(readonly(database)) as db:
                count = cleanup_history(SimpleNamespace(cache=cache, db=db,
                    database=cache.parent / 'ledger.sqlite3'), progress=lambda text: print(text, flush=True))
            print(f'已清理旧版解压目录/缓存 {count} 个；原始压缩包、版本记录和解析报告保留。', flush=True)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, '缓存清理未完成：'+str(exc)+'\n')


if __name__ == '__main__':
    main()
