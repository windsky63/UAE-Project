"""Resumable read-only catalogue and file downloader. See docs/使用说明.txt."""

from paths import HOME, CODE
import argparse
import csv
import errno
import faulthandler
import hashlib
import json
import os
import re
from pathlib import Path
import signal
import sqlite3
import sys
import time
import threading
from types import SimpleNamespace

import requests

from fusion_client import FusionClient, HOME, RUNTIME, WindowClosed, LogoutError, SessionExpiredError, AuthenticationRequiredError, now, check_window
from ledger import Ledger, safe_name
from paths import relative_path, resolve_path, native_path, exports_dir
from reporting import record_download, write_summary, logged_main, volume, write_push_text
from local_credentials import get_credentials
from progress import emit, DownloadProgress
from workflow import Workflow, STAGES
from scope_review import export_scope_review
from analysis_worker import notify_download_completed


class RunLock:
    def __init__(self, name='sync.lock'):
        self.name = name

    def __enter__(self):
        RUNTIME.mkdir(exist_ok=True)
        self.file = (RUNTIME / self.name).open('a+b')
        self.file.seek(0)
        self.file.write(b'0')
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError('已有同步程序在运行。')
        return self

    def __exit__(self, *args):
        self.file.close()


def watch_stop(done):
    """Capture the actual stuck call, without interrupting logout or transactions."""
    while not done.wait(.5):
        if (RUNTIME / 'stop.request').exists() or (RUNTIME / 'scheduler.exit.request').exists():
            if done.wait(30):
                return
            with (RUNTIME / 'sync-stop-stack.log').open('a', encoding='utf-8') as stream:
                stream.write('\n停止等待超过30秒；当前线程调用位置：\n')
                stream.flush()
                faulthandler.dump_traceback(file=stream, all_threads=True)
            if done.wait(30):
                return


def finish_run(ledger, run_id, status, confirmed, error):
    ledger.end_run(run_id, status, confirmed, error)
    print('任务状态已保存；文字日报在发送时读取最新统计。', flush=True)
    write_summary(ledger, run_id)
    if status in ('failed', 'download_pending', 'inventory_incomplete', 'logout_unconfirmed'):
        print('本轮仍有未完成任务：全量Excel导出延后，优先在下载窗口内恢复；错误与进度已写入运行摘要。', flush=True)
        return
    if (RUNTIME / 'stop.request').exists() or (RUNTIME / 'scheduler.exit.request').exists():
        print('停止请求已生效：完整Excel台账延后至手动导出，未完成下载将在下次启动恢复。', flush=True)
        return
    ledger.clean_partial_downloads()
    print('正在导出完整Excel台账。', flush=True)
    try_export_ledger(ledger)


def try_export_ledger(ledger):
    try:
        return export_csv(ledger)
    except (ImportError, OSError, ValueError, TypeError, KeyError, csv.Error) as exc:
        import traceback
        print(f'Excel台账暂未导出；下载、核对及注销状态仍保存在数据库：{type(exc).__name__}：{exc}', file=sys.stderr)
        traceback.print_exc()
        return None


def scan(client, ledger, max_folders=None):
    project = client.project_id
    count = 0
    while not max_folders or count < max_folders:
        folder = ledger.one("SELECT * FROM folders WHERE project=? AND snapshot=? AND tree_state='pending' ORDER BY rowid LIMIT 1", (project, ledger.snapshot_id))
        if not folder:
            break
        ledger.save_children(project, folder, client.folders(folder['id']))
        count += 1
        print('目录已保存：', folder['path'], flush=True)
        emit('目录层级', directories=count)
    count = 0
    pages, documents = 0, 0
    while not max_folders or count < max_folders:
        folder = ledger.one("SELECT * FROM folders WHERE project=? AND snapshot=? AND list_state='pending' ORDER BY rowid LIMIT 1", (project, ledger.snapshot_id))
        if not folder:
            break
        start = folder['next_start']
        total, records = client.document_page(folder['id'], start, 100)
        if start < total and not records:
            raise RuntimeError('总数仍有剩余，但分页返回空记录，停止以避免漏项。')
        if len({r['id'] for r in records}) != len(records):
            raise RuntimeError('同一页包含重复文件 ID，停止检查分页。')
        next_start = start + len(records)
        ledger.save_page(project, folder['id'], total, records, next_start, next_start >= total)
        if next_start >= total:
            count += 1
        print(f'清单已保存：{folder["path"]} {next_start}/{total}', flush=True)
        pages += 1
        documents += len(records)
        emit('文档清单', pages=pages, documents=documents)
    return ledger.finish_catalogue(project)


ASSOCIATION_BATCH_SIZE = 50
INTERLEAVED_DOWNLOAD_BATCH_SIZE = 20
# Keep verified downloads moving: a single large file often exceeds two
# minutes. Return to failed association checks after useful transfer time.
INTERLEAVED_DOWNLOAD_SECONDS = 1800


def refresh_associations(client, ledger, max_documents=None, *, on_batch=None):
    count, attempted, consecutive_failures = 0, 0, 0
    forbidden = set()
    summary = ledger.one('''SELECT COUNT(*) total,COALESCE(SUM(checked_cycle>=? AND related_state IN ('done','no_content')),0) done
      FROM documents WHERE project=? AND active=1''', (ledger.cycle, client.project_id))
    started = time.monotonic()

    def progress(phase='原件/附件核对'):
        failures = ledger.one("SELECT COUNT(*) n FROM documents WHERE project=? AND active=1 AND related_state='failed'", (client.project_id,))['n']
        emit(phase, associated_total=summary['total'], associated_done=summary['done'] + count,
             associated_failed=failures, processed=count, elapsed=round(time.monotonic() - started, 2))

    def pause(seconds):
        for _ in range(seconds * 4):
            check_window()
            time.sleep(0.25)

    def verify(document, phase):
        data = json.loads(document['metadata'])
        placeholder = data.get('hasContent') == 'false' and data.get('hasAttachments') != 'true'
        for attempt in range(1, 3):
            check_window()
            try:
                items = [] if placeholder else client.related(document['id'], data.get('virtualLinkId') if data.get('isVirtualLink') == 'true' else None)
                ledger.save_related(document, items, placeholder=placeholder)
                print('原件/附件核对：', count + 1, document['reference'] or document['id'], len(items), '个文件', flush=True)
                return True
            except (WindowClosed, KeyboardInterrupt, SessionExpiredError, sqlite3.Error):
                raise
            except Exception as exc:
                if isinstance(exc, requests.HTTPError) and exc.response is not None:
                    if exc.response.status_code in (401, 418, 429):
                        raise
                    if exc.response.status_code == 403:
                        # A single document can be forbidden while the account
                        # remains healthy. Probe protected access before treating
                        # it as a per-document failure; never bypass a bad session.
                        client.folders(client.cabinet_id)
                        error = f'{now().isoformat()} 关联核对HTTP 403；受保护目录仍可读取；{exc}'
                        with ledger.db:
                            ledger.db.execute("UPDATE documents SET related_state='failed',error=? WHERE project=? AND id=? AND folder_id=?",
                                (error, document['project'], document['id'], document['folder_id']))
                        forbidden.add((document['id'], document['folder_id']))
                        print('文档关联访问被拒绝，保留失败记录并继续其他文档：', document['id'], error, flush=True)
                        return False
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise
                error = f'{phase}第{attempt}/2次：{type(exc).__name__}: {exc}'
                print('关联核对异常：', document['reference'] or document['id'], '版本ID', document['id'],
                      '目录ID', document['folder_id'], error, flush=True)
                if attempt < 2:
                    pause(attempt)
                else:
                    with ledger.db:
                        ledger.db.execute("UPDATE documents SET related_state='failed',error=? WHERE project=? AND id=? AND folder_id=?",
                            (now().isoformat() + ' ' + error, document['project'], document['id'], document['folder_id']))
                    print('关联已暂存为失败，继续其他文档：', document['reference'] or document['id'], flush=True)
        return False

    def process(document, phase):
        nonlocal count, attempted, consecutive_failures
        attempted += 1
        if verify(document, phase):
            count += 1
            consecutive_failures = 0
        else:
            consecutive_failures += 1
        # Complete association verification and its retries before transferring:
        # later parents can reveal priority files or a different metadata view.
        progress('关联失败项补查' if phase == '补查' else '原件/附件核对')
        if consecutive_failures >= 5:
            print('连续关联失败，检查受保护目录是否仍可读取。', flush=True)
            client.folders(client.cabinet_id)
            consecutive_failures = 0

    progress()
    while not max_documents or attempted < max_documents:
        check_window()
        document = ledger.one("SELECT * FROM documents WHERE project=? AND active=1 AND related_state='pending' ORDER BY checked_cycle,size,id,folder_id LIMIT 1", (client.project_id,))
        if document is None:
            break
        process(document, '初查')
    pending = ledger.one("SELECT COUNT(*) n FROM documents WHERE project=? AND active=1 AND related_state='pending'", (client.project_id,))['n']
    if not pending:
        # Take one bounded snapshot of failures. Never select the same failed row forever.
        failures = ledger.all("SELECT * FROM documents WHERE project=? AND active=1 AND related_state='failed' ORDER BY size,id,folder_id", (client.project_id,))
        if failures:
            print('其余文档已核对，开始补查失败记录：', len(failures), '条，每条最多再尝试2次。', flush=True)
        for document in failures:
            if max_documents and attempted >= max_documents:
                break
            if (document['id'], document['folder_id']) in forbidden:
                continue
            process(document, '补查')
    if on_batch:
        on_batch()
    return ledger.finish_refresh(client.project_id)


def file_target(client, ledger, item):
    from file_layout import target_for
    return target_for(ledger, item)


def local_valid(item, base=None):
    if not item['path']:
        return False
    path = resolve_path(item['path'], base)
    if not item['sha256'] or not path.is_file() or path.stat().st_size != item['bytes']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as inp:
        for block in iter(lambda: inp.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest() == item['sha256']


PRIORITY_UNITS = frozenset((213, 27, 37, 38, 39))
EXPLICIT_UNIT = re.compile(r'(?<![A-Z0-9])(?:H\d+|GG)-(\d{2,3})(?=[_\W]|$)', re.I)
LABELLED_UNIT = re.compile(r'(?<![A-Z0-9])(?:UNITS?|单元)\s*[-_:：]?\s*(\d{1,3})(?![A-Z0-9])', re.I)
SUFFIX_UNIT = re.compile(r'(?<![A-Z0-9])(\d{1,3})\s*单元', re.I)
SUFFIX_UNIT = re.compile(r'(?<![A-Z0-9])(\d{1,3})\s*单元', re.I)


def priority_unit_match(*texts):
    """Match unit fields, never arbitrary numbers in IDs, dates or serials."""
    from business_type import ISO
    for text in texts:
        text = str(text or '')
        units = [m[1] for m in EXPLICIT_UNIT.finditer(text)]
        units += [m[1] for m in LABELLED_UNIT.finditer(text)]
        units += [m[1] for m in SUFFIX_UNIT.finditer(text)]
        units += [m[1] for m in SUFFIX_UNIT.finditer(text)]
        units += [m[2] for m in ISO.finditer(text)]
        if any(int(unit) in PRIORITY_UNITS for unit in units):
            return True
    return False


def priority_download_keys(ledger, project, *, all_active=False):
    from business_type import ledger_file_contexts
    contexts = ledger_file_contexts(ledger.db)
    result = []
    sql = 'SELECT project,id,filename FROM files WHERE project=? AND active=1'
    if not all_active:
        sql += " AND status IN ('pending','failed')"
    for item in ledger.all(sql, (project,)):
        texts = [item['filename']]
        for parent in contexts.get((item['project'],item['id']), ()):
            texts.extend(parent.get(key) for key in ('reference','filename','title','folder_path'))
        if priority_unit_match(*texts):
            result.append((item['project'],item['id']))
    return result


def next_download_item(ledger, project):
    # Keep the content lookup outside the document lookup. A flattened JOIN may
    # scan all checked documents once for every pending file on SQLite 3.45.
    return ledger.one('''SELECT i.* FROM files i WHERE i.project=? AND i.active=1
      AND i.status IN ('pending','failed') AND i.retryable=1 AND i.attempts<3 AND EXISTS(
        SELECT 1 FROM document_files x WHERE x.project=i.project
        AND x.file_id=i.id AND x.active=1 AND EXISTS(
          SELECT 1 FROM documents d WHERE d.project=x.project
          AND d.id=x.document_id AND d.folder_id=x.folder_id
          AND d.active=1 AND d.related_state='done' AND d.checked_cycle=?))
      AND NOT EXISTS(SELECT 1 FROM download_run_skips s WHERE s.project=i.project AND s.file_id=i.id)
      ORDER BY EXISTS(SELECT 1 FROM download_unit_priority p WHERE p.project=i.project AND p.file_id=i.id) DESC,
               EXISTS(SELECT 1 FROM download_update_priority u WHERE u.project=i.project AND u.file_id=i.id),i.rowid
      LIMIT 1''', (project, ledger.cycle))


def download(client, ledger, max_files=None, *, progress=None, reset_run_skips=True, time_budget_seconds=None):
    from file_layout import prepare_downloads, local_update_keys, download_structure_token, targets, LayoutError, LayoutPlanningError
    def migration_cancelled():
        check_window()
        return False
    isolate_layout = False
    stage_only = False
    def repair_conflict(exc=None, *, keys=(), paths=()):
        from local_reconcile import reconciler
        try:
            service = reconciler(ledger, check_window)
            service.schedule(paths=(*getattr(exc,'paths',()),*paths),keys=keys)
            return service.tick(.25)
        except (LayoutError,OSError) as error:
            if isinstance(error,OSError) and error.errno==errno.ENOSPC:
                raise
            print('磁盘核对暂缓，继续其他下载：',str(error),flush=True)
            return 0

    def prepare_batch():
        nonlocal isolate_layout, stage_only
        try:
            return prepare_downloads(ledger, migration_cancelled, reuse=True, wait=False, reconcile=True)
        except LayoutPlanningError as exc:
            # A partially applied journal must recover before any download.
            if (ledger.base_dir/'runtime/layout.pending').exists():
                raise
            if repair_conflict(exc):
                try:
                    return prepare_downloads(ledger, migration_cancelled, reuse=True, wait=False, reconcile=True)
                except LayoutPlanningError as remaining:
                    exc = remaining
            isolate_layout = True
            ledger._download_targets = exc.mapping
            print('路径规划发现文件冲突，保留现有文件；改为按依赖组准备，跳过冲突组并继续其他下载：',
                  str(exc), flush=True)
            return True
        except (LayoutError, OSError) as exc:
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise
            if not (ledger.base_dir/'runtime/layout.pending').exists():
                raise
            # Recovery owns the formal paths. Continue independent HTTP work in
            # staging without changing that journal or publishing over its files.
            stage_only = True
            ledger._download_targets = targets(ledger)
            print('目录迁移恢复暂未完成，保留恢复现场；其他文件继续下载到暂存区：', str(exc), flush=True)
            return True
    ledger.recover_download_receipts(check_window)
    # Exact historical identity and verified bytes can settle pending records
    # before layout planning interprets them as conflicting current owners.
    recovering_layout = (ledger.base_dir/'runtime/layout.pending').exists()
    reused = (0 if recovering_layout
              else ledger.reuse_verified_versions(migration_cancelled))
    if reused:
        print(f'已校验并复用 {reused} 个相同版本的本地副本，未重新下载。', flush=True)
    ready = prepare_batch()
    if not ready and ((ledger.base_dir/'runtime/layout.pending').exists()
            or getattr(ledger, '_download_layout_token', None) != download_structure_token(ledger)):
        stage_only = True
        ledger._download_targets = targets(ledger)
        print('目录恢复或规划仍被占用；保留正式文件，继续下载到暂存区。', flush=True)
    # Recovery may have restored an exact version from history to downloads.
    if not stage_only:
        reused = ledger.reuse_verified_versions(migration_cancelled)
        if reused:
            print(f'已校验并复用 {reused} 个相同版本的本地副本，未重新下载。', flush=True)
            if prepare_batch() is False:
                return 0
    blocked = ledger.all("""SELECT project,id,target_path FROM files WHERE active=1 AND status='failed'
        AND retryable=0 AND error LIKE '下载目标已存在%'""")
    if blocked:
        if not stage_only:
            from local_reconcile import reconciler
            reconciler(ledger,check_window).requeue_cleared_targets(blocked)
        token = download_structure_token(ledger)
        if getattr(ledger,'_local_failed_checks_token',None)!=token:
            ledger._local_failed_checks_token = token
            if repair_conflict(keys=((r['project'],r['id']) for r in blocked),
                               paths=(r['target_path'] for r in blocked if r['target_path'])):
                prepare_batch()
    analysis_database = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    notify_download_completed(ledger.base_dir, analysis_database)
    project = client.project_id
    processed = 0
    attempted_in_batch = 0
    batch_started = time.monotonic()
    maintenance_at, maintenance_attempts = batch_started, 0
    if progress is None:
        progress = DownloadProgress(ledger, project)
    else:
        progress.refresh(ledger, project)
    updates = local_update_keys(ledger, reuse=True)
    priority_token = (project, download_structure_token(ledger))
    cached_priority = getattr(ledger, '_download_priority_cache', None)
    if cached_priority is None or cached_priority[0] != priority_token:
        priority = priority_download_keys(ledger, project, all_active=True)
        ledger._download_priority_cache = (priority_token, priority)
    else:
        priority = cached_priority[1]
    pending_publications = set()
    deferred_publications = set()
    cycle = ledger.one('SELECT status FROM refresh_cycles WHERE project=?', (project,))
    inventory_complete = bool(cycle and cycle['status'] == 'complete')

    def publish_ready():
        nonlocal stage_only
        if not pending_publications:
            return True
        if not inventory_complete or stage_only:
            return False
        groups = {ledger._publication_groups.get(key, frozenset((key,)))
                  for key in pending_publications}
        for keys in sorted(groups, key=lambda group: sorted(group)):
            if keys in deferred_publications:
                continue
            try:
                if prepare_downloads(ledger, migration_cancelled, keys=keys, wait=False) is False:
                    return False
            except (LayoutError, OSError) as exc:
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise
                if (ledger.base_dir/'runtime/layout.pending').exists():
                    stage_only = True
                elif repair_conflict(exc,keys=keys):
                    # Retry just this dependency group, leaving other groups free.
                    try:
                        if prepare_downloads(ledger,migration_cancelled,keys=keys,wait=False):
                            continue
                    except (LayoutError,OSError):
                        if (ledger.base_dir/'runtime/layout.pending').exists():
                            stage_only = True
                deferred_publications.add(keys)
                print('文件已校验，发布暂缓；保留暂存和旧文件，继续其他下载：', str(exc), flush=True)
                if stage_only:
                    break
        # A recovered older journal may not include newer staged downloads.
        pending_publications.intersection_update(
            key for key in pending_publications
            if (ledger.one('SELECT path FROM files WHERE project=? AND id=?', key)['path'] or '')
            .replace('\\', '/').startswith('runtime/download_ready/'))
        return not pending_publications

    with ledger.db:
        ledger.db.execute('CREATE TEMP TABLE IF NOT EXISTS download_run_skips(project TEXT,file_id TEXT,PRIMARY KEY(project,file_id))')
        if reset_run_skips:
            ledger.db.execute('DELETE FROM download_run_skips')
        ledger.db.execute('CREATE TEMP TABLE IF NOT EXISTS download_update_priority(project TEXT,file_id TEXT,PRIMARY KEY(project,file_id))')
        ledger.db.execute('DELETE FROM download_update_priority')
        ledger.db.executemany('INSERT INTO download_update_priority VALUES(?,?)', updates)
        ledger.db.execute('CREATE TEMP TABLE IF NOT EXISTS download_unit_priority(project TEXT,file_id TEXT,PRIMARY KEY(project,file_id))')
        ledger.db.execute('DELETE FROM download_unit_priority')
        ledger.db.executemany('INSERT INTO download_unit_priority VALUES(?,?)', priority)
    pending_priority = ledger.one('''SELECT COUNT(*) FROM download_unit_priority p JOIN files f
      ON f.project=p.project AND f.id=p.file_id WHERE f.active=1 AND f.status IN ('pending','failed')''')[0]
    print(f'优先下载单元：213、27、37、38、39；命中待下载文件 {pending_priority} 个。', flush=True)
    while not max_files or processed < max_files:
        check_window()
        if (time_budget_seconds is not None and attempted_in_batch
                and time.monotonic()-batch_started >= time_budget_seconds):
            print('本批下载时间预算已到，保留队列并继续核对；当前文件已安全处理。', flush=True)
            break
        # Bulk download may occupy the entire window. Advance bounded local
        # work at safe file boundaries rather than waiting for the whole queue.
        if attempted_in_batch-maintenance_attempts>=20 or time.monotonic()-maintenance_at>=60:
            maintain_local_references(ledger)
            maintenance_at, maintenance_attempts = time.monotonic(), attempted_in_batch
            updates = local_update_keys(ledger,reuse=True)
        publish_ready()
        selection_started = time.monotonic()
        item = next_download_item(ledger, project)
        selection_seconds = time.monotonic() - selection_started
        if item is None:
            break
        item = dict(item)
        attempted_in_batch += 1
        progress.begin(item)
        target = None
        counted = False
        try:
            with ledger.db:
                ledger.db.execute("UPDATE files SET status='downloading',attempts=attempts+1,error=NULL WHERE project=? AND id=?", (project, item['id']))
            target = (resolve_path(item['target_path'], ledger.base_dir)
                      if item['target_path'] and not isolate_layout else file_target(client, ledger, item))
            # Keep new bytes separate until predecessor verification and archive.
            publish_needed = stage_only or (project, item['id']) in updates
            staged = ((ledger.base_dir / 'runtime/download_ready' /
                       hashlib.sha256((project + '\0' + item['id']).encode()).hexdigest() / 'content')
                      if publish_needed else target)
            with ledger.db:
                ledger.db.execute('UPDATE files SET target_path=? WHERE project=? AND id=?',
                                  (relative_path(target, ledger.base_dir), project, item['id']))
            if isolate_layout and not stage_only:
                keys = ledger._publication_groups.get((project, item['id']), {(project, item['id'])})
                if prepare_downloads(ledger, migration_cancelled, keys=keys, wait=False) is False:
                    with ledger.db:
                        ledger.db.execute("UPDATE files SET status='pending',attempts=attempts-1 WHERE project=? AND id=?",
                                          (project, item['id']))
                    progress.failed()
                    with ledger.db:
                        ledger.db.execute('INSERT OR IGNORE INTO download_run_skips VALUES(?,?)', (project, item['id']))
                    continue
            if target.exists() and not publish_needed:
                if repair_conflict(keys=((project,item['id']),),paths=(relative_path(target,ledger.base_dir),)):
                    saved = ledger.one('SELECT * FROM files WHERE project=? AND id=?',(project,item['id']))
                    if saved['status']=='done':
                        processed += 1
                        progress.complete(saved['bytes'])
                        continue
                    updates = local_update_keys(ledger,reuse=True)
                    publish_needed = (project,item['id']) in updates
                    if publish_needed:
                        staged = (ledger.base_dir/'runtime/download_ready'/
                                  hashlib.sha256((project+'\0'+item['id']).encode()).hexdigest()/'content')
                if not publish_needed:
                    raise LayoutError('下载目标已存在，未覆盖；请先运行路径迁移并核对台账：' + str(target),
                                      paths=(relative_path(target,ledger.base_dir),))
            staged.parent.mkdir(parents=True, exist_ok=True)
            from file_layout import download_directory_copy
            previous_path = download_directory_copy(ledger,item,target)
            item['download_activity_id'] = ledger.begin_download_activity(item,previous_path)
            transfer_started = time.monotonic()
            def update_progress(received, expected):
                progress.update(received, expected)
            update_progress.resumed = progress.resume
            result = client.download(json.loads(item['metadata']), staged, progress=update_progress,
                verified=lambda result: ledger.save_download_receipt(item, result, target))
            transfer_seconds = time.monotonic() - transfer_started
            publication_started = time.monotonic()
            completed = now().isoformat()
            with ledger.db:
                ledger.finish_download_activity(item['download_activity_id'],result,completed)
                ledger.db.execute("UPDATE files SET status='done',bytes=?,path=?,sha256=?,error=NULL,updated=?,downloaded_at=?,download_date_source='下载完成实录' WHERE project=? AND id=?",
                                  (result['bytes'], relative_path(result['path'], ledger.base_dir), result['sha256'], completed, completed, project, item['id']))
                ledger.db.execute('DELETE FROM download_receipts WHERE project=? AND file_id=?', (project, item['id']))
                ledger.db.execute('DELETE FROM download_receipt_context WHERE project=? AND file_id=?', (project,item['id']))
            if publish_needed:
                key = (project, item['id'])
                pending_publications.add(key)
                if not publish_ready():
                    print('文件已下载并校验，移动替换暂缓：', item['filename'],
                          '；等待占用释放，继续下载其他文件。', flush=True)
                result['path'] = str(resolve_path(ledger.one('SELECT path FROM files WHERE project=? AND id=?', (project, item['id']))['path'], ledger.base_dir))
            item['download_action'] = ledger.one('SELECT action FROM download_activity WHERE id=?',
                                                  (item['download_activity_id'],))['action']
            record_download(ledger.base_dir, item, 'done', result['bytes'], relative_path(result['path'], ledger.base_dir))
            processed += 1
            counted = True
            progress.complete(result['bytes'])
            print('下载完成：', item['filename'], result['bytes'], '字节', flush=True)
            print(f'下载阶段耗时：{item["filename"]}；选择任务 {selection_seconds:.3f} 秒；'
                  f'请求、接收及校验 {transfer_seconds:.3f} 秒；'
                  f'登记及迁移 {time.monotonic()-publication_started:.3f} 秒', flush=True)
            notify_download_completed(ledger.base_dir, analysis_database)
        except (KeyboardInterrupt, WindowClosed):
            if ledger.one('SELECT status FROM files WHERE project=? AND id=?', (project, item['id']))['status'] == 'done':
                raise  # Verified bytes and migration journal must survive interruption.
            progress.failed()
            if target is not None:
                ledger.clean_partial_for(target)
            with ledger.db:
                ledger.db.execute("UPDATE files SET status='pending',attempts=0,error='任务停止，等待继续' WHERE project=? AND id=?", (project, item['id']))
            record_download(ledger.base_dir, item, 'stopped', error='安全中断，已保存可验证的续传进度；下次优先尝试字节续传')
            raise
        except Exception as exc:
            if (ledger.base_dir/'runtime/layout.pending').exists():
                stage_only = True
            saved = ledger.one('SELECT status,bytes FROM files WHERE project=? AND id=?', (project, item['id']))
            if saved['status'] == 'done':
                if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                    raise
                # Completion is durable. A later notification/publication error
                # must neither redownload this file nor abort the remaining queue.
                with ledger.db:
                    ledger.db.execute('UPDATE files SET error=? WHERE project=? AND id=?',
                                      ('下载已校验，后处理暂缓：'+str(exc), project, item['id']))
                if not counted:
                    processed += 1
                    progress.complete(saved['bytes'])
                print('下载已校验，后处理暂缓，继续其他文件：', item['filename'], str(exc), flush=True)
                continue
            progress.failed()
            if target is not None:
                ledger.clean_partial_for(target)
            record_download(ledger.base_dir, item, 'failed', error=str(exc))
            permanent = (isinstance(exc, LayoutError) or
                         (isinstance(exc, requests.HTTPError) and exc.response is not None and
                          exc.response.status_code in (400, 404, 405, 410, 413, 415, 422)))
            if isinstance(exc,LayoutError):
                repair_conflict(exc,keys=((project,item['id']),))
            with ledger.db:
                ledger.db.execute("UPDATE files SET status='failed',error=?,updated=?,retryable=? WHERE project=? AND id=?",
                                  (str(exc), now().isoformat(), int(not permanent), project, item['id']))
                ledger.db.execute('INSERT OR IGNORE INTO download_run_skips VALUES(?,?)',
                                  (project, item['id']))
            http_status = (exc.response.status_code
                           if isinstance(exc, requests.HTTPError) and exc.response is not None else None)
            if http_status == 403:
                # Keep the failure retryable, but move on rather than selecting
                # the same forbidden file repeatedly during this download run.
                with ledger.db:
                    ledger.db.execute('INSERT OR IGNORE INTO download_run_skips VALUES(?,?)',
                                      (project, item['id']))
                print('下载被拒绝（HTTP 403），已记录失败并跳过本轮：',
                      item['filename'], '内容ID：', item['id'], '；继续下载其他文件。', flush=True)
                time.sleep(1)
                continue
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise
            print(f'下载失败，已记入台账：{item["filename"]}；内容ID：{item["id"]}；'
                  f'{type(exc).__name__}：{exc}；可重试：{not permanent}', flush=True)
            if permanent:
                print('永久性错误，不自动重试；请核对源文件或下载路径。', flush=True)
            time.sleep(1)

    if pending_publications:
        if not inventory_complete:
            print('本批新版本已下载并校验，完整关联核对结束前保留暂存及旧正式文件；后续自动完成归档和发布。', flush=True)
            return processed
        print('本批文件已校验；发布仍被占用时保留暂存，继续核对，后续批次恢复。', flush=True)
        publish_ready()
    # Only idle slices may expand a conflict search to downloads/history. No
    # directory walking is added to normal per-file selection or HTTP callbacks.
    service = getattr(ledger,'_disk_reconciler',None)
    if service is not None and service.jobs and not stage_only:
        try:
            if service.tick(1.0,scan=True):
                prepare_batch()
        except (LayoutError,OSError,sqlite3.Error) as exc:
            if isinstance(exc,OSError) and exc.errno==errno.ENOSPC:
                raise
            print('分段磁盘核对暂缓，保留任务继续下载：',str(exc),flush=True)
    maintain_local_references(ledger)
    return processed


def maintain_local_references(ledger):
    from file_layout import LayoutError
    try:
        # Continue pending conflict work even after the HTTP queue has drained
        # and the workflow has entered its ordinary idle maintenance phase.
        service = getattr(ledger,'_disk_reconciler',None)
        if service is not None or ledger.one("SELECT 1 FROM sqlite_master WHERE name='local_reconcile_scan_state'"):
            from local_reconcile import reconciler
            service = reconciler(ledger,check_window)
            service.resume()
            if service.jobs:
                if service.tick(.5,scan=True):
                    from file_layout import prepare_downloads_or_defer
                    def cancelled():
                        check_window()
                        return False
                    prepare_downloads_or_defer(ledger,cancelled,reuse=True,reconcile=True)
    except (LayoutError,OSError,sqlite3.Error) as exc:
        if isinstance(exc,OSError) and exc.errno==errno.ENOSPC:
            raise
        print('磁盘引用核对暂缓，保留文件和恢复记录：',str(exc),flush=True)


def export_ledger(ledger, *, supplemental=True, quiet=False):
    from business_type import EXPORT_FIELDS, classify, file_business, ledger_file_contexts
    contexts = ledger_file_contexts(ledger.db)
    target = exports_dir(ledger.base_dir) / '文件台账.xlsx'
    columns = ['工作区', '目录', '文档编号', '文档名称', '版本ID', '修订', '文件名', '关联文件状态', '内容ID', '文件类型', '下载状态', '文件大小', '已保存字节', '本地路径', 'SHA256', '错误', '下载日期（北京）', '日期来源', '平台内容状态', '文件读取权限', '平台内容创建时间', '平台内容修改时间']
    rows = ledger.all('''SELECT d.project,f.path,d.reference,d.title,d.id,d.revision,
      COALESCE(i.filename,d.filename),d.related_state,i.id,i.relation,
      COALESCE(i.status,CASE WHEN json_extract(d.metadata,'$.hasContent')='false' AND COALESCE(json_extract(d.metadata,'$.hasAttachments'),'false')!='true' THEN '未上传内容（占位记录）' WHEN d.related_state='no_content' THEN '未上传内容（占位记录）' ELSE 'not_enumerated' END),COALESCE(i.size,d.size),i.bytes,i.path,i.sha256,
      CASE WHEN d.error IS NOT NULL AND i.error IS NOT NULL THEN d.error || '; ' || i.error ELSE COALESCE(d.error,i.error) END,i.downloaded_at,i.download_date_source,
      CASE WHEN json_extract(d.metadata,'$.hasContent')='false' THEN '未上传主文件' WHEN json_extract(d.metadata,'$.hasContent')='true' THEN '已上传主文件' ELSE '待核对' END,
      json_extract(i.metadata,'$.readAccessStatus'),json_extract(i.metadata,'$.creationDate'),
      COALESCE(json_extract(i.metadata,'$.modifiedDate'),json_extract(i.metadata,'$.lastModifiedDate'),json_extract(i.metadata,'$.updatedDate'),json_extract(i.metadata,'$.uploadDate'))
      FROM documents d JOIN folders f ON f.project=d.project AND f.id=d.folder_id
      LEFT JOIN document_files x ON x.project=d.project AND x.document_id=d.id AND x.folder_id=d.folder_id AND x.active=1
      LEFT JOIN files i ON i.project=x.project AND i.id=x.file_id AND i.active=1
      WHERE d.active=1 AND f.active=1 ORDER BY f.path,d.reference''')
    import uuid
    from archive_index import current_members
    extra_columns = ['记录类型', '所属压缩包内容ID', '所属压缩包文件名', '包内完整路径', '解压状态', '包内条目序号', '内容解析状态']
    temp = target.with_suffix('.' + uuid.uuid4().hex + '.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(columns + list(EXPORT_FIELDS) + extra_columns)
        for row in rows:
            business = (file_business(dict(filename=row[6], path=row[13]), contexts.get((row[0], row[8]), ()))
                        if row[8] else classify(filename=row[6] or '', reference=row[2] or ''))
            row = list(row) + [business[key] for key in EXPORT_FIELDS.values()] + ['平台文件', '', '', '', '', '', '']
            # Prevent spreadsheet formula interpretation of platform-controlled text.
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
        for member in current_members(ledger.base_dir):
            parents = contexts.get((member['project'], member['parent_file_id']), ())
            business = classify(filename=member['filename'])
            values = {key: '' for key in columns}
            values.update({'工作区':member['project'], '目录':'；'.join(dict.fromkeys(str(p.get('folder_path') or '') for p in parents)),
                '文档编号':'；'.join(dict.fromkeys(str(p.get('reference') or '') for p in parents)),
                '文档名称':'；'.join(dict.fromkeys(str(p.get('title') or '') for p in parents)),
                '文件名':member['filename'], '内容ID':member['id'], '文件类型':'压缩包成员',
                '下载状态':'随压缩包下载（非独立下载）', '文件大小':member['bytes'], '已保存字节':member['bytes'],
                '本地路径':member['path'], 'SHA256':member['sha256'],
                '错误':'' if member['availability']=='ready' else '解压文件缺失或大小不符'})
            row = [values[key] for key in columns] + [business[key] for key in EXPORT_FIELDS.values()] + [
                '解压文件', member['parent_file_id'], member['parent_filename'], member['member_path'],
                member['extraction_state'], member['entry'], member['analysis_state']]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
    from ledger_workbook import export_workbook
    try:
        actual_target = export_workbook(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    legacy = target.with_suffix('.csv')
    if legacy.is_file() and not legacy.is_symlink():
        history = target.parent / '历史导出'
        if history.resolve().is_relative_to(target.parent.resolve()):
            history.mkdir(exist_ok=True)
            saved = history / ('文件台账-' + now().strftime('%Y%m%d-%H%M%S-%f') + '.csv')
            try:
                os.replace(legacy, saved)
            except PermissionError:
                pass  # An open historical CSV need not block the new Excel export.
    if supplemental:
        export_directory(ledger)
        export_changes(ledger)
        export_association_failures(ledger)
    if not quiet:
        print('Excel 台账：', actual_target, flush=True)
    return actual_target


# Keep the existing internal import used by offline scripts and older callers.
export_csv = export_ledger


def export_directory(ledger):
    folders = ledger.all('''SELECT f.*,COUNT(d.id) document_count FROM folders f
      LEFT JOIN documents d ON d.project=f.project AND d.folder_id=f.id AND d.active=1
      WHERE f.active=1 GROUP BY f.project,f.id ORDER BY f.name,f.id''')
    children = {}
    for folder in folders:
        children.setdefault((folder['project'], folder['parent']), []).append(folder)
    lines = ['FusionLive 当前账号可见的项目目录树',
             '导出时间（北京）：' + now().isoformat(),
             '括号内为该目录直接包含的文档记录数，含未上传内容的占位记录。', '']
    def emit(folder, prefix='', branch=''):
        lines.append(prefix + branch + folder['name'] + f'  [ID {folder["id"]}；文档 {folder["document_count"]}]')
        nodes = children.get((folder['project'], folder['id']), [])
        for index, child in enumerate(nodes):
            last = index == len(nodes) - 1
            emit(child, prefix + (('    ' if branch == '└── ' else '│   ') if branch else ''), '└── ' if last else '├── ')
    for folder in folders:
        if folder['parent'] is None:
            emit(folder)
    target = exports_dir(ledger.base_dir) / '项目总体文件目录.txt'
    temp = target.with_suffix('.txt.tmp')
    temp.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    os.replace(temp, target)


def export_changes(ledger):
    target = exports_dir(ledger.base_dir) / '本次盘点变更.csv'
    latest = ledger.one('SELECT id FROM snapshots ORDER BY id DESC LIMIT 1')
    rows = ledger.all('SELECT entity,entity_id,folder_id,kind,details,created FROM changes WHERE snapshot=? ORDER BY id', (latest['id'],)) if latest else []
    with target.with_suffix('.csv.tmp').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['对象类型', '对象ID', '目录ID', '变化类型', '详情', '北京时间'])
        for row in rows:
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in row])
    os.replace(target.with_suffix('.csv.tmp'), target)


def export_association_failures(ledger):
    target = exports_dir(ledger.base_dir) / '关联核对异常.csv'
    rows = ledger.all('''SELECT d.project,d.reference,d.id,f.path,d.revision,d.filename,d.size,d.error
      FROM documents d JOIN folders f ON f.project=d.project AND f.id=d.folder_id
      WHERE d.active=1 AND f.active=1 AND d.related_state='failed' ORDER BY d.project,f.path,d.reference''')
    temp = target.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['工作区', '文档编号', '版本ID', '平台目录', '修订', '文件名', '主文件字节', '关联核对错误'])
        for row in rows:
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
    os.replace(temp, target)


def run_stages(client, ledger, args):
    workflow = Workflow(ledger, client)
    downloaded_in_run, meter = 0, None
    prepared = False

    def transfer(limit=None, only_ready=False):
        nonlocal downloaded_in_run, meter, prepared
        if args.max_files and downloaded_in_run >= args.max_files:
            return
        if not prepared:
            restored = workflow.repair_missing_downloads()
            if restored:
                print('本地缺失或大小不符文件已恢复为待下载：', restored, flush=True)
            if args.retry_failed:
                with ledger.db:
                    ledger.db.execute("UPDATE files SET attempts=0,status='pending' WHERE project=? AND active=1 AND status='failed' AND retryable=1", (client.project_id,))
            with ledger.db:
                ledger.db.execute('CREATE TEMP TABLE IF NOT EXISTS download_run_skips(project TEXT,file_id TEXT,PRIMARY KEY(project,file_id))')
                ledger.db.execute('DELETE FROM download_run_skips')
            prepared = True
        if only_ready and not ledger.one('''SELECT 1 FROM files i WHERE i.project=? AND i.active=1
                AND i.status IN ('pending','failed') AND i.retryable=1 AND i.attempts<3
                AND NOT EXISTS(SELECT 1 FROM download_run_skips s WHERE s.project=i.project AND s.file_id=i.id)
                AND EXISTS(SELECT 1 FROM document_files x JOIN documents d
                  ON d.project=x.project AND d.id=x.document_id AND d.folder_id=x.folder_id
                  WHERE x.project=i.project AND x.file_id=i.id AND x.active=1 AND d.active=1
                  AND d.related_state='done' AND d.checked_cycle=?) LIMIT 1''', (client.project_id, ledger.cycle)):
            return
        remaining = args.max_files - downloaded_in_run if args.max_files else None
        budget = min(limit, remaining) if limit is not None and remaining is not None else limit or remaining
        if meter is None:
            meter = DownloadProgress(ledger, client.project_id)
        if only_ready:
            print('分批推进：下载本轮已核对有效的原件和附件，未核对及失败项继续保留。', flush=True)
        downloaded_in_run += download(client, ledger, budget, progress=meter, reset_run_skips=False,
            time_budget_seconds=INTERLEAVED_DOWNLOAD_SECONDS if limit is not None else None) or 0

    def transfer_checked_batch():
        # A partial association queue cannot establish project-wide priority,
        # and a later parent may change the same content's metadata again.
        if ledger.one("SELECT 1 FROM documents WHERE project=? AND active=1 AND related_state='pending' LIMIT 1", (client.project_id,)):
            return
        transfer(INTERLEAVED_DOWNLOAD_BATCH_SIZE, only_ready=True)

    print('当前工作流程：', STAGES[workflow.state['stage']], '；进度点：', workflow.state['phase'], flush=True)
    if workflow.state['stage'] == 'scope_review' and args.action != 'scan' and not args.rescan:
        export_scope_review(ledger, client.project_id)
        print('全项目盘点已完成，等待人工确认施工范围；本次不下载。', flush=True)
        return 'awaiting_scope', 0
    if args.action == 'download':
        if not workflow.download_ready():
            raise RuntimeError('只下载模式需要完整清单；请先运行 auto 或 scan 完成阶段一。')
        if workflow.state['stage'] == 'inventory':
            workflow.complete_inventory()
        if not workflow.has_approved_scope():
            raise RuntimeError('施工范围尚未人工确认，不能启动下载。')
        workflow.save(phase='download')
    elif args.rescan or args.action == 'scan':
        workflow.begin_inventory(reset_initial=workflow.state['stage'] != 'maintenance')
    elif args.action == 'resume-scan' and workflow.state['stage'] == 'inventory' and workflow.state['phase'] == 'associations':
        print('续读修复：先刷新一次目录与主清单，替换失效旧版本；同一未完成关联轮次的已核对结果继续保留。', flush=True)
        workflow.begin_inventory(reset_initial=True)
    elif (workflow.state['stage'] == 'bulk_download' and workflow.state['phase'] in ('download','idle')
          and workflow.daily_inventory_due()):
        print('每日先刷新目录及版本清单；关联核对与有效文件下载分批推进，完整盘点后确认业主新增统计。',flush=True)
        workflow.begin_inventory(new_cycle=True)
    elif workflow.state['stage'] == 'maintenance' and workflow.state['phase'] in ('idle', 'download'):
        if workflow.maintenance_due():
            print('开始每日全量盘点，比较新增、更新及附件变化。', flush=True)
            workflow.begin_inventory()

    if workflow.state['snapshot_id'] is None:
        workflow.begin_inventory()
    emit(STAGES[workflow.state['stage']], workflow_stage=STAGES[workflow.state['stage']],
         cached_inventory=workflow.state['phase'] in ('download', 'idle'),
         directories=ledger.one('SELECT COUNT(*) n FROM folders WHERE project=? AND active=1', (client.project_id,))['n'],
         documents=ledger.one('SELECT COUNT(*) n FROM documents WHERE project=? AND active=1', (client.project_id,))['n'])
    if workflow.state['phase'] == 'catalogue':
        print('阶段一/每日维护：继续读取目录和文档清单，沿用分页进度。', flush=True)
        if not scan(client, ledger, args.max_folders):
            print('目录清单尚未完整，已保存进度，本次不下载。', flush=True)
            return 'inventory_incomplete', 2
        workflow.save(phase='associations')
    if workflow.state['phase'] == 'associations':
        workflow.refresh_failed_folders(check_window)
        print('目录清单已完成；继续核对原件与附件，已完成记录不会重读。', flush=True)
        with ledger.db:
            ledger.db.execute("UPDATE documents SET related_state='pending',error=NULL WHERE project=? AND active=1 AND related_state='failed'", (client.project_id,))
        interleaved = (args.action in ('auto', 'sync') and not args.rescan
                       and workflow.state['stage'] in ('bulk_download', 'maintenance')
                       and workflow.has_approved_scope())
        if interleaved:
            print(f'先完成本轮关联核对，再统一按指定单元、全新文件、更新文件排序；'
                  f'每批最多 {INTERLEAVED_DOWNLOAD_BATCH_SIZE} 个文件或 {INTERLEAVED_DOWNLOAD_SECONDS} 秒，单文件安全完成后再切换。', flush=True)
        if not refresh_associations(client, ledger, args.max_associations,
                **({'on_batch': transfer_checked_batch} if interleaved else {})):
            print(f'关联清单尚未完整；本次已下载 {downloaded_in_run} 个已核对文件；保留关联检查点，继续补查，未将盘点标记为完成。', flush=True)
            return 'inventory_incomplete', 2
        workflow.complete_inventory()
        from file_layout import has_pending_publications, prepare_downloads_or_defer
        if interleaved or has_pending_publications(ledger):
            def publication_cancelled():
                check_window()
                return False
            # Flush already verified versions even when --max-files was reached
            # in an earlier batch. Publication must never require redownloading.
            prepare_downloads_or_defer(ledger, publication_cancelled, reuse=True, reconcile=True)
        emit('清单盘点完成', workflow_stage=STAGES[workflow.state['stage']], cached_inventory=True)
        if not interleaved:
            try_export_ledger(ledger)
        else:
            print('完整关联检查点已保存；全量Excel导出延后至收尾或后台处理，优先继续文件下载。', flush=True)
        print('本轮变更：', json.dumps(ledger.changes_summary(), ensure_ascii=False), flush=True)
        if args.action in ('scan', 'resume-scan'):
            print('盘点完成，已生成体量及目录范围汇总；确认施工范围前不会下载。', flush=True)
            return ('awaiting_scope', 0) if workflow.state['stage'] == 'scope_review' else ('done', 0)

    if workflow.state['stage'] == 'scope_review':
        print('第一阶段完成，停止任务并注销，等待人工确认施工范围。', flush=True)
        emit(STAGES['scope_review'], workflow_stage=STAGES['scope_review'], cached_inventory=True)
        return 'awaiting_scope', 0

    if args.action == 'resume-scan':
        print('完整清单已经就绪；本入口只续读盘点，不执行文件下载。', flush=True)
        return 'done', 0

    if workflow.state['phase'] == 'download':
        if not workflow.download_ready():
            raise RuntimeError('下载阶段的完整清单检查未通过，停止而不使用不完整队列。')
        print(STAGES[workflow.state['stage']], '；使用已保存清单，只下载未完成文件。', flush=True)
        transfer()
        return ('done', 0) if workflow.finish_download() else ('download_pending', 2)
    if workflow.state['phase'] == 'idle':
        workflow.repair_missing_downloads()
        if workflow.state['phase'] == 'download':
            transfer()
            return ('done', 0) if workflow.finish_download() else ('download_pending', 2)
        maintain_local_references(ledger)
        print('今日无需再次盘点；等待下一运行日执行维护。', flush=True)
        return 'done', 0
    raise RuntimeError('无法识别的工作流程进度点。')


def scope_review_only(ledger):
    projects = ledger.all('SELECT DISTINCT project FROM snapshots UNION SELECT project FROM workflows')
    if not projects:
        return False
    workflows = [Workflow(ledger, SimpleNamespace(project_id=row['project'])) for row in projects]
    if not all(w.state['stage'] == 'scope_review' for w in workflows):
        return False
    for workflow in workflows:
        export_scope_review(ledger, workflow.project)
    export_csv(ledger)
    print('已完成全项目盘点，等待人工确认范围。本次不登录、不下载。', flush=True)
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description='FusionLive 文件目录、下载与离线台账')
    parser.add_argument('action', choices=['login-test', 'auto', 'resume-scan', 'scan', 'download', 'sync', 'status', 'export', 'verify', 'volume', 'push-report'])
    parser.add_argument('--max-files', type=int)
    parser.add_argument('--max-folders', type=int)
    parser.add_argument('--max-associations', type=int, help='本次关联核对上限；未核对完整时不进入下载')
    parser.add_argument('--rescan', action='store_true', help='显式重建本轮清单；正常三阶段运行不需要此参数')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--manual-test-outside-window', action='store_true', help='仅供人工确认后的单文件登录测试使用')
    args = parser.parse_args(argv)
    if args.manual_test_outside_window and args.action != 'login-test':
        parser.error('时段外人工确认仅适用于 login-test')
    for value in (args.max_files, args.max_folders, args.max_associations):
        if value is not None and value <= 0:
            parser.error('数量上限必须大于零')
    def interrupted(*_):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, interrupted)
    with RunLock():
        if os.environ.get('FUSIONLIVE_SCHEDULED') == '1' and (RUNTIME / 'scheduler.exit.request').exists():
            print('已收到调度退出请求，本次不启动同步。', flush=True)
            return 2
        if os.environ.get('FUSIONLIVE_SCHEDULED') == '1':
            if (RUNTIME / 'stop.request').exists():
                print('已收到同步停止请求，本次不启动同步。', flush=True)
                return 2
        else:
            (RUNTIME / 'stop.request').unlink(missing_ok=True)
        done = threading.Event()
        threading.Thread(target=watch_stop, args=(done,), daemon=True, name='stop-diagnostics').start()
        ledger = Ledger()
        try:
            if args.action in ('status', 'export', 'verify', 'volume', 'push-report'):
                if args.action == 'push-report':
                    print('日报快照已生成：',write_push_text(ledger))
                    return 0
                if args.action == 'volume':
                    print(json.dumps(volume(ledger), ensure_ascii=False, indent=2))
                    return 0
                if args.action == 'export':
                    export_csv(ledger)
                elif args.action == 'verify':
                    for item in ledger.all("SELECT * FROM files WHERE status='done' AND active=1"):
                        if not local_valid(item, ledger.base_dir):
                            with ledger.db:
                                ledger.db.execute("UPDATE files SET status='pending',error='本地校验失败',attempts=0 WHERE project=? AND id=?", (item['project'], item['id']))
                    print('本地校验完成。失效文件已放回待下载队列。')
                print(json.dumps(ledger.summary(), ensure_ascii=False, indent=2))
                return 0
            if args.action in ('auto', 'sync', 'download', 'resume-scan') and not args.rescan and scope_review_only(ledger):
                return 0
            from file_layout import has_pending_publications, prepare_downloads_or_defer
            if ((ledger.base_dir / 'runtime/layout.pending').exists()
                    or (args.action in ('auto', 'sync', 'download', 'resume-scan')
                        and has_pending_publications(ledger))):
                prepare_downloads_or_defer(ledger, cancelled=lambda: (RUNTIME / 'stop.request').exists())
            username, password = get_credentials()
            if args.action == 'login-test':
                from login_test import TestClient, check_test_window
                check_test_window(manual_override=args.manual_test_outside_window)
                client = TestClient(username, password, manual_override=args.manual_test_outside_window)
            else:
                client = FusionClient(username, password)
            run_id = ledger.start_run(args.action)
            status, error, code = 'done', None, 0
            try:
                with client:
                    if args.action == 'login-test':
                        from login_test import download_one
                        status, code = download_one(client, ledger)
                    else:
                        status, code = run_stages(client, ledger, args)
            except (KeyboardInterrupt, WindowClosed) as exc:
                status, error, code = 'stopped', type(exc).__name__, 2
                print('任务已停止，已保存进度。')
            except Exception as exc:
                status, error, code = 'failed', str(exc), 1
                print('任务失败：', error, file=sys.stderr)
                import traceback
                traceback.print_exc()
                if isinstance(exc, AuthenticationRequiredError):
                    (RUNTIME / 'login.paused').write_text(f'{type(exc).__name__}：{exc}', encoding='utf-8')
            finally:
                confirmed = bool(client.logout_result and client.logout_result['confirmed'])
                if client.auth_attempted and not confirmed:
                    status, code = 'logout_unconfirmed', 3
                finish_run(ledger, run_id, status, confirmed, error)
                print(json.dumps(ledger.summary(), ensure_ascii=False))
            return code
        finally:
            done.set()
            ledger.close()


if __name__ == '__main__':
    sys.exit(logged_main(main))
