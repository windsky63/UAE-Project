"""Resumable read-only catalogue and file downloader. See docs/使用说明.txt."""

from app_paths import HOME, CODE
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

from fusion_client import FusionClient, HOME, RUNTIME, WindowClosed, LogoutError, SessionExpiredError, now, check_window
from ledger import Ledger, safe_name
from paths import relative_path, resolve_path, native_path, exports_dir
from reporting import record_download, write_summary, logged_main, volume, try_write_push_text
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
    print('任务状态已保存；正在生成下载日报。', flush=True)
    write_summary(ledger, run_id)
    if (RUNTIME / 'stop.request').exists() or (RUNTIME / 'scheduler.exit.request').exists():
        print('停止请求已生效：完整Excel台账延后至手动导出，未完成下载将在下次启动恢复。', flush=True)
        return
    ledger.clean_partial_downloads()
    print('正在导出完整Excel台账。', flush=True)
    export_csv(ledger)


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


def refresh_associations(client, ledger, max_documents=None):
    count, attempted, consecutive_failures = 0, 0, 0
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
                if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code in (401, 403, 418, 429):
                    raise
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
            process(document, '补查')
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


def priority_download_keys(ledger, project):
    from business_type import ledger_file_contexts
    contexts = ledger_file_contexts(ledger.db)
    result = []
    for item in ledger.all("SELECT project,id,filename FROM files WHERE project=? AND active=1 AND status IN ('pending','failed')", (project,)):
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
      ORDER BY EXISTS(SELECT 1 FROM download_unit_priority p WHERE p.project=i.project AND p.file_id=i.id) DESC,
               EXISTS(SELECT 1 FROM download_update_priority u WHERE u.project=i.project AND u.file_id=i.id),i.rowid
      LIMIT 1''', (project, ledger.cycle))


def download(client, ledger, max_files=None):
    from file_layout import prepare_downloads, local_update_keys, LayoutError
    def migration_cancelled():
        check_window()
        return False
    ledger.recover_download_receipts(check_window)
    prepare_downloads(ledger, migration_cancelled)
    analysis_database = ledger.db.execute('PRAGMA database_list').fetchone()[2]
    notify_download_completed(ledger.base_dir, analysis_database)
    project = client.project_id
    processed = 0
    progress = DownloadProgress(ledger, project)
    updates = local_update_keys(ledger)
    priority = priority_download_keys(ledger, project)
    with ledger.db:
        ledger.db.execute('CREATE TEMP TABLE IF NOT EXISTS download_update_priority(project TEXT,file_id TEXT,PRIMARY KEY(project,file_id))')
        ledger.db.execute('DELETE FROM download_update_priority')
        ledger.db.executemany('INSERT INTO download_update_priority VALUES(?,?)', updates)
        ledger.db.execute('CREATE TEMP TABLE IF NOT EXISTS download_unit_priority(project TEXT,file_id TEXT,PRIMARY KEY(project,file_id))')
        ledger.db.execute('DELETE FROM download_unit_priority')
        ledger.db.executemany('INSERT INTO download_unit_priority VALUES(?,?)', priority)
    print(f'优先下载单元：213、27、37、38、39；命中待下载文件 {len(priority)} 个。', flush=True)
    while not max_files or processed < max_files:
        check_window()
        selection_started = time.monotonic()
        item = next_download_item(ledger, project)
        selection_seconds = time.monotonic() - selection_started
        if item is None:
            break
        progress.begin(item)
        target = resolve_path(item['target_path'], ledger.base_dir) if item['target_path'] else file_target(client, ledger, item)
        # Download to a separate path. The existing journal publishes both versions
        # only after the new file has passed the client's size/hash checks.
        publish_needed = (project, item['id']) in updates
        staged = ((ledger.base_dir / 'runtime/download_ready' /
                   hashlib.sha256((project + '\0' + item['id']).encode()).hexdigest() / 'content')
                  if publish_needed else target)
        with ledger.db:
            ledger.db.execute("UPDATE files SET status='downloading',attempts=attempts+1,error=NULL,target_path=? WHERE project=? AND id=?", (relative_path(target, ledger.base_dir), project, item['id']))
        try:
            if target.exists() and (project, item['id']) not in updates:
                raise LayoutError('下载目标已存在，未覆盖；请先运行路径迁移并核对台账：' + str(target))
            staged.parent.mkdir(parents=True, exist_ok=True)
            transfer_started = time.monotonic()
            result = client.download(json.loads(item['metadata']), staged, progress=progress.update,
                verified=lambda result: ledger.save_download_receipt(item, result, target))
            transfer_seconds = time.monotonic() - transfer_started
            publication_started = time.monotonic()
            completed = now().isoformat()
            with ledger.db:
                ledger.db.execute("UPDATE files SET status='done',bytes=?,path=?,sha256=?,error=NULL,updated=?,downloaded_at=?,download_date_source='下载完成实录' WHERE project=? AND id=?",
                                  (result['bytes'], relative_path(result['path'], ledger.base_dir), result['sha256'], completed, completed, project, item['id']))
                ledger.db.execute('DELETE FROM download_receipts WHERE project=? AND file_id=?', (project, item['id']))
            if publish_needed:
                key = (project, item['id'])
                prepare_downloads(ledger, migration_cancelled,
                                  keys=ledger._publication_groups[key])
                result['path'] = str(resolve_path(ledger.one('SELECT path FROM files WHERE project=? AND id=?', (project, item['id']))['path'], ledger.base_dir))
            record_download(ledger.base_dir, item, 'done', result['bytes'], relative_path(result['path'], ledger.base_dir))
            processed += 1
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
            ledger.clean_partial_for(target)
            with ledger.db:
                ledger.db.execute("UPDATE files SET status='pending',attempts=0,error='任务停止，等待继续' WHERE project=? AND id=?", (project, item['id']))
            record_download(ledger.base_dir, item, 'stopped', error='安全中断，未完成临时文件已清理，下次从头下载此文件')
            raise
        except Exception as exc:
            if ledger.one('SELECT status FROM files WHERE project=? AND id=?', (project, item['id']))['status'] == 'done':
                raise  # Resume publishing on the next run; never download again.
            progress.failed()
            ledger.clean_partial_for(target)
            record_download(ledger.base_dir, item, 'failed', error=str(exc))
            permanent = (isinstance(exc, LayoutError) or
                         (isinstance(exc, requests.HTTPError) and exc.response is not None and
                          exc.response.status_code in (400, 404, 405, 410, 413, 415, 422)))
            with ledger.db:
                ledger.db.execute("UPDATE files SET status='failed',error=?,updated=?,retryable=? WHERE project=? AND id=?",
                                  (str(exc), now().isoformat(), int(not permanent), project, item['id']))
            if isinstance(exc, requests.HTTPError) and exc.response.status_code in (401, 403, 418, 429):
                raise
            if isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                raise
            print('下载失败，已记入台账：', item['filename'], type(exc).__name__, flush=True)
            if permanent:
                print('永久性错误，不自动重试；请核对源文件或下载路径。', flush=True)
            time.sleep(1)


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
        print('每日先完整盘点目录及原件/附件，统计业主新增，再继续未完成下载。',flush=True)
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
        print('目录清单已完成；继续核对原件与附件，已完成记录不会重读。', flush=True)
        with ledger.db:
            ledger.db.execute("UPDATE documents SET related_state='pending',error=NULL WHERE project=? AND active=1 AND related_state='failed'", (client.project_id,))
        if not refresh_associations(client, ledger, args.max_associations):
            print('关联清单尚未完整，已保存进度，本次不下载。', flush=True)
            return 'inventory_incomplete', 2
        workflow.complete_inventory()
        emit('清单盘点完成', workflow_stage=STAGES[workflow.state['stage']], cached_inventory=True)
        export_csv(ledger)
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
        restored = workflow.repair_missing_downloads()
        if restored:
            print('本地缺失或大小不符文件已恢复为待下载：', restored, flush=True)
        if args.retry_failed:
            with ledger.db:
                ledger.db.execute("UPDATE files SET attempts=0,status='pending',retryable=1 WHERE project=? AND active=1 AND status='failed'", (client.project_id,))
        print(STAGES[workflow.state['stage']], '；使用已保存清单，只下载未完成文件。', flush=True)
        download(client, ledger, args.max_files)
        return ('done', 0) if workflow.finish_download() else ('download_pending', 2)
    if workflow.state['phase'] == 'idle':
        workflow.repair_missing_downloads()
        if workflow.state['phase'] == 'download':
            download(client, ledger, args.max_files)
            return ('done', 0) if workflow.finish_download() else ('download_pending', 2)
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
                    target = try_write_push_text(ledger)
                    if target:
                        print('微信推送文已生成：', target)
                    return 0 if target else 1
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
            if (ledger.base_dir / 'runtime/layout.pending').exists():
                from file_layout import prepare_downloads
                prepare_downloads(ledger, cancelled=lambda: (RUNTIME / 'stop.request').exists())
            username, password = get_credentials()
            if args.action == 'login-test':
                from login_test import TestClient, check_test_window
                check_test_window(manual_override=args.manual_test_outside_window)
                client = TestClient(username, password, manual_override=args.manual_test_outside_window)
            else:
                client = FusionClient(username, password)
            run_id = ledger.start_run(args.action)
            try_write_push_text(ledger)
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
