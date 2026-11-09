"""Durable offline analysis queue, running separately from the downloader."""

from paths import HOME, CODE
import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
import time

from paths import check_output_directory
from analysis_settings import load as load_analysis_settings
from runtime_logging import BoundedLog, EventGate, operation_log
from fusion_client import now

_child = None

# Reserve the download window for downloads; migration recovery uses the same
# time predicate independently of this analysis scheduling switch.
PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW = True


def in_download_window(at=None):
    """Reserve 02:00 <= Beijing time < 10:30 for downloads every day."""
    at = (at or now()).astimezone(timezone(timedelta(hours=8)))
    return 120 <= at.hour * 60 + at.minute < 630


def analysis_paused_for_schedule(base_dir=HOME, at=None):
    """Keep parsing after today's configured briefing send time, in Beijing time."""
    if not PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW:
        return False
    from push_settings import load_settings
    at = (at or now()).astimezone(timezone(timedelta(hours=8)))
    send_time = load_settings(base_dir)['briefing_send_time_beijing']
    hour, minute = map(int, send_time.split(':'))
    return in_download_window(at) or at.hour * 60 + at.minute < hour * 60 + minute


class ProgressLog:
    """Flush to console and a shared progress log, including manual CLI runs."""
    def __init__(self, path=None, *, idle_interval=300):
        self.path = Path(path) if path else None
        self.lock = threading.Lock()
        self.warned = False
        self.gate = EventGate()
        self.output = BoundedLog(self.path) if self.path else None
        self.idle_interval = idle_interval

    def __call__(self, message):
        at = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')
        with self.lock:
            messages = self.gate.accept(str(message).replace(chr(13), ' ').replace(chr(10), ' '),
                interval=self.idle_interval if str(message).startswith('空闲等待') else None)
            if str(message).startswith(('本次运行结束', '解析程序异常', '离线分析已中断')):
                messages = self.gate.drain() + messages
            for text in messages:
                line = f'[{at} +08:00] [解析 PID={os.getpid()}] {text}'
                print(line, flush=True)
                if self.output:
                    try:
                        self.output.write(line + '\n')
                    except OSError as exc:
                        if not self.warned:
                            print(f'解析进度日志无法写入：{exc}；仍继续在窗口输出。', flush=True)
                            self.warned = True


@contextmanager
def activity(log, label, interval=30, announce=True):
    """Report liveness during blocking work, without inventing a percentage."""
    started = time.monotonic()
    state = [label]
    step_started = [started]
    stopped = threading.Event()
    def phase(message):
        state[0] = message
        step_started[0] = time.monotonic()
        log(message)
    def watch():
        while not stopped.wait(interval):
            log(f'仍在处理：{state[0]}；当前步骤耗时 {time.monotonic()-step_started[0]:.1f} 秒；'
                f'本阶段任务累计耗时 {time.monotonic()-started:.1f} 秒；进程存活，不代表解析已完成')
    if announce:
        phase(label)
    thread = threading.Thread(target=watch, daemon=True, name='analysis-progress')
    thread.start()
    try:
        yield phase
    finally:
        stopped.set()
        thread.join()


def parser_version():
    paths = {'local_analysis.py': CODE/'local_analysis.py',
             'archive_cache.py': CODE/'archive_cache.py',
             'weld_classification.py': CODE/'weld_classification.py',
             'equipment_reader.py': CODE/'equipment_reader.py',
             'prefab_rules_260818.py': CODE/'prefab_rules_260818.py',
             'analysis_worker.py': CODE/'analysis_worker.py',
             'parse_pcf_to_json.py': CODE/'parse_pcf_to_json.py',
             'parse_idf_to_json.py': CODE/'parse_idf_to_json.py',
             'business_type.py': CODE/'business_type.py',
             'document_coding_rules.json': HOME/'document_coding_rules.json',
             'material_split_rules.json': HOME/'material_split_rules.json'}
    actual = {name: hashlib.sha256(path.read_bytes()).hexdigest() for name,path in paths.items()}
    baseline = json.loads((CODE/'analysis_cache.json').read_text(encoding='utf-8'))
    if actual == baseline['source_hashes']:
        return baseline['cache_key']
    return hashlib.sha256(json.dumps(actual,sort_keys=True).encode()).hexdigest()


@contextmanager
def worker_lock(cache, name='worker.lock'):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    stream = (cache / name).open('a+b')
    acquired = False
    try:
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()


def candidate_key(source, item, version):
    stat = source['path'].stat()
    content = [item['project'], item['id'], item.get('sha256'), stat.st_size, stat.st_mtime_ns, version]
    return hashlib.sha256(json.dumps(content, ensure_ascii=True).encode()).hexdigest()


class AnalysisQueue:
    def __init__(self, database, *, base_dir=None, cache=None, log=None):
        self.database = Path(database).resolve()
        self.base = Path(base_dir).resolve() if base_dir else self.database.parent.parent
        self.cache = check_output_directory(cache or self.base / 'runtime/file_analysis', database=self.database, base_dir=self.base)
        self.cache.mkdir(parents=True, exist_ok=True)
        self.log = log or ProgressLog()
        self._last_scan_summary = None
        self._source_watch = None
        self._source_revision = None
        self._last_full_scan = float('-inf')
        self._last_cleanup = float('-inf')
        self._export_dirty, self._last_export = False, float('-inf')
        self._export_failed = False
        self.version = parser_version()
        self.db = sqlite3.connect(self.cache / 'queue.sqlite3', timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS jobs(
            key TEXT PRIMARY KEY, project TEXT NOT NULL, file_id TEXT NOT NULL,
            path TEXT NOT NULL, origin TEXT NOT NULL, expected_sha TEXT,
            state TEXT NOT NULL DEFAULT 'pending', active INTEGER NOT NULL DEFAULT 1,
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0,
            result_path TEXT, error TEXT, updated REAL NOT NULL);
          CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(active,state,retry_at);
          -- Find a completed replacement without scanning all current jobs.
          CREATE INDEX IF NOT EXISTS jobs_replacement ON jobs(project,file_id,active,state);
          CREATE TABLE IF NOT EXISTS file_inventory(
            job_key TEXT NOT NULL, project TEXT NOT NULL, unit TEXT NOT NULL,
            origin TEXT NOT NULL, kind TEXT NOT NULL, basis TEXT, sha256 TEXT, state TEXT,
            PRIMARY KEY(job_key,origin));
          CREATE VIEW IF NOT EXISTS current_file_inventory AS
            SELECT f.* FROM file_inventory f JOIN jobs j ON j.key=f.job_key WHERE j.active=1;
          CREATE TABLE IF NOT EXISTS pipeline_inventory(
            job_key TEXT NOT NULL, project TEXT NOT NULL, unit TEXT NOT NULL,
            source TEXT NOT NULL, pipeline TEXT NOT NULL, origin TEXT, sha256 TEXT, conflict INTEGER,
            PRIMARY KEY(job_key,project,unit,source,pipeline));
          CREATE VIEW IF NOT EXISTS current_pipeline_inventory AS
            SELECT p.* FROM pipeline_inventory p JOIN jobs j ON j.key=p.job_key WHERE j.active=1;
          CREATE TABLE IF NOT EXISTS archive_inventory(
            job_key TEXT NOT NULL,archive TEXT NOT NULL,entry INTEGER NOT NULL,payload TEXT NOT NULL,
            PRIMARY KEY(job_key,archive,entry));
          CREATE VIEW IF NOT EXISTS current_archive_inventory AS
            SELECT a.* FROM archive_inventory a JOIN jobs j ON j.key=a.job_key WHERE j.active=1;
          CREATE INDEX IF NOT EXISTS archive_inventory_sha ON archive_inventory(json_extract(payload,'$.sha256'));
          CREATE TABLE IF NOT EXISTS analysis_exports(name TEXT PRIMARY KEY,revision INTEGER NOT NULL);
          INSERT OR IGNORE INTO analysis_exports VALUES('results',0);
          INSERT OR IGNORE INTO analysis_exports VALUES('ledger',-1);
          INSERT OR IGNORE INTO analysis_exports VALUES('snapshot',-1);
          CREATE TABLE IF NOT EXISTS analysis_source_state(name TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        if 'retryable' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN retryable INTEGER NOT NULL DEFAULT 1')
            self.db.commit()
        if 'filename' not in {r['name'] for r in self.db.execute('PRAGMA table_info(jobs)')}:
            self.db.execute('ALTER TABLE jobs ADD COLUMN filename TEXT')
            self.db.commit()
        self._export_dirty = self.snapshot_pending('ledger')

    def snapshot_pending(self, name='snapshot'):
        return self.db.execute("SELECT revision FROM analysis_exports WHERE name='results'").fetchone()[0] != self.db.execute(
            'SELECT revision FROM analysis_exports WHERE name=?', (name,)).fetchone()[0]

    def mark_exported(self, name='snapshot'):
        with self.db:
            self.db.execute("UPDATE analysis_exports SET revision=(SELECT revision FROM analysis_exports WHERE name='results') WHERE name=?", (name,))

    def changed(self):
        self.db.execute("UPDATE analysis_exports SET revision=revision+1 WHERE name='results'")
        self._export_dirty = True

    def close(self, *, defer_exports=False):
        try:
            if not defer_exports:
                self.refresh_ledger_export(force=not self._export_failed)
        finally:
            self.db.close()
            if self._source_watch is not None:
                self._source_watch.close()

    def discover_if_changed(self):
        """Watch committed ledger writes cheaply; periodically recheck disk too."""
        if self._source_watch is None:
            self._source_watch = sqlite3.connect(self.database.as_uri() + '?mode=ro', uri=True, timeout=5)
        revision = self._source_watch.execute('PRAGMA data_version').fetchone()[0]
        if revision == self._source_revision and time.monotonic() - self._last_full_scan < 300:
            return 0
        # Store the revision before scanning: writes during the scan must cause
        # another pass. No long read transaction is held between polls.
        self._source_revision = revision
        result = self.discover()
        self._last_full_scan = time.monotonic()
        return result

    def refresh_ledger_export(self, force=False):
        counts = self.counts() if self._export_dirty else {}
        busy = any(counts.get(state, 0) for state in ('pending', 'running'))
        interval = 900 if busy else 60
        if busy and not force and self._last_export == float('-inf'):
            self._last_export = time.monotonic()
            return  # Spend the first busy interval parsing, not rebuilding Excel.
        if not self._export_dirty or (not force and time.monotonic() - self._last_export < interval):
            return
        from types import SimpleNamespace
        from archive_index import readonly
        # Throttle failed exports too: an unavailable output folder must not
        # trigger a full workbook rebuild after every parsed file.
        self._last_export = time.monotonic()
        try:
            with closing(readonly(self.database)) as db:
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'documents', 'folders', 'document_files'} <= tables:
                    return
                from sync import export_csv
                target = export_csv(SimpleNamespace(base_dir=self.base, db=db,
                    all=lambda sql, args=(): list(db.execute(sql, args))),
                    supplemental=False, quiet=True)
            self._export_dirty = False
            self._export_failed = False
            self.mark_exported('ledger')
            self._last_export = time.monotonic()
            self.log(f'Excel文件台账已更新：{target}；包含平台原件、附件及当前解压文件。')
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
            self._export_failed = True
            self.log(f'文件台账导出暂未完成：{exc}；解析结果与解压索引已保存，下次重试。')

    def discover(self):
        from local_analysis import ledger_sources
        started = last_progress = time.monotonic()
        if self._last_scan_summary is None:
            self.log(f'开始扫描下载台账：{self.database}；仅处理本地已完成下载的候选文件')
        with activity(self.log, '扫描下载台账、检查文件路径和大小', announce=False):
            sources, inventory = ledger_sources(self.database, self.base)
        inserted = 0
        requeued = 0
        changed_paths = 0
        current_keys = []
        digest = hashlib.sha256()
        for record in inventory:
            digest.update(json.dumps([record.get(name) for name in
                ('project', 'id', 'filename', 'path', 'status', 'bytes', 'sha256', 'state')],
                ensure_ascii=True, separators=(',', ':')).encode())
        inventory_signature = digest.hexdigest()
        previous = self.db.execute("SELECT value FROM analysis_source_state WHERE name='inventory'").fetchone()
        inventory_changed = previous is None or previous[0] != inventory_signature
        with self.db:
            for index, source in enumerate(sources, 1):
                item = source['item']
                key = candidate_key(source, item, self.version)
                current_keys.append(key)
                inserted += self.db.execute('''INSERT OR IGNORE INTO jobs
                    (key,project,file_id,path,origin,expected_sha,updated) VALUES(?,?,?,?,?,?,?)''',
                    (key, source['project'], item['id'], str(source['path']), source['origin'], item.get('sha256'), time.time())).rowcount
                changed_paths += self.db.execute('''UPDATE jobs SET active=1,path=?,filename=? WHERE key=?
                    AND (active!=1 OR path IS NOT ? OR filename IS NOT ?)''',
                    (str(source['path']), item['filename'], key, str(source['path']), item['filename'])).rowcount
                saved = self.db.execute('SELECT state,result_path,error FROM jobs WHERE key=?', (key,)).fetchone()
                saved_path = self.cache / saved['result_path'] if saved['result_path'] else None
                suffix = Path(item['filename']).suffix.lower()
                if (saved['state'] == 'failed' and saved['error']
                        and (suffix == '.pdf' or (suffix in ('.pcf', '.zip', '.7z')
                             and 'IDF未识别到-6管线记录' in saved['error']))):
                    # Older code derived the parser from an extensionless staged
                    # path and sent PDFs/PCFs/archives to IDF. Reset these failures; preserve
                    # other completed per-file caches.
                    self.db.execute("""UPDATE jobs SET state='pending',attempts=0,retryable=1,
                        error='旧暂存路径无扩展名导致格式误入IDF分支，已按台账文件名重新登记'
                        WHERE key=?""", (key,))
                    requeued += 1
                elif saved['state'] in ('done', 'failed') and saved_path and (not saved_path.is_file()
                        or (saved_path.parent / 'extraction.cleaned').is_file()):
                    self.db.execute("UPDATE jobs SET state='pending',attempts=0,retryable=1,error='缓存缺失，等待重新分析' WHERE key=?", (key,))
                    requeued += 1
                if time.monotonic() - last_progress >= 5:
                    self.log(f'扫描进度：{index}/{len(sources)} 个本地候选文件；新增任务 {inserted}；当前 {source["origin"]}')
                    last_progress = time.monotonic()
            retired = self.db.execute('''UPDATE jobs SET active=0 WHERE active=1
              AND key NOT IN (SELECT value FROM json_each(?))''', (json.dumps(current_keys),)).rowcount
            if inserted or requeued or retired or changed_paths or inventory_changed:
                self.changed()
            if inventory_changed:
                self.db.execute("INSERT OR REPLACE INTO analysis_source_state VALUES('inventory',?)", (inventory_signature,))
        summary = self.counts()
        unavailable = sum(r['state'] != '本地文件可分析' for r in inventory)
        signature = (summary, unavailable)
        if inserted or requeued or signature != self._last_scan_summary:
            self.log(f'扫描完成：本地候选 {len(sources)}；台账候选不可用 {unavailable}；新增任务 {inserted}；缓存缺失重新排队 {requeued}；耗时 {time.monotonic()-started:.1f} 秒；{self.describe(summary)}')
            if not sources:
                self.log('没有可解析的本地候选文件；请核对台账、下载目录和实际文件是否齐全。')
        self._last_scan_summary = signature
        self._export_dirty = self._export_dirty or bool(inserted or requeued or retired or changed_paths)
        self._export_signature = signature
        self.refresh_ledger_export()
        return inserted

    def counts(self):
        return dict(self.db.execute('SELECT state,count(*) FROM jobs WHERE active=1 GROUP BY state').fetchall())

    @staticmethod
    def describe(counts):
        total = sum(counts.values())
        return (f'当前有效任务 {total}；已完成/可复用缓存 {counts.get("done",0)}；'
                f'待解析 {counts.get("pending",0)}；处理中 {counts.get("running",0)}；失败 {counts.get("failed",0)}')

    def recover(self):
        # Called only while holding the exclusive process lock.
        with self.db:
            recovered = self.db.execute("UPDATE jobs SET state='pending',error='上次分析中断，等待恢复' WHERE state='running'").rowcount
            if recovered:
                self.changed()
        if recovered:
            self.log(f'中断恢复：已将 {recovered} 个上次未完成的任务重新排队。')

    def process_next(self):
        self.db.execute('BEGIN IMMEDIATE')
        item = self.db.execute("""SELECT * FROM jobs WHERE active=1 AND
          (state='pending' OR (state='failed' AND retryable=1 AND attempts<3 AND retry_at<=?)) ORDER BY updated,key LIMIT 1""", (time.time(),)).fetchone()
        if item is None:
            self.db.commit()
            return False
        self.db.execute("UPDATE jobs SET state='running',attempts=attempts+1,updated=? WHERE key=?", (time.time(), item['key']))
        self.db.commit()
        result_path = None
        report = None
        retryable = True
        started = time.monotonic()
        self.log(f'开始文件：{item["origin"]}；项目 {item["project"]}；第 {item["attempts"]+1}/3 次尝试；本地路径 {item["path"]}；{self.describe(self.counts())}')
        try:
            from local_analysis import analyze
            path = Path(item['path'])
            before = path.stat()
            self.log(f'文件大小：{before.st_size:,} 字节；任务 {item["key"][:12]}')
            with activity(self.log, f'读取并解析 {item["origin"]}', announce=False) as progress:
                _, report, result_path = analyze(sources_override=[{
                    'path': path, 'project': item['project'], 'origin': item['origin'],
                    'item': {'filename': item['filename'] or path.name}}],
                    output=self.cache / 'jobs' / item['key'], progress=progress,
                    extraction_cache=self.cache / 'extraction_cache')
            after = path.stat()
            if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                raise RuntimeError('源文件分析期间发生变化，结果未采用。')
            actual_sha = report['files'][0].get('sha256') if report['files'] else None
            if item['expected_sha'] and actual_sha and item['expected_sha'] != actual_sha:
                raise RuntimeError('源文件SHA256与下载台账不符，结果未采用。')
            failures = [r for r in report['files'] if r['state'] == '分析失败']
            state = 'failed' if failures else 'done'
            retryable = bool(failures) and all(r.get('retryable', True) for r in failures)
            error = failures[0].get('detail', '部分文件分析失败') if failures else None
            result_path = result_path.relative_to(self.cache).as_posix()
        except Exception as exc:
            from local_analysis import permanent_analysis_error
            retryable = not permanent_analysis_error(exc)
            state, error, result_path = 'failed', str(exc), None
            report = None
        self.log(f'保存解析队列与清单：{item["origin"]}')
        with self.db:
            self.db.execute('DELETE FROM file_inventory WHERE job_key=?', (item['key'],))
            self.db.execute('DELETE FROM pipeline_inventory WHERE job_key=?', (item['key'],))
            self.db.execute('DELETE FROM archive_inventory WHERE job_key=?', (item['key'],))
            if report is not None:
                from archive_index import report_members
                self.db.executemany('INSERT INTO archive_inventory VALUES(?,?,?,?)', [
                    (item['key'], r['archive'], r['entry'], json.dumps(r, ensure_ascii=False))
                    for r in report_members(report, self.cache / result_path)])
                self.db.executemany('INSERT INTO file_inventory VALUES(?,?,?,?,?,?,?,?)', [
                    (item['key'], r['project'], r['unit'], r['origin'], r['kind'], r['basis'], r.get('sha256'), r['state'])
                    for r in report['file_inventory']])
                self.db.executemany('INSERT INTO pipeline_inventory VALUES(?,?,?,?,?,?,?,?)', [
                    (item['key'], r['project'], r['unit'], r['source'], r['pipeline'], r['origin'], r['sha256'], int(r['conflict']))
                    for r in report['pipeline_results']])
            self.db.execute('UPDATE jobs SET state=?,error=?,result_path=?,retry_at=?,updated=?,retryable=? WHERE key=?',
                            (state, error, str(result_path) if result_path else None, time.time() + 60, time.time(), int(retryable), item['key']))
            self.changed()
        self._export_dirty = True
        self.refresh_ledger_export()
        if state == 'done':
            self.cleanup_old_cache(force=not any(self.counts().get(s, 0) for s in ('pending', 'running')))
        retry_note = '；永久性错误，不自动重试' if state == 'failed' and not retryable else ''
        self.log(f'文件结束：{item["origin"]} · {state}{retry_note}；耗时 {time.monotonic()-started:.1f} 秒；{self.describe(self.counts())}')
        if report is not None:
            welds = report.get('welds', [])
            shop = sum(r.get('分类') == 'shop' for r in welds)
            field = sum(r.get('分类') == 'field' for r in welds)
            self.log(f'文件结果：登记 {len(report["file_inventory"])} 个文件；管线记录 {len(report["pipeline_results"])}（含待核对冲突，非去重总量）；有效焊口 {len(welds)}，预制 {shop}，安装 {field}，未分类 {len(welds)-shop-field}；结果 {self.cache / result_path}')
        if error:
            policy = ('永久性错误，不自动重试' if not retryable else
                      '60 秒后允许重试' if item['attempts']+1 < 3 else '已达 3 次尝试上限，需核对源文件或错误')
            self.log(f'失败原因：{item["origin"]}：{error}；' + policy)
        return True

    def cleanup_old_cache(self, *, force=False):
        if not force and time.monotonic() - self._last_cleanup < 60:
            return
        from archive_cache import cleanup_history
        try:
            cleaned = cleanup_history(self)
            if cleaned:
                self.log(f'已清理旧版解压目录/缓存 {cleaned} 个；保留原始压缩包、版本记录和解析报告。')
        except (OSError, ValueError, sqlite3.Error) as exc:
            self.log(f'旧版解压文件暂未清理：{exc}；解析结果已保存，下次维护时重试。')
        finally:
            self._last_cleanup = time.monotonic()


def cached_snapshot(database, *, output, base_dir=None, cache=None, prefab_rule='none'):
    """Read committed per-file results. Never extract or parse original files."""
    from local_analysis import analyze, ledger_sources, export_weld_table
    database = Path(database).resolve()
    base = Path(base_dir).resolve() if base_dir else database.parent.parent
    cache = check_output_directory(cache or base / 'runtime/file_analysis', database=database, base_dir=base)
    sources, inventory = ledger_sources(database, base)
    version = parser_version()
    keys = [candidate_key(source, source['item'], version) for source in sources]
    reports, pending, failed, completed, jobs = [], 0, 0, 0, {}
    cache_db = cache / 'queue.sqlite3'
    if cache_db.exists():
        try:
            with closing(sqlite3.connect(cache_db.as_uri() + '?mode=ro', uri=True)) as db:
                db.row_factory = sqlite3.Row
                jobs = {row['key']: dict(row) for row in db.execute('''SELECT key,state,result_path,error FROM jobs
                    WHERE key IN (SELECT value FROM json_each(?))''', (json.dumps(keys),))}
        except sqlite3.OperationalError:
            pass  # The worker may be initializing its independent database.
    for source, key in zip(sources, keys):
        item = source['item']
        job = jobs.get(key)
        if not job or job['state'] in ('pending', 'running'):
            pending += 1
            item['analysis_state'] = '处理中' if job and job['state'] == 'running' else '待分析'
            continue
        item['analysis_state'] = job['state']
        if job['state'] == 'failed':
            failed += 1
        result_path = (cache / str(job['result_path'])).resolve() if job.get('result_path') else None
        if result_path and result_path.is_relative_to(cache) and result_path.is_file():
            try:
                reports.append(json.loads(result_path.read_text(encoding='utf-8')))
                completed += job['state'] == 'done'
            except (OSError, ValueError):
                failed += job['state'] != 'failed'
                item['analysis_state'] = '缓存不可读，待重试'
        else:
            failed += job['state'] != 'failed'
            item['analysis_state'] = '分析失败或缓存缺失'
    groups, report, target = analyze(sources_override=[], cached_reports=reports,
        inventory_override=inventory, output=output, prefab_rule=prefab_rule)
    unresolved = len(report.get('incomplete', []))
    report.update(completed_analysis_files=completed, pending_analysis_files=pending, failed_analysis_files=failed, unresolved_analysis_groups=unresolved,
                  analysis_complete=not (pending or failed or unresolved or report['unavailable_local_files']),
                  from_cache=True)
    if pending or failed:
        for record in groups.values():
            record['状态'] = f'已完成部分；待处理{pending}，失败{failed}'
            record['依据'] += '；后台尚未全部处理完成'
        report['groups'] = [{'project': p, 'unit': u, 'source': s, **r} for (p, u, s), r in sorted(groups.items())]
    export_weld_table(report, base)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return groups, report, target


def request_analysis_stop(base_dir):
    cache = check_output_directory(Path(base_dir) / 'runtime/file_analysis', base_dir=base_dir)
    cache.mkdir(parents=True, exist_ok=True)
    (cache / 'stop.request').write_text('stop after current local file', encoding='utf-8')


def resume_background_analysis(base_dir):
    """Only explicit startup may clear a persisted stop request."""
    cache = check_output_directory(Path(base_dir) / 'runtime/file_analysis', base_dir=base_dir)
    (cache / 'stop.request').unlink(missing_ok=True)


def wait_for_analysis_exit(base_dir, log=print):
    cache = Path(base_dir) / 'runtime/file_analysis'
    announced = 0
    while True:
        with worker_lock(cache) as available:
            if available:
                return
        if time.monotonic() >= announced:
            log('等待后台解压、解析完成当前文件并保存进度；不会强制终止。')
            announced = time.monotonic() + 30
        time.sleep(.2)


def ensure_background_worker(base_dir, database):
    """Fast download hook; heavy parsing happens only in an independent process."""
    global _child
    if analysis_paused_for_schedule(base_dir):
        return
    if _child and _child.poll() is None:
        return
    base = Path(base_dir).resolve()
    cache = check_output_directory(base / 'runtime/file_analysis', database=database, base_dir=base)
    if ((cache / 'stop.request').exists() or (base / 'runtime/scheduler.exit.request').exists()
            or (base / 'runtime/layout.pending').exists()):
        return
    with worker_lock(cache) as available:
        if not available:
            return
    env = {k: v for k, v in os.environ.items() if k not in ('FUSIONLIVE_USERNAME', 'FUSIONLIVE_PASSWORD')}
    env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1')
    worker_log = operation_log('analysis-worker.log', base)
    BoundedLog(worker_log).write('启动后台解析；此文件保存标准错误，进度见 analysis.log。\n')
    with worker_log.open('a', encoding='utf-8') as log:
        settings = load_analysis_settings(base)
        _child = subprocess.Popen([sys.executable, '-B', str(CODE / 'analysis_worker.py'),
            '--database', str(database), '--base-dir', str(base), '--watch', '--idle-exit', str(settings['analysis_auto_idle_exit_seconds'])],
            cwd=base, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=log, env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)


def notify_download_completed(base_dir, database):
    try:
        ensure_background_worker(base_dir, database)
    except Exception as exc:
        # Analysis can never turn a successfully downloaded file into a failure.
        print(f'后台分析暂未启动，不影响下载：{type(exc).__name__}：{exc}', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线后台解压解析；与下载并行，不登录、不下载')
    parser.add_argument('--database', type=Path, default=HOME / 'runtime/ledger.sqlite3')
    parser.add_argument('--base-dir', type=Path)
    parser.add_argument('--watch', action='store_true', help='持续发现已完成的新文件')
    parser.add_argument('--idle-exit', type=float, help='后台空闲多少秒后退出；0表示持续等待，默认读取页面配置')
    parser.add_argument('--poll-seconds', type=float, help='扫描间隔，默认读取页面配置')
    parser.add_argument('--idle-log-seconds', type=float, help='空闲提示间隔，默认读取页面配置')
    control = parser.add_mutually_exclusive_group()
    control.add_argument('--stop', action='store_true')
    control.add_argument('--resume', action='store_true', help='明确恢复之前停止的后台分析')
    args = parser.parse_args(argv)
    base = args.base_dir or args.database.resolve().parent.parent
    if args.stop:
        request_analysis_stop(base)
        ProgressLog(base / 'runtime/file_analysis/progress.log')('已提交安全停止请求：完成当前文件并保存结果后退出。')
        return 0
    settings = load_analysis_settings(base)
    args.poll_seconds = settings['analysis_poll_seconds'] if args.poll_seconds is None else args.poll_seconds
    args.idle_exit = settings['analysis_idle_exit_seconds'] if args.idle_exit is None else args.idle_exit
    args.idle_log_seconds = settings['analysis_idle_log_seconds'] if args.idle_log_seconds is None else args.idle_log_seconds
    import math
    if (any(not math.isfinite(value) for value in (args.poll_seconds, args.idle_exit, args.idle_log_seconds))
            or args.poll_seconds < 0.1 or args.idle_log_seconds < 0.1 or args.idle_exit < 0):
        parser.error('扫描/提示间隔须至少0.1秒，空闲退出时间须为非负有限数值')
    cache = check_output_directory(base / 'runtime/file_analysis', database=args.database, base_dir=base)
    cache.mkdir(parents=True, exist_ok=True)
    stop = cache / 'stop.request'
    bundled = (base.parent / '管理中心').is_dir() and (base.parent / 'codex-wechat').is_dir()
    log = ProgressLog(operation_log('analysis.log', base) if bundled else cache / 'progress.log',
                      idle_interval=args.idle_log_seconds)
    if analysis_paused_for_schedule(base):
        log('尚未到今日管道简报发送时间，或仍在下载时段；暂不启动解压解析，满足时间条件后由常驻调度自动重试。')
        return 0
    log(f'解析程序启动；台账 {args.database.resolve()}；缓存目录 {cache}；进度日志 {cache / "progress.log"}')
    if args.resume:
        resume_background_analysis(base)
    with worker_lock(cache) as acquired:
        if not acquired:
            log('已有后台分析任务，无需重复启动；请查看同目录 progress.log 中该进程的进度。')
            return 0
        # A child starting concurrently with safe exit must never erase its stop.
        if stop.exists():
            log('后台分析已收到停止请求；下次启动工具或使用--resume恢复。')
            return 0
        log(f'正在初始化解析队列；模式 {"持续监测" if args.watch else "处理当前队列后退出"}；扫描间隔 {args.poll_seconds:g} 秒；空闲提示间隔 {args.idle_log_seconds:g} 秒；空闲退出 {args.idle_exit:g} 秒（0 为持续等待）')
        try:
            queue = AnalysisQueue(args.database, base_dir=base, cache=cache, log=log)
        except Exception as exc:
            log(f'队列初始化失败：{type(exc).__name__}：{exc}')
            raise
        log(f'解析缓存版本：{queue.version}；版本和文件信息不变的已完成结果直接复用')
        try:
            queue.recover()
            if not analysis_paused_for_schedule(base):
                queue.discover_if_changed()
            last_activity = last_scan = time.monotonic()
            last_idle = float('-inf')
            export_pending = queue.snapshot_pending()
            while not stop.exists():
                if analysis_paused_for_schedule(base):
                    log('当前已不在允许解析的时段；解析进度已保存，暂停解析并延后汇总导出。')
                    break
                if time.monotonic() - last_scan >= args.poll_seconds:
                    discovered = queue.discover_if_changed()
                    if discovered or queue.snapshot_pending():
                        export_pending = True
                    if discovered:
                        last_activity = time.monotonic()
                    last_scan = time.monotonic()
                if analysis_paused_for_schedule(base) or stop.exists():
                    continue
                if queue.process_next():
                    export_pending = True
                    last_activity = time.monotonic()
                    last_idle = float('-inf')
                    continue
                queue.cleanup_old_cache()
                if export_pending:
                    _, report, _ = cached_snapshot(args.database, base_dir=base, cache=cache,
                                                   output=cache / 'export_snapshots')
                    log(f'焊口表已导出：{report["weld_table_file"]}；有效焊口 {len(report["welds"])}')
                    export_pending = False
                    queue.mark_exported()
                if not args.watch or (args.idle_exit and time.monotonic() - last_activity >= args.idle_exit):
                    break
                if time.monotonic() - last_idle >= args.idle_log_seconds:
                    retry = queue.db.execute("SELECT min(retry_at) FROM jobs WHERE active=1 AND state='failed' AND retryable=1 AND attempts<3").fetchone()[0]
                    retry_note = f'；下一次失败重试约 {max(0, retry-time.time()):.0f} 秒后' if retry is not None else ''
                    log(f'空闲等待：当前没有可立即执行的任务；{queue.describe(queue.counts())}{retry_note}；仍监测新文件，不会重复解析有效缓存')
                    last_idle = time.monotonic()
                time.sleep(min(args.poll_seconds, 1))
            if stop.exists() and not analysis_paused_for_schedule(base):
                if export_pending:
                    cached_snapshot(args.database, base_dir=base, cache=cache,
                                    output=cache / 'export_snapshots')
                    queue.mark_exported()
                log('已收到停止请求，当前文件已处理完毕，准备退出。')
            log(f'本次运行结束：{queue.describe(queue.counts())}')
        except KeyboardInterrupt:
            log('离线分析已中断；未完成任务将在下次启动恢复。')
        except Exception as exc:
            log(f'解析程序异常：{type(exc).__name__}：{exc}')
            raise
        finally:
            queue.close(defer_exports=analysis_paused_for_schedule(base))
    log('后台解压、解析已退出，进度已保存。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
