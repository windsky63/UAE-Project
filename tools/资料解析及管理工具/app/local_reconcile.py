"""Conflict-only disk reconciliation. Normal downloads never walk these trees."""
from collections import defaultdict, deque
from contextlib import closing
from pathlib import Path
import hashlib
import json
import os
import sqlite3
import time

from file_layout import confined, PublicationChecks, LayoutError
from fusion_client import now
from paths import relative_path


def backup(ledger, label):
    if ledger.db.in_transaction:
        raise LayoutError('当前数据库事务尚未提交，磁盘修复和历史清理稍后继续。')
    folder = ledger.base_dir/'runtime/backups'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder/(label+'-'+now().strftime('%Y%m%d-%H%M%S-%f')+'.sqlite3')
    with closing(sqlite3.connect(target)) as db:
        ledger.db.backup(db)
    return target


class DiskReconciler:
    def __init__(self, ledger, check=lambda: None):
        self.ledger, self.check = ledger, check
        self.jobs, self.seen = {}, set()
        self.hashes, self.partial = {}, {}
        self.index = defaultdict(list)
        self.walk = None
        self.scan_complete = False
        self.scan_errors = False
        self.backed_up = False
        self.visited = {}
        self.saved_pending = []
        self.scan_expected = set()
        self.scan_cycle = None
        self.inflight = None
        self.saved_jobs = []

    def _restore_scan(self):
        cycle = json.dumps([tuple(r) for r in self.ledger.all('SELECT project,cycle FROM refresh_cycles ORDER BY project')])
        if self.scan_cycle==cycle:
            return
        if self.scan_cycle is not None:
            self.jobs.clear()
            self.seen.clear()
        self.scan_cycle = cycle
        self.visited, self.saved_pending, self.scan_expected, self.saved_jobs = {}, [], set(), []
        self.walk, self.scan_complete, self.scan_errors = None, False, False
        if not self.ledger.one("SELECT 1 FROM sqlite_master WHERE name='local_reconcile_scan_state'"):
            return
        row = self.ledger.one('SELECT cycle,payload FROM local_reconcile_scan_state WHERE id=1')
        if row and row['cycle']==cycle:
            state = json.loads(row['payload'])
            self.visited = state['visited']
            self.saved_pending = state['pending']
            self.scan_expected = {tuple(r) for r in state['expected']}
            self.scan_complete = state['complete']
            self.scan_errors = state['errors']
            self.saved_jobs = [tuple(r) for r in state.get('jobs',())]

    def _save_scan(self):
        if self.scan_cycle is None:
            return
        pending = {p for j in self.jobs.values() for p in j['candidates']}
        if self.inflight:
            pending.add(self.inflight)
        state = dict(visited=self.visited,pending=sorted(pending),expected=sorted(self.scan_expected),
                     complete=self.scan_complete,errors=self.scan_errors,
                     jobs=[('history' if j['history'] else 'file',j['row']['project'],j['row']['id'])
                           for j in self.jobs.values()])
        with self.ledger.db:
            self.ledger.db.execute('''CREATE TABLE IF NOT EXISTS local_reconcile_scan_state(
                id INTEGER PRIMARY KEY,cycle TEXT,payload TEXT)''')
            self.ledger.db.execute('INSERT OR REPLACE INTO local_reconcile_scan_state VALUES(1,?,?)',
                                  (self.scan_cycle,json.dumps(state,ensure_ascii=False)))

    def requeue_cleared_targets(self, rows):
        """An old target-exists failure can retry once the blocker has moved."""
        ledger, changed = self.ledger, 0
        if (ledger.base_dir/'runtime/layout.pending').exists():
            return 0
        for row in rows:
            self.check()
            path = row['target_path']
            if not path or confined(ledger.base_dir,path).exists():
                continue
            if not self.backed_up:
                backup(ledger,'before-local-reconcile')
                self.backed_up = True
            with ledger.db:
                count = ledger.db.execute("""UPDATE files SET status='pending',retryable=1,attempts=0,
                    error='原目标路径冲突已排除，等待下载' WHERE project=? AND id=? AND target_path=?
                    AND status='failed' AND retryable=0 AND error LIKE '下载目标已存在%'""",
                    (row['project'],row['id'],path)).rowcount
                ledger.db.execute('''CREATE TABLE IF NOT EXISTS local_reconcile_audit(
                    id INTEGER PRIMARY KEY,created TEXT,payload TEXT)''')
                if count:
                    ledger.db.execute('INSERT INTO local_reconcile_audit(created,payload) VALUES(?,?)',
                        (now().isoformat(),json.dumps(dict(kind='requeue',project=row['project'],
                         id=row['id'],path=path),ensure_ascii=False)))
            changed += count
        if changed:
            print(f'原路径冲突已排除，重新加入下载队列：{changed} 个文件。',flush=True)
        return changed

    def verify(self, relative, size, digest, deadline):
        """Resume large hashes across bounded idle slices; never trust size alone."""
        self.check()
        if time.monotonic() >= deadline:
            return None
        path = confined(self.ledger.base_dir, relative)
        try:
            before = path.stat()
            if not path.is_file() or before.st_size != size:
                return False
            stamp = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
            with PublicationChecks.guard(path) as stream:
                opened = os.fstat(stream.fileno())
                held = PublicationChecks.stamp(opened)
                width = 4 if os.name=='nt' else 5
                if stamp[:width] != held[:width]:
                    return False
                cached = self.hashes.get(relative)
                if cached and cached[0]==stamp and cached[2]==held:
                    return cached[1]==digest
                partial = self.partial.get(relative)
                offset,value = ((partial[1],partial[2]) if partial and partial[0]==stamp and partial[3]==held
                                else (0,hashlib.sha256()))
                stream.seek(offset)
                while True:
                    self.check()
                    if time.monotonic() >= deadline:
                        self.partial[relative] = (stamp, offset, value, held)
                        return None
                    block = stream.read(1024*1024)
                    if not block:
                        break
                    value.update(block)
                    offset += len(block)
                held_after = PublicationChecks.stamp(os.fstat(stream.fileno()))
            after = path.stat()
            if stamp != PublicationChecks.stamp(after) or held!=held_after:
                self.partial.pop(relative, None)
                return False
            self.hashes[relative] = (stamp, value.hexdigest(), held_after)
            self.partial.pop(relative, None)
            return value.hexdigest() == digest
        except FileNotFoundError:
            return False

    def resume(self):
        if self.jobs:
            return
        self._restore_scan()
        if self.saved_jobs:
            identities = set(self.saved_jobs)
            keys = set()
            for kind,project,identifier in identities:
                if kind=='history':
                    row = self.ledger.one('SELECT file_id FROM file_history WHERE project=? AND id=?',(project,identifier))
                    if row:
                        keys.add((project,row['file_id']))
                else:
                    keys.add((project,identifier))
            self.schedule(keys=keys,only=identities)

    def schedule(self, *, paths=(), keys=(), only=None):
        """Use recorded candidates first, including completed migration targets."""
        ledger = self.ledger
        if (ledger.base_dir/'runtime/layout.pending').exists():
            return
        self._restore_scan()
        has_moves = bool(ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='layout_moves'"))
        originals = {relative_path(p, ledger.base_dir) for p in paths if p}
        paths = {p.casefold() for p in originals}
        keys = set(keys)
        files = [dict(r) for r in ledger.all('SELECT * FROM files')]
        histories = [dict(r) for r in ledger.all('SELECT * FROM file_history')]
        touched = { (r['project'],r['id']) for r in files
                    if any(p and relative_path(p, ledger.base_dir).casefold() in paths
                           for p in (r['path'],r['target_path'])) }
        touched.update((r['project'],r['file_id']) for r in histories
                       if r['path'] and relative_path(r['path'], ledger.base_dir).casefold() in paths)
        touched.update(keys)
        contents = defaultdict(list)
        for r in files+histories:
            if r['sha256'] and r['path']:
                contents[(r['project'],r['bytes'],r['sha256'])].append(r['path'])
        for row in histories+files:
            is_history = 'file_id' in row
            if only is not None and ('history' if is_history else 'file',row['project'],row['id']) not in only:
                continue
            key = (row['project'],row['file_id'] if is_history else row['id'])
            if key not in touched:
                continue
            expected = [(row['bytes'],row['sha256'])] if row['sha256'] else []
            if not is_history and not expected and row['fingerprint']:
                expected = [(h['bytes'],h['sha256']) for h in histories
                            if (h['project'],h['file_id'])==key and h['fingerprint']==row['fingerprint'] and h['sha256']]
            for size, digest in set(expected):
                if not isinstance(size,int) or size<0:
                    continue
                identity = ('history' if is_history else 'file', row['project'],row['id'],row['path'],row['fingerprint'],digest)
                if identity in self.seen:
                    continue
                self.seen.add(identity)
                candidates = [row['path'], row.get('target_path'), *originals]
                candidates.extend(contents[(row['project'],size,digest)])
                # An interrupted reference update may be recorded only here.
                recorded = (ledger.all("SELECT payload FROM layout_moves WHERE state='done' AND payload LIKE ?", ('%'+digest+'%',))
                            if has_moves else ())
                for move in recorded:
                    payload = json.loads(move['payload'])
                    if payload.get('sha256')==digest and payload.get('bytes')==size:
                        candidates.append(payload['target'])
                candidates.extend(self.index[size])
                candidates.extend(self.saved_pending)
                self.jobs[identity] = dict(row=row, history=is_history, size=size, digest=digest,
                                           candidates=deque(dict.fromkeys(p for p in candidates if p)), tried=set())
        expected = {(j['size'],j['digest']) for j in self.jobs.values()}
        if self.scan_expected and not expected <= self.scan_expected:
            # A genuinely new conflict needs a new search, not an old absence proof.
            self.visited, self.walk, self.scan_complete, self.scan_errors = {}, None, False, False
        self.scan_expected.update(expected)

    def _commit(self, job, path):
        row, ledger = job['row'], self.ledger
        if job['history'] and path == row['path']:
            return False
        if path is not None and not job['history'] and row['status']=='done' and path==row['path']:
            return False
        if not self.backed_up:
            backup(ledger, 'before-local-reconcile')
            self.backed_up = True
        with ledger.db:
            ledger.db.execute('''CREATE TABLE IF NOT EXISTS local_reconcile_audit(
                id INTEGER PRIMARY KEY,created TEXT,payload TEXT)''')
            if job['history']:
                count = ledger.db.execute('UPDATE file_history SET path=? WHERE id=? AND path IS ? AND sha256=? AND bytes=?',
                    (path,row['id'],row['path'],job['digest'],job['size'])).rowcount
            elif not row['active']:
                # Inactive versions can still own a conflicting local reference.
                # Repair only that reference; preserve their version/hash/status
                # and never put an obsolete version back in the download queue.
                count = ledger.db.execute('''UPDATE files SET path=? WHERE project=? AND id=?
                    AND active=0 AND path IS ? AND fingerprint IS ? AND sha256=? AND bytes=?''',
                    (path,row['project'],row['id'],row['path'],row['fingerprint'],job['digest'],job['size'])).rowcount
            elif path is not None:
                # Exact history fingerprint supplied the expected hash for a
                # pending version. An older fingerprint never marks it done.
                count = ledger.db.execute("""UPDATE files SET path=?,bytes=?,sha256=?,status='done',error=NULL,
                    retryable=1,attempts=0,download_date_source='磁盘核验恢复',updated=?
                    WHERE project=? AND id=? AND path IS ? AND fingerprint IS ?""",
                    (path,job['size'],job['digest'],now().isoformat(),row['project'],row['id'],row['path'],row['fingerprint'])).rowcount
            else:
                count = ledger.db.execute("""UPDATE files SET path=NULL,status='pending',attempts=0,retryable=1,
                    error='磁盘核对未找到当前版本的有效副本，等待重新下载',updated=?,downloaded_at=NULL,
                    download_date_source=NULL WHERE project=? AND id=? AND path IS ? AND fingerprint IS ?
                    AND status='done'""",(now().isoformat(),row['project'],row['id'],row['path'],row['fingerprint'])).rowcount
            if count:
                ledger.db.execute('INSERT INTO local_reconcile_audit(created,payload) VALUES(?,?)',
                    (now().isoformat(),json.dumps(dict(kind='history' if job['history'] else 'file',
                     project=row['project'],id=row['id'],old_path=row['path'],path=path,
                     bytes=job['size'],sha256=job['digest']),ensure_ascii=False)))
        if count:
            for name in ('_download_layout_token','_download_reconciled_token','_download_updates_token'):
                setattr(ledger,name,None)
            print('磁盘核对已修正文件引用：', row['id'], path or '有效副本未找到，保留版本记录', flush=True)
        return bool(count)

    def _entries(self):
        def failed(_):
            self.scan_errors = True
        for folder in ('downloads','history'):
            root = confined(self.ledger.base_dir, folder)
            if not root.exists():
                continue
            for parent, dirs, names in os.walk(root, followlinks=False, onerror=failed):
                self.check()
                dirs[:] = [n for n in dirs if not (Path(parent)/n).is_symlink()
                           and not getattr(Path(parent)/n,'is_junction',lambda: False)()]
                directory = relative_path(Path(parent),self.ledger.base_dir)
                checkpoint = self.visited.get(directory,{})
                if checkpoint.get('complete'):
                    continue
                for name in sorted(names):
                    if name<=checkpoint.get('last',''):
                        continue
                    self.visited[directory] = dict(last=name,complete=False)
                    if name.endswith(('.part','.tmp')):
                        continue
                    yield relative_path(Path(parent)/name,self.ledger.base_dir)
                self.visited[directory] = dict(last='',complete=True)

    def tick(self, seconds=.25, *, scan=False):
        try:
            return self._tick(seconds,scan=scan)
        finally:
            if scan and self.walk is not None:
                try:
                    self._save_scan()
                except (OSError,sqlite3.Error) as exc:
                    print('磁盘扫描检查点暂未保存，保留原文件及版本记录：',str(exc),flush=True)

    def _tick(self, seconds=.25, *, scan=False):
        if not self.jobs or (self.ledger.base_dir/'runtime/layout.pending').exists():
            return 0
        deadline, repaired = time.monotonic()+seconds, 0
        while self.jobs and time.monotonic()<deadline:
            worked = False
            for identity,job in list(self.jobs.items()):
                while job['candidates'] and time.monotonic()<deadline:
                    candidate = job['candidates'][0]
                    if candidate in job['tried']:
                        job['candidates'].popleft()
                        continue
                    try:
                        matched = self.verify(candidate,job['size'],job['digest'],deadline)
                    except OSError as exc:
                        self.scan_errors = True
                        print('磁盘核对暂不能读取，保留记录：', candidate, str(exc), flush=True)
                        matched = False
                    if matched is None:
                        return repaired
                    job['tried'].add(candidate)
                    job['candidates'].popleft()
                    worked = True
                    if matched:
                        repaired += self._commit(job,candidate)
                        del self.jobs[identity]
                        break
            if not scan or time.monotonic()>=deadline:
                break
            if self.scan_complete:
                break  # Unproven records remain isolated; never guess their version.
            if self.walk is None:
                self.walk = iter(self._entries())
                print('已知路径未能解决冲突，空闲时分段扫描下载及历史目录。', flush=True)
            try:
                candidate = next(self.walk)
                self.inflight = candidate
            except StopIteration:
                self.scan_complete = True
                if not self.scan_errors:
                    # Only an exhaustive, error-free scan can prove absence.
                    # Keep version/hash evidence and requeue missing current bytes.
                    for identity,job in list(self.jobs.items()):
                        if not job['candidates']:
                            repaired += self._commit(job,None)
                            del self.jobs[identity]
                break
            try:
                size = confined(self.ledger.base_dir,candidate).stat().st_size
            except OSError:
                self.scan_errors = True
                continue
            self.index[size].append(candidate)
            for job in self.jobs.values():
                if job['size']==size and candidate not in job['tried']:
                    job['candidates'].append(candidate)
            self.inflight = None
        return repaired


def reconciler(ledger, check=lambda: None):
    service = getattr(ledger,'_disk_reconciler',None)
    if service is None:
        service = ledger._disk_reconciler = DiskReconciler(ledger,check)
    service.check = check
    return service
