from pathlib import Path
import os, runpy, sys
COMMANDS = {'unit_text', 'drawing_file_delivery', 'import_result_cache', 'scheduler', 'analysis_worker', 'local_analysis', 'briefing', 'drawing_briefing', 'archive_cache', 'push_setup'}
def main():
    args = sys.argv[1:]
    if '--data-home' in args:
        i = args.index('--data-home')
        if i+1>=len(args) or args[i+1].startswith('--'):
            print('错误：--data-home 后需要数据目录路径',file=sys.stderr)
            return 2
        os.environ['PROJECT_DATA_HOME'] = str(Path(args[i+1]).resolve())
        del args[i:i+2]
    if not args or args[0] not in COMMANDS:
        print('Usage: main.py <command> [--data-home DIRECTORY] [arguments]')
        return 2
    app = Path(__file__).resolve().parent / 'app'
    sys.path.insert(0, str(app))
    sys.argv = [str(app / (args[0] + '.py')), *args[1:]]
    from log_policy import DailyLog, StreamLog, utf8_streams
    utf8_streams()
    original = sys.stderr
    stream = StreamLog(original, DailyLog(Path(os.environ.get('PROJECT_DATA_HOME', app.parent)),
                                         'analysis', 'cli-diagnostics.log', captured=True), 'analysis')
    sys.stderr = stream
    try:
        runpy.run_path(sys.argv[0], run_name='__main__')
    except Exception:
        import traceback
        traceback.print_exc()
        return 1
    finally:
        try: stream.finish()
        finally: sys.stderr = original
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
