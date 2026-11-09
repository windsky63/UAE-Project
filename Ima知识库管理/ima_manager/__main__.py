import argparse
import getpass
import json
import signal
import sqlite3
import sys
import threading
from pathlib import Path
from contextlib import nullcontext

from .api import ImaClient, ImaError, ImaQuotaError, ImaRateLimitError
from .config import RUNTIME, load_credentials, load_settings, save_credentials, atomic_json
from .sync import Engine, Store, scan


def output(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description='ima 知识库管理：默认仅预览，sync/watch 加 --execute 才上传或同步逻辑删除。')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('gui', help='显示管理中心入口（兼容旧命令）')
    sub.add_parser('stop', help='请求自动或手动同步安全停止')
    sub.add_parser('control', help=argparse.SUPPRESS)
    sub.add_parser('configure', help='交互填写凭证并使用 Windows DPAPI 加密保存')
    sub.add_parser('bases', help='列出有权添加内容的知识库')
    sub.add_parser('status', help='查看本机最近上传记录')
    collection = sub.add_parser('collect', help='只读下载数据库，提取项目管理规定和分类图纸到待同步；不上传、不移动源文件')
    collection.add_argument('--source')
    inputs = collection.add_mutually_exclusive_group()
    inputs.add_argument('--database', help='同步工具 runtime/ledger.sqlite3 路径，只读')
    inputs.add_argument('--ledger', help='兼容模式：显式从 Excel 快照提取')
    collection.add_argument('--downloads')
    collection.add_argument('--dry-run', action='store_true')
    for name in ('scan', 'plan', 'sync', 'watch'):
        child = sub.add_parser(name)
        child.add_argument('--source', help='本地资料目录；默认使用已保存设置或待同步目录')
        child.add_argument('--top-only', action='store_true', help='只扫描顶层文件')
        if name != 'scan':
            child.add_argument('--kb', help='从 bases 返回的真实知识库 ID')
            child.add_argument('--folder', help='从 list 返回的真实文件夹 ID；留空为根目录')
            inputs = child.add_mutually_exclusive_group()
            inputs.add_argument('--database', help='同步工具 runtime/ledger.sqlite3 路径，只读')
            inputs.add_argument('--ledger', help='兼容模式：显式从 Excel 快照提取')
            child.add_argument('--downloads', help='原下载目录，只读')
            child.add_argument('--no-collect', action='store_true', help='本轮跳过台账资料复制')
        if name in ('sync', 'watch'):
            child.add_argument('--execute', action='store_true', help='实际执行上传及删除目录对应的云端移动')
        if name == 'watch':
            child.add_argument('--interval', type=int, help='本地定时检查间隔秒数，至少 10 秒；不会增加每日同步次数')
    for name in ('list', 'search'):
        child = sub.add_parser(name)
        child.add_argument('--kb')
        child.add_argument('--folder')
        if name == 'search':
            child.add_argument('query')
    child = sub.add_parser('resolve', help='人工核对入库不确定记录后解锁')
    child.add_argument('--kb')
    child.add_argument('--folder')
    child.add_argument('--path', required=True)
    child.add_argument('--outcome', choices=['uploaded', 'absent'], required=True)
    child.add_argument('--execute', action='store_true', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'gui':
            print('请打开项目根目录的“打开管理中心.cmd”，选择“ima 知识库”。')
            return 0
        if args.command == 'stop':
            RUNTIME.mkdir(parents=True, exist_ok=True)
            (RUNTIME / 'watch-stop.request').write_text('stop', encoding='ascii')
            print('已请求安全停止，当前文件或请求完成后退出。')
            return 0
        if args.command == 'control':
            from .control import run
            output(run(json.load(sys.stdin)))
            return 0
        if args.command == 'configure':
            save_credentials(input('Client ID: '), getpass.getpass('API Key（输入不显示）: '))
            print('凭证已加密保存在本机，绑定当前 Windows 用户。')
            return 0
        if args.command == 'status':
            output(Store(RUNTIME / 'state.sqlite3').history())
            return 0
        settings = load_settings()
        if args.command == 'watch' and settings.get('auto_sync_enabled', True) is not True:
            raise ImaError('自动同步已关闭，请在管理中心开启并保存，或使用手动同步。')
        if getattr(args, 'ledger', None):
            settings['ledger_path'] = args.ledger
            settings['_legacy_ledger_mode'] = True
        if getattr(args, 'downloads', None):
            settings['downloads_path'] = args.downloads
            if not getattr(args, 'database', None):
                settings['database_path'] = str(Path(args.downloads).resolve().parent / 'runtime/ledger.sqlite3')
        if getattr(args, 'database', None):
            settings['database_path'] = args.database
        if getattr(args, 'no_collect', False):
            settings['collect_enabled'] = False
        source = getattr(args, 'source', None) or settings['source']
        recursive = not getattr(args, 'top_only', False) and settings.get('recursive', True)
        if args.command == 'scan':
            output(scan(source, recursive))
            return 0
        if args.command == 'collect':
            from .collect import prepare_documents
            report = prepare_documents(dict(settings, collect_enabled=True), source,
                dry_run=args.dry_run, notify=lambda text: print(text, file=sys.stderr))
            output(report)
            return 0  # Per-file problems remain in the report; usable copies are ready to sync.
        client = ImaClient(*load_credentials())
        if args.command == 'bases':
            output(client.bases())
            return 0
        kb = args.kb or settings.get('kb', '')
        folder = args.folder if args.folder is not None else settings.get('folder', '')
        if args.command == 'list':
            output(client.listing(kb, folder))
            return 0
        if args.command == 'search':
            output(client.search(kb, args.query))
            return 0
        engine = Engine(client, Store(RUNTIME / 'state.sqlite3'))
        if args.command == 'resolve':
            engine.resolve_upload(kb, folder, args.path, args.outcome == 'uploaded')
            print('已记录人工核对结果。')
            return 0
        if args.command == 'watch':
            args.interval = args.interval if args.interval is not None else settings.get('interval_seconds', 60)
            if not args.execute or args.interval < 10:
                parser.error('watch 需要 --execute，且 --interval 至少为 10。')
        stopped = threading.Event()
        def interruptible_api_wait(seconds):
            if stopped.wait(seconds):
                raise ImaError('收到安全停止请求，已停止等待和后续 ima 请求。')
        client.sleep = interruptible_api_wait
        signal.signal(signal.SIGINT, lambda *_: stopped.set())
        from .watch import watch_session, claim_daily_sync, validate_sync_time
        sync_time = validate_sync_time(settings.get('sync_time', '13:00'))
        if args.command == 'watch':
            args.interval = min(args.interval, 60)
            print(f'定时同步已启动：北京时间每天 {sync_time}；一天最多一次，手动同步也计入次数；失败后次日再试。', file=sys.stderr, flush=True)
        session = (watch_session(RUNTIME, stopped, automatic=args.command == 'watch')
                   if args.command in ('sync', 'watch') else nullcontext())
        with session:
          from .watch import ActivityLog
          activity=ActivityLog(lambda text:print(text,file=sys.stderr,flush=True),redact=getattr(client,'redact',lambda value:value))
          while not stopped.is_set():
            if getattr(args, 'execute', False) and not claim_daily_sync(
                    RUNTIME, scheduled=args.command == 'watch', sync_time=sync_time):
                if args.command != 'watch':
                    print('今天已执行过同步，一天最多一次；请于次日再执行。', file=sys.stderr)
                    return 1
                stopped.wait(args.interval)
                continue
            reset_interval = getattr(client, 'reset_interval', None)
            if callable(reset_interval):
                reset_interval()
            stage = '资料提取'
            try:
                activity.phase(stage)
                from .collect import prepare_documents
                collection_report = prepare_documents(settings, source, dry_run=not getattr(args, 'execute', False),
                                                       notify=activity.notify, stop=stopped)
                if stopped.is_set():
                    break
                stage = '生成同步计划'
                activity.phase(stage)
                rows = engine.plan(source, kb, folder, recursive)
                if getattr(args, 'execute', False):
                    stage = '执行同步'
                    activity.phase(stage)
                    from .daily import ensure_journal
                    ensure_journal(source)
                    rows = engine.execute(rows, kb, folder, stop=stopped, notify=activity.notify)
                stage = '保存同步报告'
                activity.phase(stage)
                report = {'layout': 'relative-tree-v1', 'source': source, 'knowledge_base_id': kb, 'folder_id': folder,
                          'executed': getattr(args, 'execute', False), 'files': rows, 'collection': collection_report}
                atomic_json(RUNTIME / 'latest-plan.json', report)
                output(report)
                activity.finish('stopped' if stopped.is_set() else 'pending' if any(row['status'] in ('failed','uncertain','conflict','blocked') for row in rows) else 'complete')
            except (ImaError, OSError, ValueError, sqlite3.Error) as exc:
                activity.finish('stopped' if stopped.is_set() else 'failed')
                if isinstance(exc, (ImaQuotaError, ImaRateLimitError)):
                    print(f'ima {exc.code} 持续拒绝请求，已中止本轮同步，保留已成功回执。', file=sys.stderr, flush=True)
                    if stage == '执行同步':
                        report = {'layout': 'relative-tree-v1', 'source': source, 'knowledge_base_id': kb,
                                  'folder_id': folder, 'executed': True, 'files': rows,
                                  'collection': collection_report, 'aborted': True,
                                  'error_code': exc.code, 'error': str(exc)}
                        atomic_json(RUNTIME / 'latest-plan.json', report)
                if args.command != 'watch':
                    raise
                detail = f'{stage}失败（{type(exc).__name__}）：{exc}'
                print(f'自动同步本轮失败：{detail}；今日次数已使用，次日定时再试。', file=sys.stderr, flush=True)
                try:
                    from .daily import record
                    record(source, '同步任务', '（任务）', '失败', detail=detail)
                except (OSError, ValueError, sqlite3.Error) as journal_exc:
                    print(f'失败记录写入失败：{journal_exc}', file=sys.stderr, flush=True)
                stopped.wait(args.interval)
                continue
            if args.command != 'watch':
                if any(r['status'] in ('failed', 'uncertain') for r in rows):
                    return 1
                return 0
            stopped.wait(args.interval)
        return 0
    except (ImaError, OSError, ValueError, sqlite3.Error) as exc:
        actual = args.command == 'collect' and not getattr(args, 'dry_run', False)
        actual = actual or (args.command in ('sync', 'watch') and getattr(args, 'execute', False))
        if actual and 'source' in locals():
            try:
                from .daily import record
                record(source, '资料复制' if args.command == 'collect' else '同步任务', '（任务）', '失败', detail=str(exc))
            except (OSError, ValueError, sqlite3.Error):
                pass  # Keep the original failure visible even if the journal itself is unwritable.
        print(f'ima操作失败：operation={args.command}；type={type(exc).__name__}；reason={exc}', file=sys.stderr)
        return 1
    finally:
        if 'activity' in locals():
            activity.close()


if __name__ == '__main__':
    raise SystemExit(main())
