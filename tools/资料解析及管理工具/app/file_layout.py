"""Platform-named current files, short hierarchical overflow paths and recoverable moves."""
from paths import HOME
from collections import defaultdict, OrderedDict
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
PREDECESSOR_POLICY = 'associated-member-v4'


class LayoutError(RuntimeError):
    def __init__(self, message, *, paths=()):
        super().__init__(message)
        self.paths = tuple(paths)


class LayoutPlanningError(LayoutError):
    """A read-only planning failure; unrelated dependency groups can proceed."""
    def __init__(self, message, mapping, *, paths=()):
        super().__init__(message, paths=paths)
        self.mapping = mapping


class LayoutTargetConflict(LayoutPlanningError):
    """A planning conflict before any journal or file mutation."""
    def __init__(self, target, mapping):
        super().__init__('迁移目标已有文件，未覆盖：' + target, mapping, paths=(target,))


def download_structure_token(ledger):
    """Track catalogue changes in connection-local state, without durable audit writes.

    Download counters and publishing to already allocated paths do not change the
    dependency graph. Other connections are conservatively detected by data_version.
    """
    db = ledger.db
    if not getattr(ledger, '_download_revision_ready', False):
        db.execute('CREATE TEMP TABLE download_structure_revision AS SELECT 0 AS value')
        watched = {
            'files': ('project', 'id', 'document_id', 'folder_id', 'filename', 'relation',
                      'active', 'fingerprint', 'metadata', 'size'),
            'documents': ('project', 'id', 'folder_id', 'first_id', 'reference', 'filename', 'title', 'active'),
            'folders': ('project', 'id', 'path', 'active'),
            'document_files': ('project', 'document_id', 'folder_id', 'file_id', 'active'),
            'file_history': ('project', 'file_id', 'fingerprint', 'bytes', 'sha256'),
            'refresh_cycles': ('project', 'cycle', 'status'),
            'workflows': ('project', 'stage', 'phase', 'cycle'),
        }
        for table, requested in watched.items():
            columns = {r[1] for r in db.execute(f'PRAGMA main.table_info({table})')}
            fields = [name for name in requested if name in columns]
            if not fields:
                continue
            for event in ('INSERT', 'DELETE'):
                db.execute(f'''CREATE TEMP TRIGGER download_revision_{table}_{event}
                  AFTER {event} ON main.{table} BEGIN
                  UPDATE download_structure_revision SET value=value+1; END''')
            condition = ' OR '.join(f'OLD.{name} IS NOT NEW.{name}' for name in fields)
            db.execute(f'''CREATE TEMP TRIGGER download_revision_{table}_UPDATE
              AFTER UPDATE OF {','.join(fields)} ON main.{table} WHEN {condition} BEGIN
              UPDATE download_structure_revision SET value=value+1; END''')
        ledger._download_revision_ready = True
    return (db.execute('SELECT value FROM download_structure_revision').fetchone()[0],
            db.execute('PRAGMA main.data_version').fetchone()[0])


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


def same_member(old, new, mapping):
    """A package association alone never proves two files are versions."""
    if old.get('relation') and new.get('relation') and old['relation'] != new['relation']:
        return False
    return (member_name(old['filename']) == member_name(new['filename'])
            or (old['id'] == old['document_id'] and new['id'] == new['document_id'])
            or bool(old.get('path') and old['path'].replace('\\', '/').casefold() ==
                    mapping[(new['project'], new['id'])]['path'].replace('\\', '/').casefold()))


def member_name(filename):
    """Only strip the revision of a named Checkprint, never arbitrary digits."""
    name = str(filename).casefold()
    return re.sub(r'[_ -]+\d+[_ -]+(?=checkprint\b)', '_', name)


def replacement_links(ledger, files, mapping, documents=None, *, conservative=True):
    """Match inactive versions to current files, including changed platform IDs."""
    if documents is None:
        documents = {(r['project'], r['id'], r['folder_id']): dict(r)
                     for r in ledger.all('SELECT * FROM documents')}
    # files.document_id is the first parent observed, often a historical package.
    # Consult every association so a package cannot hide the file's own lineage.
    contexts = defaultdict(set)
    main = set()
    keys = {(r['project'],r['id']) for r in files}
    if documents is not None:
        associations = ledger.all('SELECT project,document_id,folder_id,file_id FROM document_files')
        for assoc in associations:
            key = (assoc['project'],assoc['file_id'])
            if key not in keys:
                continue
            doc_key = (assoc['project'],assoc['document_id'],assoc['folder_id'])
            document = documents.get(doc_key)
            if document is None:
                row = ledger.one('SELECT * FROM documents WHERE project=? AND id=? AND folder_id=?',doc_key)
                if row:
                    document = documents[doc_key] = dict(row)
            if document:
                contexts[key].add(doc_key)
                if assoc['file_id'] == assoc['document_id']:
                    main.add(key)
    def families(row):
        key = (row['project'],row['id'])
        doc_keys = contexts[key] | {(row['project'],row['document_id'],row['folder_id'])}
        result = set()
        for doc_key in doc_keys:
            document = documents.get(doc_key,{})
            first = document.get('first_id')
            reference = document.get('reference')
            if first:
                result.add(('lineage',str(first)))
            elif reference:
                result.add(('reference',reference))
            result.add(('document',doc_key[1]))
        return result
    family_keys = {(r['project'],r['id']):families(r) for r in files}
    own_families = defaultdict(set)
    for (project,identifier,_),document in documents.items():
        if document.get('first_id'):
            own_families[(project,identifier)].add(('lineage',str(document['first_id'])))
        elif document.get('reference'):
            own_families[(project,identifier)].add(('reference',document['reference']))
    by_name, by_family, by_main, by_path = (defaultdict(list) for _ in range(4))
    def is_main(row):
        key = (row['project'], row['id'])
        # A document can also be attached to a package. Its last API context
        # must not turn its own document identity into an attachment member.
        return key in main or key in own_families or row['id'] == row['document_id']

    for row in files:
        if row['active']:
            key = (row['project'],row['id'])
            by_name[(row['project'],member_name(row['filename']))].append(row)
            for family in family_keys[key]:
                by_family[(row['project'],family)].append(row)
            if is_main(row):
                for family in own_families[key]:
                    by_main[(row['project'],family)].append(row)
            target = mapping[key]['path'].replace('\\','/').casefold()
            if target:
                by_path[(row['project'],target)].append(row)
    links = {}
    for old in files:
        if old['active']:
            continue
        matches, family = [], []
        old_key = (old['project'],old['id'])
        old_families = family_keys[old_key]
        own_main = is_main(old)
        candidates = list(by_name[(old['project'],member_name(old['filename']))])
        if old['path']:
            candidates.extend(by_path[(old['project'],old['path'].replace('\\','/').casefold())])
        if own_main:
            for token in own_families[old_key]:
                candidates.extend(by_main[(old['project'],token)])
        for token in old_families:
            if conservative:
                family.extend(by_family[(old['project'],token)])
        for new in {r['id']: r for r in candidates}.values():
            # A document and its attachment can share a filename. They are not
            # interchangeable predecessor versions.
            new_key = (new['project'],new['id'])
            # Own-document lineage survives changing package attachment labels.
            new_main = is_main(new)
            if own_main != new_main:
                continue
            both_main = own_main and new_main
            common = old_families & family_keys[new_key]
            # Two main documents in one package are still different documents.
            own_lineage = both_main and bool(own_families[old_key] & own_families[new_key])
            if old['relation'] and new['relation'] and old['relation'] != new['relation'] and not own_lineage:
                continue
            member = member_name(old['filename']) == member_name(new['filename'])
            same_path = old['path'] and old['path'].casefold() == mapping[(new['project'], new['id'])]['path'].casefold()
            if (common and member) or own_lineage or same_path:
                matches.append(new)
        # Renamed attachments may have no filename match. Keep the predecessor
        # until every possible successor in its document family is downloaded.
        family = [r for r in {r['id']:r for r in family}.values()
                  if is_main(old) == is_main(r) and
                  (not old.get('relation') or not r.get('relation') or old['relation']==r['relation'])]
        links[(old['project'], old['id'])] = matches or (family if conservative else [])
    return links


def local_update_keys(ledger, *, reuse=False):
    """Existing same-member bytes in downloads/history make a task an update."""
    token = download_structure_token(ledger) if reuse else None
    if reuse and getattr(ledger, '_download_updates_token', None) == token:
        return set(ledger._download_updates)
    files = [dict(r) for r in ledger.all('SELECT * FROM files')]
    from inventory import source_file_identity
    source_members = defaultdict(set)
    for row in files:
        identity = source_file_identity(row)
        if identity:
            source_members[identity].add((row['project'],row['id']))
    ledger._download_source_members = source_members
    mapping = getattr(ledger, '_download_targets', None) or targets(ledger)
    links = replacement_links(ledger, files, mapping)
    version_links = replacement_links(ledger, files, mapping, conservative=False)
    predecessors = defaultdict(set)
    for key, versions in version_links.items():
        for row in versions:
            predecessors[(row['project'],row['id'])].add(key)
    ledger._download_predecessors = predecessors
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
        return path and path.replace('\\', '/').startswith(('downloads/', 'history/')) and confined(ledger.base_dir, path).is_file()
    for row in histories:
        if present(row['path']):
            key = (row['project'], row['file_id'])
            result.add(key)
            # Reconciliation may already have archived the inactive files row,
            # while its newest history still represents the pending successor.
            # That successor must use guarded publication, even at a new path.
            result.update((r['project'], r['id']) for r in version_links.get(key, ()))
    for row in files:
        if present(row['path']):
            key = (row['project'],row['id'])
            if row['active']:
                result.add(key)
            result.update((r['project'], r['id']) for r in version_links.get(key, ()))
    if reuse:
        ledger._download_updates_token = token
        ledger._download_updates = frozenset(result)
    return result


def download_directory_copy(ledger, item, target):
    """Observe an actual same-file copy in downloads immediately before HTTP."""
    candidates = [relative_path(target,ledger.base_dir),item.get('path')]
    key = (item['project'],item['id'])
    keys = {key, *getattr(ledger,'_download_predecessors',{}).get(key,())}
    from inventory import source_file_identity
    identity = source_file_identity(item)
    if identity:
        members = getattr(ledger, '_download_source_members', None)
        if members is not None:
            keys.update(members.get(identity, ()))
        else:
            keys.update((r['project'],r['id']) for r in ledger.all('SELECT * FROM files WHERE project=?',(item['project'],))
                        if source_file_identity(r)==identity)
    for project,identifier in sorted(keys):
        candidates.extend(r['path'] for r in ledger.all('SELECT path FROM files WHERE project=? AND id=?',(project,identifier)))
        candidates.extend(r['path'] for r in ledger.all('SELECT path FROM file_history WHERE project=? AND file_id=? AND path IS NOT NULL',(project,identifier)))
    for value in dict.fromkeys(candidates):
        if not value:
            continue
        path = relative_path(value,ledger.base_dir)
        if path.startswith('downloads/') and confined(ledger.base_dir,path).is_file():
            return path
    return None


def retained_predecessors(ledger, files, histories, current, replacements, mapping, downloaded):
    """Newest available historical bytes for each not-yet-downloaded current ID."""
    candidates = defaultdict(list)
    by_key = {(r['project'],r['id']): r for r in files}
    for row, identifier, rank in [
            *((r, r['file_id'], (r['archived'] or '', int(r['id']))) for r in histories),
            *((r, r['id'], (r.get('updated') or r.get('downloaded_at') or '', 0))
              for r in files if not r['active'] and r['status']=='done')]:
        path = row['path']
        if not path or not row['sha256']:
            continue
        relative = relative_path(path,ledger.base_dir)
        if not relative.startswith(('downloads/', 'history/')):
            continue
        source = confined(ledger.base_dir,relative)
        if not source.is_file() or source.stat().st_size != row['bytes']:
            continue
        key = (row['project'],identifier)
        successors = [current[key]] if key in current else replacements.get(key, ())
        for successor in successors:
            if key not in current:
                old = by_key.get(key)
                if old is None:
                    continue
            if not downloaded(successor):
                candidates[(successor['project'],successor['id'])].append(
                    dict(row=row, path=relative, bytes=row['bytes'], sha256=row['sha256'], rank=rank))
    keepers = {key:max(values,key=lambda value:value['rank']) for key,values in candidates.items()}
    refs = defaultdict(list)
    for row in histories:
        if row['path']:
            refs[relative_path(row['path'],ledger.base_dir).casefold()].append(['file_history',row['id']])
    for row in files:
        if row['path']:
            refs[relative_path(row['path'],ledger.base_dir).casefold()].append(['files',row['project'],row['id']])
    return keepers, refs


def plan(ledger, *, untracked=True, progress=None, keys=None, reconcile=False):
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
    replacements = replacement_links(ledger, files, mapping, documents, conservative=not reconcile)
    def downloaded(row):
        return (row and row['status'] == 'done' and row['path'] and row['sha256'] and
                confined(ledger.base_dir, row['path']).is_file() and
                confined(ledger.base_dir, row['path']).stat().st_size == row['bytes'])
    incomplete_projects = {row['project'] for row in files if row['active'] and not downloaded(row)}
    moves, missing, known, used = [], [], set(), {}
    shared_sources = defaultdict(list)
    stationary_sources = set()
    requirements = defaultdict(dict)
    deferred_projects = set()
    if ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='workflows'"):
        deferred_projects = {row[0] for row in ledger.all('''SELECT w.project FROM workflows w
            JOIN refresh_cycles c ON c.project=w.project WHERE c.status!='complete'
            AND w.phase IN ('catalogue','associations')''')}
    closed_projects = {row['project'] for row in ledger.all("SELECT project FROM refresh_cycles WHERE status='complete'")}
    keepers, keeper_refs = ({}, {})
    if reconcile:
        keepers, keeper_refs = retained_predecessors(ledger,files,histories,current,replacements,mapping,downloaded)
    keeper_paths = {value['path'].casefold() for value in keepers.values()}
    local_requirements = defaultdict(dict)

    def keep_old(source, successors, project):
        if not reconcile or project not in closed_projects:
            return ((successors and not all(downloaded(row) for row in successors))
                    or (not successors and project in incomplete_projects))
        if source.casefold() in keeper_paths:
            return True
        pending = [(row['project'],row['id']) for row in successors if not downloaded(row)]
        if any(key not in keepers for key in pending):
            return True
        for key in pending:
            keeper = keepers[key]
            local_requirements[source.casefold()][keeper['path']] = dict(
                path=keeper['path'],bytes=keeper['bytes'],sha256=keeper['sha256'])
        return False
    active_sources = defaultdict(list)
    if progress:
        progress.phase('检查当前文件引用', len(files))
    for index, row in enumerate(files, 1):
        if progress:
            progress.update(done=index-1, current=row['path'] or row['filename'])
        if row['active'] and row['path']:
            active_sources[relative_path(row['path'], ledger.base_dir).casefold()].append(row)
    def add(source, destination, ref, row, *, copy=False):
        source = relative_path(source, ledger.base_dir)
        known.add(source.casefold())
        shared_sources[source.casefold()].append(row)
        if source == destination:
            stationary_sources.add(source.casefold())
            return
        path = confined(ledger.base_dir, source)
        if not path.is_file():
            missing.append(source)
            return
        identity = (source.casefold(), destination.casefold())
        if identity in used:
            move = used[identity]
            move['refs'].append(ref)
            return
        move = dict(source=source, target=destination, refs=[ref],
                    bytes=row.get('bytes') if row.get('bytes') is not None else path.stat().st_size,
                    sha256=row.get('sha256'))
        if copy:
            move['copy'] = True
        used[identity] = move
        moves.append(move)
    if progress:
        progress.update(done=len(files), force=True)
        progress.phase('扫描历史版本文件', len(histories))
    for index, row in enumerate(histories, 1):
        if row['project'] in deferred_projects:
            if row['path']:
                known.add(relative_path(row['path'], ledger.base_dir).casefold())
            continue
        if progress:
            progress.update(done=index-1, current=row['path'] or '无本地路径')
        if row['path'] and not row['path'].startswith('history/'):
            source = relative_path(row['path'], ledger.base_dir)
            replacement = current.get((row['project'], row['file_id']))
            successors = [replacement] if replacement else replacements.get((row['project'], row['file_id']), [])
            if keep_old(source,successors,row['project']):
                known.add(source.casefold())
                if not confined(ledger.base_dir, source).is_file():
                    missing.append(source)
                continue
            owners = active_sources.get(source.casefold(), [])
            for successor in successors:
                if downloaded(successor):
                    requirements[source.casefold()][(successor['project'], successor['id'])] = successor
            copy = bool(owners)
            if owners and (not row['sha256'] or any(
                    owner['sha256'] != row['sha256'] or owner['bytes'] != row['bytes']
                    or owner['status'] != 'done' for owner in owners)):
                raise LayoutError('历史与当前版本引用同一路径且内容身份不一致，保留原文件，需核对：' + source, paths=(source,))
            add(source, history_path(source, 'version-' + str(row['id'])),
                ['file_history', row['id']], row, copy=copy)
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
                if source.startswith('runtime/download_ready/'):
                    if row['project'] in deferred_projects:
                        # Streaming inventory can verify a new version before
                        # all predecessors are known. Keep its verified bytes
                        # staged until the metadata round can publish safely.
                        known.add(source.casefold())
                        continue
                destination = mapping[(row['project'], row['id'])]['path']
            else:
                successors = replacements.get((row['project'], row['id']), [])
                if keep_old(source,successors,row['project']):
                    known.add(source.casefold())
                    continue
                # An incomplete inventory is not evidence of platform removal.
                cycle = ledger.one('SELECT status FROM refresh_cycles WHERE project=?', (row['project'],))
                if not cycle or cycle['status'] != 'complete':
                    known.add(source.casefold())
                    continue
                for successor in successors:
                    if downloaded(successor):
                        requirements[source.casefold()][(successor['project'], successor['id'])] = successor
                token = hashlib.sha256((row['project'] + '\0' + row['id']).encode()).hexdigest()[:16]
                destination = source if source.startswith('history/') else history_path(source, 'inactive-' + token)
            add(source, destination, ['files', row['project'], row['id']], row)
    # A retained history alias must follow its verified physical file when the
    # current row moves. Explicit archive/copy jobs keep their own destinations.
    assigned = {tuple(ref) for move in moves for ref in move['refs']}
    aliases = defaultdict(list)
    for row in histories:
        if row['path'] and row['sha256']:
            aliases[relative_path(row['path'], ledger.base_dir).casefold()].append(row)
    for move in moves:
        if move.get('copy'):
            continue
        for row in aliases[move['source'].casefold()]:
            ref = ('file_history', row['id'])
            if ref not in assigned and row['sha256'] == move['sha256'] and row['bytes'] == move['bytes']:
                move['refs'].append(list(ref))
                assigned.add(ref)
    target_owners = defaultdict(set)
    for owner, entry in mapping.items():
        target_owners[entry['path'].casefold()].add(owner)
    restored = set()
    for key, keeper in sorted(keepers.items()):
        source = keeper['path']
        if (key[0] not in closed_projects or source == mapping[key]['path']
                or (not source.startswith('history/') and
                    not (target_owners[source.casefold()] - {key}))
                or source.casefold() in restored):
            continue
        restored.add(source.casefold())
        for ref in keeper_refs[source.casefold()]:
            add(source,mapping[key]['path'],ref,keeper['row'])
    # Version/association aliases may legitimately share verified bytes. Stage
    # independent copies before the last move, retaining any stationary owner.
    # Different content claims still need disk reconciliation, never a blind copy.
    source_moves = defaultdict(list)
    for move in moves:
        source_moves[move['source'].casefold()].append(move)
    for source, jobs in source_moves.items():
        claims = shared_sources[source]
        if len(claims) < 2:
            continue
        digest, size = claims[0].get('sha256'), claims[0].get('bytes')
        if not digest or size is None or any(
                row.get('sha256') != digest or row.get('bytes') != size for row in claims):
            raise LayoutError('同一物理文件的版本内容身份不一致，请先核对：' + jobs[0]['source'],
                              paths=(jobs[0]['source'],))
        moving = [job for job in jobs if not job.get('copy')]
        for job in moving if source in stationary_sources else moving[:-1]:
            job['copy'] = True
    moves.sort(key=lambda move: not move.get('copy', False))
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
    # Use the same destination planner as manual maintenance, but inspect only
    # this batch's archives. Never run a full historical-tree repair per download.
    archives = {m['target']:m['source'] for m in moves if m['target'].startswith('history/')
                and not m['source'].startswith('history/')}
    if archives:
        from history_repair import plan as history_plan
        shared = {m['source']:m for m in history_plan(
            ledger, extra_sources=archives, scan_existing=False)['moves']}
        for move in moves:
            if move['target'] in shared:
                final = shared[move['target']]
                move.update(target=final['target'],platform_path=final['platform_path'])
    sources = {m['source'].casefold() for m in moves if not m.get('copy')}
    destinations = set()
    if progress:
        progress.phase('检查迁移目标与覆盖冲突', len(moves))
    for index, move in enumerate(moves, 1):
        if reconcile:
            move['reconciled'] = True
            move['predecessor_policy'] = PREDECESSOR_POLICY
        if local_requirements.get(move['source'].casefold()):
            move['requires_local'] = list(local_requirements[move['source'].casefold()].values())
        if move['target'].startswith('history/') and requirements.get(move['source'].casefold()):
            move['requires'] = [dict(project=r['project'], file_id=r['id'], sha256=r['sha256'],
                bytes=r['bytes'], fingerprint=r.get('fingerprint'))
                for _,r in sorted(requirements[move['source'].casefold()].items())]
        if progress:
            progress.update(done=index-1, current=move['target'])
        dest = move['target'].casefold()
        if dest in destinations:
            raise LayoutError('迁移目标重名：' + move['target'])
        destinations.add(dest)
        target = confined(ledger.base_dir, move['target'])
        if target.exists() and dest not in sources:
            raise LayoutTargetConflict(move['target'], mapping)
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


class PublicationChecks:
    """Reuse proof only within one publication, with bounded open read guards."""
    def __init__(self, cancelled, progress, limit=32):
        self.cancelled, self.progress, self.limit = cancelled, progress, limit
        self.proofs = OrderedDict()

    @staticmethod
    def stamp(status):
        return (status.st_dev, status.st_ino, status.st_size, status.st_mtime_ns, status.st_ctime_ns)

    @staticmethod
    def guard(path):
        if os.name != 'nt':
            return path.open('rb')
        # Deny content writes, allow our own same-volume renames and hardlinks.
        import ctypes
        from ctypes import wintypes
        import msvcrt
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                           wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        handle = create(str(path), 0x80000000, 0x1 | 0x4, None, 3, 0x08000000, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
        except BaseException:
            kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel.CloseHandle(handle)
            raise
        return os.fdopen(descriptor, 'rb')

    def forget(self, path):
        proof = self.proofs.pop(str(path), None)
        if proof:
            proof[2].close()

    def checksum(self, path):
        if self.cancelled():
            raise LayoutError('迁移已停止')
        key, before = str(path), path.stat()
        proof = self.proofs.get(key)
        if proof and self.stamp(before) == proof[0] and self.stamp(os.fstat(proof[2].fileno())) == proof[3]:
            self.proofs.move_to_end(key)
            return proof[1]
        self.forget(path)
        try:
            guard = self.guard(path)
        except OSError:
            # Existing writers or unsupported volumes retain the full verification path.
            return checksum(path, self.cancelled, self.progress)
        try:
            held = os.fstat(guard.fileno())
            # Windows stat() and fstat() can report different ctime semantics.
            width = 4 if os.name == 'nt' else 5
            if self.stamp(held)[:width] != self.stamp(before)[:width]:
                raise LayoutError('校验前文件身份已变化：' + str(path))
            value = checksum(path, self.cancelled, self.progress)
            after = path.stat()
            held_after = os.fstat(guard.fileno())
            if self.stamp(after) != self.stamp(before) or self.stamp(held_after) != self.stamp(held):
                raise LayoutError('校验期间文件发生变化：' + str(path))
            self.proofs[key] = (self.stamp(after), value, guard, self.stamp(held_after))
            guard = None
            while len(self.proofs) > self.limit:
                _, old = self.proofs.popitem(last=False)
                old[2].close()
            return value
        finally:
            if guard is not None:
                guard.close()

    def rename(self, source, target):
        before = source.stat()
        proof = self.proofs.pop(str(source), None)
        try:
            os.rename(source, target)
            after = target.stat()
            same = ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) ==
                    (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns))
            if (proof and before.st_ino and proof[0] == self.stamp(before) and same
                    and (os.name == 'nt' or before.st_ctime_ns == after.st_ctime_ns)):
                self.proofs[str(target)] = (self.stamp(after), proof[1], proof[2],
                                            self.stamp(os.fstat(proof[2].fileno())))
                proof = None
        finally:
            if proof:
                proof[2].close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        for _, _, guard, _ in self.proofs.values():
            guard.close()
        self.proofs.clear()


def apply(ledger, proposal=None, *, cancelled=lambda: False, log=print, progress=None, incremental=False,
          recover_only=False):
    with PublicationChecks(cancelled, progress) as checks:
        return _apply(ledger, proposal, cancelled=cancelled, log=log, progress=progress,
                      incremental=incremental, recover_only=recover_only, checks=checks)


def _apply(ledger, proposal=None, *, cancelled, log, progress, incremental,
           recover_only, checks):
    """Journal before moving; resume staging/publishing after any interruption."""
    base = ledger.base_dir
    if not incremental:
        ledger._download_layout_token = None
        ledger._download_reconciled_token = None
        ledger._download_updates_token = None
        ledger._download_priority_cache = None
    runtime = base / 'runtime'
    runtime.mkdir(exist_ok=True)
    pending = runtime / 'layout.pending'
    pending.write_text('Do not start downloads or analysis until migration completes.', encoding='utf-8')
    if progress:
        progress.phase('检查数据库与迁移恢复记录')
    ensure_schema(ledger.db)
    jobs = ledger.all("SELECT * FROM layout_moves WHERE state!='done'")
    unsafe = [json.loads(job['payload']) for job in jobs if
              (move := json.loads(job['payload']))['source'].startswith('downloads/')
              and move['target'].startswith('history/') and not move.get('copy')
              and (move.get('reconciled') or move.get('requires_local'))
              and move.get('predecessor_policy') != PREDECESSOR_POLICY]
    if unsafe:
        if not all(job['state']=='pending' and confined(base,(move := json.loads(job['payload']))['source']).is_file()
                   and not confined(base,'runtime/path_migration/'+job['id']+'/'+Path(move['source']).name).exists()
                   for job in jobs):
            raise LayoutError('旧版归档计划已部分移动文件，已阻止继续执行；请保留runtime/path_migration和台账备份，核对暂存文件后恢复，勿删除迁移标记。')
        backup = runtime/'backups'/('before-legacy-replan-'+now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
        backup.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(backup)) as target:
            ledger.db.backup(target)
        backup.with_suffix('.json').write_text(json.dumps([dict(job) for job in jobs],ensure_ascii=False,indent=2),encoding='utf-8')
        with ledger.db:
            ledger.db.executemany('DELETE FROM layout_moves WHERE id=?', ((job['id'],) for job in jobs))
        jobs = []
        proposal = None if recover_only else plan(ledger,untracked=False,reconcile=True,progress=progress)
        log('旧版占位归档计划已备份并撤销，改用同一文件身份重新规划。')
    # Older journals can already contain a valid replacement publication. Add
    # guards to those records without discarding their chosen targets or backup.
    recorded = [(job, json.loads(job['payload'])) for job in jobs]
    by_target = defaultdict(list)
    for _, move in recorded:
        by_target[move['target'].casefold()].append(move)
    for job, move in recorded:
        if (job['state'] != 'pending' or not move['target'].startswith('history/')
                or move.get('requires') or move.get('requires_local') or move.get('reconciled')):
            continue
        required = []
        for replacement in by_target[move['source'].casefold()]:
            for ref in replacement['refs']:
                if ref[0] != 'files':
                    continue
                row = ledger.one('SELECT * FROM files WHERE project=? AND id=?', ref[1:])
                if row and row['active'] and row['sha256'] == replacement['sha256']:
                    required.append(dict(project=row['project'], file_id=row['id'], sha256=row['sha256'],
                                         bytes=row['bytes'], fingerprint=row['fingerprint']))
        if required:
            move['requires'] = required
            with ledger.db:
                ledger.db.execute('UPDATE layout_moves SET payload=? WHERE id=?',
                                  (json.dumps(move, ensure_ascii=False), job['id']))
    jobs = ledger.all("SELECT * FROM layout_moves WHERE state!='done'")
    legacy_archives = [json.loads(job['payload']) for job in jobs
                       if job['state']=='pending']
    if (jobs and all(job['state']=='pending' for job in jobs)
            and any(move['source'].startswith('downloads/') and move['target'].startswith('history/')
                    and not move.get('requires') and not move.get('requires_local')
                    and not move.get('reconciled') and not move.get('copy') for move in legacy_archives)
            and all(confined(base,move['source']).is_file() and not
                    confined(base,'runtime/path_migration/'+job['id']+'/'+Path(move['source']).name).exists()
                    for job,move in zip(jobs,legacy_archives))):
        # An older release may have journaled premature archives. If no bytes
        # moved yet, discard the untouched plan and rebuild with current guards.
        proposal = plan(ledger,untracked=False)
        with ledger.db:
            ledger.db.executemany("DELETE FROM layout_moves WHERE id=? AND state='pending'",
                                 ((job['id'],) for job in jobs))
        jobs = []
        incremental = False
        log('旧迁移计划尚未移动文件，已按新版下载完成条件重新规划。')
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
                    previous = ledger.one('SELECT state FROM layout_moves WHERE id=?', (identifier,))
                    if previous and previous['state'] == 'done':
                        # A staging path can be reused after an earlier publish.
                        # The live plan proves there is work again; keep the old
                        # audit record and journal a separate recoverable attempt.
                        identifier = hashlib.sha256(identifier.encode() + os.urandom(16)).hexdigest()
                    ledger.db.execute('INSERT OR IGNORE INTO layout_moves VALUES(?,?,?)',
                                      (identifier, json.dumps(move, ensure_ascii=False), 'pending'))
                    if progress:
                        progress.update(done=index, current=move['source'])
        jobs = ledger.all("SELECT * FROM layout_moves WHERE state!='done'")
    # Clear absent predecessors BEFORE publishing new bytes at the same path.
    # Checking only after publication would let old history claim the new file.
    # Preserve the version/hash evidence; only its missing local reference clears.
    if proposal is not None:
        with ledger.db:
            for source in proposal.get('missing', ()):
                if not confined(base, source).exists():
                    ledger.db.execute('UPDATE file_history SET path=NULL WHERE path=?', (source,))
                    ledger.db.execute('UPDATE files SET path=NULL WHERE active=0 AND path=?', (source,))
    if progress:
        progress.message(f'本次待处理 {len(jobs):,} 个文件；先校验并暂存整批文件，再发布到目标目录。')
        progress.phase('校验源文件并暂存', len(jobs))
    # Validate replacement bytes before moving ANY predecessor. A done flag or
    # a matching size alone must never authorize removing the last formal copy.
    verified_replacements = set()
    payloads = [(job,json.loads(job['payload'])) for job in jobs]
    replacement_paths = defaultdict(list)
    keeper_paths = defaultdict(list)
    for job, payload in payloads:
        keeper_paths[(payload['source'].casefold(),payload['sha256'])].extend([
            'runtime/path_migration/'+job['id']+'/'+Path(payload['source']).name,payload['target']])
        for ref in payload['refs']:
            if ref[0] == 'files':
                replacement_paths[(*ref[1:],payload['sha256'])].extend([
                    'runtime/path_migration/'+job['id']+'/'+Path(payload['source']).name, payload['target']])
    for _,move in payloads:
        for required in move.get('requires_local', []):
            candidates = [required['path'], *keeper_paths[(required['path'].casefold(),required['sha256'])]]
            if not any((candidate := confined(base,relative)).is_file()
                    and candidate.stat().st_size==required['bytes']
                    and checks.checksum(candidate)==required['sha256'] for relative in dict.fromkeys(candidates)):
                raise LayoutError('保留的最新历史副本校验未通过，尚未归档其他旧版：'+required['path'])
        for required in move.get('requires', []):
            identity = (required['project'],required['file_id'],required['sha256'],required.get('fingerprint'))
            if identity in verified_replacements:
                continue
            if cancelled():
                raise LayoutError('迁移已停止，下次可继续。')
            current = ledger.one('SELECT * FROM files WHERE project=? AND id=?', identity[:2])
            if (not current or not current['active'] or current['status']!='done'
                    or current['sha256']!=required['sha256'] or current['bytes']!=required['bytes']
                    or (required.get('fingerprint') and current['fingerprint']!=required['fingerprint'])):
                raise LayoutError('替代版本尚未完成或身份已变化，保留旧正式文件：'+required['file_id'])
            paths = [current['path']] if current['path'] else []
            paths.extend(replacement_paths[identity[:3]])
            matched = False
            for relative in dict.fromkeys(paths):
                candidate = confined(base,relative)
                if (candidate.is_file() and candidate.stat().st_size==required['bytes']
                        and checks.checksum(candidate)==required['sha256']):
                    matched = True
                    break
            if not matched:
                raise LayoutError('替代版本SHA256校验未通过，保留旧正式文件：'+required['file_id'])
            verified_replacements.add(identity)
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
                actual = checks.checksum(source)
                if move['sha256'] and actual != move['sha256']:
                    raise LayoutError('源文件SHA256与台账不符：' + move['source'])
                move['sha256'] = actual
                # Persist checksum before the first rename, including untracked files.
                with ledger.db:
                    ledger.db.execute('UPDATE layout_moves SET payload=? WHERE id=?',
                                      (json.dumps(move, ensure_ascii=False), job['id']))
                staging.parent.mkdir(parents=True, exist_ok=True)
                if move.get('copy'):
                    # Identical historical/current content may share a path.
                    # Preserve current bytes; journal and verify a history copy.
                    temporary = staging.with_name('history-copy.part')
                    try:
                        with source.open('rb') as inp, temporary.open('wb') as out:
                            while block := inp.read(1024 * 1024):
                                if cancelled():
                                    raise LayoutError('历史副本保存已停止，下次可继续。')
                                out.write(block)
                            out.flush()
                            os.fsync(out.fileno())
                        if (temporary.stat().st_size != move['bytes'] or
                                checks.checksum(temporary) != move['sha256']):
                            raise LayoutError('历史副本校验失败，保留当前文件：' + move['source'])
                        shutil.copystat(source, temporary)
                        checks.rename(temporary, staging)
                    finally:
                        temporary.unlink(missing_ok=True)
                else:
                    checks.rename(source, staging)
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
            if target.stat().st_size != move['bytes'] or checks.checksum(target) != move['sha256']:
                raise LayoutError('目标内容冲突，保留迁移现场：' + move['target'])
            if staging.exists():
                if checks.checksum(staging) != move['sha256']:
                    raise LayoutError('暂存内容不一致，保留源文件：' + move['target'])
                staging.unlink()
        else:
            if staging.stat().st_size != move['bytes'] or checks.checksum(staging) != move['sha256']:
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
        # Only this completed job's empty staging directory can be removed.
        # Preserve unknown files and every pending job's recovery bytes.
        try:
            staging.parent.rmdir()
        except OSError:
            pass
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
    if not incremental:
        ledger._download_targets = {(entry['project'], entry['file_id']):
            {name: value for name, value in entry.items() if name not in ('project', 'file_id')}
            for entry in mapping}
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
                        if move.get('copy'):
                            continue  # Current analysis still belongs to the original path.
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


def has_pending_publications(ledger):
    """A verified staged file is pending work even without a migration journal."""
    return any(relative_path(row['path'], ledger.base_dir).casefold().startswith('runtime/download_ready/')
               for row in ledger.all("SELECT path FROM files WHERE active=1 AND status='done' AND path IS NOT NULL AND sha256 IS NOT NULL"))


def prepare_downloads(ledger, cancelled=lambda: False, *, keys=None, wait=True, reuse=False, reconcile=False):
    """Pause readers before moving; optionally defer publication when readers are busy."""
    from analysis_worker import worker_lock
    base = ledger.base_dir
    marker = base / 'runtime/layout.pending'
    marker.parent.mkdir(exist_ok=True)
    token = download_structure_token(ledger)
    # A partial catalogue cannot prove that an unreferenced file is obsolete.
    reconcile = bool(reconcile and keys is None
        and ledger.one("SELECT 1 FROM refresh_cycles WHERE status='complete'")
        and not ledger.one("SELECT 1 FROM refresh_cycles WHERE status!='complete'"))
    needs_reconciliation = reconcile and getattr(ledger, '_download_reconciled_token', None) != token
    if (reuse and keys is None and not marker.exists()
            and not needs_reconciliation
            and getattr(ledger, '_download_layout_token', None) == token):
        # Pending verified replacements still need publication when a later batch
        # starts. Restrict it to their dependency groups, never to all files.
        staged = ledger.all("""SELECT project,id FROM files WHERE active=1 AND status='done'
          AND replace(path,char(92),'/') LIKE 'runtime/download_ready/%'""")
        if not staged:
            return True
        groups = getattr(ledger, '_publication_groups', {})
        keys = set()
        for row in staged:
            key = (row['project'], row['id'])
            keys.update(groups.get(key, (key,)))
    # Resume the journal before inspecting old references, which may point to staged files.
    # A prior interrupted batch always uses the full recovery path.
    if marker.exists():
        keys = None
    try:
        proposal = None if marker.exists() else plan(ledger, untracked=reconcile and keys is None,
                                                    keys=keys, reconcile=reconcile and keys is None)
    except LayoutPlanningError:
        raise
    except (LayoutError, OSError) as exc:
        # No journal or filesystem changes have started. Do not classify apply/
        # recovery failures this way: those may already own staged source bytes.
        mapping = ledger._download_targets if keys is not None else targets(ledger)
        raise LayoutPlanningError(str(exc), mapping, paths=getattr(exc, 'paths', ())) from exc
    reconciled = reconcile and keys is None and not marker.exists()
    if marker.exists() or proposal['moves']:
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
                        if not wait:
                            return False
                        print('等待当前后台任务释放文件后迁移：' + str(folder), flush=True)
                        announced = True
                    time.sleep(.2)
            marker.write_text('path migration in progress', encoding='utf-8')
            try:
                apply(ledger, proposal, cancelled=cancelled, incremental=keys is not None)
            except OSError as exc:
                if not wait and getattr(exc, 'winerror', None) in (32, 33):
                    # The journal and verified staging bytes remain durable.
                    return False
                raise
        resume_analysis_after_migration(base)
    else:
        apply(ledger, proposal, cancelled=cancelled, incremental=keys is not None)
    if keys is None:
        current_token = download_structure_token(ledger)
        ledger._download_layout_token = current_token if current_token == token else None
        if reconciled:
            ledger._download_reconciled_token = current_token if current_token == token else None
    return True


def prepare_downloads_or_defer(ledger, cancelled=lambda: False, **kwargs):
    """Automatic orchestration may defer a conflict; manual maintenance still raises."""
    from local_reconcile import reconciler
    def check():
        if cancelled():
            raise KeyboardInterrupt('任务已停止，保留文件及恢复记录。')
    for attempt in range(2):
        check()
        try:
            return prepare_downloads(ledger,cancelled,wait=False,**kwargs)
        except (LayoutError,OSError) as exc:
            import errno
            if isinstance(exc,OSError) and exc.errno==errno.ENOSPC:
                raise
            if attempt==0 and not (ledger.base_dir/'runtime/layout.pending').exists():
                service = reconciler(ledger,check)
                service.schedule(paths=getattr(exc,'paths',()),keys=kwargs.get('keys') or ())
                if service.tick(.25):
                    continue
            print('目录准备暂缓，保留现场；独立文件仍可继续下载：',str(exc),flush=True)
            return False


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
        if (not ledger.one("SELECT 1 FROM refresh_cycles WHERE status='complete'")
                or ledger.one("SELECT 1 FROM refresh_cycles WHERE status!='complete'")):
            raise LayoutError('完整盘点尚未完成，请先续接盘点，再整理有效下载目录。')
        if (base/'runtime/layout.pending').exists():
            if not args.apply:
                raise LayoutError('有未完成的迁移，请先执行 --apply 恢复后再预览。')
            apply(ledger, recover_only=True, progress=progress, log=progress.message)
        proposal = plan(ledger, reconcile=True, progress=progress)
        if args.apply:
            count = apply(ledger, proposal, progress=progress, log=progress.message)
            print(f'迁移完成：{count} 个文件。')
        else:
            proposal = proposal or plan(ledger, progress=progress)
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
