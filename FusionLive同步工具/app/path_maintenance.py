"""One offline, recoverable plan for current paths, historical folders and rollback."""
from app_paths import HOME
from collections import defaultdict
from contextlib import ExitStack, closing
from pathlib import Path
import argparse
import json
import os
import sqlite3
import sys

import file_layout as layout
import history_repair as history
from fusion_client import now


def pending_jobs(ledger):
    result = []
    for table in ('layout_moves', 'history_repairs'):
        if ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)):
            result.extend(dict(engine=table, **json.loads(r['payload']))
                          for r in ledger.all(f"SELECT payload FROM {table} WHERE state!='done'"))
    if (ledger.base_dir/'runtime/layout.pending').exists() and not result:
        result.append(dict(engine='layout_moves', detail='完成上次迁移的路径映射与收尾'))
    return result


def references(ledger):
    refs = defaultdict(list)
    for row in ledger.all('SELECT * FROM file_history WHERE path IS NOT NULL'):
        refs[row['path']].append(['file_history', row['id']])
    for row in ledger.all('SELECT * FROM files WHERE path IS NOT NULL'):
        refs[row['path']].append(['files', row['project'], row['id']])
    return refs


def restorations(ledger, mapping):
    """Restore one newest available predecessor, keeping its historical identity."""
    files = [dict(r) for r in ledger.all('SELECT * FROM files')]
    histories = [dict(r) for r in ledger.all('SELECT * FROM file_history')]
    by_file = defaultdict(list)
    by_path = defaultdict(list)
    for row in histories:
        by_file[(row['project'], row['file_id'])].append(row)
    for row in [*histories, *files]:
        if row['path']:
            by_path[row['path']].append(row)
    links = layout.replacement_links(ledger, files, mapping)
    all_refs = references(ledger)
    predecessors = defaultdict(list)
    for old in files:
        if not old['active']:
            for new in links.get((old['project'], old['id']), []):
                predecessors[(new['project'], new['id'])].append(old)
    moves, warnings, used = [], [], set()
    def exists(path, prefix):
        return bool(path and path.startswith(prefix) and layout.confined(ledger.base_dir, path).is_file())
    for item in files:
        if not item['active']:
            continue
        # The current content is already downloaded; ordinary layout handles its path.
        if (item['status'] == 'done' and item['path'] and item['sha256'] and
                layout.confined(ledger.base_dir, item['path']).is_file() and
                layout.confined(ledger.base_dir, item['path']).stat().st_size == item['bytes']):
            continue
        if item['status'] == 'done':
            warnings.append('当前版本标记为已下载但本地缺失或大小不符，请先校验并重新入队；未用旧版替代：'+item['filename'])
            continue
        if exists(item['path'], 'downloads/'):
            continue
        key = (item['project'], item['id'])
        old_files = predecessors[key]
        identifiers = {item['id'], *(r['id'] for r in old_files)}
        versions = [dict(r, rank=(r['archived'] or '', r['id'])) for identifier in identifiers
                    for r in by_file[(item['project'], identifier)]]
        versions.extend(dict(r, rank=(r['updated'] or '', 0)) for r in old_files)
        if any(exists(r['path'], 'downloads/') for r in versions):
            continue
        candidates = [r for r in versions if exists(r['path'], 'history/') and r['sha256']]
        if not candidates:
            continue
        candidate = max(candidates, key=lambda r: r['rank'])
        source, target = candidate['path'], mapping[key]['path']
        size = layout.confined(ledger.base_dir, source).stat().st_size
        recorded = by_path[source]
        if (len({r['sha256'] for r in recorded if r['sha256']}) > 1 or
                any(r['bytes'] is not None and r['bytes'] != size for r in recorded)):
            raise layout.LayoutError('待恢复历史文件与台账冲突，保留原件：'+source)
        if source.casefold() in used:
            moves = [m for m in moves if m['source'].casefold() != source.casefold()]
            warnings.append('同一历史文件对应多个当前文件，保留原件，需核对：'+source)
            continue
        used.add(source.casefold())
        moves.append(dict(source=source, target=target, refs=all_refs[source],
                          bytes=size,
                          sha256=candidate['sha256'],
                          reason='新版尚未下载，恢复最近可用旧版本',
                          current_project=item['project'], current_file_id=item['id']))
    return moves, warnings


def plan(ledger, progress=None):
    recovery = pending_jobs(ledger)
    if recovery:
        return dict(format=1, policy='unified-path-maintenance-v1', recovery=recovery,
                    moves=[], missing=[], mapping=[], warnings=['须先恢复上次迁移，再生成完整整理方案。'])
    regular = layout.plan(ledger, progress=progress)
    mapping = {(r['project'], r['file_id']): r for r in regular['mapping']}
    restore, warnings = restorations(ledger, mapping)
    # Plan historical destinations against projected archives, without copying data
    # or changing the live ledger during preview.
    archives = {r['target']: r['source'] for r in regular['moves'] if r['target'].startswith('history/')}
    excluded = [r['source'] for r in [*regular['moves'], *restore] if r['source'].startswith('history/')]
    shared = history.plan(ledger, progress, extra_sources=archives, exclude=excluded)
    merged = {r['source']: r for r in shared['moves']}
    refs = references(ledger)
    moves = []
    for move in regular['moves']:
        move = dict(move, reason='整理当前文件路径')
        if move['target'] in merged:
            final = merged.pop(move['target'])
            move.update(target=final['target'], platform_path=final['platform_path'], reason='整理历史目录')
        moves.append(move)
    for move in merged.values():
        moves.append(dict(move, refs=refs.get(move['source'], [['untracked']]), reason='整理历史目录'))
    moves.extend(restore)
    sources, destinations = set(), set()
    for move in moves:
        source, target = move['source'].casefold(), move['target'].casefold()
        if source in sources or target in destinations:
            raise layout.LayoutError('整理方案存在重复源文件或目标，未移动文件：'+move['source'])
        sources.add(source)
        destinations.add(target)
    for move in moves:
        target = layout.confined(ledger.base_dir, move['target'])
        if target.exists() and move['target'].casefold() not in sources:
            raise layout.LayoutError('整理目标已有文件，保留两份原件，请先核对：'+move['target'])
        if layout.length(ledger.base_dir/move['target']) > layout.LIMIT:
            raise layout.LayoutError('整理目标路径过长：'+move['target'])
    return dict(regular, policy='unified-path-maintenance-v1', moves=moves,
                warnings=warnings, recovery=[], restored=len(restore),
                history_files=sum(m['reason'] == '整理历史目录' for m in moves))


def apply(ledger, progress=None, cancelled=lambda: False):
    base = ledger.base_dir
    if (not (base/'downloads').is_dir() and not (base/'history').is_dir() and
            not (base/'runtime/path_migration').is_dir() and
            ledger.one('SELECT 1 FROM files WHERE path IS NOT NULL LIMIT 1')):
        raise layout.LayoutError('此目录只有台账、没有downloads或history实际文件；请在服务器真实数据目录执行。')
    backup = base/'runtime/backups'/('before-unified-paths-'+now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
    backup.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(backup)) as db:
        ledger.db.backup(db)
    recovery = pending_jobs(ledger)
    if any(r['engine'] == 'layout_moves' for r in recovery):
        layout.apply(ledger, progress=progress, cancelled=cancelled,
                     log=progress.message if progress else print)
    if any(r['engine'] == 'history_repairs' for r in recovery):
        history.apply(ledger, progress=progress, cancelled=cancelled)
    proposal = plan(ledger, progress)
    if proposal['recovery']:
        raise layout.LayoutError('上次迁移未能恢复完成，保留现场。')
    report = base/'runtime/path_maintenance_applied.json'
    report.write_text(json.dumps(dict(proposal, backup=str(backup)), ensure_ascii=False, indent=2), encoding='utf-8')
    layout.apply(ledger, proposal, progress=progress, cancelled=cancelled,
                 log=progress.message if progress else print)
    # Remove only empty historical folders; no historical content is deleted.
    for parent, _, _ in os.walk(base/'history', topdown=False, followlinks=False):
        if Path(parent) != base/'history':
            try:
                Path(parent).rmdir()
            except OSError:
                pass
    return proposal


def main(argv=None):
    parser = argparse.ArgumentParser(description='统一整理下载/历史目录，恢复新版未下载的旧文件。默认只预览。')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--base-dir', type=Path, default=HOME)
    args = parser.parse_args(argv)
    base = args.base_dir.resolve()
    database = base/'runtime/ledger.sqlite3'
    if not database.is_file():
        raise layout.LayoutError('未找到下载台账：'+str(database))
    from analysis_worker import worker_lock
    with ExitStack() as stack:
        progress = stack.enter_context(layout.MigrationProgress(base))
        if args.apply:
            for folder, name in ((base/'runtime', 'scheduler.lock'), (base/'runtime', 'sync.lock'),
                                 (base/'runtime/file_analysis', 'worker.lock'),
                                 (base/'runtime/wechat_delivery', 'worker.lock'),
                                 (base/'runtime', 'history_repair.lock'), (base/'runtime', 'path_maintenance.lock')):
                if not stack.enter_context(worker_lock(folder, name)):
                    raise layout.LayoutError('请先安全退出同步、调度和后台任务：'+str(folder/name))
        db = stack.enter_context(closing(sqlite3.connect(database if args.apply else database.as_uri()+'?mode=ro', uri=not args.apply)))
        db.row_factory = sqlite3.Row
        ledger = layout.ReadLedger(db, base)
        if args.apply:
            result = apply(ledger, progress)
        else:
            result = plan(ledger, progress)
            output = base/'runtime/path_maintenance_preview.json'
            output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
            progress.message('预览已保存：'+str(output))
        progress.message(f'{"整理完成" if args.apply else "只读预览"}：移动 {len(result["moves"])} 个文件；'
                         f'恢复旧版 {result.get("restored",0)} 个；历史整理 {result.get("history_files",0)} 个；'
                         f'缺失源文件 {len(result["missing"])} 个；待恢复迁移 {len(result["recovery"])} 项。')
        for warning in result['warnings']:
            progress.message('需核对：'+warning)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (layout.LayoutError, OSError, sqlite3.Error) as exc:
        print('文件目录整理未完成：'+str(exc), file=sys.stderr)
        sys.exit(2)
