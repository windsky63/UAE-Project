import argparse
import json
from pathlib import Path
import shutil
import time

def main():
    parser = argparse.ArgumentParser(description='Single-machine WeChat + Codex listener')
    parser.add_argument('--config', default='wechat.codex.json')
    parser.add_argument('--check', action='store_true', help='Read-only environment checks; no key extraction, Codex call or send')
    parser.add_argument('--once', action='store_true', help='Capture one cycle, then finish at most one Codex task')
    parser.add_argument('--jobs', action='store_true', help='Inspect recent job states without opening WeChat')
    parser.add_argument('--stop', action='store_true', help='Request a graceful stop without opening WeChat')
    args = parser.parse_args()
    try:
        from .config import load_config
        config = load_config(args.config)
    except (ImportError, OSError, ValueError) as exc:
        if not args.check:
            raise
        from .diagnostics import dependency_status
        print(json.dumps({'config_valid': False, 'ready_for_capture_attempt': False,
                          'live_validation_complete': False, 'dependencies': dependency_status(),
                          'errors': [str(exc), '请确认配置文件有效，并运行安装单机版.cmd 安装依赖。']}, ensure_ascii=False, indent=2))
        raise SystemExit(1) from None
    if args.check:
        from .diagnostics import check_environment
        report = check_environment(config)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if report['errors']:
            raise SystemExit(1)
        return
    stop_path = Path(config.database_cache) / 'listener.stop.request'
    if args.stop:
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text('stop after current cycle', encoding='utf-8')
        print('已请求安全停止监听，等待当前任务结束。', flush=True)
        return
    if args.jobs:
        from .state import State
        state = State(config.state_path)
        try:
            print(json.dumps([{**dict(r), 'task_number': state.task_number(r['id'])}
                              for r in state.db.execute('SELECT id,chat_id,status,usage,error FROM jobs ORDER BY created_at DESC LIMIT 30')], ensure_ascii=False, indent=2))
        finally:
            state.close()
        return
    from .desktop_lock import DesktopLock
    lock = None
    state = spool = listener = None
    try:
        lock = DesktopLock(str(Path(config.database_cache) / 'bridge.lock'))
        stop_path.unlink(missing_ok=True)
        if not config.chats:
            print('未配置监听会话，已进入待配置状态；保存会话后安全停止并重新启动以生效。', flush=True)
            while not args.once and not stop_path.exists():
                time.sleep(config.poll_seconds)
            return
        if config.codex_enabled and shutil.which(config.codex_command[0]) is None:
            raise RuntimeError('Codex CLI not found; configure codex_command before enabling generation')
        from .state import State
        from .wechat_capture import LocalDatabaseDriver
        from .spool import DesktopSpool
        from .codex_runner import CodexRunner
        from .listener import Listener
        state = State(config.state_path)
        state.recover_interrupted()
        driver = LocalDatabaseDriver(config)
        spool = DesktopSpool(config.state_path + '.captures')
        listener = Listener(config, driver, spool, state, CodexRunner(config))
        print('Local listener ready; codex_enabled=' + str(config.codex_enabled) + '; allow_send=' + str(config.allow_send), flush=True)
        while not stop_path.exists():
            try:
                listener.tick()
            except Exception as exc:
                print('Listener cycle failed: ' + type(exc).__name__, flush=True)
                if args.once:
                    raise
            if args.once:
                while listener.active and not listener.active[1].done():
                    listener.report_progress()
                    listener.retry_notices()
                    time.sleep(0.2)
                # Finish and deliver only the active job, without starting another one.
                if listener.active:
                    listener.finish_active()
                listener.deliver()
                break
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print('Stopping listener.', flush=True)
    finally:
        if listener:
            listener.close()
        if spool:
            spool.db.close()
        if state:
            state.close()
        if lock:
            lock.close()


if __name__ == '__main__':
    main()
