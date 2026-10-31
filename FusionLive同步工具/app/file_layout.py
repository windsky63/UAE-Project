"""Platform-named current files, short hierarchical overflow paths and recoverable moves."""
from app_paths import HOME
from collections import defaultdict
from contextlib import closing, ExitStack
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import threading

from fusion_client import now
from paths import relative_path, resolve_path, plain_path, native_path

LIMIT = 230  # Leave room for .part and application-specific suffixes.


class LayoutError(RuntimeError):
    pass


class MigrationProgress:
    """Bounded console/file updates plus a heartbeat during blocking filesystem calls."""
    def __init__(self, base, *, output=None, interval=2, heartbeat=5):
        from runtime_logging import BoundedLog
        self.output = output or (lambda line: print(line, flush=True))
        self.file = BoundedLog(Path(base) / 'runtime/path_migration.log')
        self.interval, self.heartbeat = interval, heartbeat
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.started = self.changed = self.stage_started = time.monotonic()
        self.last_output = 0
        self.stage = '启动'
        self.done = self.total = self.current = self.byte_count = self.byte_total = None
        self.thread = None

    def __enter__(self):
        def watch():
            while not self.stopped.wait(self.heartbeat):
                self.update(force=True, heartbeat=True)
        self.thread = threading.Thread(target=watch, daemon=True, name='migration-progress')
        self.thread.start()
        self.update(force=True)
        return self

    def __exit__(self, kind, value, tb):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=1)
        if kind is not None:
            self.message('迁移未完成，保留恢复记录：' + str(value))

    def message(self, line):
        with self.lock:
            text = now().strftime('[%H:%M:%S] ') + str(line).replace('\n', ' ')
            self.output(text)
            try:
                self.file.write(text + '\n')
            except OSError:
                pass  # Console progress must survive a full/unwritable log disk.

    def phase(self, name, total=None):
        with self.lock:
            self.stage = name
            self.stage_started = self.changed = time.monotonic()
            self.done, self.total = 0, total
            self.current = self.byte_count = self.byte_total = None
            self.update(force=True)

    def update(self, *, done=None, total=None, current=None, force=False, heartbeat=False):
        with self.lock:
            at = time.monotonic()
            if done is not None:
                self.done = done
                self.changed = at
            if total is not None:
                self.total = total
            if current is not None and current != self.current:
                self.current = current
                self.byte_count = self.byte_total = None
                self.changed = at
            if not force and at - self.last_output < self.interval:
                return
            parts = [self.stage]
            if self.total is not None:
                parts.append(f'{self.done or 0:,}/{self.total:,}' +
                             (f'（{(self.done or 0)/self.total:.1%}）' if self.total else ''))
            elif self.done:
                parts.append(f'已检查 {self.done:,} 项')
            if self.current:
                parts.append('当前：' + str(self.current))
            if self.byte_total is not None:
                parts.append(f'当前文件 {self.byte_count or 0:,}/{self.byte_total:,} 字节' +
                             (f'（{(self.byte_count or 0)/self.byte_total:.1%}）' if self.byte_total else ''))
            parts.append(f'本阶段 {at-self.stage_started:.0f} 秒；总耗时 {at-self.started:.0f} 秒')
            if heartbeat:
                parts.append(f'仍在处理，距进度更新 {at-self.changed:.0f} 秒')
            self.last_output = at
            self.message('；'.join(parts))

    def transfer(self, count, total):
        with self.lock:
            self.byte_count, self.byte_total = count, total
            self.changed = time.monotonic()
            self.update()


def length(path):
    return len(str(path).encode('utf-16-le')) // 2


def component(name):
    """Preserve normal names; use the requested readable compatibility suffix."""
    if not name or name in ('.', '..'):
        raise LayoutError('无效平台名称：' + repr(name))
    result = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '-', name).rstrip(' .')
    reserved = name.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL',
            *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if result != name or reserved:
        suffix = Path(result).suffix
        result = (result[:-len(suffix)] if suffix else result) + '-兼容' + suffix
    if length(result) > 255:
        raise LayoutError('单个名称超过Windows限制，未截短：' + repr(name))
    return result


def platform_folder(ledger, item):
    folder = ledger.one('''SELECT f.path FROM document_files x JOIN documents d
      ON d.project=x.project AND d.id=x.document_id AND d.folder_id=x.folder_id
      JOIN folders f ON f.project=d.project AND f.id=d.folder_id
      WHERE x.project=? AND x.file_id=? AND x.active=1 AND d.active=1 AND f.active=1
      ORDER BY (f.id=?) DESC,f.path LIMIT 1''', (item['project'], item['id'], item['folder_id']))
    if folder is None:
        folder = ledger.one('SELECT path FROM folders WHERE project=? AND id=?',
                            (item['project'], item['folder_id']))
    if folder is None:
        raise LayoutError('找不到平台目录：' + str(item['id']))
    return tuple(folder['path'].split('/'))


def targets(ledger, extra_item=None, *, progress=None):
    """Keep platform hierarchy and number only names that actually collide."""
    if progress:
        progress.phase('读取当前文件与平台目录')
    rows = ledger.all('SELECT * FROM files WHERE active=1 ORDER BY project,id')
    if extra_item is not None and not any((r['project'], r['id']) ==
                                         (extra_item['project'], extra_item['id']) for r in rows):
        rows = [*rows, extra_item]
    groups = {}
    if progress:
        progress.phase('构建平台目录映射', len(rows))
    for index, raw in enumerate(rows, 1):
        if progress:
            progress.update(done=index-1, current=raw['filename'])
        item = dict(raw)
        folder = platform_folder(ledger, item)
        component(item['filename'])
        groups.setdefault((item['project'], folder), []).append(item)
    result, directories, paths, candidates = {}, {}, {}, {}
    if progress:
        progress.update(done=len(rows), force=True)
        progress.phase('检查目录层级和路径长度', len(groups))
    for index, ((project, folder), items) in enumerate(groups.items(), 1):
        if progress:
            progress.update(done=index-1, current='/'.join(folder))
        longest = component(max(items, key=lambda r: length(component(r['filename'])))['filename'])
        local_folder = tuple(component(p) for p in folder)
        parent = Path('downloads', *local_folder)
        omitted = ()
        reserve = len(str(len(items))) + 1 if len(items) > 1 else 0
        if length(ledger.base_dir / parent / longest) + reserve > LIMIT:
            for cut in range(1, len(folder)):
                candidate = Path('downloads', 'long_path', *local_folder[cut:])
                if length(ledger.base_dir / candidate / longest) + reserve <= LIMIT:
                    parent, omitted = candidate, folder[:cut]
                    break
            else:
                raise LayoutError('原文件名及接续目录仍过长，请缩短工具安装根目录：' + '/'.join(folder))
        # Detect merged directories, including different ancestors and case-only names.
        local_parts = parent.parts[1:]
        original_start = len(omitted)
        start = 1 if omitted else 0  # long_path is a reserved local container.
        for index in range(start + 1, len(local_parts) + 1):
            local = '/'.join(local_parts[:index]).casefold()
            original = (project, folder[:original_start + index - start])
            if local in directories and directories[local] != original:
                raise LayoutError('短路径目录冲突，补充父级后仍无法在长度限制内区分：' + str(parent))
            directories[local] = original
        for item in items:
            relative = (parent / component(item['filename'])).as_posix()
            key = relative.casefold()
            owner = (item['project'], item['id'])
            candidates[owner] = dict(path=relative, platform_path='/'.join((*folder, item['filename'])),
                                     original_filename=item['filename'], omitted_parent='/'.join(omitted))
    reserved = {v['path'].casefold() for v in candidates.values()}
    if progress:
        progress.update(done=len(groups), force=True)
        progress.phase('分配同名文件与兼容名称', len(candidates))
    counts = {}
    for entry in candidates.values():
        counts[entry['path'].casefold()] = counts.get(entry['path'].casefold(), 0) + 1
    if ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='local_paths'"):
        for old in ledger.all('SELECT * FROM local_paths ORDER BY project,file_id'):
            owner = (old['project'], old['file_id'])
            entry = candidates.get(owner)
            if entry and old['platform_path'] == entry['platform_path']:
                previous, desired = Path(old['local_path']), Path(entry['path'])
                family = previous.name == desired.name or bool(re.fullmatch(
                    re.escape(desired.stem) + r'-[1-9][0-9]*' + re.escape(desired.suffix), previous.name))
                if (previous.parent == desired.parent and family and old['local_path'].casefold() not in paths
                        and length(ledger.base_dir/previous) <= LIMIT):
                    entry['path'] = old['local_path']
                    result[owner] = entry
                    paths[old['local_path'].casefold()] = owner
    for index, (owner, entry) in enumerate(candidates.items(), 1):
        if progress:
            progress.update(done=index-1, current=entry['path'])
        if owner in result:
            continue
        desired = Path(entry['path'])
        relative = desired.as_posix()
        if counts[relative.casefold()] > 1 or relative.casefold() in paths:
            number = 1
            while True:
                candidate = desired.with_name(desired.stem + '-' + str(number) + desired.suffix).as_posix()
                if candidate.casefold() not in reserved and candidate.casefold() not in paths:
                    relative = candidate
                    break
                number += 1
        if length(ledger.base_dir/relative) > LIMIT:
            raise LayoutError('同名区分后的路径过长，请缩短工具根目录：' + relative)
        paths[relative.casefold()] = owner
        result[owner] = dict(entry, path=relative)
    # A file must not occupy a directory used by a different file.
    if progress:
        progress.update(done=len(candidates), force=True)
        progress.phase('检查文件与目录冲突', len(paths))
    for index, path in enumerate(paths, 1):
        if progress:
            progress.update(done=index-1, current=path)
        parent = Path(path).parent
        while parent.as_posix() != '.':
            if parent.as_posix() in paths:
                raise LayoutError('文件名与目录名冲突：' + path)
            parent = parent.parent
    if progress:
        progress.update(done=len(paths), force=True)
    return result


def target_for(ledger, item):
    mapping = getattr(ledger, '_download_targets', None)
    if not mapping or (item['project'], item['id']) not in mapping:
        mapping = targets(ledger, item)
    return resolve_path(mapping[(item['project'], item['id'])]['path'], ledger.base_dir)


def checksum(path, cancelled=lambda: False, progress=None):
    value = hashlib.sha256()
    count, total = 0, path.stat().st_size
    if progress:
        progress.transfer(0, total)
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            if cancelled():
                raise LayoutError('已停止迁移；保留日志，下次继续。')
            value.update(block)
            count += len(block)
            if progress:
                progress.transfer(count, total)
    if progress:
        progress.update()
    return value.hexdigest()


def confined(base, relative):
    result = resolve_path(relative, base)
    if not Path(plain_path(result)).is_relative_to(base):
        raise LayoutError('路径越界：' + str(relative))
    probe = base
    for part in Path(relative).parts:
        probe = probe / part
        actual = native_path(probe)
        if actual.is_symlink() or getattr(actual, 'is_junction', lambda: False)():
            raise LayoutError('迁移路径不能包含目录链接：' + str(probe))
        if not actual.exists():
            break
    return result


def history_path(source, token):
    parts = Path(source).parts
    if parts and parts[0].lower() == 'downloads':
        parts = parts[1:]
    return Path('history', token, *parts).as_posix()


def replacement_links(ledger, files, mapping, documents=None):
    """Match inactive versions to current files, including changed platform IDs."""
    if documents is None:
        documents = {(r['project'], r['id'], r['folder_id']): dict(r)
                     for r in ledger.all('SELECT * FROM documents')}
    current = defaultdict(list)
    for row in files:
        if row['active']:
            current[(row['project'], row['folder_id'])].append(row)
    links = {}
    for old in files:
        if old['active']:
            continue
        previous = documents.get((old['project'], old['document_id'], old['folder_id']), {})
        matches = []
        for new in current[(old['project'], old['folder_id'])]:
            # A document and its attachment can share a filename. They are not
            # interchangeable predecessor versions.
            if old['relation'] and new['relation'] and old['relation'] != new['relation']:
                continue
            document = documents.get((new['project'], new['document_id'], new['folder_id']), {})
            lineage = previous.get('first_id') and previous['first_id'] == document.get('first_id')
            same_document = old['document_id'] == new['document_id']
            same_member = (old['filename'].casefold() == new['filename'].casefold() or
                           (old['id'] == old['document_id'] and new['id'] == new['document_id']))
            same_path = old['path'] and old['path'].casefold() == mapping[(new['project'], new['id'])]['path'].casefold()
            if ((lineage or same_document) and same_member) or same_path:
                matches.append(new)
        links[(old['project'], old['id'])] = matches
    return links


def local_update_keys(ledger):
    """Only actual predecessor files in downloads make a task an update."""
    files = [dict(r) for r in ledger.all('SELECT * FROM files')]
    mapping = getattr(ledger, '_download_targets', None) or targets(ledger)
    links = replacement_links(ledger, files, mapping)
    histories = [dict(r) for r in ledger.all('SELECT * FROM file_history')]
    # Build dependency components once per download run. Shared predecessors
    # must stay put until every linked successor has finished downloading.
    parents = {}

    def root(key):
        parents.setdefault(key, key)
        while parents[key] != key:
            parents[key] = parents[parents[key]]
            key = parents[key]
        return key

    def join(a, b):
        parents[root(a)] = root(b)

    owners = {}
    for row in files + histories:
        key = (row['project'], row.get('file_id', row.get('id')))
        root(key)
        paths = [row['path']]
        if key in mapping:
            paths.append(mapping[key]['path'])
        for path in filter(None, paths):
            path = relative_path(path, ledger.base_dir).casefold()
            if path in owners:
                join(key, owners[path])
            owners[path] = key
    for key, successors in links.items():
        for row in successors:
            join(key, (row['project'], row['id']))
    groups = defaultdict(set)
    for key in parents:
        groups[root(key)].add(key)
    components = {key: frozenset(group) for key, group in groups.items()}
    ledger._publication_groups = {key: components[root(key)] for key in parents}
    result = set()
    def present(path):
        return path and path.replace('\\', '/').startswith('downloads/') and confined(ledger.base_dir, path).is_file()
    for row in histories:
        if present(row['path']):
            result.add((row['project'], row['file_id']))
    for row in files:
        if present(row['path']):
            result.update((r['project'], r['id']) for r in links.get((row['project'], row['id']), [row]))
    return result


def plan(ledger, *, untracked=True, progress=None, keys=None):
    """Read-only plan. Missing physical files never become successful downloads."""
    mapping = ledger._download_targets if keys is not None else targets(ledger, progress=progress)
    if progress:
        progress.phase('读取现有文件和历史版本记录')
    if keys is None:
        files = [dict(row) for row in ledger.all('SELECT * FROM files')]
        histories = [dict(row) for row in ledger.all('SELECT * FROM file_history')]
        documents = None
    else:
        files, histories, documents = [], [], {}
        for key in sorted(keys):
            files.extend(dict(r) for r in ledger.all('SELECT * FROM files WHERE project=? AND id=?', key))
            histories.extend(dict(r) for r in ledger.all('SELECT * FROM file_history WHERE project=? AND file_id=?', key))
        for row in files:
            key = (row['project'], row['document_id'], row['folder_id'])
            if key not in documents:
                doc = ledger.one('SELECT * FROM documents WHERE project=? AND id=? AND folder_id=?', key)
                if doc:
                    documents[key] = dict(doc)
    current = {(r['project'], r['id']): r for r in files if r['active']}
    replacements = replacement_links(ledger, files, mapping, documents)
    def downloaded(row):
        return (row and row['status'] == 'done' and row['path'] and row['sha256'] and
                confined(ledger.base_dir, row['path']).is_file() and
                confined(ledger.base_dir, row['path']).stat().st_size == row['bytes'])
    moves, missing, known, used = [], [], set(), {}
    active_sources = set()
    if progress:
        progress.phase('检查当前文件引用', len(files))
    for index, row in enumerate(files, 1):
        if progress:
            progress.update(done=index-1, current=row['path'] or row['filename'])
        if row['active'] and row['path']:
            active_sources.add(relative_path(row['path'], ledger.base_dir))
    def add(source, destination, ref, row):
        source = relative_path(source, ledger.base_dir)
        known.add(source.casefold())
        if source == destination:
            return
        path = confined(ledger.base_dir, source)
        if not path.is_file():
            missing.append(source)
            return
        if source in used:
            move = used[source]
            if move['target'] != destination:
                raise LayoutError('同一物理文件被多个版本引用，请先核对：' + source)
            move['refs'].append(ref)
            return
        move = dict(source=source, target=destination, refs=[ref],
                    bytes=row.get('bytes') if row.get('bytes') is not None else path.stat().st_size,
                    sha256=row.get('sha256'))
        used[source] = move
        moves.append(move)
    if progress:
        progress.update(done=len(files), force=True)
        progress.phase('扫描历史版本文件', len(histories))
    for index, row in enumerate(histories, 1):
        if progress:
            progress.update(done=index-1, current=row['path'] or '无本地路径')
        if row['path'] and not row['path'].startswith('history/'):
            source = relative_path(row['path'], ledger.base_dir)
            replacement = current.get((row['project'], row['file_id']))
            successors = [replacement] if replacement else replacements.get((row['project'], row['file_id']), [])
            if successors and not all(downloaded(r) for r in successors):
                known.add(source.casefold())
                if not confined(ledger.base_dir, source).is_file():
                    missing.append(source)
                continue
            if source in active_sources:
                raise LayoutError('历史与当前版本引用同一路径，需核对：' + source)
            add(source, history_path(source, 'version-' + str(row['id'])),
                ['file_history', row['id']], row)
        elif row['path']:
            known.add(relative_path(row['path'], ledger.base_dir).casefold())
    if progress:
        progress.update(done=len(histories), force=True)
        progress.phase('扫描已登记文件', len(files))
    for index, row in enumerate(files, 1):
        if progress:
            progress.update(done=index-1, current=row['path'] or row['filename'])
        if row['path']:
            source = relative_path(row['path'], ledger.base_dir)
            if row['active']:
                destination = mapping[(row['project'], row['id'])]['path']
            else:
                successors = replacements.get((row['project'], row['id']), [])
                if successors and not all(downloaded(r) for r in successors):
                    known.add(source.casefold())
                    continue
                # An incomplete inventory is not evidence of platform removal.
                cycle = ledger.one('SELECT status FROM refresh_cycles WHERE project=?', (row['project'],))
                if not cycle or cycle['status'] != 'complete':
                    known.add(source.casefold())
                    continue
                token = hashlib.sha256((row['project'] + '\0' + row['id']).encode()).hexdigest()[:16]
                destination = source if source.startswith('history/') else history_path(source, 'inactive-' + token)
            add(source, destination, ['files', row['project'], row['id']], row)
    if untracked and (ledger.base_dir / 'downloads').is_dir():
        if progress:
            progress.update(done=len(files), force=True)
            progress.phase('扫描下载目录中的未登记文件')
        inspected = 0
        for parent, dirs, names in os.walk(ledger.base_dir / 'downloads', followlinks=False):
            if progress:
                progress.update(done=inspected, current=parent)
            for name in dirs:
                confined(ledger.base_dir, relative_path(Path(parent) / name, ledger.base_dir))
            for name in names:
                inspected += 1
                if progress:
                    progress.update(done=inspected, current=str(Path(parent)/name))
                source = relative_path(Path(parent) / name, ledger.base_dir)
                if source.casefold() not in known:
                    token = hashlib.sha256(source.encode()).hexdigest()[:16]
                    add(source, history_path(source, 'untracked-' + token), ['untracked'], {})
    sources = {m['source'].casefold() for m in moves}
    destinations = set()
    if progress:
        progress.phase('检查迁移目标与覆盖冲突', len(moves))
    for index, move in enumerate(moves, 1):
        if progress:
            progress.update(done=index-1, current=move['target'])
        dest = move['target'].casefold()
        if dest in destinations:
            raise LayoutError('迁移目标重名：' + move['target'])
        destinations.add(dest)
        target = confined(ledger.base_dir, move['target'])
        if target.exists() and dest not in sources:
            raise LayoutError('迁移目标已有文件，未覆盖：' + move['target'])
    if progress:
        progress.update(done=len(moves), force=True)
        progress.message(f'计划已生成：待迁移 {len(moves):,} 个；源文件缺失 {len(set(missing)):,} 个；当前路径 {len(mapping):,} 个。')
    return dict(format=1, policy='platform-names-v1', moves=moves, missing=sorted(set(missing)),
                mapping=[dict(project=k[0], file_id=k[1], **mapping[k])
                         for k in (mapping if keys is None else sorted(keys)) if k in mapping])


def ensure_schema(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS layout_moves(
      id TEXT PRIMARY KEY,payload TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending');
      CREATE TABLE IF NOT EXISTS local_paths(
      project TEXT,file_id TEXT,platform_path TEXT,local_path TEXT,omitted_parent TEXT,
      PRIMARY KEY(project,file_id));''')
    db.execute('CREATE INDEX IF NOT EXISTS file_history_content ON file_history(project,file_id)')
    db.execute('CREATE INDEX IF NOT EXISTS layout_moves_state ON layout_moves(state)')
    columns = {row[1] for row in db.execute('PRAGMA table_info(local_paths)')}
    with db:
        for column in ('original_filename', 'local_directory', 'local_filename'):
            if column not in columns:
                db.execute('ALTER TABLE local_paths ADD COLUMN ' + column + ' TEXT')


def publish(source, target, expected, cancelled, progress=None):
    """No replacement of existing files; prefer a same-volume hardlink then unlink."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except FileExistsError:
        raise LayoutError('目标已存在，保留原文件：' + str(target))
    except OSError:
        # Non-NTFS volumes: exclusive copy, fsync, verify, then remove the source.
        temporary = source.with_name('copy-' + source.name)
        temporary.unlink(missing_ok=True)  # Reserved journal staging directory only.
        created = False
        try:
            count, total = 0, source.stat().st_size
            if progress:
                progress.message('当前卷不支持硬链接，正在复制并校验：' + str(target))
                progress.transfer(0, total)
            with source.open('rb') as inp, temporary.open('xb') as out:
                created = True
                while block := inp.read(1024 * 1024):
                    if cancelled():
                        raise LayoutError('迁移已停止')
                    out.write(block)
                    count += len(block)
                    if progress:
                        progress.transfer(count, total)
                out.flush()
                os.fsync(out.fileno())
            if checksum(temporary, cancelled, progress) != expected:
                raise LayoutError('迁移副本校验失败')
            shutil.copystat(source, temporary)
            if target.exists():
                raise LayoutError('迁移目标已存在，未覆盖：' + str(target))
            os.rename(temporary, target)
        except BaseException:
            if created:
                temporary.unlink(missing_ok=True)
            raise
    source.unlink()


def apply(ledger, proposal=None, *, cancelled=lambda: False, log=print, progress=None, incremental=False,
          recover_only=False):
    """Journal before moving; resume staging/publishing after any interruption."""
    base = ledger.base_dir
    runtime = base / 'runtime'
    runtime.mkdir(exist_ok=True)
    pending = runtime / 'layout.pending'
    pending.write_text('Do not start downloads or analysis until migration completes.', encoding='utf-8')
    if progress:
        progress.phase('检查数据库与迁移恢复记录')
    ensure_schema(ledger.db)
    jobs = ledger.all("SELECT * FROM layout_moves WHERE state!='done'")
    if not jobs and not recover_only:
        proposal = proposal or plan(ledger, progress=progress)
        if proposal['moves'] and not incremental:
            backup = runtime / 'backups' / ('before-path-layout-' + now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
            backup.parent.mkdir(exist_ok=True)
            if progress:
                progress.phase('备份数据库（进度单位：数据库页）')
                progress.update(current=str(backup), force=True)
            def backup_progress(status, remaining, total):
                if cancelled():
                    raise LayoutError('备份已停止，原数据未移动。')
                if progress:
                    progress.update(done=total-remaining, total=total)
            with closing(sqlite3.connect(backup)) as target:
                ledger.db.backup(target, pages=256, progress=backup_progress)
        if proposal['moves']:
            if progress:
                progress.update(force=True)
                progress.phase('保存迁移恢复记录', len(proposal['moves']))
            with ledger.db:
                for index, move in enumerate(proposal['moves'], 1):
                    identifier = hashlib.sha256(json.dumps(move, sort_keys=True).encode()).hexdigest()
                    ledger.db.execute('INSERT OR IGNORE INTO layout_moves VALUES(?,?,?)',
                                      (identifier, json.dumps(move, ensure_ascii=False), 'pending'))
                    if progress:
                        progress.update(done=index, current=move['source'])
        jobs = ledger.all("SELECT * FROM layout_moves WHERE state!='done'")
    if progress:
        progress.message(f'本次待处理 {len(jobs):,} 个文件；先校验并暂存整批文件，再发布到目标目录。')
        progress.phase('校验源文件并暂存', len(jobs))
    for index, job in enumerate(jobs, 1):
        if cancelled():
            raise LayoutError('迁移已停止，下次可继续。')
        move = json.loads(job['payload'])
        if progress:
            progress.update(done=index-1, current=move['source'])
        source = confined(base, move['source'])
        staging = confined(base, 'runtime/path_migration/' + job['id'] + '/' + Path(move['source']).name)
        if job['state'] == 'pending':
            if not staging.exists():
                if source.stat().st_size != move['bytes']:
                    raise LayoutError('源文件大小与台账不符：' + move['source'])
                actual = checksum(source, cancelled, progress)
                if move['sha256'] and actual != move['sha256']:
                    raise LayoutError('源文件SHA256与台账不符：' + move['source'])
                move['sha256'] = actual
                # Persist checksum before the first rename, including untracked files.
                with ledger.db:
                    ledger.db.execute('UPDATE layout_moves SET payload=? WHERE id=?',
                                      (json.dumps(move, ensure_ascii=False), job['id']))
                staging.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, staging)
            with ledger.db:
                ledger.db.execute("UPDATE layout_moves SET state='staged' WHERE id=?", (job['id'],))
        if progress:
            progress.update(done=index)
    staged_jobs = ledger.all("SELECT * FROM layout_moves WHERE state='staged'")
    if progress:
        progress.update(done=len(jobs), force=True)
        progress.phase('校验暂存文件、发布并更新台账', len(staged_jobs))
    for index, job in enumerate(staged_jobs, 1):
        if cancelled():
            raise LayoutError('迁移已停止，下次可继续。')
        move = json.loads(job['payload'])
        if progress:
            progress.update(done=index-1, current=move['target'])
        staging = confined(base, 'runtime/path_migration/' + job['id'] + '/' + Path(move['source']).name)
        target = confined(base, move['target'])
        if target.exists():
            # Recovery after publishing, before committing references.
            if target.stat().st_size != move['bytes'] or checksum(target, cancelled, progress) != move['sha256']:
                raise LayoutError('目标内容冲突，保留迁移现场：' + move['target'])
            if staging.exists():
                if checksum(staging, cancelled, progress) != move['sha256']:
                    raise LayoutError('暂存内容不一致，保留源文件：' + move['target'])
                staging.unlink()
        else:
            if staging.stat().st_size != move['bytes'] or checksum(staging, cancelled, progress) != move['sha256']:
                raise LayoutError('迁移暂存文件校验失败：' + move['source'])
            publish(staging, target, move['sha256'], cancelled, progress)
        with ledger.db:
            for ref in move['refs']:
                if ref[0] == 'files':
                    ledger.db.execute('UPDATE files SET path=?,target_path=CASE WHEN active=1 THEN ? ELSE NULL END WHERE project=? AND id=?',
                                      (move['target'], move['target'], ref[1], ref[2]))
                elif ref[0] == 'file_history':
                    ledger.db.execute('UPDATE file_history SET path=? WHERE id=?', (move['target'], ref[1]))
            if move.get('platform_path') and move['target'].startswith('history/'):
                ledger.db.execute('CREATE TABLE IF NOT EXISTS history_paths(path TEXT PRIMARY KEY,platform_path TEXT,original_path TEXT)')
                ledger.db.execute('INSERT OR REPLACE INTO history_paths VALUES(?,?,?)',
                                  (move['target'], move['platform_path'], move['source']))
            ledger.db.execute("UPDATE layout_moves SET state='done' WHERE id=?", (job['id'],))
            tables = {r[0] for r in ledger.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'report_local_versions' in tables:
                ledger.db.execute('UPDATE report_local_versions SET path=? WHERE path=?', (move['target'], move['source']))
            if 'report_local_paths' in tables:
                ledger.db.execute('INSERT OR IGNORE INTO report_local_paths SELECT ?,first_day,size FROM report_local_paths WHERE path=?',
                                  (move['target'].casefold(), move['source'].casefold()))
        log('路径已迁移：' + move['source'] + ' -> ' + move['target'])
        if progress:
            progress.update(done=index)
    if progress:
        progress.update(done=len(staged_jobs), force=True)
    # Recovery finishes the recorded batch and its references. It must never
    # discover and begin a new batch of unrelated file moves.
    final_plan = (dict(mapping=[dict(project=key[0], file_id=key[1], **value)
                               for key, value in targets(ledger, progress=progress).items()], missing=[]) if recover_only
                  else proposal or plan(ledger, untracked=False, progress=progress))
    mapping = final_plan['mapping']
    # Missing predecessors must not later refer to newly downloaded bytes at the
    # same filename. Keep their hashes/version records, but clear absent paths.
    with ledger.db:
        for source in final_plan['missing']:
            if not confined(base, source).exists():
                ledger.db.execute('UPDATE file_history SET path=NULL WHERE path=?', (source,))
                ledger.db.execute('UPDATE files SET path=NULL WHERE active=0 AND path=?', (source,))
    if progress:
        progress.phase('更新原名和本地路径映射', len(mapping))
    with ledger.db:
        for index, entry in enumerate(mapping, 1):
            ledger.db.execute('''INSERT OR REPLACE INTO local_paths
              (project,file_id,platform_path,local_path,omitted_parent,original_filename,local_directory,local_filename)
              VALUES(?,?,?,?,?,?,?,?)''',
                (entry['project'], entry['file_id'], entry['platform_path'], entry['path'], entry['omitted_parent'],
                 entry['original_filename'], Path(entry['path']).parent.as_posix(), Path(entry['path']).name))
            ledger.db.execute('UPDATE files SET target_path=? WHERE project=? AND id=? AND active=1',
                              (entry['path'], entry['project'], entry['file_id']))
            if progress:
                progress.update(done=index, current=entry['path'])
    queue_path = base / 'runtime/file_analysis/queue.sqlite3'
    if progress:
        progress.update(done=len(mapping), force=True)
        progress.phase('更新解析队列引用')
    if queue_path.exists():
        with closing(sqlite3.connect(queue_path)) as queue:
            if queue.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone():
                with queue:
                    queue.execute('CREATE INDEX IF NOT EXISTS jobs_path_migration ON jobs(path)')
                    completed_jobs = ([dict(payload=job['payload']) for job in jobs] if incremental else
                                      ledger.all("SELECT payload FROM layout_moves WHERE state='done'"))
                    for index, job in enumerate(completed_jobs, 1):
                        move = json.loads(job['payload'])
                        queue.execute('UPDATE jobs SET path=replace(path,?,?),origin=replace(origin,?,?) WHERE path=?',
                            (str(base/move['source']), str(base/move['target']),
                             str(base/move['source']), str(base/move['target']), str(base/move['source'])))
                        if progress:
                            progress.update(done=index, total=len(completed_jobs), current=move['target'])
    temporary = runtime / 'path_mapping.json.tmp'
    if progress:
        progress.phase('写入路径映射文件')
    if not incremental:
        temporary.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, runtime / 'path_mapping.json')
    # Preserve unknown files in history; remove only empty old directories.
    if progress:
        progress.phase('清理空的旧目录')
    inspected = 0
    for parent, dirs, names in (() if incremental else os.walk(base / 'downloads', topdown=False, followlinks=False)):
        if progress:
            progress.update(done=inspected, current=parent)
        for name in dirs:
            inspected += 1
            try:
                (Path(parent) / name).rmdir()
            except OSError:
                pass
    pending.unlink(missing_ok=True)
    if progress:
        progress.phase('迁移完成', len(jobs))
        progress.update(done=len(jobs), force=True)
    return len(jobs)


class ReadLedger:
    def __init__(self, db, base):
        self.db, self.base_dir = db, base
    def one(self, sql, args=()):
        return self.db.execute(sql, args).fetchone()
    def all(self, sql, args=()):
        return self.db.execute(sql, args).fetchall()


def pause_analysis_for_migration(base):
    """Own only the stop we create; an existing/manual stop stays untouched."""
    import uuid
    base = Path(base)
    stop = base / 'runtime/file_analysis/stop.request'
    if stop.exists():
        return
    token = 'path migration: ' + uuid.uuid4().hex
    owner = base / 'runtime/layout.analysis_pause.json'
    temporary = owner.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(dict(token=token)), encoding='utf-8')
    os.replace(temporary, owner)
    stop.parent.mkdir(parents=True, exist_ok=True)
    stop.write_text(token, encoding='utf-8')


def resume_analysis_after_migration(base):
    base = Path(base)
    owner = base / 'runtime/layout.analysis_pause.json'
    if not owner.exists() or (base / 'runtime/scheduler.exit.request').exists():
        return
    stop = base / 'runtime/file_analysis/stop.request'
    token = json.loads(owner.read_text(encoding='utf-8'))['token']
    if stop.exists() and stop.read_text(encoding='utf-8') == token:
        stop.unlink()
    owner.unlink(missing_ok=True)


def prepare_downloads(ledger, cancelled=lambda: False, *, keys=None):
    """Stop parsers before moving files; never begin another heavy task on the timer thread."""
    from analysis_worker import worker_lock
    base = ledger.base_dir
    marker = base / 'runtime/layout.pending'
    marker.parent.mkdir(exist_ok=True)
    # Resume the journal before inspecting old references, which may point to staged files.
    # A prior interrupted batch always uses the full recovery path.
    if marker.exists():
        keys = None
    proposal = None if marker.exists() else plan(ledger, untracked=False, keys=keys)
    if marker.exists() or proposal['moves']:
        marker.write_text('path migration in progress', encoding='utf-8')
        pause_analysis_for_migration(base)
        with ExitStack() as stack:
            for folder in (base/'runtime/file_analysis', base/'runtime/wechat_delivery'):
                announced = False
                while True:
                    if cancelled():
                        raise LayoutError('迁移已停止，下次可继续。')
                    guard = worker_lock(folder)
                    if guard.__enter__():
                        stack.callback(guard.__exit__, None, None, None)
                        break
                    guard.__exit__(None, None, None)
                    if not announced:
                        print('等待当前后台任务释放文件后迁移：' + str(folder), flush=True)
                        announced = True
                    time.sleep(.2)
            apply(ledger, proposal, cancelled=cancelled, incremental=keys is not None)
        resume_analysis_after_migration(base)
    else:
        apply(ledger, proposal, cancelled=cancelled, incremental=keys is not None)
    if keys is None:
        ledger._download_targets = targets(ledger)


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线路径迁移；默认只生成预览，不移动文件。')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--base-dir', type=Path, default=HOME)
    args = parser.parse_args(argv)
    base = args.base_dir.resolve()
    database = base / 'runtime/ledger.sqlite3'
    if not database.exists():
        print('无下载台账，无需迁移。')
        return 0
    from analysis_worker import worker_lock
    with ExitStack() as stack:
        progress = stack.enter_context(MigrationProgress(base))
        progress.phase('检查任务占用并打开台账')
        progress.update(current=str(database), force=True)
        if args.apply:
            for folder, name in ((base/'runtime', 'scheduler.lock'), (base/'runtime', 'sync.lock'),
                                 (base/'runtime/file_analysis', 'worker.lock'),
                                 (base/'runtime/wechat_delivery', 'worker.lock')):
                if not stack.enter_context(worker_lock(folder, name)):
                    raise LayoutError('请先停止任务再迁移：' + str(folder / name))
        db = stack.enter_context(closing(sqlite3.connect(database if args.apply else database.as_uri()+'?mode=ro',
                                                        uri=not args.apply)))
        db.row_factory = sqlite3.Row
        ledger = ReadLedger(db, base)
        if args.apply:
            count = apply(ledger, progress=progress, log=progress.message)
            print(f'迁移完成：{count} 个文件。')
        else:
            proposal = plan(ledger, progress=progress)
            output = base / 'runtime/path_migration_preview.json'
            progress.phase('写入迁移预览')
            output.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding='utf-8')
            progress.phase('预览完成')
            print(f'预览：{len(proposal["moves"])} 个文件移动；{len(proposal["missing"])} 个源文件缺失；{len(proposal["mapping"])} 个当前路径。')
            print(str(output))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (LayoutError, OSError, sqlite3.Error) as exc:
        print('路径迁移未完成：' + str(exc), file=sys.stderr)
        sys.exit(2)
