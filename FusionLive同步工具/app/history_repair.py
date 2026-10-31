"""Merge historical batch directories without moving or rewriting current files."""
from app_paths import HOME
from contextlib import closing, ExitStack
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys

from file_layout import (LayoutError, MigrationProgress, ReadLedger, checksum,
                         confined, component, length, LIMIT, publish)
from fusion_client import now

BATCH = re.compile(r'^(version|inactive|untracked)-')


def guard(ledger):
    if (ledger.base_dir/'runtime/layout.pending').exists():
        raise LayoutError('原目录迁移尚未完成，请先完成原迁移，再修复历史目录。')
    if ledger.one("SELECT 1 FROM sqlite_master WHERE name='layout_moves'"):
        if ledger.one("SELECT 1 FROM layout_moves WHERE state!='done'"):
            raise LayoutError('原目录迁移仍有未完成文件，请先完成原迁移。')


def plan(ledger, progress=None, *, extra_sources=None, exclude=()):
    guard(ledger)
    base = ledger.base_dir
    extra_sources = extra_sources or {}
    excluded = {p.casefold() for p in exclude}
    folders = {(str(r['project']), str(r['id'])): r['path']
               for r in ledger.all('SELECT project,id,path FROM folders')}
    by_id = {}
    for (_, fid), path in folders.items():
        by_id.setdefault(fid, set()).add(path)
    parents = {}
    if ledger.one("SELECT 1 FROM sqlite_master WHERE name='local_paths'"):
        for row in ledger.all('SELECT * FROM local_paths'):
            key = str(PurePosixPath(row['local_path']).parent)
            parents.setdefault(key, set()).add(str(PurePosixPath(row['platform_path']).parent))
    references = {}
    for row in ledger.all('SELECT path,bytes,sha256 FROM file_history WHERE path IS NOT NULL'):
        references.setdefault(row['path'], []).append(dict(row))
    for row in ledger.all('SELECT path,bytes,sha256,active FROM files WHERE path IS NOT NULL'):
        if row['active'] and row['path'].startswith('history/') and row['path'].casefold() not in excluded:
            raise LayoutError('当前有效文件引用了history，需先核对：'+row['path'])
        if not row['active']:
            references.setdefault(row['path'], []).append(dict(row))
    sources = []
    if progress:
        progress.phase('扫描历史批次目录')
    for parent, dirs, names in os.walk(base/'history', followlinks=False):
        for name in [*dirs, *names]:
            relative = (Path(parent)/name).relative_to(base).as_posix()
            confined(base, relative)
        for name in names:
            relative = (Path(parent)/name).relative_to(base).as_posix()
            parts = PurePosixPath(relative).parts
            if len(parts) >= 4 and BATCH.match(parts[1]) and relative.casefold() not in excluded:
                sources.append(relative)
    sources.extend(source for source in extra_sources if source.casefold() not in excluded)
    reserved, moves, parent_owners = set(), [], {}
    entries, longest = [], {}
    for source in sorted(sources):
        if progress:
            progress.update(done=len(moves), total=len(sources), current=source)
        parts = PurePosixPath(source).parts
        original = parts[2:]
        parent = '/'.join(original[:-1])
        # Old paths encode the original folder ID; never drop the versioned name.
        folder_id = None
        if len(original) == 3 and original[0].casefold() in ('_long_paths', 'longpath', 'longpaths'):
            folder_id = original[1]
        elif match := re.search(r'__(\d+)$', original[-2]):
            folder_id = match[1]
        matches = by_id.get(folder_id, set())
        mapped = matches or parents.get('downloads/'+parent, set())
        if len(mapped) > 1:
            raise LayoutError('历史目录映射存在歧义：'+source)
        if mapped:
            parent = next(iter(mapped))
        elif original[0].casefold() in ('long_path', '_long_paths', 'longpath', 'longpaths'):
            raise LayoutError('无法恢复历史短路径的原目录，保留原文件：'+source)
        folder = tuple(component(p) for p in PurePosixPath(parent).parts)
        filename = component(original[-1])
        platform_path = PurePosixPath(parent, original[-1]).as_posix()
        token = parts[1]
        budget_name = Path(filename).stem+'__'+token+'-'+str(len(sources))+Path(filename).suffix
        if parent not in longest or length(budget_name) > length(longest[parent]):
            longest[parent] = budget_name
        entries.append((source, parent, folder, filename, platform_path, token))
    for source, parent, folder, filename, platform_path, token in entries:
        if progress:
            progress.update(done=len(moves), total=len(entries), current=source)
        target = Path('history', *folder, filename)
        # Keep one shared folder per platform directory even for Windows overflow.
        # Reserve room for a stable version suffix before choosing the parent.
        suffix = '__'+token
        numbered = longest[parent]
        if length(base/target.with_name(numbered)) > LIMIT:
            for cut in range(1, len(folder)):
                shortened = Path('history', 'long_path', *folder[cut:], filename)
                if length(base/shortened.with_name(numbered)) <= LIMIT:
                    target = shortened
                    break
            else:
                raise LayoutError('历史文件名及接续目录过长，请缩短安装根目录：'+source)
        candidate = target
        number = 0
        while candidate.as_posix().casefold() in reserved or confined(base, candidate.as_posix()).exists():
            extra = suffix + (('-'+str(number)) if number else '')
            candidate = target.with_name(Path(filename).stem+extra+Path(filename).suffix)
            number += 1
        if length(base/candidate) > LIMIT:
            raise LayoutError('历史同名区分后的路径过长：'+str(candidate))
        # Distinct platform parents must never silently merge after shortening.
        parent_key = str(candidate.parent).casefold()
        if parent_key in parent_owners and parent_owners[parent_key] != parent:
            raise LayoutError('历史短目录合并冲突：'+parent)
        parent_owners[parent_key] = parent
        reserved.add(candidate.as_posix().casefold())
        physical_source = extra_sources.get(source, source)
        physical = confined(base, physical_source)
        size = physical.stat().st_size
        refs = references.get(physical_source, [])
        hashes = {r['sha256'] for r in refs if r['sha256']}
        if len(hashes) > 1 or any(r['bytes'] is not None and r['bytes'] != size for r in refs):
            raise LayoutError('历史文件与台账记录冲突：'+source)
        moves.append(dict(source=source, target=candidate.as_posix(), platform_path=platform_path,
                          bytes=size, sha256=next(iter(hashes), None)))
    return dict(format=1, policy='history-shared-folders-v1', moves=moves)


def apply(ledger, proposal=None, progress=None, cancelled=lambda: False):
    guard(ledger)
    db, base = ledger.db, ledger.base_dir
    if not ledger.one("SELECT 1 FROM sqlite_master WHERE name='history_repairs'"):
        proposal = proposal or plan(ledger, progress)
    pending = ledger.all("SELECT * FROM history_repairs WHERE state!='done' ORDER BY source") if ledger.one(
        "SELECT 1 FROM sqlite_master WHERE name='history_repairs'") else []
    if not pending:
        proposal = proposal or plan(ledger, progress)
        if not proposal['moves']:
            return 0
        backup = base/'runtime/backups'/('before-history-repair-'+now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
        backup.parent.mkdir(parents=True, exist_ok=True)
        if progress:
            progress.phase('备份历史修复前台账')
        with closing(sqlite3.connect(backup)) as target:
            db.backup(target)
    db.execute('CREATE TABLE IF NOT EXISTS history_repairs(source TEXT PRIMARY KEY,payload TEXT,state TEXT)')
    db.execute('CREATE TABLE IF NOT EXISTS history_paths(path TEXT PRIMARY KEY,platform_path TEXT,original_path TEXT)')
    with db:
        if not pending:
            for move in proposal['moves']:
                db.execute('INSERT OR REPLACE INTO history_repairs VALUES(?,?,?)',
                           (move['source'], json.dumps(move, ensure_ascii=False), 'pending'))
    jobs = ledger.all("SELECT * FROM history_repairs WHERE state!='done' ORDER BY source")
    if progress:
        progress.phase('校验并合并历史文件（不处理downloads）', len(jobs))
    for index, job in enumerate(jobs, 1):
        if cancelled():
            raise LayoutError('历史修复已停止，可再次执行继续。')
        move = json.loads(job['payload'])
        if not move['source'].startswith('history/') or not move['target'].startswith('history/'):
            raise LayoutError('修复记录超出history范围。')
        if progress:
            progress.update(done=index-1, current=move['source'])
        source, target = (confined(base, move[k]) for k in ('source', 'target'))
        if source.exists():
            actual = checksum(source, cancelled, progress)
            if source.stat().st_size != move['bytes'] or (move['sha256'] and actual != move['sha256']):
                raise LayoutError('历史源文件校验失败：'+move['source'])
            move['sha256'] = actual
            with db:
                db.execute('UPDATE history_repairs SET payload=? WHERE source=?',
                           (json.dumps(move, ensure_ascii=False), move['source']))
        if target.exists():
            if (not move['sha256'] or target.stat().st_size != move['bytes']
                    or checksum(target, cancelled, progress) != move['sha256']):
                raise LayoutError('历史目标内容冲突，保留原件：'+move['target'])
            if source.exists():
                source.unlink()
        elif source.exists():
            publish(source, target, move['sha256'], cancelled, progress)
        else:
            raise LayoutError('历史源与目标均缺失：'+move['source'])
        with db:
            db.execute('UPDATE file_history SET path=? WHERE path=?',(move['target'],move['source']))
            db.execute('UPDATE files SET path=? WHERE path=? AND active=0',(move['target'],move['source']))
            db.execute('INSERT OR REPLACE INTO history_paths VALUES(?,?,?)',
                       (move['target'],move['platform_path'],move['source']))
            db.execute("UPDATE history_repairs SET state='done' WHERE source=?",(move['source'],))
        if progress:
            progress.update(done=index)
    # Only remove empty directories beneath history; downloads is never traversed.
    for parent, _, _ in os.walk(base/'history', topdown=False, followlinks=False):
        if Path(parent) != base/'history':
            try:
                Path(parent).rmdir()
            except OSError:
                pass
    return len(jobs)


def main(argv=None):
    parser = argparse.ArgumentParser(description='仅合并history批次目录；不迁移downloads。默认只预览。')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--base-dir', type=Path, default=HOME)
    args = parser.parse_args(argv)
    base = args.base_dir.resolve()
    database = base/'runtime/ledger.sqlite3'
    if not database.is_file():
        raise LayoutError('未找到下载台账，未执行历史修复。')
    from analysis_worker import worker_lock
    with ExitStack() as stack:
        progress = stack.enter_context(MigrationProgress(base))
        if args.apply:
            for folder, name in ((base/'runtime','scheduler.lock'), (base/'runtime','sync.lock'),
                                 (base/'runtime/file_analysis','worker.lock'),
                                 (base/'runtime/wechat_delivery','worker.lock'),
                                 (base/'runtime','history_repair.lock')):
                if not stack.enter_context(worker_lock(folder, name)):
                    raise LayoutError('请先停止当前任务再修复历史目录：'+str(folder/name))
        db = stack.enter_context(closing(sqlite3.connect(database if args.apply else database.as_uri()+'?mode=ro',uri=not args.apply)))
        db.row_factory = sqlite3.Row
        ledger = ReadLedger(db,base)
        if args.apply:
            count = apply(ledger, progress=progress)
            progress.message(f'历史目录修复完成：{count} 个文件；downloads未改动。')
        else:
            proposal = plan(ledger,progress)
            output = base/'runtime/history_repair_preview.json'
            output.write_text(json.dumps(proposal,ensure_ascii=False,indent=2),encoding='utf-8')
            progress.message(f'历史修复预览：{len(proposal["moves"])} 个文件；不处理downloads。')
            print(str(output))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (LayoutError, OSError, sqlite3.Error) as exc:
        print('历史修复未完成：'+str(exc),file=sys.stderr)
        sys.exit(2)
