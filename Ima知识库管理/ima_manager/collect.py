"""Copy ledger-selected procedures and drawings; source files are read-only."""
from collections import Counter, defaultdict
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import zipfile

from .api import ImaError
from .config import RUNTIME, atomic_json
from .download_paths import DownloadPaths, existing_download_path
from .sync import DELETE_DIRECTORY, fingerprint


def rules():
    from . import collection_rules
    return collection_rules


def open_source(path, stop, notify):
    """Share read access with NAS; briefly wait for Windows exclusive handles."""
    for attempt in range(3):
        try:
            return path.open('rb')
        except PermissionError as exc:
            code = getattr(exc, 'winerror', None)
            # CPython's CRT open may expose sharing violations as EACCES only.
            if code not in (32, 33) and not (os.name == 'nt' and code is None and exc.errno == 13):
                raise
            if attempt == 2:
                raise ImaError('源文件持续被占用或无读取权限，保留现有副本，本次跳过；请核对权限，下次定时同步再试。') from None
            delay = attempt + 1
            notify(f'资料提取：{path.name} 正被其他程序占用，{delay} 秒后重试读取')
            if stop is not None:
                if stop.wait(delay):
                    raise ImaError('已安全停止，未替换现有副本。')
            else:
                time.sleep(delay)


def remove_local_update_history(source):
    """Remove only collection-generated old copies, never manual deletion entries."""
    deleted = source / DELETE_DIRECTORY
    history = deleted / '更新旧版'
    def linked(path):
        return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()
    if not history.exists() and not linked(history):
        return 0
    if (linked(deleted) or linked(history) or not history.is_dir()
            or not history.resolve().is_relative_to(source)
            or history.resolve() != source / DELETE_DIRECTORY / '更新旧版'):
        raise ImaError('本地更新旧版目录位置异常，未清理。')
    count = 0
    for current, directories, names in os.walk(history, followlinks=False):
        for name in directories + names:
            path = Path(current) / name
            if linked(path) or not path.resolve().is_relative_to(history):
                raise ImaError('本地更新旧版目录含链接或越界路径，未清理。')
        count += len(names)
    # Validate the absolute target before recursive removal on Windows.
    shutil.rmtree(history)
    return count


class HashingWriter:
    """Hash the bytes as they are staged, avoiding a second temporary-file read."""
    def __init__(self, stream):
        self.stream = stream
        self.digest = hashlib.sha256()

    def write(self, chunk):
        written = self.stream.write(chunk)
        self.digest.update(chunk[:written])
        return written

    def seek(self, *args):
        return self.stream.seek(*args)


@contextmanager
def collect_lock(runtime):
    runtime.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(runtime / 'collect-lock.sqlite3', timeout=0)
    try:
        try:
            db.execute('CREATE TABLE IF NOT EXISTS guard (id INTEGER)')
            db.execute('BEGIN IMMEDIATE')
        except sqlite3.OperationalError:
            raise ImaError('另一个资料提取任务正在运行。') from None
        yield
    finally:
        db.close()


def read_rows(ledger, selection):
    import openpyxl
    workbook = openpyxl.load_workbook(ledger, read_only=True, data_only=True)
    try:
        main = workbook['文件台账'] if '文件台账' in workbook.sheetnames else workbook.worksheets[0]
        sheets = [workbook['完整字段'], main] if '完整字段' in workbook.sheetnames and main.title != '完整字段' else [main]
        for sheet in sheets:
            required = {'文件名', '业务类型', '目录'}
            if sheet.title == '完整字段':
                required |= {'工作区', '内容ID'}
            headers = None
            for number, cells in enumerate(sheet.iter_rows(values_only=True), 1):
                if headers is None:
                    if required.issubset(set(cells)):
                        headers = cells
                    continue
                row = dict(zip(headers, cells))
                if selection(row):
                    yield number, row
            if headers is not None:
                return
        raise ImaError('未找到文件台账表头，需要文件名、业务类型、目录列。')
    finally:
        workbook.close()


def collect_documents(ledger, downloads, source, *, dry_run=False, runtime=None,
                      notify=lambda message: None, stop=None, from_database=False):
    runtime = Path(runtime or RUNTIME)
    ledger, downloads, source = Path(ledger).resolve(), Path(downloads).resolve(), Path(source).resolve()
    if not ledger.is_file():
        if from_database:
            raise ImaError('下载数据库不存在，请设置同步工具的 runtime/ledger.sqlite3 路径。')
        raise ImaError('文件台账不存在，请设置服务器上的文件台账.xlsx 路径。')
    if not downloads.is_dir():
        raise ImaError('下载原文件在服务器上；请在服务器运行或设置可访问的 downloads 路径。')
    if source == downloads or source.is_relative_to(downloads) or downloads.is_relative_to(source):
        raise ImaError('待同步目录与下载源目录不能相互包含。')
    with collect_lock(runtime), DownloadPaths(ledger, downloads, database=ledger if from_database else None) as paths, \
            closing(sqlite3.connect(runtime / 'collection-pending.sqlite3')) as pending:
        # Keep each source durable without rollback-journal file creation and
        # directory flushes per row. FULL preserves committed rows on interruption.
        pending.execute('PRAGMA journal_mode=WAL')
        pending.execute('PRAGMA synchronous=FULL')
        pending.execute('CREATE TABLE IF NOT EXISTS entries (identity TEXT PRIMARY KEY, payload TEXT NOT NULL)')
        try:
            return _collect(ledger, downloads, source, dry_run, runtime, notify, stop, paths, from_database, pending)
        except zipfile.BadZipFile:
            raise ImaError('文件台账不是有效的 XLSX，或正在更新，请稍后重试。') from None


def _collect(ledger, downloads, source, dry_run, runtime, notify, stop, paths, from_database, pending):
    rule = rules()
    state_path = runtime / 'collection-state.json'
    try:
        states = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
        if not isinstance(states, dict):
            raise ValueError()
        for identity, payload in pending.execute('SELECT identity,payload FROM entries'):
            states[identity] = json.loads(payload)
    except (OSError, ValueError):
        raise ImaError('资料提取记录损坏，请先核对 runtime/collection-state.json。') from None
    if from_database:
        from .database_source import read_database
        rows = read_database(ledger, rule.selected, downloads=downloads)
    else:
        rows = list(read_rows(ledger, rule.selected))
    wanted = {str(row.get(key) or '').casefold() for _, row in rows
              for key in ('文件名', '所属压缩包文件名') if row.get(key)}
    index = None
    # Only cache hashes while file metadata still matches. Never trust a cached
    # hash after a user edit or a publication during this pass.
    fingerprints = {}
    planned = {}

    def exists(path):
        return str(path) in planned or path.exists()

    def destination_digest(path):
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ImaError('副本输出路径不安全')
        if str(path) in planned:
            return planned[str(path)]
        stat = path.stat()
        stamp = (stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        cached = fingerprints.get(str(path))
        if cached and cached[0] == stamp:
            return cached[1]
        with path.open('rb') as reader:
            digest = fingerprint(reader)
        after = path.stat()
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != stamp:
            raise ImaError('副本在校验期间发生变化，请稍后重试')
        fingerprints[str(path)] = (stamp, digest)
        return digest

    owners = defaultdict(set)
    variants = defaultdict(set)
    for identity, state in states.items():
        owners[state.get('destination')].add(identity)
        variants[state.get('base_destination', state.get('destination'))].add(state.get('destination'))

    def fallback_index():
        nonlocal index
        if index is None:
            index = defaultdict(list)
            # Build only when ledger paths fail; ordinary incremental passes avoid a full tree walk.
            for current, directories, names in os.walk(downloads, followlinks=False):
                if stop is not None and stop.is_set():
                    raise ImaError('资料提取已请求停止。')
                directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()
                                  and (Path(current) / name).resolve().is_relative_to(downloads)]
                for name in names:
                    path = Path(current) / name
                    if name.casefold() in wanted and path.is_file() and not path.is_symlink() and rule.inside(downloads, path):
                        index[name.casefold()].append(path)
        return index

    def locate(row, archive):
        name = str(row.get('所属压缩包文件名' if archive else '文件名') or '')
        project = str(row.get('工作区') or '').strip()
        file_id = str(row.get('所属压缩包内容ID' if archive else '内容ID') or '').strip()

        def existing(value):
            return existing_download_path(downloads, value)

        if file_id:
            if not project:
                raise ImaError('缺少工作区，不能按内容ID唯一定位源文件')
            registered = row['_registered_paths'] if '_registered_paths' in row else paths.lookup(project, file_id)
            if registered is not None:
                for value in registered:
                    found = existing(value)
                    if found is not None:
                        return found
                detail = '；'.join(str(value) for value in registered) if registered else '尚未登记下载路径'
                raise ImaError(f'内容ID {file_id}（工作区 {project}）的路径不可用：{detail}')
            # An exported exact path remains usable when the downloader DB is not deployed.
            found = existing(row.get('本地路径')) if row.get('本地路径') else None
            if found is not None:
                return found
            raise ImaError('缺少该内容ID对应的可用路径，未按文件名或数字后缀猜选')
        candidates = []
        local = rule.relative_path(row.get('本地路径'))
        if local and (not archive or local.name == name):
            candidates.append(downloads / local)
        directory, safe_name = rule.relative_path(row.get('目录')), rule.relative_path(name)
        if directory and safe_name and len(safe_name.parts) == 1:
            candidates.append(downloads / directory / safe_name)
        for path in candidates:
            if rule.inside(downloads, path) and not path.is_symlink() and path.is_file():
                return path
        matches = fallback_index().get(name.casefold(), [])
        if len(matches) == 1:
            return matches[0]
        raise ImaError('文件缺失' if not matches else '存在多个同名源文件，无法唯一定位')

    report = {'ledger': str(ledger), 'downloads': str(downloads), 'source': str(source),
              'source_type': 'database' if from_database else 'xlsx',
              'database': str(ledger) if from_database else None,
              'dry_run': dry_run, 'scope': 'all', 'include_attachments': False,
              'include_guides': False, 'collection_roots': ['项目管理规定', '图纸'],
              'selected_rows': len(rows), 'files': []}
    report['removed_local_history_files'] = 0 if dry_run else remove_local_update_history(source)
    seen = set()
    for number, row in rows:
        if stop is not None and stop.is_set():
            report['cancelled'] = True
            break
        entry = {'ledger_row': number, 'name': str(row.get('文件名') or ''), 'status': '',
                 'project': str(row.get('工作区') or ''), 'file_id': str(row.get('内容ID') or ''),
                 'archive_file_id': str(row.get('所属压缩包内容ID') or '')}
        entry['record_source'] = row.get('_record_source', 'xlsx')
        report['files'].append(entry)
        if entry['name'] in ('', '未命名'):
            entry.update(status='placeholder', reason='台账只有占位记录')
            continue
        try:
            filename = rule.relative_path(entry['name'])
            if filename is None or len(filename.parts) != 1:
                raise ImaError('台账文件名不安全，未复制')
            category = rule.classify(row)
            if category == '99_待核对':
                raise ImaError('无法归入现有 01～07 分类，请核对台账专业或文档编号')
            archive = bool(row.get('所属压缩包文件名') and row.get('包内完整路径'))
            origin = locate(row, archive)
            member = rule.normalized(row.get('包内完整路径')) if archive else ''
            safe_member = rule.relative_path(member) if archive else None
            if archive and (safe_member is None or safe_member.name != entry['name']):
                raise ImaError('ZIP 成员路径不安全或文件名与台账不匹配')
            identity = hashlib.sha256(json.dumps([str(downloads), str(source), str(origin), member],
                                                ensure_ascii=False).encode()).hexdigest()
            if identity in seen:
                entry['status'] = 'duplicate'
                continue
            seen.add(identity)
            # The ledger title can omit the collision suffix present on the downloaded file.
            # Preserve the actual source basename, matching the existing flattened collection.
            relative = rule.relative_destination(row, category, safe_member.name if archive else origin.name)
            destination = source / relative
            if not destination.resolve().is_relative_to(source) or destination.is_symlink():
                raise ImaError('副本输出路径不安全')
            entry.update(origin=str(origin) + ('!/' + member if archive else ''), destination=str(destination),
                         business_type=str(row.get('业务类型') or ''), category=category,
                         collection_root=relative.parts[0])
            previous = states.get(identity, {})
            base_destination = destination
            entry.update(source_identity=identity, base_destination=str(base_destination))
            # Reuse the previously assigned name across incremental passes.
            if previous.get('base_destination') == str(base_destination):
                remembered = Path(previous['destination'])
                prefix = base_destination.stem + '-'
                if (remembered.parent == base_destination.parent and remembered.suffix == base_destination.suffix
                        and remembered.stem.startswith(prefix) and remembered.stem[len(prefix):].isdigit()):
                    destination = remembered
                    relative = destination.relative_to(source)
                    entry.update(destination=str(destination), identity_tag=previous.get('identity_tag', ''))
            if destination.is_symlink() or not destination.resolve().is_relative_to(source):
                raise ImaError('副本输出路径不安全')
            origin_stat = origin.stat()
            source_stamp = [origin_stat.st_size, origin_stat.st_mtime_ns]
            if (destination.is_file() and previous.get('destination') == str(destination)
                    and previous.get('source_stamp') == source_stamp):
                dest_stat = destination.stat()
                if previous.get('destination_stamp') == [dest_stat.st_size, dest_stat.st_mtime_ns]:
                    entry.update(status='unchanged', sha256=previous['sha256'])
                    continue
            # Stage in private runtime. Never extract archive paths directly to disk.
            with tempfile.TemporaryFile(dir=runtime) as buffer:
                writer = HashingWriter(buffer)
                with open_source(origin, stop, notify) as original_stream:
                    before = os.fstat(original_stream.fileno())
                    if archive:
                        from .archive_read import copy_member
                        copy_member(original_stream, member, writer, runtime)
                    else:
                        shutil.copyfileobj(original_stream, writer)
                    after = os.fstat(original_stream.fileno())
                    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                        raise ImaError('源文件在复制期间发生变化，请稍后重试')
                    published = origin.stat()
                    if (published.st_ino, published.st_size, published.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                        raise ImaError('源文件在复制期间被替换，保留现有副本，请下次同步重试')
                digest = writer.digest.hexdigest()
                entry['sha256'] = digest
                replacing = False
                if exists(destination):
                    old_digest = destination_digest(destination)
                    if old_digest == digest:
                        entry['status'] = 'unchanged'
                    elif (previous.get('destination') == str(destination)
                          and previous.get('sha256') == old_digest
                          and not (owners[str(destination)] - {identity})):
                        replacing = True
                    else:
                        # First reuse any existing variant with identical bytes.
                        # Include on-disk variants for recovery after publication
                        # but before its state transaction was committed.
                        candidates = set(variants[str(base_destination)])
                        if base_destination.parent.is_dir():
                            candidates.update(str(p) for p in base_destination.parent.iterdir()
                                              if p.stem.startswith(base_destination.stem + '-')
                                              and p.stem[len(base_destination.stem) + 1:].isdigit()
                                              and p.suffix == base_destination.suffix)
                        matched = None
                        for value in sorted(candidates | {str(base_destination)}):
                            candidate = Path(value)
                            if candidate.parent != base_destination.parent:
                                continue
                            if (str(candidate) in planned or candidate.is_file()) and destination_digest(candidate) == digest:
                                matched = candidate
                                break
                        if matched is not None:
                            destination = matched
                            entry['status'] = 'unchanged'
                        else:
                            # Keep the visible identity simple: -1, -2, ... .
                            number_suffix = 1
                            while True:
                                candidate = base_destination.with_name(base_destination.stem + '-' + str(number_suffix) + base_destination.suffix)
                                if candidate.is_symlink() or not candidate.resolve().is_relative_to(source):
                                    raise ImaError('副本输出路径不安全')
                                if not exists(candidate):
                                    destination = candidate
                                    break
                                number_suffix += 1
                        relative = destination.relative_to(source)
                        entry.update(destination=str(destination), reason='同名不同内容，按来源身份保留；相同内容复用已有副本')
                        entry.pop('identity_tag', None)
                        if destination != base_destination:
                            entry['identity_tag'] = '-' + destination.stem[len(base_destination.stem) + 1:]
                if dry_run and (replacing or not exists(destination)):
                    entry['status'] = 'would_update' if replacing else 'would_copy'
                    planned[str(destination)] = digest
                    variants[str(base_destination)].add(str(destination))
                    continue
                if not dry_run and (replacing or not destination.exists()):
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.parent.resolve().is_relative_to(source):
                        raise ImaError('副本输出目录位置异常')
                    buffer.seek(0)
                    descriptor, temporary_name = tempfile.mkstemp(prefix='.ima-copy-', dir=destination.parent)
                    temporary = Path(temporary_name)
                    try:
                        with os.fdopen(descriptor, 'wb') as writer:
                            shutil.copyfileobj(buffer, writer)
                        if replacing:
                            with destination.open('rb') as existing:
                                if fingerprint(existing) != old_digest:
                                    raise ImaError('旧版副本在更新期间发生变化，未替换')
                            os.replace(temporary, destination)
                            entry['old_sha256'] = old_digest
                        elif os.name == 'nt':
                            os.rename(temporary, destination)  # Windows rename never replaces an existing file.
                        else:
                            os.link(temporary, destination)  # Atomic publish, with no overwrite.
                    finally:
                        temporary.unlink(missing_ok=True)
                    entry['status'] = 'updated' if replacing else 'copied'
                if not dry_run:
                    final_stat = destination.stat()
                    states[identity] = {'destination': str(destination), 'sha256': digest, 'status': 'copied',
                                        'base_destination': str(base_destination), 'source_identity': identity,
                                        'identity_tag': entry.get('identity_tag', ''),
                                        'origin': entry['origin'], 'name': entry['name'],
                                        'project': entry['project'], 'file_id': entry['file_id'],
                                        'archive_file_id': entry['archive_file_id'],
                                        'source_stamp': source_stamp,
                                        'destination_stamp': [final_stat.st_size, final_stat.st_mtime_ns]}
                    with pending:
                        pending.execute('INSERT OR REPLACE INTO entries VALUES (?,?)',
                                        (identity, json.dumps(states[identity], ensure_ascii=False)))
                    owners[previous.get('destination')].discard(identity)
                    owners[str(destination)].add(identity)
                    variants[str(base_destination)].add(str(destination))
        except (OSError, ValueError, KeyError, RuntimeError, zipfile.BadZipFile, NotImplementedError) as exc:
            entry.update(status='error', reason=str(exc))
        detail = f"：{entry['reason']}" if entry.get('reason') else ''
        notify(f"资料提取：{entry['name']} — {entry['status']}{detail}")
    if not dry_run:
        # Each row is already durable in SQLite. Checkpoint the legacy JSON once,
        # then clear the journal; interruptions at either step are recoverable.
        atomic_json(state_path, states)
        with pending:
            pending.execute('DELETE FROM entries')
    report['counts'] = dict(Counter(e['status'] for e in report['files']))
    report['has_issues'] = bool(report['counts'].get('error') or report['counts'].get('conflict'))
    if report['has_issues']:
        notify(f"资料提取完成：失败 {report['counts'].get('error', 0)} 项，冲突 {report['counts'].get('conflict', 0)} 项；其余可用文件可继续预览和同步。")
    atomic_json(runtime / 'collection-report.json', report)
    from .daily import record_collection
    record_collection(source, report)
    return report


def prepare_documents(settings, source, *, dry_run=False, notify=lambda message: None, stop=None):
    if not settings.get('collect_enabled', True):
        return None
    legacy = settings.get('_legacy_ledger_mode', False)
    database = settings.get('database_path') or str(Path(settings['downloads_path']).parent / 'runtime/ledger.sqlite3')
    return collect_documents(settings['ledger_path'] if legacy else database, settings['downloads_path'], source,
                             dry_run=dry_run, notify=notify, stop=stop, from_database=not legacy)
