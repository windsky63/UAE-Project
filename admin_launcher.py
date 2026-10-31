"""Portable management-center entry point; the .cmd wrapper stays ASCII-only."""
from datetime import datetime
from pathlib import Path
import runpy
import sys
import traceback


def main():
    root = Path(__file__).resolve().parent
    application = root / '\u7ba1\u7406\u4e2d\u5fc3'
    logfile = application / 'runtime' / 'launcher.log'
    try:
        if sys.version_info < (3, 11):
            raise RuntimeError('Python 3.11 or newer is required.')
        if not (application / 'server.py').is_file():
            raise FileNotFoundError(f'Missing server.py under {application}. Extract the complete update into the project root.')
        if not (application / 'web' / 'dist' / 'index.html').is_file():
            raise FileNotFoundError('Missing management web/dist/index.html. Extract the complete update package.')
        logfile.parent.mkdir(parents=True, exist_ok=True)
        sys.path.insert(0, str(application))
        # Normalize old PowerShell UTF-16/legacy logs before appending new UTF-8.
        if (application / 'log_tools.py').is_file():
            from log_tools import normalize_logs
            normalized=normalize_logs(root)
            if normalized['converted']:
                print(f'Historical logs converted to UTF-8: {len(normalized["converted"])}; originals backed up.',flush=True)
        with logfile.open('a', encoding='utf-8') as output:
            output.write(f'[{datetime.now().astimezone().isoformat()}] Start; Python {sys.version.split()[0]}; executable={sys.executable}; root={root}\n')
        sys.argv[0] = str(application / 'server.py')
        runpy.run_path(sys.argv[0], run_name='__main__')
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        detail = traceback.format_exc()
        try:
            logfile.parent.mkdir(parents=True, exist_ok=True)
            with logfile.open('a', encoding='utf-8') as output:
                output.write(detail + '\n')
        except OSError:
            pass
        print(detail, file=sys.stderr, flush=True)
        print(f'Management center startup failed. See: {logfile}', file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
