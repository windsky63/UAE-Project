"""One-way, checkpointed SMB upload of verified FusionLive downloads. Standard library only."""
from __future__ import annotations

import csv
from contextlib import contextmanager
from collections import OrderedDict
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
import threading
import struct
import uuid

HOME = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
VERSION = '2026.11.09-nas-logging.1'
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
    remote_hash: object = None
    verification_method: str = 'smb'


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
    from nas_remote_hash import RemoteHashSettings
    method = raw.get('verification_method','ssh')
    if method not in ('ssh','smb'):
        raise ValueError('verification_method须为ssh或smb。')
    remote = raw.get('remote_hash',{})
    if not isinstance(remote,dict):raise ValueError('remote_hash须为配置对象。')
    remote = {**remote,'enabled':method=='ssh','fallback_to_smb':False}
    remote_hash = RemoteHashSettings.parse(remote, base)
    return Settings(source,ledger,target,base,chunk,attempts,bool(raw.get('preserve_previous_versions',True)),scope,remote_hash,method)


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
    formal_guards: tuple[tuple[str, int, str], ...] = ()


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


def safe_remote(settings, relative, *, _validated=None):
    # One lstat provides both POSIX link type and Windows reparse attributes.
    # Do not call is_symlink/is_junction as well: each repeats SMB metadata I/O.
    # A caller may share checks only within one target/partial path resolution;
    # publication and migration rechecks always start with a fresh set.
    validated = set() if _validated is None else _validated
    path = native(settings.target)
    for part in ('', *PurePosixPath(checked_relative(relative)).parts):
        if part:
            path /= part
        if path in validated:
            continue
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info,'st_file_attributes',0) & getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0x400)):
            raise Conflict('NAS历史路径包含重解析点，保留现场：'+relative)
        validated.add(path)
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


def formal_downloads(files, histories, documents, library, check):
    """Choose one physical download per current content, using explicit lineage.

    Old schemas without content identity retain their previous conservative
    behavior; filenames alone cannot prove two files are successive versions.
    """
    if not files or not {'project','id','document_id','folder_id','active','filename'} <= files[0].keys():
        return None, {}
    current = {(r['project'],r['id']):r for r in files if r['active']}
    docs = {(r['project'],r['id'],r['folder_id']):r for r in documents}
    by_document, by_lineage, by_reference = {}, {}, {}
    for key,row in current.items():
        document = docs.get((row['project'],row['document_id'],row['folder_id']),{})
        by_document.setdefault((row['project'],row['document_id']),set()).add(key)
        if document.get('first_id'):
            by_lineage.setdefault((row['project'],document['first_id']),set()).add(key)
        if document.get('reference'):
            by_reference.setdefault((row['project'],document['reference']),set()).add(key)
    members = {}
    for old in files:
        check()
        key = (old['project'],old['id'])
        if key in current:
            members[key] = [key]
            continue
        previous = docs.get((old['project'],old['document_id'],old['folder_id']),{})
        matches = []
        possible = set(by_document.get((old['project'],old['document_id']),()))
        possible.update(by_lineage.get((old['project'],previous.get('first_id')),()))
        possible.update(by_reference.get((old['project'],previous.get('reference')),()))
        for new_key in sorted(possible):
            new = current[new_key]
            if old['project'] != new['project'] or (old.get('relation') and new.get('relation')
                    and old['relation'] != new['relation']):
                continue
            document = docs.get((new['project'],new['document_id'],new['folder_id']),{})
            family = (old['document_id']==new['document_id'] or
                (previous.get('first_id') and previous['first_id']==document.get('first_id')) or
                (previous.get('reference') and previous['reference']==document.get('reference')
                 and not (previous.get('first_id') and document.get('first_id'))))
            member = (old['filename'].casefold()==new['filename'].casefold() or
                      (old['id']==old['document_id'] and new['id']==new['document_id']))
            if family and member:
                matches.append(new_key)
        members[key] = matches
    def local(row):
        text = plain(row.get('path') or '').replace('\\','/')
        if Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            parts = PureWindowsPath(text).parts
            positions = [i for i,p in enumerate(parts) if p.casefold() in ('downloads','history')]
            text = '/'.join(parts[positions[0]:]) if positions else ''
        if not text.startswith('downloads/') or any(p in ('.','..') for p in text.split('/')):
            return None
        path = library/text
        if not row.get('sha256') or not path.resolve().is_relative_to(library.resolve()):
            return None
        try:
            if path.is_file() and path.stat().st_size==row.get('bytes'):
                return text
        except FileNotFoundError:
            pass
        return None
    chosen = {}
    for key,row in current.items():
        if row['status']=='done' and (path := local(row)):
            chosen[key] = (path,row)
    versions = [(r,r['file_id'],(str(r.get('archived') or ''),int(r['id']))) for r in histories]
    versions.extend((r,r['id'],(str(r.get('updated') or r.get('downloaded_at') or ''),0))
                    for r in files if not r['active'] and r['status']=='done')
    for row,identifier,_ in sorted(versions,key=lambda value:value[2],reverse=True):
        check()
        if not (path := local(row)):
            continue
        for key in members.get((row['project'],identifier),()):
            if key not in chosen:
                chosen[key] = (path,current[key])
    selected = {}
    for key,(path,owner) in chosen.items():
        if path.casefold() in selected and selected[path.casefold()] != owner:
            raise Conflict('同一历史文件对应多个当前内容，不能任意指定NAS正式文件：'+path)
        selected[path.casefold()] = owner
    return selected, members


def candidates(settings, check=lambda:None, diagnostics=None):
    """Snapshot the downloader ledger read-only, including retained historical files."""
    if (settings.ledger.parent/'layout.pending').exists():
        raise Stopped('本地目录整理尚未完成，暂停NAS迁移和上传。')
    db = sqlite3.connect(settings.ledger.as_uri()+'?mode=ro',uri=True,timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.execute('BEGIN')
        files = [dict(r) for r in db.execute('SELECT * FROM files')]
        current = [r for r in files if r['status']=='done']
        histories = [dict(r) for r in db.execute('SELECT * FROM file_history')]
        documents = [dict(r) for r in db.execute('SELECT * FROM documents')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='documents'").fetchone() else []
        if (db.execute("SELECT 1 FROM sqlite_master WHERE name='refresh_cycles'").fetchone()
                and db.execute("SELECT 1 FROM refresh_cycles WHERE status!='complete'").fetchone()):
            raise Stopped('下载清单尚未完整核对，暂停NAS正式目录整理。')
        rows = [(r, False) for r in current] + [(r, True) for r in histories]
        folders = {(str(r['project']), str(r['id'])): r['path'] for r in db.execute('SELECT project,id,path FROM folders')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='folders'").fetchone() else {}
        mappings = [dict(r) for r in db.execute('SELECT * FROM local_paths')] if db.execute("SELECT 1 FROM sqlite_master WHERE name='local_paths'").fetchone() else []
        history_paths = {r['path']: dict(r) for r in db.execute('SELECT * FROM history_paths')} if db.execute("SELECT 1 FROM sqlite_master WHERE name='history_paths'").fetchone() else {}
        moves = [json.loads(r['payload']) for r in db.execute("SELECT payload FROM layout_moves WHERE state='done'")] if db.execute("SELECT 1 FROM sqlite_master WHERE name='layout_moves'").fetchone() else []
    finally:
        db.close()
    root = settings.source.resolve()
    library = settings.ledger.parent.parent
    formal, members = formal_downloads(files,histories,documents,library,check)
    candidate_members, formal_items = {}, {}
    source_prefix = root.relative_to((library/'downloads').resolve()).parts
    folded_prefix = tuple(p.casefold() for p in source_prefix)
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

    def exclude(row, historical, reason, blocking=True):
        nonlocal ignored
        ignored += 1
        if diagnostics is not None:
            diagnostics.append(dict(reason=reason,path=row.get('path'),
                project=row.get('project'),file_id=row.get('file_id',row.get('id')),
                historical=historical,blocks_mirror=blocking))

    for row, historical in rows:
        check()
        if not row['path']:
            if historical or row.get('active',1) == 0:
                # HistoryRetention deliberately clears these paths while
                # retaining version evidence. No physical copy is expected.
                exclude(row,historical,'历史记录无保留路径',False)
                continue
            folder = folders.get((str(row.get('project')),str(row.get('folder_id'))))
            outside = bool(folder and tuple(p.casefold() for p in folder.replace('\\','/').split('/')[:len(source_prefix)])
                           != tuple(p.casefold() for p in source_prefix))
            exclude(row,historical,'范围外记录' if outside else '台账缺少本地路径',not outside)
            continue
        text = plain(row['path']).replace('\\','/')
        if Path(text).is_absolute() or PureWindowsPath(text).is_absolute():
            parts = PureWindowsPath(text).parts
            indexes = [i for i,p in enumerate(parts) if p.casefold() in ('downloads', 'history')]
            if not indexes:
                exclude(row,historical,'绝对路径无法定位downloads或history')
                continue
            text = '/'.join(parts[indexes[0]:])
        unresolved = library/text
        source = unresolved.resolve()
        # Resolve history and overflow paths outside source_root without widening scope.
        source_parts = PurePosixPath(text).parts
        if any(p in ('.', '..') for p in source_parts):
            exclude(row,historical,'路径包含非法层级')
            continue
        token = None
        history_mapping = history_paths.get(text)
        grouped_history = False
        if history_mapping:
            local_parts = (*PurePosixPath(history_mapping['platform_path']).parts[:-1], source.name)
            grouped_history = True
        elif source_parts[0] == 'history' and tuple(p.casefold() for p in source_parts[1:1+len(source_prefix)]) == folded_prefix:
            local_parts = source_parts[1:]
            grouped_history = True
        elif len(source_parts) >= 3 and source_parts[0] == 'history':
            token = source_parts[1]
            local_parts = source_parts[2:]
        elif source_parts and source_parts[0] == 'downloads':
            local_parts = source_parts[1:]
        else:
            exclude(row,historical,'路径不属于downloads或history')
            continue
        if tuple(p.casefold() for p in local_parts[:len(source_prefix)]) == folded_prefix:
            local_relative = '/'.join(local_parts[len(source_prefix):])
        elif local_parts and local_parts[0].casefold() == 'long_path':
            local_relative = '/'.join(local_parts)
        else:
            exclude(row,historical,'配置源范围外的记录',False)
            continue
        if (not source.is_relative_to(library.resolve()) or unresolved.is_symlink()
                or any(p.is_symlink() or getattr(p, 'is_junction', lambda: False)()
                       for p in unresolved.parents if p != library and p.is_relative_to(library))):
            exclude(row,historical,'路径越界或包含链接')
            continue
        if not row['sha256']:
            exclude(row,historical,'台账缺少SHA256')
            continue
        identity = (str(source).casefold(), row['sha256'])
        if identity in seen_sources:
            continue
        # The downloader keeps the last usable predecessor in downloads until
        # replacement publication is safe. An inactive flag alone must not
        # remove that formal copy from NAS. Legacy schemas without a trusted
        # original name retain their existing conservative history treatment.
        owner = formal.get(text.casefold()) if formal is not None else None
        retained_formal = source_parts[0] == 'downloads' and (bool(owner) if formal is not None else bool(row.get('filename')))
        if owner:
            row = dict(row, filename=owner['filename'])
        historical = (historical or source_parts[0] == 'history' or row.get('active', 1) == 0) and not retained_formal
        if formal is not None and source_parts[0]=='downloads':
            historical = not retained_formal
        if source.name.lower().endswith(('.part','.tmp')):
            exclude(row,historical,'临时文件',False)
            continue
        try:
            stat = native(source).stat()
            if not native(source).is_file():
                exclude(row,historical,'本地路径不是普通文件')
                continue
            if stat.st_size != row['bytes']:
                exclude(row,historical,'本地大小与台账不符')
                continue
        except FileNotFoundError:
            exclude(row,historical,'本地文件不存在')
            continue
        error = ''
        aliases = []
        try:
            project = str(row.get('project', ''))
            mapping = by_file.get((project, str(row.get('file_id',row.get('id'))), text))
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
            if row.get('filename'):
                filename = checked_relative(row['filename'])
                if '/' in filename:
                    raise ValueError('台账原文件名包含目录，未生成NAS正式路径：'+filename)
                # Current mappings may carry stable collision numbers; keep
                # those names instead of merging distinct platform files.
                mapped_name = mapping.get('local_filename') if mapping else None
                if mapped_name:
                    filename = checked_relative(mapped_name)
                    if '/' in filename:
                        raise ValueError('本地文件名映射包含目录：'+filename)
                formal_relative = checked_relative(str(PurePosixPath(relative).with_name(filename)))
                if historical:
                    aliases.append(formal_relative)
                else:
                    relative = formal_relative
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
        candidate_members[key] = members.get((row.get('project'),row.get('file_id',row.get('id'))),())
        if owner:
            formal_items[(owner['project'],owner['id'])] = item
        seen_sources.add(identity)
    # A live destination must never be taken away for another candidate's history.
    result = [replace(item, legacy_relatives=tuple(a for a in item.legacy_relatives
              if a.casefold() not in items), formal_guards=tuple(dict.fromkeys(
                  (keeper.relative,keeper.size,keeper.sha256)
                  for key in candidate_members.get(item.relative.casefold(),())
                  if (keeper := formal_items.get(key)) and keeper.relative != item.relative)))
              for item in items.values()]
    return sorted(result,key=lambda x:(x.relative.startswith('history/'),x.relative.casefold())), ignored


def signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def hash_stamp(path):
    """Live content-change evidence; Windows ctime alone is a creation time."""
    info = path.stat()
    changed = info.st_ctime_ns
    if os.name == 'nt':
        class BasicInfo(ctypes.Structure):
            _fields_ = [(name,ctypes.c_longlong) for name in
                        ('created','accessed','written','changed')] + [('attributes',wintypes.DWORD)]
        kernel = ctypes.WinDLL('kernel32',use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = [wintypes.LPCWSTR,wintypes.DWORD,wintypes.DWORD,ctypes.c_void_p,
                           wintypes.DWORD,wintypes.DWORD,wintypes.HANDLE]
        create.restype = wintypes.HANDLE
        handle = create(str(path),0x80,7,None,3,0x02000000,None)
        if handle == ctypes.c_void_p(-1).value:
            return None  # Unsupported/access-limited servers retain full reads.
        query = kernel.GetFileInformationByHandleEx
        query.argtypes = [wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        close = kernel.CloseHandle
        close.argtypes = [wintypes.HANDLE]
        try:
            basic = BasicInfo()
            if not query(handle,0,ctypes.byref(basic),ctypes.sizeof(basic)) or not basic.changed:
                return None
            changed = basic.changed
            if not plain(path).startswith('\\\\'):
                # Local timestamp writes can collide within one clock tick.
                # Use the journal sequence as well; unavailable evidence means
                # a full reread, never a guessed cache hit. SMB has no USN API.
                # https://learn.microsoft.com/en-us/windows/win32/api/winioctl/ni-winioctl-fsctl_read_file_usn_data
                ioctl=kernel.DeviceIoControl
                ioctl.argtypes=[wintypes.HANDLE,wintypes.DWORD,ctypes.c_void_p,wintypes.DWORD,
                               ctypes.c_void_p,wintypes.DWORD,ctypes.POINTER(wintypes.DWORD),ctypes.c_void_p]
                versions=ctypes.create_string_buffer(struct.pack('<HH',2,3))
                output=ctypes.create_string_buffer(4096)
                returned=wintypes.DWORD()
                if not ioctl(handle,0x900EB,versions,4,output,len(output),ctypes.byref(returned),None):
                    return None
                major=struct.unpack_from('<H',output.raw,4)[0] if returned.value>=8 else 0
                offset={2:24,3:40}.get(major)
                if offset is None or returned.value<offset+8:
                    return None
                usn=struct.unpack_from('<q',output.raw,offset)[0]
                if usn<=0:return None
                changed=(changed,usn)
        finally:
            close(handle)
        after = path.stat()
        if (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns) != (
                after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns):
            return None
    if not info.st_ino or not changed:
        return None
    return info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,changed


def optional_signature(path):
    """Read existence and metadata in one request; preserve access/I/O errors."""
    try:
        return signature(path)
    except FileNotFoundError:
        return None


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
        self.db.execute('''CREATE INDEX IF NOT EXISTS path_migrations_target
          ON path_migrations(scope,new_relative,sha256,state)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS archive_copies(
          scope TEXT,source_relative TEXT,sha256 TEXT,archive_relative TEXT,state TEXT,updated TEXT,
          PRIMARY KEY(scope,source_relative,sha256))''')
        self.db.commit()
        self.wire = 0
        self.nas_read_bytes = 0
        self.remote_hash_files = self.remote_hash_bytes = 0
        self.remote_hasher = None
        self.remote_hash_disabled = False
        self.hash_proofs = OrderedDict()
        self.hash_cache_hits = 0
        self.force_hash = False
        self.mirror = False
        self.timings = dict(path_checks=0.0, nas_hash=0.0, local_hash=0.0, upload=0.0,
                            state=0.0, migration_finish=0.0, publish=0.0)
        self.timings['backup_copy'] = 0.0
        self.activity = ('file', time.monotonic())
        self.current_file = '准备处理'
        self.phase_lock = threading.RLock()
        self.phase_state = None
        self.phase_number = 0
        self.status = None
        self.protected = set()
        self.formal_proofs = {}
        self.moved_paths = {}
        # Discovery hints only. Never use cached metadata to authorize a move.
        self.scan_directories = OrderedDict()
        for row in self.db.execute("SELECT old_relative,new_relative,sha256 FROM path_migrations WHERE state IN ('moved','completed') AND scope=?", (settings.scope,)):
            self.moved_paths.setdefault((row['old_relative'].casefold(), row['sha256']), []).append(row['new_relative'])
        self.started = self.last_progress = time.monotonic()

    def close(self):
        try:
            if self.remote_hasher:
                self.remote_hasher.close()
        finally:
            self.db.close()

    def begin_phase(self, name, total=None, total_bytes=None):
        at = time.monotonic()
        with self.phase_lock:
            self.phase_number += 1
            self.phase_state = dict(name=name,number=self.phase_number,total=total,total_bytes=total_bytes,
                completed=0,completed_bytes=0,current_bytes=0,current_total=0,current_file='',
                started=at,updated=at,state='running')
        if self.status:
            self.status(f'阶段开始：{name}；总量={total if total is not None else "待扫描"}'
                        +(f'；总字节={total_bytes}' if total_bytes is not None else ''))

    def phase_file(self, relative, size=0):
        with self.phase_lock:
            self.current_file = relative
            if self.phase_state:
                self.phase_state.update(current_file=relative,current_bytes=0 if self.phase_state['name']=='本地预检' else None,
                                        current_total=size,updated=time.monotonic())

    def phase_bytes(self, count):
        with self.phase_lock:
            if (self.phase_state and self.phase_state['state']=='running' and self.phase_state['name']=='本地预检'
                    and count != self.phase_state['current_bytes']):
                self.phase_state.update(current_bytes=count,updated=time.monotonic())

    def phase_advance(self, size=0):
        with self.phase_lock:
            if self.phase_state:
                self.phase_state['completed'] += 1
                self.phase_state['completed_bytes'] += size
                self.phase_state.update(current_bytes=0,updated=time.monotonic())

    def phase_snapshot(self):
        with self.phase_lock:
            if not self.phase_state:
                return None
            value = dict(self.phase_state)
        at = time.monotonic()
        value.update(elapsed_seconds=round(at-value.pop('started'),1),
                     idle_seconds=round(at-value.pop('updated'),1))
        return value

    def finish_phase(self):
        value = self.phase_snapshot()
        if value:
            with self.phase_lock:
                self.phase_state['state'] = 'complete'
            if self.status:
                self.status(f'阶段完成：{value["name"]}；已处理={value["completed"]}'
                            f'；已完成字节={value["completed_bytes"]}；阶段耗时={value["elapsed_seconds"]:.1f}秒')

    @contextmanager
    def measure(self, phase):
        started = time.monotonic()
        previous = self.activity
        self.activity = (phase, started)
        try:
            yield
        finally:
            self.timings[phase] += time.monotonic() - started
            self.activity = previous

    def remote(self, relative, *, _validated=None):
        with self.measure('path_checks'):
            return safe_remote(self.s, relative, _validated=_validated)

    def row(self, item):
        return self.db.execute('SELECT * FROM uploads WHERE scope=? AND relative=?',(self.s.scope,item.relative)).fetchone()

    def save(self, item, status, partial=0, remote_mtime=None, error=None):
        with self.measure('state'), self.db:
            self.db.execute('''INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?)
              ON CONFLICT(scope,relative) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,
              sha256=excluded.sha256,status=excluded.status,remote_mtime_ns=excluded.remote_mtime_ns,
              partial_bytes=excluded.partial_bytes,updated=excluded.updated,error=excluded.error,
              remote_sha256=CASE WHEN excluded.status='done' THEN excluded.sha256 ELSE uploads.remote_sha256 END''',
              (self.s.scope,item.relative,item.size,item.mtime_ns,item.sha256,status,remote_mtime,partial,now().isoformat(),error,
               item.sha256 if status=='done' else None))
        if status=='done' and remote_mtime is not None:
            # Publication/adoption already verified these bytes. Reuse that
            # proof for subsequent predecessor archives in this upload round.
            try:
                proof = hash_stamp(native(self.s.target)/PurePosixPath(item.relative))
            except FileNotFoundError:
                proof = None
            if proof is not None and proof[2:4] == (item.size,remote_mtime):
                self.formal_proofs[(item.relative.casefold(),item.sha256)] = proof

    def destination(self, item):
        return self.destination_relative(item.relative)

    def destination_relative(self, relative):
        relative = checked_relative(relative)
        validated = set()
        target = self.remote(relative, _validated=validated)
        part = self.remote(self.partial_relative(relative), _validated=validated)
        return target, part

    def partial_relative(self, relative):
        tag = hashlib.sha256(relative.encode()).hexdigest()[:24]
        return str(PurePosixPath(relative).with_name('.fusionlive-'+self.s.scope+'-'+tag+'.part'))

    def scan_exists(self, relative):
        """Bounded per-directory name snapshots for the read-only migration pass."""
        relative = checked_relative(relative)
        path = PurePosixPath(relative)
        key = str(path.parent).casefold()
        if key not in self.scan_directories:
            self.check()
            # Validate the directory chain before listing; a matching file is
            # subsequently revalidated by the ordinary migration implementation.
            parent = self.remote(relative).parent
            try:
                with os.scandir(parent) as entries:
                    names = set()
                    for entry in entries:
                        self.check()
                        names.add(entry.name.casefold())
            except FileNotFoundError:
                names = set()
            self.scan_directories[key] = names
            if len(self.scan_directories) > 128:
                self.scan_directories.popitem(last=False)
        else:
            self.scan_directories.move_to_end(key)
        return path.name.casefold() in self.scan_directories[key]

    def previous_paths(self, item):
        paths = list(item.legacy_relatives)
        seen = {p.casefold() for p in paths}
        for previous in paths:
            for moved in self.moved_paths.get((previous.casefold(), item.sha256), ()):
                if moved.casefold() not in seen:
                    seen.add(moved.casefold())
                    paths.append(moved)
        return paths

    def invalidate_scan(self, item):
        # A move can create/remove its parent chain. Other directories remain
        # discovery hints only and need not be listed again after every move.
        affected = set()
        for relative in (item.relative, *self.previous_paths(item)):
            parent = PurePosixPath(checked_relative(relative)).parent
            affected.update(str(p).casefold() for p in (parent, *parent.parents))
        for key in list(self.scan_directories):
            if key in affected:
                del self.scan_directories[key]

    def migration_record(self, item, previous, state):
        with self.db:
            self.db.execute('''INSERT INTO path_migrations VALUES(?,?,?,?,?,?)
              ON CONFLICT(scope,old_relative,new_relative) DO UPDATE SET
              sha256=excluded.sha256,state=excluded.state,updated=excluded.updated''',
                (self.s.scope,previous,item.relative,item.sha256,state,now().isoformat()))

    def finish_migrations(self, item):
        with self.measure('migration_finish'), self.db:
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
        previous_paths = self.previous_paths(item)
        for previous in reversed(previous_paths):
            if previous.casefold() in self.protected or previous.casefold() == item.relative.casefold():
                continue
            self.check()
            old_target, old_part = self.destination_relative(previous)
            before = optional_signature(old_target)
            if before is not None:
                self.emit(item,0,True,'核对旧路径文件')
                if (before[0] != item.size or self.hash_file(old_target,item) != item.sha256):
                    if self.mirror:
                        continue  # Reconcile it only after the canonical files are ready.
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
                self.destination_relative(previous)
                self.destination(item)
                if target.exists() or signature(old_target) != before:
                    raise Conflict('迁移前NAS路径状态发生变化，未覆盖：'+item.relative)
                self.formal_ready(item)
                old_target.rename(target)
                self.save(item,'done',remote_mtime=target.stat().st_mtime_ns)
                self.migration_record(item,previous,'moved')
                self.remove_empty_parents(old_target.parent)
                self.emit(item,item.size,True,'旧路径迁移完成')
                return 'migrated',0
            old = self.db.execute('SELECT * FROM uploads WHERE scope=? AND relative=?',
                                  (self.s.scope,previous)).fetchone()
            old_part_stat = optional_signature(old_part) if old and not part.exists() else None
            if (old_part_stat is not None and old
                    and old['sha256'] == item.sha256 and old['size'] == item.size
                    and old_part_stat[0] <= item.size):
                # The normal upload loop validates every byte of this prefix.
                target.parent.mkdir(parents=True,exist_ok=True)
                self.migration_record(item,previous,'partial_planned')
                self.save(item,'uploading',old_part_stat[0])
                self.check()
                if part.exists():
                    raise Conflict('目标续传文件在迁移前出现，未覆盖。')
                self.formal_ready(item)
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

    def archive_legacy_copies(self, item):
        """Retire every proven alias, including when the canonical file exists.

        Preserve bytes in history rather than deleting duplicates. Unknown or
        concurrently modified content is a conflict, never a deletion candidate.
        """
        if item.mapping_error:
            raise Conflict(item.mapping_error)
        aliases = list(item.legacy_relatives)
        seen = {p.casefold() for p in aliases}
        for alias in aliases:
            for moved in self.moved_paths.get((alias.casefold(), item.sha256), ()):
                if moved.casefold() not in seen:
                    aliases.append(moved)
                    seen.add(moved.casefold())
        aliases = [previous for previous in aliases
                   if previous.casefold() not in self.protected
                   and previous.casefold() != item.relative.casefold()
                   and PurePosixPath(previous).parts[0] != 'history']
        if not aliases:
            return 0
        self.formal_ready(item)
        verified = False
        moved_count = 0
        for previous in aliases:
            self.check()
            source = self.remote(previous)
            before = optional_signature(source)
            if before is None:
                for row in self.db.execute('''SELECT new_relative,sha256 FROM path_migrations
                        WHERE scope=? AND old_relative=? AND state='planned' ''', (self.s.scope, previous)).fetchall():
                    if row['sha256'] != item.sha256 or not row['new_relative'].startswith('history/'):
                        continue
                    archived = self.remote(row['new_relative'])
                    if (archived.is_file() and archived.stat().st_size == item.size
                            and self.hash_file(archived, item) == item.sha256):
                        self.migration_record(replace(item, relative=row['new_relative']), previous, 'moved')
                continue
            if before[0] != item.size or self.hash_file(source, item) != item.sha256:
                if self.mirror:
                    continue
                raise Conflict('正式目录旧路径内容不匹配，保留待核对：'+previous)
            if not verified:
                target = self.remote(item.relative)
                target_before = signature(target)
                if (target_before[0] != item.size or self.hash_file(target, item) != item.sha256
                        or signature(target) != target_before or self.hash_file(item.path) != item.sha256):
                    raise Conflict('归档前目标或本地内容校验失败：'+item.relative)
                self.unchanged_source(item)
                verified = True
            relative = history_relative(item.relative, 'duplicate|'+self.s.scope+'|'+previous, item.sha256)
            archive = self.remote(relative)
            if archive.exists():
                raise Conflict('旧路径归档目标已存在，保留两份待核对：'+previous)
            archive.parent.mkdir(parents=True, exist_ok=True)
            archived = replace(item, relative=relative)
            self.migration_record(archived, previous, 'planned')
            self.check()
            self.remote(previous)
            self.remote(relative)
            if signature(source) != before or archive.exists():
                raise Conflict('归档前旧路径状态变化，保留原件：'+previous)
            self.formal_ready(item)
            source.rename(archive)
            self.migration_record(archived, previous, 'moved')
            self.remove_empty_parents(source.parent)
            moved_count += 1
        return moved_count

    def backup_managed(self, target, item, old):
        digest = old['remote_sha256']
        relative = history_relative(item.relative, 'nas|'+self.s.scope+'|'+item.relative+'|'+digest, digest)
        archive = self.remote(relative)
        with self.db:
            self.db.execute('''INSERT INTO archive_copies VALUES(?,?,?,?,?,?)
                ON CONFLICT(scope,source_relative,sha256) DO UPDATE SET
                archive_relative=excluded.archive_relative,state=excluded.state,updated=excluded.updated''',
                (self.s.scope,item.relative,digest,relative,'planned',now().isoformat()))
        if archive.exists():
            if self.hash_file(target) != digest:
                raise Conflict('远端文件在上传期间被其他程序修改，未覆盖。')
            if self.hash_file(archive) != digest:
                raise Conflict('历史备份目标已有不同内容，未覆盖：'+relative)
        else:
            archive.parent.mkdir(parents=True,exist_ok=True)
            tag = hashlib.sha256((self.s.scope+'|'+relative).encode()).hexdigest()[:24]
            partial_relative = str(PurePosixPath(relative).with_name('.fusionlive-archive-'+tag+'.part'))
            backup_part = self.remote(partial_relative)
            # Restart only this deterministic private copy; official old bytes remain intact.
            # Verify the old source while copying it. A separate full pre-copy
            # hash would reread the same NAS bytes immediately before this pass.
            copied_digest = hashlib.sha256()
            with self.measure('backup_copy'), target.open('rb') as source, backup_part.open('wb') as backup:
                while True:
                    self.check()
                    data = source.read(self.s.chunk)
                    if not data:
                        break
                    copied_digest.update(data)
                    self.nas_read_bytes += len(data)
                    backup.write(data)
                backup.flush()
                os.fsync(backup.fileno())
            if copied_digest.hexdigest() != digest:
                raise Conflict('远端文件在上传期间被其他程序修改，未覆盖。')
            if self.hash_file(backup_part) != digest:
                raise OSError('远端旧版本备份校验失败，未覆盖。')
            self.check()
            self.remote(relative)
            if archive.exists():
                raise Conflict('历史备份目标在发布前出现，保留现场：'+relative)
            backup_part.rename(archive)
        with self.db:
            self.db.execute("UPDATE archive_copies SET state='verified',updated=? WHERE scope=? AND source_relative=? AND sha256=?",
                            (now().isoformat(),self.s.scope,item.relative,digest))
        return relative

    def hash_file(self, path, item=None):
        self.check()
        key = str(path)
        temporary = path.name.startswith('.fusionlive-') and path.name.endswith('.part')
        stamp = None if temporary else hash_stamp(path)
        proof = self.hash_proofs.get(key)
        if not self.force_hash and stamp is not None and proof and proof[0] == stamp:
            self.hash_proofs.move_to_end(key)
            self.hash_cache_hits += 1
            return proof[1]
        self.hash_proofs.pop(key,None)
        def remember(value):
            after = None if temporary else hash_stamp(path)
            if stamp is not None and after != stamp:
                raise Conflict('文件在SHA256校验期间发生变化，未采用校验结果。')
            if stamp is not None and after == stamp:
                self.hash_proofs[key] = (after,value)
                if len(self.hash_proofs)>4096:self.hash_proofs.popitem(last=False)
            return value
        digest = hashlib.sha256()
        size=0
        remote = path.is_relative_to(native(self.s.target))
        if remote and self.s.remote_hash and not self.remote_hash_disabled:
            from nas_remote_hash import RemoteHasher, RemoteHashError
            if self.remote_hasher is None:
                self.remote_hasher = RemoteHasher(self.s.remote_hash,native(self.s.target),self.check)
            try:
                with self.measure('nas_hash'):
                    before = path.stat()
                    value = self.remote_hasher.hash(path.relative_to(native(self.s.target)).as_posix(),before.st_size)
                    after = path.stat()
                    fields = ('st_dev','st_ino','st_size','st_mtime_ns','st_ctime_ns')
                    if any(getattr(before,k)!=getattr(after,k) for k in fields):
                        raise Conflict('NAS文件在远端校验期间发生变化，未采用校验结果。')
                self.remote_hash_files += 1
                self.remote_hash_bytes += before.st_size
                if item:self.emit(item,before.st_size,phase='NAS本机校验')
                return remember(value)
            except (RemoteHashError,OSError) as exc:
                self.remote_hasher.close()
                raise Conflict('SSH本机校验失败，未回退SMB，保留文件及进度：'+str(exc)) from exc
        with self.measure('nas_hash' if remote else 'local_hash'), path.open('rb') as stream:
            while True:
                self.check()
                data = stream.read(self.s.chunk)
                if not data:
                    break
                digest.update(data)
                size+=len(data)
                if not remote:
                    self.phase_bytes(size)
                if remote:self.nas_read_bytes+=len(data)
                if item:self.emit(item,size,phase='回读校验')
        return remember(digest.hexdigest())

    def unchanged_source(self, item):
        if signature(item.path) != (item.size,item.mtime_ns):
            raise Conflict('本地文件在上传期间变化，保留远端正式文件：'+item.relative)

    def formal_ready(self, item, *, required=True):
        """Keep the chosen formal bytes available before retiring older copies."""
        for relative,size,digest in item.formal_guards:
            self.check()
            target = self.remote(relative)
            try:
                before = signature(target)
                content = hash_stamp(target)
                key = (relative.casefold(),digest)
                cached = (not self.force_hash and content is not None and self.formal_proofs.get(key)==content)
                valid = before[0]==size and (cached or
                    (self.hash_file(target)==digest and signature(target)==before))
            except FileNotFoundError:
                valid = False
            if not valid:
                if required:
                    raise Conflict('待保留的NAS正式副本尚未校验就绪，保留更早旧版：'+relative)
                return False
            if content is not None:self.formal_proofs[key] = content
        return True

    def emit(self, item, count, force=False, phase='上传'):
        at = time.monotonic()
        if self.progress and (force or at-self.last_progress>=30):
            self.progress({'phase':phase,'file':item.relative,'current_bytes':count,'current_total':item.size,
                           'sent_bytes':self.wire,'average_MBps':self.wire/max(at-self.started,0.001)/1e6,
                           'nas_read_bytes':self.nas_read_bytes,'timings_seconds':dict(self.timings)})
            self.last_progress = at

    def one(self, item, verify_all=False):
        previous = self.force_hash
        self.force_hash = previous or verify_all
        try:
            return self._one(item,verify_all)
        finally:
            self.force_hash = previous

    def _one(self, item, verify_all=False):
        self.current_file = item.relative
        self.activity = ('file', time.monotonic())
        self.check()
        if item.mapping_error:
            raise Conflict(item.mapping_error)
        self.formal_ready(item)
        self.unchanged_source(item)
        old = self.row(item)
        same_source = bool(old and (old['size'],old['mtime_ns'],old['sha256'])==(item.size,item.mtime_ns,item.sha256))
        if not verify_all and same_source and old['status']=='done':
            # Confirm the real destination live, but do not inspect a partial
            # path or old aliases when this exact file is already confirmed.
            cached_target = self.remote(item.relative)
            try:
                cached_stat = signature(cached_target)
            except FileNotFoundError:
                cached_stat = None
            if cached_stat == (item.size,old['remote_mtime_ns']):
                self.finish_migrations(item)
                return 'skipped',0
        target, part = self.destination(item)
        migrated = self.restore_legacy(item,target,part)
        if migrated:
            return migrated
        # A partial-path migration may have created the canonical upload row.
        # Refresh it before deciding how many already stored bytes to resume.
        old = self.row(item)
        same_source = bool(old and (old['size'],old['mtime_ns'],old['sha256'])==(item.size,item.mtime_ns,item.sha256))
        target_stat = optional_signature(target)
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
                if not self.mirror:
                    raise Conflict('远端同名文件与本地不同且不能确认归本任务管理，未覆盖：'+item.relative)
                # A full mirror uses the verified local manifest as authority.
                # Preserve the actual remote bytes, including unmanaged edits.
                old = dict(old or {}, remote_sha256=remote_hash)
        target.parent.mkdir(parents=True,exist_ok=True)
        # Only our deterministic temporary filename is reused/truncated; never arbitrary NAS files.
        part_stat = optional_signature(part) if same_source else None
        resume = part_stat[0] if part_stat is not None and part_stat[0]<=item.size else 0
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
            with self.measure('upload'), part.open('ab') as output:
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
        self.emit(item,0,True,'回读NAS校验')
        if part.stat().st_size!=item.size or self.hash_file(part,item)!=item.sha256:
            raise OSError('NAS回读SHA256校验失败。')
        self.check()
        self.unchanged_source(item)
        # Recheck aliases after the long transfer, before publication.
        if target_stat:
            self.destination(item)
            # Keep the previous managed version before replacing it.
            if self.s.preserve or self.mirror:
                self.backup_managed(target,item,old)
            elif self.hash_file(target)!=old['remote_sha256']:
                raise Conflict('远端文件在上传期间被其他程序修改，未覆盖。')
            if signature(target)!=target_stat:
                raise Conflict('远端文件在上传期间变化，未覆盖。')
            self.destination(item)
            with self.measure('publish'):
                os.replace(part,target)
        else:
            # On Windows rename does not overwrite a file created by another process meanwhile.
            self.destination(item)
            with self.measure('publish'):
                part.rename(target)
        try:
            with self.measure('publish'):
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
        # Existing destinations belong to the upload/adoption pass. Most files
        # need no migration; avoid per-file SMB stat calls and guard hashing here.
        previous_paths = [previous for previous in self.previous_paths(item)
                          if previous.casefold() not in self.protected
                          and previous.casefold() != item.relative.casefold()]
        if not previous_paths:
            return None
        if self.scan_exists(item.relative):
            return None
        possible = False
        for previous in reversed(previous_paths):
            if self.scan_exists(previous) or self.scan_exists(self.partial_relative(previous)):
                possible = True
                break
        if not possible:
            return None
        if not self.formal_ready(item,required=False):
            return None  # Retry after formal files are ready in the upload pass.
        self.unchanged_source(item)
        try:
            target, part = self.destination(item)
            return self.restore_legacy(item, target, part)
        finally:
            # Includes partial moves and interrupted moves. Refresh affected
            # directories and their ancestors, preserving unrelated snapshots.
            self.invalidate_scan(item)


class DirectoryMirror:
    """Reconcile a complete logical download manifest, retaining displaced bytes.

    The sibling archive is outside target_root. Journal intent before each
    atomic rename so interruption never loses the original relative path.
    A live SQLite connection detects ledger commits throughout the round.
    """

    def __init__(self, settings):
        self.s = settings
        if os.name == 'nt' and str(settings.target).rstrip('\\') == PureWindowsPath(str(settings.target)).anchor.rstrip('\\'):
            raise ValueError('目录镜像要求target_root为共享内的专用子目录。')
        self.ledger = sqlite3.connect(settings.ledger.as_uri()+'?mode=ro', uri=True, timeout=15)
        self.version = self.ledger.execute('PRAGMA data_version').fetchone()[0]
        self.archive_settings = replace(settings, target=settings.target.with_name(settings.target.name+'.nas-archive'))
        self.files, self.directories = {}, {}
        self.archived = self.renamed = 0
        self.incomplete = False

    def close(self):
        self.ledger.close()

    def check(self):
        if ((self.s.ledger.parent/'layout.pending').exists()
                or self.ledger.execute('PRAGMA data_version').fetchone()[0] != self.version):
            raise Stopped('同步台账或目录在本轮发生变化，暂停目录镜像，下一轮重新核对。')

    def configure(self, uploader, items, ignored):
        self.u = uploader
        if ignored:
            raise Stopped(f'存在 {ignored} 条源范围内记录无法核实，暂停本轮上传和目录镜像；具体原因及路径见本轮报告。')

        def directory(relative):
            for path in reversed((PurePosixPath(relative), *PurePosixPath(relative).parents)):
                if str(path) == '.':
                    continue
                text = checked_relative(str(path))
                key = text.casefold()
                if key in self.directories and self.directories[key] != text:
                    raise Conflict('同步台账的文件夹大小写不一致：'+text)
                self.directories[key] = text

        for item in items:
            if item.mapping_error:
                raise Conflict(item.mapping_error)
            text = checked_relative(item.relative)
            key = text.casefold()
            if key in self.files:
                raise Conflict('同步台账目标文件重复：'+text)
            self.files[key] = item
            directory(str(PurePosixPath(text).parent))

        # Include registered empty folders, not merely parents of downloaded files.
        tables = {r[0] for r in self.ledger.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        scoped_folder = False
        if 'folders' in tables:
            prefix = self.s.source.resolve().relative_to((self.s.ledger.parent.parent/'downloads').resolve()).parts
            columns = {r[1] for r in self.ledger.execute('PRAGMA table_info(folders)')}
            query = 'SELECT path FROM folders' + (' WHERE active=1' if 'active' in columns else '')
            for (folder,) in self.ledger.execute(query):
                parts = str(folder).replace('\\','/').split('/')
                if tuple(p.casefold() for p in parts[:len(prefix)]) != tuple(p.casefold() for p in prefix):
                    continue
                scoped_folder = True
                if len(parts) > len(prefix):
                    directory('/'.join(parts[len(prefix):]))
        if not items and not scoped_folder:
            raise Stopped('同步清单为空且没有登记的源目录，暂停目录镜像。')
        if set(self.files) & set(self.directories):
            raise Conflict('同步台账同一路径同时登记为文件和文件夹。')
        # A registered file still awaiting download is not a surplus NAS file.
        # Allow the existing verified predecessor selected by formal_downloads;
        # otherwise postpone whole-tree cleanup until its bytes are available.
        self.ledger.row_factory = sqlite3.Row
        rows = [dict(r) for r in self.ledger.execute('SELECT * FROM files')]
        pending = [r for r in rows if r.get('active',1) and r['status'] != 'done']
        if pending:
            histories = [dict(r) for r in self.ledger.execute('SELECT * FROM file_history')]
            documents = [dict(r) for r in self.ledger.execute('SELECT * FROM documents')] if 'documents' in tables else []
            selected,_ = formal_downloads(rows,histories,documents,self.s.ledger.parent.parent,self.u.check)
            available = {plain(item.path).casefold() for item in items if not item.relative.startswith('history/')}
            covered = {(owner['project'],owner['id']) for path,owner in (selected or {}).items()
                       if plain(native(self.s.ledger.parent.parent/path)).casefold() in available}
            folders = {(str(r['project']),str(r['id'])):r['path']
                       for r in self.ledger.execute('SELECT * FROM folders')} if 'folders' in tables else {}
            prefix = self.s.source.resolve().relative_to((self.s.ledger.parent.parent/'downloads').resolve()).as_posix().casefold()
            for row in pending:
                folder = folders.get((str(row.get('project')),str(row.get('folder_id'))))
                if folder:
                    text = folder.replace('\\','/').casefold()
                    if text != prefix and not text.startswith(prefix+'/'):
                        continue
                if (row.get('project'),row.get('id')) not in covered:
                    self.incomplete = True
        if self.incomplete:
            raise Stopped('仍有登记文件尚未下载且无已校验的保留副本，暂停本轮上传和目录镜像，等待同步完成。')
        self.check()
        # Validate every local source before displacing even a path blocker.
        self.u.begin_phase('本地预检',len(items),sum(item.size for item in items))
        for item in items:
            self.u.check()
            self.u.phase_file(item.relative,item.size)
            self.u.unchanged_source(item)
            if self.u.hash_file(item.path) != item.sha256:
                raise Conflict('本地SHA256与下载台账不符，暂停目录镜像：'+item.relative)
            self.u.unchanged_source(item)
            self.u.phase_advance(item.size)
        self.check()
        self.u.finish_phase()
        self.u.db.execute('''CREATE TABLE IF NOT EXISTS mirror_archives(
            id TEXT PRIMARY KEY,scope TEXT,source_relative TEXT,archive_path TEXT,
            kind TEXT,sha256 TEXT,state TEXT,updated TEXT)''')
        self.u.db.commit()
        self.u.mirror = True

    @staticmethod
    def stamp(path):
        info = path.lstat()
        return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def inventory(self):
        self.u.begin_phase('NAS目录扫描')
        result = {}
        root = native(self.s.target)
        self.u.remote('__mirror_root_check__')  # Validate the root, even when empty.
        if not root.exists():
            self.u.finish_phase()
            return result
        pending = [root]
        while pending:
            parent = pending.pop()
            self.u.check(); self.check()
            with os.scandir(parent) as entries:
                for entry in entries:
                    relative = Path(entry.path).relative_to(root).as_posix()
                    path = self.u.remote(relative)  # Reject links and junctions before traversal.
                    self.u.phase_file(relative)
                    stamp = self.stamp(path)
                    is_dir = stat.S_ISDIR(stamp[2])
                    if not is_dir and not stat.S_ISREG(stamp[2]):
                        raise Conflict('NAS路径不是普通文件或文件夹：'+relative)
                    key = relative.casefold()
                    if key in result:
                        raise Conflict('NAS存在仅大小写不同的重复路径：'+relative)
                    result[key] = (relative, is_dir, stamp)
                    self.u.phase_advance()
                    if is_dir:
                        pending.append(path)
        self.u.finish_phase()
        return result

    def archive(self, node):
        relative, is_dir, stamp = node
        self.u.check(); self.check()
        source = self.u.remote(relative)
        if self.stamp(source) != stamp:
            raise Conflict('NAS文件在目录核对后变化，未移动：'+relative)
        digest = None if is_dir else self.u.hash_file(source)
        token = uuid.uuid4().hex
        archive_relative = token+'/'+relative
        archive = safe_remote(self.archive_settings, archive_relative)
        archive.parent.mkdir(parents=True, exist_ok=True)
        with self.u.db:
            self.u.db.execute('INSERT INTO mirror_archives VALUES(?,?,?,?,?,?,?,?)',
                (token,self.s.scope,relative,plain(archive),'directory' if is_dir else 'file',
                 digest,'planned',now().isoformat()))
        self.u.check(); self.check()
        source = self.u.remote(relative)
        archive = safe_remote(self.archive_settings, archive_relative)
        if self.stamp(source) != stamp or archive.exists():
            raise Conflict('目录归档前路径状态变化，保留现场：'+relative)
        source.rename(archive)  # Same share, unique destination; never delete bytes.
        with self.u.db:
            self.u.db.execute("UPDATE mirror_archives SET state='moved',updated=? WHERE id=?", (now().isoformat(),token))
        self.archived += 1

    def prepare(self):
        """Retire file/directory type blockers before uploading canonical files."""
        nodes = self.inventory()
        # All nodes were validated before the first rename, including descendants.
        blockers = [n for key,n in nodes.items()
                    if (key in self.files and n[1]) or (key in self.directories and not n[1])]
        for node in sorted(blockers, key=lambda n:len(PurePosixPath(n[0]).parts)):
            if self.u.remote(node[0]).exists():
                self.archive(node)
        self.ensure_directories()
        for key,node in nodes.items():
            if key not in self.files or node[1]:
                continue
            item = self.files[key]
            # Parent casing may already have changed above. Correct the leaf
            # before upload so case-sensitive NAS exports cannot gain duplicates.
            source_relative = str(PurePosixPath(item.relative).with_name(PurePosixPath(node[0]).name))
            if source_relative != item.relative:
                self.u.check(); self.check()
                source = self.u.remote(source_relative)
                if self.stamp(source) != node[2]:
                    raise Conflict('NAS文件在更正名称之前变化：'+source_relative)
                source.rename(self.u.remote(item.relative))
                self.renamed += 1

    def ensure_directories(self):
        root = native(self.s.target)
        self.u.remote('__mirror_root_check__')
        root.mkdir(parents=True, exist_ok=True)
        for relative in sorted(self.directories.values(), key=lambda r:(len(PurePosixPath(r).parts),r)):
            self.u.check(); self.check()
            target = self.u.remote(relative)
            with os.scandir(target.parent) as entries:
                matches = [entry.name for entry in entries if entry.name.casefold() == target.name.casefold()]
            if len(matches) > 1:
                raise Conflict('NAS存在仅大小写不同的文件夹：'+relative)
            if matches and matches[0] != target.name:
                source = self.u.remote(str(PurePosixPath(relative).with_name(matches[0])))
                source.rename(target)
                self.renamed += 1
            else:
                target.mkdir(exist_ok=True)

    def reconcile(self):
        """Only retire surplus entries after ALL canonical bytes are verified."""
        self.u.check(); self.check()
        self.u.begin_phase('正式文件核验',len(self.files),sum(item.size for item in self.files.values()))
        for item in self.files.values():
            self.u.check()
            self.u.phase_file(item.relative,item.size)
            self.u.unchanged_source(item)
            if self.u.hash_file(item.path) != item.sha256:
                raise Conflict('本地文件在目录镜像期间变化：'+item.relative)
            target = self.u.remote(item.relative)
            before = self.stamp(target)
            if (not stat.S_ISREG(before[2]) or before[3] != item.size
                    or self.u.hash_file(target) != item.sha256 or self.stamp(target) != before):
                raise Conflict('目录镜像前正式文件校验失败：'+item.relative)
            self.u.phase_advance(item.size)
        self.u.finish_phase()
        nodes = self.inventory()
        # Files first: every byte goes to the external archive; only empty
        # obsolete directories are removed. No recursive deletion is used.
        surplus=[node for key,node in nodes.items() if not node[1] and key not in self.files]
        self.u.begin_phase('多余文件归档',len(surplus))
        for node in surplus:
            self.u.phase_file(node[0],node[2][3])
            self.archive(node)
            self.u.phase_advance(node[2][3])
        self.u.finish_phase()
        self.u.begin_phase('多余空目录清理')
        for key,node in sorted(nodes.items(),key=lambda pair:len(PurePosixPath(pair[1][0]).parts),reverse=True):
            if node[1] and key not in self.directories:
                self.u.check(); self.check()
                self.u.phase_file(node[0])
                self.u.remote(node[0]).rmdir()
                self.u.phase_advance()
        self.u.finish_phase()
        self.ensure_directories()
        nodes = self.inventory()
        for key,item in self.files.items():
            relative, is_dir, stamp = nodes[key]
            if relative != item.relative:
                self.u.check(); self.check()
                source = self.u.remote(relative)
                if self.stamp(source) != stamp:
                    raise Conflict('NAS文件在更正文件名之前变化：'+relative)
                source.rename(self.u.remote(item.relative))
                self.renamed += 1
        final = self.inventory()
        expected = {key:(item.relative,False) for key,item in self.files.items()}
        expected.update({key:(text,True) for key,text in self.directories.items()})
        if {key:node[:2] for key,node in final.items()} != expected:
            raise Conflict('NAS目录在整理期间发生变化，尚未与同步清单完全一致。')
        self.u.begin_phase('镜像最终校验',len(self.files),sum(item.size for item in self.files.values()))
        for item in self.files.values():
            self.u.check()
            self.u.phase_file(item.relative,item.size)
            target = self.u.remote(item.relative)
            before = self.stamp(target)
            if (before[3] != item.size or self.u.hash_file(target) != item.sha256
                    or self.stamp(target) != before):
                raise Conflict('NAS正式文件在目录整理期间变化：'+item.relative)
            self.u.phase_advance(item.size)
        self.check()
        self.u.finish_phase()
        return dict(archived=self.archived,renamed=self.renamed,
                    files=len(self.files),directories=len(self.directories),
                    archive_root=str(self.archive_settings.target))


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
