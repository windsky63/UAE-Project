"""Read-only stop diagnostics: no requests, downloads, model calls or process signals."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import time

from manager import locked, tail, clean_log


def diagnose(root):
    root = Path(root).resolve()
    base = root / 'FusionLive同步工具'
    runtime = base / 'runtime'
    at = datetime.now(timezone(timedelta(hours=8)))
    locks = [('调度程序', runtime / 'scheduler.lock'), ('下载/注销', runtime / 'sync.lock'),
             ('后台解压解析', runtime / 'file_analysis/worker.lock'),
             ('简报微信推送', runtime / 'wechat_delivery/worker.lock')]
    report = dict(checked_at_beijing=at.isoformat(), root=str(root), locks=[], stop_requests=[],
                  processes=[], logs=[], warnings=[])
    for label, path in locks:
        report['locks'].append(dict(task=label, file=str(path), exists=path.is_file(),
                                   occupied_or_inaccessible=locked(path)))
    for path in (runtime / 'scheduler.exit.request', runtime / 'stop.request',
                 runtime / 'file_analysis/stop.request'):
        report['stop_requests'].append(dict(file=str(path), exists=path.is_file(),
            modified_beijing=datetime.fromtimestamp(path.stat().st_mtime, at.tzinfo).isoformat() if path.is_file() else None))
    try:
        import psutil
        for process in psutil.process_iter(['pid', 'name', 'cmdline', 'create_time']):
            try:
                if process.pid == os.getpid():
                    continue
                arguments = process.info['cmdline'] or []
                if 'python' not in (process.info['name'] or '').lower():
                    continue
                if not any(str(root).casefold() in argument.casefold() for argument in arguments):
                    continue
                # Do not output full command lines, environment variables or secrets.
                scripts = [Path(argument).name for argument in arguments if argument.lower().endswith('.py')]
                try:
                    opened_paths = {Path(opened.path).resolve() for opened in process.open_files()}
                    held = [label for label, path in locks if path in opened_paths]
                except (psutil.Error, OSError):
                    held = ['无法读取此进程的打开文件列表']
                report['processes'].append(dict(pid=process.pid, name=process.info['name'], scripts=scripts,
                    open_lock_files=held, uptime_seconds=round(time.time() - process.info['create_time'], 1)))
            except (psutil.Error, OSError, ValueError):
                continue
    except ImportError:
        report['warnings'].append('psutil不可用；仍可查看锁和日志，不安装任何依赖。')
    candidates = [base / 'logs' / at.date().isoformat() / 'scheduler.log',
                  runtime / 'file_analysis/progress.log', runtime / 'file_analysis/worker.log']
    admin_runtime = root / '管理中心/runtime'
    if admin_runtime.is_dir():
        candidates += sorted(admin_runtime.glob('*-sync-stop-*.log'), key=lambda path: path.stat().st_mtime, reverse=True)[:1]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            report['logs'].append(dict(file=str(path),
                modified_beijing=datetime.fromtimestamp(path.stat().st_mtime, at.tzinfo).isoformat(),
                tail=clean_log('\n'.join(tail(path, 8192).splitlines()[-12:]))))
        except OSError as exc:
            report['warnings'].append(f'{path.name}: {type(exc).__name__}')
    report['warnings'].append('占用或访问受限都可能导致锁探测失败；进程打开文件列表可能不完整，不能单靠锁文件存在判断进程仍运行。')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    print(json.dumps(diagnose(args.root), ensure_ascii=False, indent=2))
