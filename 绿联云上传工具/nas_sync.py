"""One-way, checkpointed SMB upload of verified FusionLive downloads. Standard library only."""
from __future__ import annotations

import csv
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath, PurePosixPath
import re
import sqlite3
import stat
import time

HOME = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
VERSION = '2026.10.30-unified-history.1'
COMPACT_MARKERS = {'_long_paths', 'longpath', 'longpaths'}
HISTORY_MARKER = re.compile(r'__hist-[0-9a-f]{16}-[0-9a-f]{16}$')


def now():
    return datetime.now(BEIJING)


def native(path):
    value = str(Path(path).absolute())
    if os.name == 'nt' and not value.startswith('\\\\?\\'):
        value = '\\\\?\\UNC\\'+value[2:] if value.startswith('\\\\') else '\\\\?\\'+value
    return Path(value)


def plain(value):
    value = str(value)
    if value.startswith('\\\\?\\UNC\\'):
        return '\\\\'+value[8:]
    return value[4:] if value.startswith('\\\\?\\') else value


@dataclass
class Settings:
    source: Path
    ledger: Path
    target: Path
    base: Path
    chunk: int = 1048576
    attempts: int = 2
    preserve: bool = True
    scope: str = ''


def load_settings(path=None):
    path = Path(path or HOME/'config.json').resolve()
    raw = json.loads(path.read_text(encoding='utf-8-sig'))
    base = path.parent
    def local(value):
        return (base/Path(value)).resolve()
    source, ledger = local(raw['source_root']), local(raw['download_ledger'])
    target = Path(raw['target_root'])
    if not PureWindowsPath(str(target)).is_absolute() or not str(target).startswith('\\\\'):
        raise ValueError('target_root必须为NAS的UNC共享子目录。')
    if (raw.get('start_hour_beijing',12),raw.get('stop_hour_beijing',0)) != (12,0):
        raise ValueError('当前版本固定为北京时间12:00至次日00:00。')
    chunk, attempts = int(raw.get('chunk_bytes',1048576)), int(raw.get('retry_attempts',2))
    if not 65536 <= chunk <= 8*1048576 or not 1 <= attempts <= 5:
        raise ValueError('分块大小或重试次数超出允许范围。')
    scope = hashlib.sha256((str(raw['source_root']).replace('\\','/').casefold()+'|'+str(target).casefold()).encode()).hexdigest()[:16]
    if (not ledger.is_file() or not any(p.is_dir() for p in
            (source, ledger.parent.parent/'downloads', ledger.parent.parent/'history'))):
        raise FileNotFoundError('未找到本地HABSHANDBN源目录或下载SQLite台账，请检查相对路径。')
    return Settings(source,ledger,target,base,chunk,attempts,bool(raw.get('preserve_previous_versions',True)),scope)


class NETRESOURCEW(ctypes.Structure):
    _fields_ = [('dwScope',wintypes.DWORD),('dwType',wintypes.DWORD),
                ('dwDisplayType',wintypes.DWORD),('dwUsage',wintypes.DWORD),
                ('lpLocalName',wintypes.LPWSTR),('lpRemoteName',wintypes.LPWSTR),
                ('lpComment',wintypes.LPWSTR),('lpProvider',wintypes.LPWSTR)]


def connect(target):
    """Use the existing Windows session, or optional environment-only SMB credentials."""
    user, password = os.environ.get('UGREEN_SMB_USERNAME'), os.environ.get('UGREEN_SMB_PASSWORD')
    if bool(user) != bool(password):
        raise ValueError('UGREEN_SMB_USERNAME和UGREEN_SMB_PASSWORD需同时设置。')
    if user:
        if os.name != 'nt':
            raise RuntimeError('环境变量SMB认证适配仅支持Windows。')
        share = PureWindowsPath(str(target)).anchor.rstrip('\\')
        call = ctypes.WinDLL('mpr',use_last_error=True).WNetAddConnection2W
        call.argtypes = [ctypes.POINTER(NETRESOURCEW),wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD]
        call.restype = wintypes.DWORD
        resource = NETRESOURCEW(dwType=1,lpRemoteName=share)
        result = call(ctypes.byref(resource),password,user,0)
        if result == 1219:
            raise RuntimeError('Windows已有其他账号的NAS连接（1219）；请在资源管理器检查，不会自动断开其他连接。')
        if result:
            raise OSError(result,ctypes.FormatError(result))
    # Access the share itself first, to distinguish unavailable SMB from a new target directory.
    share = Path(PureWindowsPath(str(target)).anchor)
    if os.name == 'nt':
        with os.scandir(native(share)):
            pass
    if native(target).exists() and not native(target).is_dir():
        raise ValueError('NAS目标不是目录。')


class Stopped(Exception):
    pass


class Conflict(Exception):
    pass


@dataclass(frozen=True)
class Candidate:
    path: Path
    relative: str
    size: int
    mtime_ns: int
    sha256: str
    legacy_relatives: tuple[str, ...] = ()
    mapping_error: str = ''


def checked_relative(value):
    """Validate the complete SMB-relative path, without shortening components."""
    text = str(value).replace('\\', '/')
    parts = text.split('/')
    if (not text or PureWindowsPath(text).drive or text.startswith('/')
            or any(p in ('', '.', '..') or p.endswith((' ', '.'))
                   or re.search(r'[<>:"|?*\x00-\x1f]', p) for p in parts)):
        raise ValueError('非法或SMB不能表示的相对路径：'+text)
    return '/'.join(parts)


def history_relative(platform, identity, digest, preserve_marker=True):
    """One mirrored platform tree; every historical leaf has a stable identity."""
    path = PurePosixPath(checked_relative(platform))
    if path.parts[0] == 'history':
        path = PurePosixPath(*path.parts[1:])
    if not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('历史文件缺少有效SHA256。')
    stem, extension = path.stem, path.suffix
    if HISTORY_MARKER.search(stem):
        if preserve_marker and stem.endswith('-'+digest[:16]):
            return checked_relative('history/'+str(path))
        stem = HISTORY_MARKER.sub('', stem)
    token = hashlib.sha256(identity.encode('utf-8')).hexdigest()[:16]
    suffix = '__hist-'+token+'-'+digest[:16]+extension
    # Leave room within the SMB component limit, including supplementary characters.
    if max(len(suffix.encode('utf-16-le'))//2,len(suffix.encode('utf-8'))) >= 240:
        raise ValueError('历史文件扩展名过长，未生成路径。')
    while max(len((stem+suffix).encode('utf-16-le'))//2,len((stem+suffix).encode('utf-8'))) > 240:
        stem = stem[:-1]
    return checked_relative(str(PurePosixPath('history', path.parent, stem+suffix)))


def safe_remote(settings, relative):
    path = native(settings.target)
    for part in ('', *PurePosixPath(checked_relative(relative)).parts):
        if part:
            path /= part
        if path.is_symlink() or getattr(path, 'is_junction', lambda: False)():
            raise Conflict('NAS历史路径包含链接，保留现场：'+relative)
        try:
            attributes = getattr(path.lstat(),'st_file_attributes',0)
        except FileNotFoundError:
            continue
        if attributes & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400):
            raise Conflict('NAS历史路径包含重解析点，保留现场：'+relative)
    return path


def platform_relative(relative, project, folder_hint, folders, source_prefix):
    """Use the ledger's original platform directory, retaining the versioned filename.

    The folder ID encoded by the downloader takes precedence over today's parent
    reference, so retained historical files can also be restored correctly.
    """
    parts = PurePosixPath(relative).parts
    compact = any(p.casefold() in COMPACT_MARKERS for p in parts[:-1])
    folder_id = None
    if compact:
        if len(parts) != 3 or parts[0].casefold() not in COMPACT_MARKERS:
            raise ValueError('短路径目录结构不明确，不能恢复：'+relative)
        folder_id = parts[1]
    elif len(parts) > 1 and (match := re.search(r'__(\d+)$', parts[-2])):
        folder_id = match[1]
    else:
        folder_id = folder_hint
    folder = folders.get((str(project), str(folder_id))) if folder_id else None
    if folder is None:
        if compact:
            raise ValueError('下载台账缺少短路径对应的平台目录，未上传：'+relative)
        return checked_relative(relative)
    original = folder.replace('\\', '/').split('/')
    prefix = tuple(p.casefold() for p in source_prefix)
    if tuple(p.casefold() for p in original[:len(prefix)]) != prefix:
        raise ValueError('平台目录不属于配置的源根目录：'+folder)
    return checked_relative('/'.join([*original[len(prefix):], parts[-1]]))


def candidates(settings, check=lambda:None):
    """Snapshot the downloader ledger read-only, including retained historical files."""
    if (settings.ledger.parent/'layout.pending').exists():
        raise Stopped('本地目录整理尚未完成，暂停NAS迁移和上传。')
    db = sqlite3.connect(settings.ledger.as_uri()+'?mode=ro',uri=True,timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        current = [dict(r) for r in db.execute("SELECT * FROM files WHERE status='done'")]
        histories = [dict(r) for r in db.execute('SELECT * FROM file_history')]
        rows = [(r, False) for r in current] + [(r, True) for r in histories]
        folders = {(str(r['project']), str(r['id'])): r['path'] for r in db.execute('SELECT project,id,path FROM folders')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='folders'").fetchone() else {}
        mappings = [dict(r) for r in db.execute('SELECT * FROM local_paths')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='local_paths'").fetchone() else []
        history_paths = {r['path']: dict(r) for r in db.execute('SELECT * FROM history_paths')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_paths'").fetchone() else {}
        moves = [json.loads(r['payload']) for r in db.execute("SELECT payload FROM layout_moves WHERE state='done'")] if db.execute("SELECT 1 FROM sqlite_master WHERE name='layout_moves'").fetchone() else []
    finally:
        db.close()
    root = settings.source.resolve()
    library = settings.ledger.parent.parent
    source_prefix = root.relative_to((library/'downloads').resolve()).parts
    # Follow explicit, hash-qualified move journals, never match by content alone.
    predecessors = {}
    for move in moves:
        predecessors.setdefault((move['target'], move['sha256']), []).append(move['source'])

    def old_relatives(path, digest):
        pending, visited, result = [path], {path}, []
        while pending:
            for old in predecessors.get((pending.pop(), digest), ()):
                if old in visited:
                    continue
                visited.add(old)
                pending.append(old)
                parts = PurePosixPath(old).parts
                prefix = ('downloads', *source_prefix)
                if parts[:len(prefix)] == prefix:
                    result.append(checked_relative('/'.join(parts[len(prefix):])))
                elif len(parts) > 2 and parts[0] == 'history':
                    offset = 1 if parts[1:1+len(source_prefix)] == source_prefix else 2
                    if parts[offset:offset+len(source_prefix)] == source_prefix:
                        result.append(checked_relative('/'.join(('history', *parts[1:offset], *parts[offset+len(source_prefix):]))))
        return result
    by_file, by_parent = {}, {}
    for mapping in mappings:
        project = str(mapping['project'])
        local = mapping['local_path'].replace('\\', '/')
        by_file[(project, str(mapping['file_id']), local)] = mapping
        by_parent.setdefault((project, str(PurePosixPath(local).parent)), set()).add(
            str(PurePosixPath(mapping['platform_path']).parent))
    items, ignored = {}, 0
    seen_sources = set()
    for row, historical in rows:
        check()
        if not row['path'] or not row['sha256']:
            ignored += 1
            continue
        text = plain(row['path']).replace('\\','/')
        if Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            parts = PureWindowsPath(text).parts
            indexes = [i for i,p in enumerate(parts) if p.casefold() in ('downloads', 'history')]
            if not indexes:
                ignored += 1
                continue
            text = '/'.join(parts[indexes[0]:])
        unresolved = library/text
        source = unresolved.resolve()
        # Resolve history and overflow paths outside source_root without widening scope.
        source_parts = PurePosixPath(text).parts
        if any(p in ('.', '..') for p in source_parts):
            ignored += 1
            continue
        token = None
        history_mapping = history_paths.get(text)
        grouped_history = False
        if history_mapping:
            local_parts = (*PurePosixPath(history_mapping['platform_path']).parts[:-1], source.name)
            grouped_history = True
        elif source_parts[0] == 'history' and source_parts[1:1+len(source_prefix)] == source_prefix:
            local_parts = source_parts[1:]
            grouped_history = True
        elif len(source_parts) >= 3 and source_parts[0] == 'history':
            token = source_parts[1]
            local_parts = source_parts[2:]
        elif source_parts and source_parts[0] == 'downloads':
            local_parts = source_parts[1:]
        else:
            ignored += 1
            continue
        if local_parts[:len(source_prefix)] == source_prefix:
            local_relative = '/'.join(local_parts[len(source_prefix):])
        elif local_parts and local_parts[0] == 'long_path':
            local_relative = '/'.join(local_parts)
        else:
            ignored += 1
            continue
        if (not source.is_relative_to(library.resolve()) or unresolved.is_symlink()
                or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                       for p in unresolved.parents if p != library and p.is_relative_to(library))):
            ignored += 1
            continue
        identity = (str(source).casefold(), row['sha256'])
        if identity in seen_sources:
            continue
        historical = historical or source_parts[0] == 'history' or row.get('active', 1) == 0
        if source.name.lower().endswith(('.part','.tmp')):
            ignored += 1
            continue
        try:
            stat = native(source).stat()
            if not native(source).is_file() or stat.st_size != row['bytes']:
                ignored += 1
                continue
        except FileNotFoundError:
            ignored += 1
            continue
        error = ''
        aliases = []
        try:
            project = str(row.get('project', ''))
            mapping = by_file.get((project, str(row.get('id')), text))
            # Historical paths can reuse a parent mapping only when it is unambiguous.
            parent_paths = by_parent.get((project, str(PurePosixPath('downloads', *local_parts).parent)), set())
            if history_mapping:
                relative = platform_relative(source.name, project, 'mapped',
                    {(project, 'mapped'): str(PurePosixPath(history_mapping['platform_path']).parent)}, source_prefix)
                old_parts = PurePosixPath(history_mapping['original_path']).parts
                old_relative = str(PurePosixPath(relative).with_name(old_parts[-1]))
                aliases.append('history/'+old_parts[1]+'/'+old_relative)
            elif mapping or (local_parts[0] == 'long_path' and len(parent_paths) == 1):
                folder = str(PurePosixPath(mapping['platform_path']).parent) if mapping else next(iter(parent_paths))
                relative = platform_relative(PurePosixPath(local_relative).name, project, 'mapped',
                    {(project, 'mapped'): folder}, source_prefix)
            elif local_parts[0] == 'long_path':
                raise ValueError('下载台账缺少long_path对应的完整平台目录，未上传：'+text)
            else:
                relative = platform_relative(local_relative, project, row.get('folder_id'), folders, source_prefix)
            aliases += [local_relative, relative]
            aliases += old_relatives(text, row['sha256'])
            if historical:
                if grouped_history:
                    relative = checked_relative('history/'+relative)
                else:
                    token = token or ('version-' + str(row['id']) if 'id' in row and 'file_id' in row else
                        'retained-' + hashlib.sha256((project+'|'+str(row.get('file_id', row.get('id', '')))+'|'+row['sha256']).encode()).hexdigest()[:20])
                    relative = checked_relative('history/'+token+'/'+relative)
                aliases.append(relative)
                # Batch identifiers belong in filenames, never in the directory tree.
                platform = relative.removeprefix('history/') if grouped_history else '/'.join(PurePosixPath(relative).parts[2:])
                version = str(row.get('id', ''))+'|'+str(row.get('file_id', ''))+'|'+str(row.get('fingerprint', ''))+'|'+str(row.get('archived', ''))
                identity = 'local|'+project+'|'+version
                if not any(row.get(k) for k in ('id','file_id','fingerprint','archived')):
                    identity += '|'+str(history_mapping['original_path'] if history_mapping else text)
                relative = history_relative(platform, identity, row['sha256'])
        except ValueError as exc:
            relative, error = local_relative, str(exc)
        aliases = tuple(dict.fromkeys(a for a in aliases if a.casefold() != relative.casefold()))
        item = Candidate(native(source),relative,stat.st_size,stat.st_mtime_ns,row['sha256'],aliases,error)
        key = relative.casefold()
        if key in items and items[key].sha256 != item.sha256:
            raise Conflict('同一路径有冲突的下载台账记录：'+relative)
        if key in items:
            item = replace(item, legacy_relatives=tuple(dict.fromkeys((*items[key].legacy_relatives, *aliases))))
        items[key] = item
        seen_sources.add(identity)
    # A live destination must never be taken away for another candidate's history.
    result = [replace(item, legacy_relatives=tuple(a for a in item.legacy_relatives
              if a.casefold() not in items)) for item in items.values()]
    return sorted(result,key=lambda x:x.relative.casefold()), ignored


def signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


class Uploader:
    def __init__(self, settings, check=lambda:None, progress=None):
        self.s, self.check, self.progress = settings, check, progress
        (settings.base/'runtime').mkdir(exist_ok=True)
        self.db = sqlite3.connect(settings.base/'runtime/upload.sqlite3',timeout=15)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('''CREATE TABLE IF NOT EXISTS uploads(
          scope TEXT,relative TEXT,size INTEGER,mtime_ns INTEGER,sha256 TEXT,status TEXT,
          remote_mtime_ns INTEGER,partial_bytes INTEGER DEFAULT 0,updated TEXT,error TEXT,remote_sha256 TEXT,
          PRIMARY KEY(scope,relative))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS path_migrations(
          scope TEXT,old_relative TEXT,new_relative TEXT,sha256 TEXT,state TEXT,updated TEXT,
          PRIMARY KEY(scope,old_relative,new_relative))''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS archive_copies(
          scope TEXT,source_relative TEXT,sha256 TEXT,archive_relative TEXT,state TEXT,updated TEXT,
          PRIMARY KEY(scope,source_relative,sha256))''')
        self.db.commit()
        self.wire = 0
        self.protected = set()
        self.moved_paths = {}
        for row in self.db.execute("SELECT old_relative,new_relative,sha256 FROM path_migrations WHERE state IN ('moved','completed') AND scope=?", (settings.scope,)):
            self.moved_paths.setdefault((row['old_relative'].casefold(), row['sha256']), []).append(row['new_relative'])
        self.started = self.last_progress = time.monotonic()

    def close(self):
        self.db.close()

    def row(self, item):
        return self.db.execute('SELECT * FROM uploads WHERE scope=? AND relative=?',(self.s.scope,item.relative)).fetchone()

    def save(self, item, status, partial=0, remote_mtime=None, error=None):
        with self.db:
            self.db.execute('''INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(scope,relative) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
              sha256=excluded.sha256,status=excluded.status,remote_mtime_ns=excluded.remote_mtime_ns,
              partial_bytes=excluded.partial_bytes,updated=excluded.updated,error=excluded.error,
              remote_sha256=CASE WHEN excluded.status='done' THEN excluded.sha256 ELSE uploads.remote_sha256 END''',
              (self.s.scope,item.relative,item.size,item.mtime_ns,item.sha256,status,remote_mtime,partial,now().isoformat(),error,
               item.sha256 if status=='done' else None))

    def destination(self, item):
        return self.destination_relative(item.relative)

    def destination_relative(self, relative):
        rel = Path(checked_relative(relative))
        target = native(self.s.target/rel)
        tag = hashlib.sha256(relative.encode()).hexdigest()[:24]
        part = target.with_name('.fusionlive-'+self.s.scope+'-'+tag+'.part')
        return target, part

    def migration_record(self, item, previous, state):
        with self.db:
            self.db.execute('''INSERT INTO path_migrations VALUES(?,?,?,?,?,?)
              ON CONFLICT(scope,old_relative,new_relative) DO UPDATE SET
              sha256=excluded.sha256,state=excluded.state,updated=excluded.updated''',
                (self.s.scope,previous,item.relative,item.sha256,state,now().isoformat()))

    def finish_migrations(self, item):
        with self.db:
            self.db.execute("""UPDATE path_migrations SET state='completed',updated=?
              WHERE scope=? AND new_relative=? AND sha256=?
              AND state IN ('planned','partial_planned','partial_moved')""",
              (now().isoformat(),self.s.scope,item.relative,item.sha256))

    def restore_legacy(self, item, target, part):
        """Migrate a verified old NAS path within the share, without retransmission.

        Existing canonical destinations use the ordinary adoption/conflict rules.
        No old directory tree or duplicate file is recursively deleted.
        """
        if target.exists():
            return None
        previous_paths = list(item.legacy_relatives)
        seen = {p.casefold() for p in previous_paths}
        for previous in previous_paths:
            for moved in self.moved_paths.get((previous.casefold(), item.sha256), ()):
                if moved.casefold() not in seen:
                    seen.add(moved.casefold())
                    previous_paths.append(moved)
        for previous in reversed(previous_paths):
            if previous.casefold() in self.protected or previous.casefold() == item.relative.casefold():
                continue
            self.check()
            old_target, old_part = self.destination_relative(previous)
            if old_target.exists():
                before = signature(old_target)
                self.emit(item,0,True,'核对旧路径文件')
                if (before[0] != item.size or self.hash_file(old_target,item) != item.sha256):
                    raise Conflict('旧NAS路径文件内容已变化，保留原文件：'+previous)
                if self.hash_file(item.path) != item.sha256:
                    raise Conflict('本地SHA256与下载台账不符，未迁移旧路径：'+previous)
                self.unchanged_source(item)
                if signature(old_target) != before:
                    raise Conflict('旧NAS路径文件在校验期间变化，未迁移：'+previous)
                target.parent.mkdir(parents=True,exist_ok=True)
                self.migration_record(item,previous,'planned')
                self.check()
                self.unchanged_source(item)
                if target.exists() or signature(old_target) != before:
                    raise Conflict('迁移前NAS路径状态发生变化，未覆盖：'+item.relative)
                old_target.rename(target)
                self.save(item,'done',remote_mtime=target.stat().st_mtime_ns)
                self.migration_record(item,previous,'moved')
                self.remove_empty_parents(old_target.parent)
                self.emit(item,item.size,True,'旧路径迁移完成')
                return 'migrated',0
            old = self.db.execute('SELECT * FROM uploads WHERE scope=? AND relative=?',
                                  (self.s.scope,previous)).fetchone()
            if (not part.exists() and old_part.exists() and old
                    and old['sha256'] == item.sha256 and old['size'] == item.size
                    and old_part.stat().st_size <= item.size):
                # The normal upload loop validates every byte of this prefix.
                target.parent.mkdir(parents=True,exist_ok=True)
                self.migration_record(item,previous,'partial_planned')
                self.save(item,'uploading',old_part.stat().st_size)
                self.check()
                if part.exists():
                    raise Conflict('目标续传文件在迁移前出现，未覆盖。')
                old_part.rename(part)
                self.migration_record(item,previous,'partial_moved')
        return None

    def remove_empty_parents(self, folder):
        root = native(self.s.target)
        while folder != root and folder.is_relative_to(root):
            try:
                folder.rmdir()  # Only the just-migrated path; nonempty folders stop here.
            except OSError:
                break
            folder = folder.parent

    def backup_managed(self, target, item, old):
        digest = old['remote_sha256']
        relative = history_relative(item.relative, 'nas|'+self.s.scope+'|'+item.relative+'|'+digest, digest)
        archive = safe_remote(self.s, relative)
        with self.db:
            self.db.execute('''INSERT INTO archive_copies VALUES(?,?,?,?,?,?)
                ON CONFLICT(scope,source_relative,sha256) DO UPDATE SET
                archive_relative=excluded.archive_relative,state=excluded.state,updated=excluded.updated''',
                (self.s.scope,item.relative,digest,relative,'planned',now().isoformat()))
        if archive.exists():
            if self.hash_file(archive) != digest:
                raise Conflict('历史备份目标已有不同内容，未覆盖：'+relative)
        else:
            archive.parent.mkdir(parents=True,exist_ok=True)
            tag = hashlib.sha256((self.s.scope+'|'+relative).encode()).hexdigest()[:24]
            partial_relative = str(PurePosixPath(relative).with_name('.fusionlive-archive-'+tag+'.part'))
            backup_part = safe_remote(self.s, partial_relative)
            # Restart only this deterministic private copy; official old bytes remain intact.
            with target.open('rb') as source, backup_part.open('wb') as backup:
                while True:
                    self.check()
                    data = source.read(self.s.chunk)
                    if not data:
                        break
                    backup.write(data)
                backup.flush()
                os.fsync(backup.fileno())
            if self.hash_file(backup_part) != digest:
                raise OSError('远端旧版本备份校验失败，未覆盖。')
            self.check()
            safe_remote(self.s, relative)
            if archive.exists():
                raise Conflict('历史备份目标在发布前出现，保留现场：'+relative)
            backup_part.rename(archive)
        with self.db:
            self.db.execute("UPDATE archive_copies SET state='verified',updated=? WHERE scope=? AND source_relative=? AND sha256=?",
                            (now().isoformat(),self.s.scope,item.relative,digest))
        return relative

    def hash_file(self, path, item=None):
        digest = hashlib.sha256()
        size=0
        with path.open('rb') as stream:
            while True:
                self.check()
                data = stream.read(self.s.chunk)
                if not data:
                    break
                digest.update(data)
                size+=len(data)
                if item:self.emit(item,size,phase='回读校验')
        return digest.hexdigest()

    def unchanged_source(self, item):
        if signature(item.path) != (item.size,item.mtime_ns):
            raise Conflict('本地文件在上传期间变化，保留远端正式文件：'+item.relative)

    def emit(self, item, count, force=False, phase='上传'):
        at = time.monotonic()
        if self.progress and (force or at-self.last_progress>=30):
            self.progress({'phase':phase,'file':item.relative,'current_bytes':count,'current_total':item.size,
                           'sent_bytes':self.wire,'average_MBps':self.wire/max(at-self.started,0.001)/1e6})
            self.last_progress = at

    def one(self, item, verify_all=False):
        self.check()
        if item.mapping_error:
            raise Conflict(item.mapping_error)
        self.unchanged_source(item)
        target, part = self.destination(item)
        migrated = self.restore_legacy(item,target,part)
        if migrated:
            return migrated
        old = self.row(item)
        same_source = bool(old and (old['size'],old['mtime_ns'],old['sha256'])==(item.size,item.mtime_ns,item.sha256))
        target_stat = signature(target) if target.exists() else None
        if (not verify_all and same_source and old['status']=='done'
                and target_stat==(item.size,old['remote_mtime_ns'])):
            self.finish_migrations(item)
            return 'skipped',0
        if target_stat:
            self.emit(item,0,True,'比较远端文件')
            remote_hash = self.hash_file(target,item)
            local_hash = self.hash_file(item.path)
            self.unchanged_source(item)
            if local_hash != item.sha256:
                raise Conflict('本地SHA256与下载台账不符：'+item.relative)
            if remote_hash==local_hash:
                self.save(item,'done',remote_mtime=target.stat().st_mtime_ns)
                self.finish_migrations(item)
                return 'adopted',0
            if not old or remote_hash != old['remote_sha256']:
                raise Conflict('远端同名文件与本地不同且不能确认归本任务管理，未覆盖：'+item.relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        # Only our deterministic temporary filename is reused/truncated; never arbitrary NAS files.
        resume = part.stat().st_size if same_source and part.exists() and part.stat().st_size<=item.size else 0
        if not resume:
            with part.open('wb'):
                pass
        self.save(item,'uploading',resume)
        digest = hashlib.sha256()
        with item.path.open('rb') as source:
            if resume:
                valid = True
                with part.open('rb') as previous:
                    remaining = resume
                    while remaining:
                        self.check()
                        count = min(self.s.chunk,remaining)
                        data, stored = source.read(count), previous.read(count)
                        if len(data)!=count or data!=stored:
                            valid = False
                            break
                        digest.update(data)
                        remaining -= count
                        self.emit(item,resume-remaining,phase='校验续传部分')
                if not valid:
                    resume = 0
                    digest = hashlib.sha256()
                    source.seek(0)
                    with part.open('wb'):
                        pass
            size, saved = resume, resume
            self.emit(item,size,True,'继续上传' if resume else '开始上传')
            with part.open('ab') as output:
                while True:
                    self.check()
                    data = source.read(self.s.chunk)
                    if not data:
                        break
                    output.write(data)
                    digest.update(data)
                    size += len(data)
                    self.wire += len(data)
                    if size-saved >= 8*1048576:
                        output.flush()
                        self.save(item,'uploading',size)
                        saved = size
                    self.emit(item,size)
                output.flush()
                os.fsync(output.fileno())
        self.unchanged_source(item)
        if size!=item.size or digest.hexdigest()!=item.sha256:
            raise Conflict('本地内容与下载台账不符，未提交远端正式文件：'+item.relative)
        self.emit(item,size,True,'回读NAS校验')
        if part.stat().st_size!=item.size or self.hash_file(part,item)!=item.sha256:
            raise OSError('NAS回读SHA256校验失败。')
        self.check()
        self.unchanged_source(item)
        if target_stat:
            # Keep the previous managed version before replacing it.
            if self.hash_file(target)!=old['remote_sha256']:
                raise Conflict('远端文件在上传期间被其他程序修改，未覆盖。')
            if self.s.preserve:
                self.backup_managed(target,item,old)
            if signature(target)!=target_stat:
                raise Conflict('远端文件在上传期间变化，未覆盖。')
            os.replace(part,target)
        else:
            # On Windows rename does not overwrite a file created by another process meanwhile.
            part.rename(target)
        try:
            os.utime(target,ns=(item.mtime_ns,item.mtime_ns))
        except OSError:
            pass
        self.save(item,'done',remote_mtime=target.stat().st_mtime_ns)
        self.finish_migrations(item)
        self.emit(item,size,True,'上传完成')
        return 'uploaded',item.size-resume

    def migrate(self, item):
        """Migration-only pass: never send a new file or publish a partial file."""
        self.check()
        if item.mapping_error:
            raise Conflict(item.mapping_error)
        self.unchanged_source(item)
        target, part = self.destination(item)
        return self.restore_legacy(item, target, part)


def audit(settings, item, result, sent=0, error=''):
    folder = settings.base/'logs'/now().strftime('%Y-%m-%d')
    folder.mkdir(parents=True,exist_ok=True)
    target = folder/'uploads.csv'
    new = not target.exists()
    with target.open('a',encoding='utf-8-sig',newline='') as stream:
        writer = csv.writer(stream)
        if new:
            writer.writerow(['北京时间','相对路径','结果','文件字节','本次发送字节','SHA256','错误'])
        row = [now().isoformat(),item.relative,result,item.size,sent,item.sha256,error]
        writer.writerow(["'"+v if isinstance(v,str) and v.startswith(('=','+','-','@')) else v for v in row])
