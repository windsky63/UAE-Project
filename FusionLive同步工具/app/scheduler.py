"""Resident daily scheduler; leave this console open. Beijing time throughout."""

from paths import HOME, CODE
from datetime import datetime, timedelta
from contextlib import closing
import os
import queue
import signal
import subprocess
import sqlite3
import sys
import threading
import time
import traceback

from fusion_client import HOME, RUNTIME, now
from reporting import day_folder
from sync import RunLock
from local_credentials import get_credentials
from progress import Progress
from preflight import check_before_login, CheckCancelled
from briefing_schedule import run_daily_briefing
from analysis_worker import request_analysis_stop, resume_background_analysis, wait_for_analysis_exit, worker_lock

START, STOP = 120, 630
RETRY_SECONDS = 60
_log_warned = False


def eligible(at, last_day=None):
    return START <= at.hour * 60 + at.minute < STOP and at.date().isoformat() != last_day


def next_start(at):
    target = at.replace(hour=2, minute=0, second=0, microsecond=0)
    return target if at < target else target + timedelta(days=1)


def log(message):
    global _log_warned
    line = now().strftime('[%m-%d %H:%M:%S] ') + message
    from runtime_logging import BoundedLog, operation_log
    print(line, flush=True)
    try:
        BoundedLog(operation_log('scheduler.log', HOME)).write(line + '\n')
    except OSError as exc:
        if not _log_warned:
            print(f'调度日志写入失败，常驻调度继续执行：{type(exc).__name__}：{exc}', file=sys.stderr, flush=True)
            _log_warned = True


def request_stop():
    RUNTIME.mkdir(exist_ok=True)
    (RUNTIME / 'stop.request').write_text(now().isoformat(), encoding='utf-8')


def stopped_today(at):
    marker = RUNTIME / 'stop.request'
    try:
        try:
            requested = datetime.fromisoformat(marker.read_text(encoding='utf-8').strip())
            if requested.tzinfo is None:
                raise ValueError('停止标记缺少时区')
            stop_day = requested.astimezone(at.tzinfo).date()
        except (ValueError, UnicodeError):
            stop_day = datetime.fromtimestamp(marker.stat().st_mtime, at.tzinfo).date()
        if stop_day < at.date():
            marker.unlink(missing_ok=True)
            log('已清除往日停止标记，恢复今天的下载调度。')
            return False
        return True
    except FileNotFoundError:
        return False


def request_full_stop():
    RUNTIME.mkdir(exist_ok=True)
    (RUNTIME / 'scheduler.exit.request').write_text(now().isoformat(), encoding='utf-8')
    request_analysis_stop(HOME)
    request_stop()


def wait_for_full_exit(timeout=None):
    locks = ((RUNTIME, 'scheduler.lock', '调度程序（含简报线程收尾）'), (RUNTIME, 'sync.lock', '下载/注销'),
             (RUNTIME / 'file_analysis', 'worker.lock', '后台解压解析'),
             (RUNTIME / 'wechat_delivery', 'worker.lock', '简报微信推送'))
    announced = 0
    started = time.monotonic()
    while True:
        pending = []
        for folder, name, label in locks:
            with worker_lock(folder, name) as available:
                if not available:
                    pending.append(f'{label} [{folder / name}]')
        if not pending:
            return True
        if timeout is not None and time.monotonic() - started >= timeout:
            log('安全停止等待超时，任务仍未退出；查看 runtime/sync-stop-stack.log 和 scheduler.log。'
                '需要立即中断时运行：python main.py scheduler --force-stop；此操作不能保证平台注销成功。')
            return False
        if time.monotonic() >= announced:
            log(f'安全停止已等待 {time.monotonic()-started:.0f} 秒；尚未退出：' + '；'.join(pending))
            announced = time.monotonic() + 30
        time.sleep(.2)


def force_stop():
    """Explicit emergency action: only kill verified lock holders in this install."""
    import psutil
    request_full_stop()
    locks = {str((RUNTIME / name).resolve()).casefold() for name in ('sync.lock', 'scheduler.lock')}
    log('紧急停止：仅结束本目录持有同步/调度锁的进程；平台注销可能未确认，自动登录将暂停。')
    (RUNTIME / 'login.paused').write_text('紧急中断后需要人工确认平台会话已注销。', encoding='utf-8')
    targets = []
    for process in psutil.process_iter(['pid', 'name', 'cmdline']):
        if process.pid == os.getpid() or 'python' not in (process.info['name'] or '').lower():
            continue
        try:
            args = process.info['cmdline'] or []
            scripts = {str((CODE / name).resolve()).casefold() for name in ('sync.py', 'scheduler.py')}
            direct = any(str(os.path.realpath(arg)).casefold() in scripts for arg in args if arg.endswith('.py'))
            launcher = str((HOME / 'main.py').resolve()).casefold()
            via_main = any(str(os.path.realpath(arg)).casefold() == launcher for arg in args if arg.endswith('.py'))
            if not (direct or via_main) or '--stop' in args or '--force-stop' in args:
                continue
            opened = {str(os.path.realpath(entry.path)).casefold() for entry in process.open_files()}
            if locks & opened:
                targets.append(process)
        except (psutil.Error, OSError):
            continue
    for process in targets:
        try:
            process.kill()
            log(f'已中断本目录锁持有进程 PID={process.pid}。')
        except psutil.NoSuchProcess:
            pass
    _, alive = psutil.wait_procs(targets, timeout=10)
    return 5 if alive or not wait_for_full_exit(timeout=10) else 0


def wait_for_publisher(publisher, label='简报'):
    started = time.monotonic()
    announced = 0
    while publisher.is_alive():
        if time.monotonic() >= announced:
            log(f'等待{label}线程安全退出，已等待 {time.monotonic()-started:.0f} 秒。')
            # Routine queue waits are not errors. Keep opt-in diagnostics out
            # of the operational log and console consumed by the log viewer.
            if os.environ.get('FUSIONLIVE_LOG_DETAIL') == '1':
                from runtime_logging import BoundedLog
                frame = sys._current_frames().get(publisher.ident)
                stack = ''.join(traceback.format_stack(frame)) if frame is not None else '线程堆栈暂不可用'
                try:
                    BoundedLog(RUNTIME/'scheduler-thread-stacks.txt').write(
                        f'{now().isoformat()} {label}\n{stack}\n')
                except OSError as exc:
                    log(f'线程诊断写入失败：{type(exc).__name__}：{exc}')
            announced = time.monotonic() + 30
        publisher.join(timeout=.5)


def run_worker(username, password, exiting, messages, action='auto'):
    def check_cancelled():
        consume_commands(messages, exiting)
        at = now()
        return (exiting.is_set() or (RUNTIME/'stop.request').exists()
                or (RUNTIME/'scheduler.exit.request').exists()
                or not START <= at.hour*60+at.minute < STOP)
    try:
        check_before_login(cancelled=check_cancelled, log=log)
    except CheckCancelled as exc:
        log(str(exc))
        return 2
    except RuntimeError as exc:
        log(str(exc))
        return 4
    env = dict(os.environ, FUSIONLIVE_USERNAME=username, FUSIONLIVE_PASSWORD=password,
               FUSIONLIVE_SCHEDULED='1',
               PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    child = subprocess.Popen([sys.executable, '-u', str(CODE / 'sync.py'), action, '--retry-failed'],
                             cwd=HOME, env=env, stdin=subprocess.DEVNULL,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             encoding='utf-8', errors='replace',
                             creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0)
    lines = queue.Queue()
    def read_output():
        for line in child.stdout:
            lines.put(line.rstrip())
        child.stdout.close()
    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    counts = {'目录': 0, '清单': 0, '下载': 0}
    progress = Progress()
    last_progress, stopping = time.monotonic(), False
    last_progress_snapshot = last_progress_phase = None
    try:
        while child.poll() is None or reader.is_alive() or not lines.empty():
            consume_commands(messages, exiting)
            at = now()
            deadline = at.hour * 60 + at.minute >= STOP or at.hour < 2
            if exiting.is_set() or deadline or (RUNTIME / 'scheduler.exit.request').exists():
                if (RUNTIME / 'scheduler.exit.request').exists():
                    exiting.set()
                if exiting.is_set():
                    request_analysis_stop(HOME)
                if not stopping:
                    request_stop()
                    log('正在安全收尾：停止下载、清理临时文件并注销，请等待。')
                    stopping = True
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                line = ''
            if progress.accept(line):
                pass
            elif line.startswith('目录已保存'):
                counts['目录'] += 1
            elif line.startswith('清单已保存'):
                counts['清单'] += 1
            elif line.startswith('下载完成'):
                counts['下载'] += 1
            elif line and not line.startswith(('{', 'CSV 台账')):
                # Per-file audit data stays in the ledger; run.log samples busy categories.
                if not line.startswith('原件/附件核对'):
                    log(line)
            snapshot = tuple(progress.values.get(key) for key in (
                'phase', 'directories', 'pages', 'documents', 'downloads',
                'associated_done', 'associated_failed', 'current_file', 'current_bytes'))
            phase = progress.values.get('phase')
            interval = 30 if snapshot != last_progress_snapshot else 300
            if phase != last_progress_phase or time.monotonic() - last_progress >= interval:
                log(progress.message() + '。详细记录见 logs。')
                last_progress = time.monotonic()
                last_progress_snapshot, last_progress_phase = snapshot, phase
        code = child.wait()
        if 'download_total_files' in progress.values:
            log(progress.message())
        completed = progress.values.get('downloads', counts['下载'])
        log(f'当日任务结束，退出码 {code}；下载完成 {completed} 个。注销结果见当日 run-*-summary.json。')
        database = RUNTIME / 'ledger.sqlite3'
        if database.exists():
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                latest = db.execute('SELECT status FROM runs ORDER BY id DESC LIMIT 1').fetchone()
                if latest and latest[0] in ('logout_unconfirmed', 'running'):
                    code = 3
                tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if code == 0 and 'workflows' in tables:
                    stages = [r[0] for r in db.execute('SELECT stage FROM workflows')]
                    if stages and all(stage == 'scope_review' for stage in stages):
                        log('全项目盘点已完成，等待人工确认施工范围；保留简报定时任务，确认前不会下载。')
        if code == 3:
            (RUNTIME / 'login.paused').write_text('同步退出异常；检查 last_logout.json 后人工恢复登录。', encoding='utf-8')
            log('同步退出异常，暂停后续自动登录；保留离线分析及简报定时任务。请检查 runtime/last_logout.json。')
        return code
    finally:
        if child.poll() is None:
            request_stop()
            # Never forcibly kill a worker that is still logging out.
            log('等待子任务完成注销。')
            child.wait()


def consume_commands(messages, exiting):
    while not messages.empty():
        command = messages.get_nowait().strip().lower()
        if command in ('q', 'quit', 'exit'):
            request_full_stop()
            exiting.set()
        elif command in ('s', 'stop'):
            request_stop()
            log('已请求停止今天的任务；常驻窗口保留，明天继续。')


def start_sync_task(exiting):
    RUNTIME.mkdir(exist_ok=True)
    (RUNTIME / 'stop.request').unlink(missing_ok=True)
    def monitor():
        try:
            username, password = get_credentials()
            if exiting.is_set():
                return
            log('按已保存阶段继续：首次全量盘点、集中下载或每日维护。')
            threading.current_thread().result = run_worker(username, password, exiting, queue.Queue())
        except Exception as exc:
            log(f'同步启动或执行失败：{type(exc).__name__}：{exc}\n{traceback.format_exc()}')
    task = threading.Thread(target=monitor, daemon=True, name='sync-monitor')
    task.started_day = now().date().isoformat()
    task.start()
    return task


def main():
    if '--force-stop' in sys.argv:
        return force_stop()
    if '--stop' in sys.argv:
        request_full_stop()
        log('已请求停止同步、定时推送和后台解压解析，正在等待安全收尾。')
        if not wait_for_full_exit(timeout=120):
            return 5
        print('安全退出完成：同步、定时推送和后台解压解析均已停止。', flush=True)
        return 0
    if '--check' in sys.argv:
        print('Scheduler: Beijing download 02:00-10:30; publication times from runtime/wechat_push.json; Q=safe exit.')
        return 0
    exiting, messages = threading.Event(), queue.Queue()
    with RunLock('scheduler.lock'):
        (RUNTIME / 'scheduler.exit.request').unlink(missing_ok=True)
        (RUNTIME / 'stop.request').unlink(missing_ok=True)
        def commands():
            for line in sys.stdin:
                messages.put(line)
        threading.Thread(target=commands, daemon=True).start()
        def stop_all(*_):
            request_full_stop()
            exiting.set()
        signal.signal(signal.SIGINT, stop_all)
        signal.signal(signal.SIGTERM, stop_all)
        log('常驻调度已启动：北京时间02:00—10:30同步；推送文和管道简报按管理中心保存的独立时间发布。Q回车或Ctrl+C安全退出；S回车只停止今天同步。')
        if '--resume-now' in sys.argv:
            at = now()
            if not START <= at.hour * 60 + at.minute < STOP:
                log('续读盘点入口仅在北京时间 02:00—10:30 内立即执行；本次不登录。')
                return 2
            log('立即续读修复：必要时刷新目录与主清单以替换旧版本，保留未变化关联进度，不启动文件下载。')
            if (RUNTIME / 'login.paused').exists():
                log('账号登录仍被禁止，本次不启动平台任务。')
                return 4
            username, password = get_credentials()
            return run_worker(username, password, exiting, messages, action='resume-scan')
        last_day, announced = None, None
        retry_at = 0
        resume_background_analysis(HOME)
        publisher = threading.Thread(target=run_daily_briefing, args=(HOME, exiting, log), daemon=True,
                                     name='daily-briefing')
        publisher.start()
        sync_task = None
        try:
            while not exiting.is_set():
                consume_commands(messages, exiting)
                if (RUNTIME / 'scheduler.exit.request').exists():
                    exiting.set()
                if exiting.is_set():
                    break
                if sync_task and sync_task.is_alive():
                    exiting.wait(.2)
                    continue
                at = now()
                if sync_task is not None:
                    if getattr(sync_task, 'result', None) == 0:
                        last_day = getattr(sync_task, 'started_day', at.date().isoformat())
                    sync_task = None
                    retry_at = time.monotonic() + RETRY_SECONDS
                    if last_day != at.date().isoformat():
                        log(f'本轮同步已退出；下载窗口内 {RETRY_SECONDS} 秒后继续核对及重试未完成任务。')
                if last_day and last_day != at.date().isoformat():
                    last_day = None
                if stopped_today(at):
                    last_day = at.date().isoformat()
                # A manual stop lasts for this day only. Network failures never
                # mark the day complete; every pass resumes durable checkpoints.
                if eligible(at, last_day) and time.monotonic() >= retry_at:
                    if (RUNTIME / 'login.paused').exists():
                        retry_at = time.monotonic() + RETRY_SECONDS
                        log('账号登录仍被禁止；60秒后重新检查。请核对 runtime/login.paused 与 last_logout.json；离线任务继续。')
                    else:
                        sync_task = start_sync_task(exiting)
                    announced = None
                else:
                    waiting_retry = eligible(at, last_day)
                    target = f'retry:{retry_at}' if waiting_retry else next_start(at).isoformat()
                    if target != announced:
                        if waiting_retry:
                            log(f'仍在下载窗口内，约 {max(0, retry_at-time.monotonic()):.0f} 秒后重试未完成任务或重新检查登录限制。')
                        else:
                            log('保持窗口开启；等待下一次同步：' + target + '，每日简报和微信推送独立定时执行。')
                        announced = target
                    time.sleep(0.5)
        finally:
            exiting.set()
            request_full_stop()
            if sync_task:
                wait_for_publisher(sync_task, label='同步监控')
            wait_for_publisher(publisher)
            wait_for_analysis_exit(HOME, log)
        log('常驻调度已安全退出。')
    return 0


if __name__ == '__main__':
    sys.exit(main())
