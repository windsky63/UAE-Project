"""Retain the first downloaded content and two immediate predecessor contents.

Only registered history files are eligible. Metadata/version evidence remains;
physical deletion is journaled and can resume after a crash.
"""
from collections import defaultdict, deque
from contextlib import ExitStack, closing
import json
import os
import sqlite3
import time
from pathlib import Path

from analysis_worker import worker_lock
from file_layout import confined, download_structure_token, PublicationChecks
from fusion_client import now
from local_reconcile import backup, reconciler


def _families(ledger, files):
    own = defaultdict(list)
    for row in ledger.all('SELECT project,id,first_id,active FROM documents'):
        own[(row['project'],row['id'])].append(dict(row))
    result = {}
    for row in files:
        key = (row['project'],row['id'])
        documents = own[key]
        data = json.loads(row['metadata'] or '{}')
        first = next((d['first_id'] for d in documents if d['first_id']),None)
        if first:
            family = (row['project'],'document',str(first))
        elif data.get('firstVersionId'):
            # Attachment members of a package remain separate version families.
            family = (row['project'],row['relation'],str(data['firstVersionId']),row['filename'].casefold())
        else:
            family = (row['project'],'file',row['id'])
        result[key] = (family,any(d['active'] for d in documents))
    return result


class HistoryRetention:
    def __init__(self, ledger, check, *, log=None):
        self.ledger, self.check = ledger, check
        self.log = log if log is not None else lambda message: print(message,flush=True)
        self.queue = deque()
        self.signature = None
        self.backed_up = False
        self.storage_ready = False
        self.deleted_files = self.deleted_bytes = self.cleared_missing = self.skipped_files = 0
        self.planned_groups = self.planned_files = 0
        self.phase, self.current_path = '规划保留范围', ''

    def _prepare_storage(self):
        if self.storage_ready:
            return
        # Every deletion rechecks live references and clears paths. Without
        # these indexes, each of those safety checks scans the entire ledger.
        # Keep the deletion journal and its crash-recovery transactions intact.
        with self.ledger.db:
            self.ledger.db.execute('CREATE INDEX IF NOT EXISTS files_retention_path ON files(path)')
            self.ledger.db.execute('CREATE INDEX IF NOT EXISTS file_history_retention_path ON file_history(path)')
            self.ledger.db.execute('''CREATE TABLE IF NOT EXISTS history_retention_jobs(
                path TEXT PRIMARY KEY,payload TEXT,state TEXT,created TEXT)''')
            self.ledger.db.execute('''CREATE INDEX IF NOT EXISTS history_retention_pending
                ON history_retention_jobs(state) WHERE state='pending' ''')
        self.storage_ready = True

    def _plan(self):
        ledger = self.ledger
        files = [dict(r) for r in ledger.all('SELECT * FROM files')]
        histories = [dict(r) for r in ledger.all('SELECT * FROM file_history WHERE path IS NOT NULL')]
        families = _families(ledger,files)
        groups, owners = defaultdict(list), defaultdict(list)
        active_paths = {r['path'].casefold() for r in files if r['active'] and r['path']}
        currents = defaultdict(list)
        for row in files:
            family, own_active = families[(row['project'],row['id'])]
            if row['active']:
                currents[family].append((row,own_active))
            if row['path']:
                owners[row['path'].casefold()].append((family,row))
            if not row['active'] and row['path'] and row['path'].startswith('history/') and row['sha256']:
                groups[family].append(dict(path=row['path'],sha256=row['sha256'],bytes=row['bytes'],
                    rank=(row['downloaded_at'] or row['updated'] or '',0)))
        for row in histories:
            family = families.get((row['project'],row['file_id']),((row['project'],'file',row['file_id']),False))[0]
            owners[row['path'].casefold()].append((family,row))
            if row['sha256']:
                groups[family].append(dict(path=row['path'],sha256=row['sha256'],bytes=row['bytes'],
                                          rank=(row['archived'] or '',row['id'])))
        for family,versions in groups.items():
            current = currents[family]
            authoritative = [r for r,own in current if own] or [r for r,_ in current]
            if len(authoritative)!=1:
                continue
            latest = authoritative[0]
            if (latest['status']!='done' or not latest['sha256'] or not latest['path']
                    or not latest['path'].startswith('downloads/')
                    or any(r['status'] in ('pending','downloading') or
                           (r['path'] or '').startswith('runtime/') for r,_ in current)):
                continue
            by_content = defaultdict(list)
            for version in versions:
                by_content[(version['sha256'],version['bytes'])].append(version)
            latest_content = (latest['sha256'],latest['bytes'])
            ordered = sorted((v for v in by_content if v!=latest_content),
                             key=lambda v:min(r['rank'] for r in by_content[v]))
            initial = min(by_content,key=lambda v:min(r['rank'] for r in by_content[v]))
            retained = set(ordered[-2:])
            if initial!=latest_content:
                retained.add(initial)
            anchors = [deque([(latest['path'],latest['bytes'],latest['sha256'])])]
            protected = set()
            valid = True
            for content in retained:
                # Prefer the earliest registered existing copy; never delete the
                # last copy of a retained content merely because its path broke.
                candidates = sorted(by_content[content],key=lambda r:r['rank'])
                # Verification chooses an existing canonical copy incrementally.
                # Metadata planning itself performs no full-tree stat/hash pass.
                present = candidates[0]
                protected.add(present['path'].casefold())
                anchors.append(deque((r['path'],r['bytes'],r['sha256']) for r in candidates))
            if not valid:
                continue
            deletes = {}
            for version in versions:
                path = version['path']
                key = path.casefold()
                if not path.startswith('history/') or key in active_paths or key in protected:
                    continue
                refs = owners[key]
                if any(f!=family or r['sha256']!=version['sha256'] or r['bytes']!=version['bytes'] for f,r in refs):
                    continue
                deletes[key] = version
            if deletes:
                self.queue.append(dict(anchors=deque(anchors),guards=[],deletes=deque(deletes.values())))

    def _finish_delete(self, payload):
        ledger = self.ledger
        path = payload['path']
        with ledger.db:
            ledger.db.execute('UPDATE file_history SET path=NULL WHERE path=? AND sha256=? AND bytes=?',
                              (path,payload['sha256'],payload['bytes']))
            ledger.db.execute('UPDATE files SET path=NULL,target_path=NULL WHERE active=0 AND path=? AND sha256=? AND bytes=?',
                              (path,payload['sha256'],payload['bytes']))
            if ledger.one("SELECT 1 FROM sqlite_master WHERE name='history_paths'"):
                ledger.db.execute('DELETE FROM history_paths WHERE path=?',(path,))
            ledger.db.execute("UPDATE history_retention_jobs SET state='done' WHERE path=?",(path,))

    def _pending(self, service, deadline, payloads=None):
        if payloads is None:
            if not self.ledger.one("SELECT 1 FROM sqlite_master WHERE name='history_retention_jobs'"):
                return 0
            payloads = (json.loads(row['payload']) for row in self.ledger.all(
                "SELECT payload FROM history_retention_jobs WHERE state='pending'"))
        removed = 0
        for payload in payloads:
            if time.monotonic()>=deadline:
                break
            self.check()
            self.phase, self.current_path = '清理过期副本', payload['path']
            path = confined(self.ledger.base_dir,payload['path'])
            if not payload['path'].startswith('history/'):
                continue
            # New references invalidate an old deletion intent.
            if self.ledger.one('SELECT 1 FROM files WHERE active=1 AND path=?',(payload['path'],)):
                continue
            absolute = str(path).replace('\\','/').casefold()
            if any(p==absolute or p.startswith(absolute+'!') or p.startswith(absolute+'/')
                   for p in getattr(self,'reader_paths',())):
                continue
            if path.exists():
                ready = True
                for relative,size,digest in payload.get('guards',()):
                    verified = service.verify(relative,size,digest,deadline)
                    if verified is None:
                        return removed
                    if not verified:
                        ready = False
                        break
                if not ready:
                    continue
                # Do not remove a path newly claimed by another version.
                for table in ('files','file_history'):
                    if self.ledger.one(f'SELECT 1 FROM {table} WHERE path=? AND (sha256 IS NOT ? OR bytes IS NOT ?)',
                                       (payload['path'],payload['sha256'],payload['bytes'])):
                        ready = False
                if not ready:
                    continue
                matched = service.verify(payload['path'],payload['bytes'],payload['sha256'],deadline)
                if matched is None:
                    break
                if not matched:
                    continue
                with ExitStack() as guards:
                    # Keep verified bytes protected through the unlink/DB commit.
                    for relative,_,_ in [*payload.get('guards',()),
                                          (payload['path'],payload['bytes'],payload['sha256'])]:
                        candidate = confined(self.ledger.base_dir,relative)
                        handle = guards.enter_context(PublicationChecks.guard(candidate))
                        proof = service.hashes.get(relative)
                        if (not proof or PublicationChecks.stamp(candidate.stat())!=proof[0]
                                or PublicationChecks.stamp(os.fstat(handle.fileno()))!=proof[2]):
                            ready = False
                            break
                    if not ready:
                        continue
                    with self.ledger.db:
                        # Acquire the SQLite writer only for final reference
                        # checks and deletion, never while hashing large files.
                        self.ledger.db.execute('UPDATE history_retention_jobs SET state=state WHERE path=?',
                                               (payload['path'],))
                        if self.ledger.one('SELECT 1 FROM files WHERE active=1 AND path=?',(payload['path'],)):
                            continue
                        if any(self.ledger.one(f'SELECT 1 FROM {table} WHERE path=? AND (sha256 IS NOT ? OR bytes IS NOT ?)',
                                (payload['path'],payload['sha256'],payload['bytes'])) for table in ('files','file_history')):
                            continue
                        path.unlink()
                        self.deleted_files += 1
                        self.deleted_bytes += payload['bytes']
                        self._finish_delete(payload)
                removed += 1
                continue
            self._finish_delete(payload)
            self.cleared_missing += 1
            removed += 1
        return removed

    def tick(self, seconds=.5):
        ledger = self.ledger
        if ((ledger.base_dir/'runtime/layout.pending').exists() or
                ledger.one("SELECT 1 FROM refresh_cycles WHERE status!='complete'") or
                not ledger.one("SELECT 1 FROM refresh_cycles WHERE status='complete'")):
            return 0
        deadline = time.monotonic()+seconds
        with ExitStack() as stack:
            for folder in ('runtime/file_analysis','runtime/wechat_delivery'):
                if not stack.enter_context(worker_lock(ledger.base_dir/folder)):
                    return 0
            self.reader_paths = set()
            queue = ledger.base_dir/'runtime/file_analysis/queue.sqlite3'
            if queue.is_file():
                with closing(sqlite3.connect(queue.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                    if db.execute("SELECT 1 FROM sqlite_master WHERE name='jobs'").fetchone():
                        columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
                        if {'active','state','path','origin'} <= columns:
                            for path,origin in db.execute("SELECT path,origin FROM jobs WHERE active=1 AND state!='done'"):
                                self.reader_paths.update(str(p).replace('\\','/').casefold() for p in (path,origin) if p)
            self._prepare_storage()
            service = reconciler(ledger,self.check)
            removed = self._pending(service,deadline)
            signature = (download_structure_token(ledger),ledger.one('SELECT MAX(id) FROM file_history')[0],
                         ledger.one("SELECT MAX(updated) FROM files WHERE active=1 AND status='done'")[0])
            if not self.queue and signature!=self.signature:
                planning_started = time.monotonic()
                self._plan()
                self.signature = signature
                candidates = sum(len(group['deletes']) for group in self.queue)
                self.planned_groups, self.planned_files = len(self.queue), candidates
                self.log(f'历史保留规划：{len(self.queue)} 组、{candidates} 个登记候选待核验；'
                         f'规划耗时 {time.monotonic()-planning_started:.3f} 秒。')
                # Large ledgers can take longer to plan than the whole work
                # slice. Always grant a bounded execution slice AFTER planning;
                # otherwise a fresh scheduled process may never reach hashing.
                deadline = time.monotonic()+seconds
            while self.queue and time.monotonic()<deadline:
                self.check()
                group = self.queue[0]
                if group['anchors']:
                    candidates = group['anchors'][0]
                    if not candidates:
                        self.skipped_files += len(group['deletes'])
                        self.queue.popleft()
                        continue
                    path,size,digest = candidates[0]
                    self.phase, self.current_path = '核验保留副本', path
                    match = service.verify(path,size,digest,deadline)
                    if match is None:
                        break
                    if not match:
                        candidates.popleft()
                        continue
                    group['guards'].append((path,size,digest))
                    group['anchors'].popleft()
                    continue
                if not group['deletes']:
                    self.queue.popleft()
                    continue
                payload = group['deletes'][0]
                if payload['path'] in {r[0] for r in group['guards']}:
                    group['deletes'].popleft()
                    continue
                payload = dict(payload,guards=group['guards'])
                path = confined(ledger.base_dir,payload['path'])
                if path.exists():
                    matched = service.verify(payload['path'],payload['bytes'],payload['sha256'],deadline)
                    if matched is None:
                        break
                    if not matched:
                        self.skipped_files += 1
                        group['deletes'].popleft()
                        continue
                if not self.backed_up:
                    backup(ledger,'before-history-retention')
                    self.backed_up = True
                with ledger.db:
                    ledger.db.execute("INSERT OR REPLACE INTO history_retention_jobs VALUES(?,?,'pending',?)",
                        (payload['path'],json.dumps(payload,ensure_ascii=False),now().isoformat()))
                # Old interrupted jobs were already retried at the start of
                # this slice. Process only this new intent, rather than scanning
                # the journal and retrying every pinned job for every deletion.
                removed += self._pending(service,deadline,payloads=(payload,))
                group['deletes'].popleft()
            if removed:
                self.log(f'历史保留策略：清理 {removed} 个过期或重复副本；保留初次下载内容及最近两次旧内容。')
            return removed


def prune_history(ledger, check=lambda: None, seconds=.5):
    service = getattr(ledger,'_history_retention',None)
    if service is None:
        service = ledger._history_retention = HistoryRetention(ledger,check)
    service.check = check
    return service.tick(seconds)


class ManualStopped(Exception):
    pass


PROGRESS_PREFIX = '@history-cleanup '


def run_manual(base, job_id=None, *, progress=None, cancelled=lambda: False):
    """Only the explicit manual action calls the retention engine."""
    import uuid
    from ledger import Ledger
    base = Path(base).resolve()
    runtime = base/'runtime'
    if not (runtime/'ledger.sqlite3').is_file():
        raise ValueError('未找到下载台账，未执行清理。')
    identity = job_id or uuid.uuid4().hex
    started, last_write = now().isoformat(), [0.0]
    service = None
    owns_lock = False
    def write(state='running',message='',force=False):
        if not force and time.monotonic()-last_write[0]<.5:
            return
        data = dict(job_id=identity,state=state,manual_only=True,started=started,updated=now().isoformat(),
                    phase=service.phase if service else '准备清理',current_file=service.current_path if service else '',
                    planned_files=service.planned_files if service else 0,
                    remaining_groups=len(service.queue) if service else 0,
                    deleted_files=service.deleted_files if service else 0,
                    deleted_bytes=service.deleted_bytes if service else 0,
                    cleared_missing=service.cleared_missing if service else 0,
                    skipped_files=service.skipped_files if service else 0,message=message)
        try:
            if progress:
                progress(data)
        except (OSError,ValueError):
            # Losing the display channel must not damage deletion recovery.
            pass
        finally:
            last_write[0] = time.monotonic()
    def check():
        if cancelled():
            raise ManualStopped()
        write()
    try:
        with ExitStack() as stack:
            if not stack.enter_context(worker_lock(runtime,'history_retention.lock')):
                raise ValueError('历史清理任务正在运行，请查看当前进度。')
            owns_lock = True
            write(force=True)
            if not stack.enter_context(worker_lock(runtime,'sync.lock')):
                raise ValueError('同步下载正在运行，请先安全停止下载后再手动清理。')
            if (runtime/'layout.pending').exists():
                raise ValueError('文件迁移尚未完成，请先恢复迁移后再手动清理。')
            ledger = stack.enter_context(closing(Ledger(runtime/'ledger.sqlite3',base_dir=base)))
            if (not ledger.one("SELECT 1 FROM refresh_cycles WHERE status='complete'") or
                    ledger.one("SELECT 1 FROM refresh_cycles WHERE status!='complete'")):
                raise ValueError('目录盘点尚未完成，未执行清理。')
            service = HistoryRetention(ledger,check,log=lambda message: None)
            while True:
                check()
                service.tick(seconds=5)
                write(force=True)
                if service.signature is None:
                    raise ValueError('后台解析或文件投递正在使用文件，请先安全停止相关任务后再手动清理。')
                if not service.queue:
                    break
            write('succeeded','手动清理完成；仍被引用或无法核验的文件保留。',True)
            print(f'手动历史清理完成：删除 {service.deleted_files} 个副本，释放 {service.deleted_bytes} 字节；'
                  f'清理 {service.cleared_missing} 条失效路径，跳过 {service.skipped_files} 个候选。',flush=True)
        return 0
    except (ManualStopped,KeyboardInterrupt):
        if owns_lock:
            write('stopped','已安全停止；保留文件、核验和删除恢复记录。',True)
        print('手动历史清理已安全停止；已完成的删除和恢复记录保留。',flush=True)
        return 0
    except Exception as exc:
        write('failed',str(exc),True)
        print('手动历史清理未完成：'+str(exc),flush=True)
        return 2


def main(argv=None):
    import argparse
    import sys
    import threading
    from paths import HOME
    parser = argparse.ArgumentParser(description='仅手动清理历史文件；不会由同步或调度自动执行。')
    parser.add_argument('--base-dir',type=Path,default=HOME)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--execute',action='store_true')
    mode.add_argument('--stop',action='store_true')
    parser.add_argument('--job-id')
    parser.add_argument('--progress-pipe',action='store_true',help='通过标准输出发送进度、标准输入接收安全停止命令')
    args = parser.parse_args(argv)
    if args.stop:
        parser.error('请在启动清理的管理中心点击停止；独立命令行清理请按Ctrl+C。')
    stop = threading.Event()
    def commands():
        try:
            pending = b''
            while True:
                block = os.read(sys.stdin.fileno(),4096)
                if not block:
                    break
                pending += block
                while b'\n' in pending:
                    line,pending = pending.split(b'\n',1)
                    try:
                        command = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(command,dict) and command.get('command')=='stop' and command.get('job_id')==args.job_id:
                        stop.set()
        except OSError:
            pass
        finally:
            # The manager closed: do not leave an unmanageable cleanup behind.
            stop.set()
    def emit(data):
        print(PROGRESS_PREFIX+json.dumps(data,ensure_ascii=False),flush=True)
    if args.progress_pipe:
        threading.Thread(target=commands,daemon=True,name='history-cleanup-control').start()
    try:
        return run_manual(args.base_dir,args.job_id,progress=emit if args.progress_pipe else None,cancelled=stop.is_set)
    except BrokenPipeError:
        # Manager disconnected after safe stop; avoid another stdout failure
        # during interpreter shutdown.
        sys.stdout = open(os.devnull,'w')
        return 0


if __name__=='__main__':
    raise SystemExit(main())
