"""Single-instance watch with a cooperative stop request."""
from contextlib import contextmanager
import os
import threading
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from .api import ImaError
from .config import load_settings


class ActivityLog:
    """Observe long phases without replacing durable per-file receipts."""
    def __init__(self, output, redact=lambda value:value, interval=30):
        self.output,self.redact,self.interval=output,redact,interval
        self.lock=threading.RLock()
        self.phase_name='启动'
        self.started=self.step_started=time.monotonic()
        self.current=''
        self.latest_success=''
        self.events=self.suppressed=0
        self.last_report=float('-inf')
        self.finished=threading.Event()
        self.worker=threading.Thread(target=self._watch,daemon=True,name='ima-log-progress')
        self.worker.start()

    def write(self, message, level='INFO'):
        at=datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        try:
            rendered=self.redact(message)
            if not isinstance(rendered,str):
                rendered='日志脱敏返回非文本，原文未输出。'
        except Exception as exc:
            rendered=f'日志脱敏异常：{type(exc).__name__}；原文未输出。'
            level='ERROR'
        self.output(f'[{at}] [{level}] [ima PID={os.getpid()} run={os.environ.get("PROJECT_TOOLS_RUN_ID","standalone")}] '+rendered)

    def flush(self):
        if self.suppressed:
            self.write(f'ima同步进度：合并 {self.suppressed} 条成功结果；最近结果：{self.latest_success}')
            self.suppressed=0

    def phase(self, name):
        with self.lock:
            self.flush()
            self.phase_name=name
            self.started=self.step_started=time.monotonic()
            self.current=''
            self.events=0
            self.last_report=float('-inf')
            self.write('同步阶段：'+name)

    def notify(self, message):
        with self.lock:
            self.current=str(message)
            self.events+=1
            self.step_started=time.monotonic()
            success=bool(re.search(r'(?:—|：|:)\s*(?:copied|unchanged|done|deleted|duplicate|skipped)\b',message))
            if success and self.step_started-self.last_report<self.interval:
                self.suppressed+=1
                self.latest_success=str(message)
                return
            self.flush()
            evidence=re.sub(r'(?:失败|冲突|error|failed|uncertain|conflict|blocked)["\s:：=]*0\b','',message,flags=re.I)
            level='ERROR' if re.search(r'\b(?:error|failed)\b|失败',evidence,re.I) else 'WARNING' if re.search(r'\b(?:uncertain|conflict|blocked)\b|待核对|重试',evidence,re.I) else 'INFO'
            self.write(str(message),level)
            self.last_report=self.step_started

    def finish(self, state):
        with self.lock:
            self.flush()
            self.write(f'ima阶段结束：阶段={self.phase_name}；state={state}；结果事件={self.events}；耗时={time.monotonic()-self.started:.1f}秒',
                       'ERROR' if state=='failed' else 'WARNING' if state in ('pending','stopped') else 'INFO')
            self.phase_name='等待下一轮'
            self.current=''
            self.started=self.step_started=time.monotonic()
            self.events=0

    def _watch(self):
        while not self.finished.wait(self.interval):
            with self.lock:
                if self.phase_name=='等待下一轮':continue
                self.write(f'ima同步心跳：阶段={self.phase_name}；本阶段结果事件={self.events}；阶段耗时={time.monotonic()-self.started:.1f}秒；'
                           f'距最近结果={time.monotonic()-self.step_started:.1f}秒；最近结果={self.current or "尚无结果"}；等待不表示操作完成')

    def close(self):
        self.finished.set()
        self.worker.join(timeout=1)
        with self.lock:self.flush()


def validate_sync_time(value):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        raise ImaError('每日同步时间须为 HH:MM（00:00 至 23:59，北京时间）。')
    return value


def claim_daily_sync(runtime, *, scheduled=False, sync_time='13:00', at=None):
    """Reserve today's run before any work; failures and restarts consume the run."""
    validate_sync_time(sync_time)
    at = (at or datetime.now(timezone.utc)).astimezone(timezone(timedelta(hours=8)))
    if scheduled and at.strftime('%H:%M') < sync_time:
        return False
    runtime.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(runtime / 'daily-sync.sqlite3', timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS daily_run (id INTEGER PRIMARY KEY CHECK(id=1), day TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        previous = db.execute('SELECT day FROM daily_run WHERE id=1').fetchone()
        today = at.date().isoformat()
        if previous and previous[0] >= today:
            return False
        db.execute('INSERT OR REPLACE INTO daily_run VALUES (1,?)', (today,))
        db.commit()
        return True
    finally:
        db.close()


@contextmanager
def watch_session(runtime, stopped, *, automatic=True):
    runtime.mkdir(parents=True, exist_ok=True)
    path = runtime / 'watch.lock'
    with path.open('a+b') as stream:
        stream.seek(0)
        if not stream.read(1):
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        acquired = False
        finished = threading.Event()
        thread = None
        try:
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except OSError:
                raise ImaError('ima 自动同步已经运行。') from None
            if automatic and load_settings().get('auto_sync_enabled', True) is not True:
                raise ImaError('自动同步已关闭，请使用手动同步。')
            request = runtime / 'watch-stop.request'
            request.unlink(missing_ok=True)
            def monitor():
                while not finished.wait(.25):
                    try:
                        enabled = not automatic or load_settings().get('auto_sync_enabled', True) is True
                    except (ImaError, OSError, ValueError):
                        enabled = False  # Stop safely if the persisted switch cannot be read.
                    if request.exists() or not enabled:
                        stopped.set()
                        return
            thread = threading.Thread(target=monitor, daemon=True)
            thread.start()
            yield
        finally:
            finished.set()
            if thread:
                thread.join(timeout=2)
            if acquired:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
