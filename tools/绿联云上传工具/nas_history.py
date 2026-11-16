"""Explicit, previewable migration of old NAS-only history; never scan current files."""
import os
from pathlib import Path, PurePosixPath
import re

from nas_sync import (Candidate, Conflict, HISTORY_MARKER, VERSION, checked_relative,
                      history_relative, native, now, safe_remote, signature)

BATCH = re.compile(r'^(version|inactive|untracked|retained)-')
STAMP = re.compile(r'^\d{8}-\d{6}-\d{6}$')


def _source(relative):
    parts = PurePosixPath(checked_relative(relative)).parts
    if len(parts) < 3:
        raise Conflict('历史路径层级不足：'+relative)
    if parts[0] == '.fusionlive_upload_history':
        if not STAMP.fullmatch(parts[1]):
            raise Conflict('备份批次名称无法确认：'+relative)
        return '/'.join(parts[2:])
    if parts[0] == 'history':
        return '/'.join(parts[2:] if BATCH.match(parts[1]) else parts[1:])
    raise Conflict('迁移源不属于历史目录：'+relative)


def _platform(relative, trusted=False):
    parts = PurePosixPath(relative).parts
    if parts and parts[0] == 'history':
        parts = parts[1:]
        if parts and BATCH.match(parts[0]):
            parts = parts[1:]
    if not parts or (not trusted and any(
            p.casefold() in ('long_path','_long_paths','longpath','longpaths') or re.search(r'__\d+$',p)
            for p in parts[:-1])):
        raise Conflict('没有可靠的完整平台目录映射：'+relative)
    return checked_relative('/'.join(parts))


def _references(uploader, items):
    refs = {}
    for item in items:
        if item.mapping_error:
            continue
        aliases = [item.relative, *item.legacy_relatives]
        seen = {a.casefold() for a in aliases}
        for alias in aliases:
            for moved in uploader.moved_paths.get((alias.casefold(),item.sha256), ()):
                if moved.casefold() not in seen:
                    seen.add(moved.casefold())
                    aliases.append(moved)
        for alias in aliases:
            refs.setdefault((alias.casefold(),item.sha256),set()).add(_platform(item.relative,True))
    return refs


def _resolve(uploader, refs, origin, digest):
    matches = refs.get((origin.casefold(),digest),set())
    if len(matches) == 1:
        return next(iter(matches))
    if len(matches) > 1:
        raise Conflict('同一路径和内容对应多个版本，保留原件：'+origin)
    old = uploader.db.execute('''SELECT sha256,remote_sha256 FROM uploads
        WHERE scope=? AND relative=? COLLATE NOCASE''',(uploader.s.scope,origin)).fetchone()
    if not old or digest not in (old['sha256'],old['remote_sha256']):
        raise Conflict('缺少路径及SHA256相符的台账依据，保留原件：'+origin)
    visited = set()
    while origin.casefold() not in visited:
        visited.add(origin.casefold())
        following = set(uploader.moved_paths.get((origin.casefold(),digest),()))
        if not following:
            return _platform(origin)
        if len(following) != 1:
            raise Conflict('旧路径迁移记录有歧义：'+origin)
        origin = following.pop()
    raise Conflict('旧路径迁移记录存在循环：'+origin)


def plan(uploader, items):
    refs = _references(uploader,items)
    entries = []
    root = native(uploader.s.target)
    for top in ('.fusionlive_upload_history','history'):
        try:
            folder = safe_remote(uploader.s,top)
        except (Conflict,OSError) as exc:
            entries.append(dict(source=top,status='conflict',error=str(exc)))
            continue
        if not folder.exists():
            continue
        if not folder.is_dir():
            entries.append(dict(source=top,status='conflict',error='历史入口不是目录。'))
            continue
        def walk_error(exc):
            entries.append(dict(source=str(exc.filename),status='conflict',error=str(exc)))
        for parent, dirs, files in os.walk(folder,followlinks=False,onerror=walk_error):
            uploader.check()
            for name in list(dirs):
                rel = (Path(parent)/name).relative_to(root).as_posix()
                try:
                    safe_remote(uploader.s,rel)
                except Conflict as exc:
                    dirs.remove(name)
                    entries.append(dict(source=rel,status='conflict',error=str(exc)))
            for name in files:
                source = (Path(parent)/name).relative_to(root).as_posix()
                if top == 'history' and HISTORY_MARKER.search(PurePosixPath(name).stem):
                    continue
                entry = dict(source=source,status='conflict')
                try:
                    uploader.check()
                    path = safe_remote(uploader.s,source)
                    if name.lower().endswith(('.part','.tmp')):
                        raise Conflict('未完成或未知临时文件，保留原件。')
                    origin = _source(source)
                    before = signature(path)
                    digest = uploader.hash_file(path)
                    if signature(path) != before:
                        raise Conflict('预览校验期间文件变化，保留原件。')
                    platform = _resolve(uploader,refs,origin,digest)
                    target = history_relative(platform,'legacy|'+uploader.s.scope+'|'+source,digest,False)
                    destination = safe_remote(uploader.s,target)
                    if destination.exists():
                        raise Conflict('目标已存在，保留两份文件并等待核对：'+target)
                    entry.update(target=target,sha256=digest,size=before[0],mtime_ns=before[1],status='ready')
                except (Conflict,ValueError,OSError) as exc:
                    entry['error'] = str(exc)
                entries.append(entry)
    seen = {e['source'] for e in entries}
    for journal in uploader.db.execute("SELECT * FROM path_migrations WHERE scope=? AND state='planned'",(uploader.s.scope,)):
        if journal['old_relative'] in seen:
            continue
        try:
            _source(journal['old_relative'])
            target = safe_remote(uploader.s,journal['new_relative'])
            source = safe_remote(uploader.s,journal['old_relative'])
            if source.exists() or not target.is_file():
                continue
            size,mtime = signature(target)
            entries.append(dict(source=journal['old_relative'],target=journal['new_relative'],
                size=size,mtime_ns=mtime,sha256=journal['sha256'],status='ready'))
        except (Conflict,ValueError,OSError) as exc:
            entries.append(dict(source=journal['old_relative'],status='conflict',error=str(exc)))
    return dict(format=1,version=VERSION,scope=uploader.s.scope,target_root=str(uploader.s.target),
                created=now().isoformat(),entries=entries)


def apply(uploader, proposal):
    if (proposal.get('format') != 1 or proposal.get('scope') != uploader.s.scope or
            proposal.get('target_root') != str(uploader.s.target)):
        raise Conflict('预览清单与当前NAS配置不一致，请重新预览。')
    results = []
    for entry in proposal['entries']:
        result = dict(entry)
        if entry.get('status') != 'ready':
            results.append(result)
            continue
        try:
            uploader.check()
            _source(entry['source'])
            parts = PurePosixPath(checked_relative(entry['target'])).parts
            if parts[0] != 'history' or not HISTORY_MARKER.search(PurePosixPath(parts[-1]).stem):
                raise Conflict('目标不是带身份标识的统一历史路径。')
            if not re.fullmatch('[0-9a-f]{64}',entry['sha256']):
                raise Conflict('预览清单SHA256无效。')
            source = safe_remote(uploader.s,entry['source'])
            target = safe_remote(uploader.s,entry['target'])
            item = Candidate(source,entry['target'],entry['size'],entry['mtime_ns'],entry['sha256'])
            if target.exists():
                journal = uploader.db.execute('''SELECT sha256,state FROM path_migrations
                    WHERE scope=? AND old_relative=? AND new_relative=?''',
                    (uploader.s.scope,entry['source'],entry['target'])).fetchone()
                if (not source.exists() and journal and journal['sha256'] == item.sha256 and
                        target.stat().st_size == item.size and uploader.hash_file(target) == item.sha256):
                    uploader.migration_record(item,entry['source'],'moved')
                    result['status'] = 'recovered'
                else:
                    raise Conflict('目标已存在，未覆盖也未删除原文件。')
            else:
                if signature(source) != (item.size,item.mtime_ns) or uploader.hash_file(source) != item.sha256:
                    raise Conflict('源文件与预览不符，请重新核对。')
                target.parent.mkdir(parents=True,exist_ok=True)
                uploader.migration_record(item,entry['source'],'planned')
                uploader.check()
                safe_remote(uploader.s,entry['source'])
                safe_remote(uploader.s,entry['target'])
                if target.exists() or signature(source) != (item.size,item.mtime_ns):
                    raise Conflict('迁移前路径状态变化，保留原文件。')
                source.rename(target)
                uploader.migration_record(item,entry['source'],'moved')
                uploader.remove_empty_parents(source.parent)
                result['status'] = 'moved'
        except (Conflict,ValueError,OSError) as exc:
            result.update(status='conflict',error=str(exc))
        results.append(result)
    return dict(proposal,finished=now().isoformat(),entries=results)
