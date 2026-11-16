"""Root CMD delegates startup to the management centre, never to parked files."""
import runpy
import sys
from pathlib import Path
from datetime import datetime
import traceback


def main():
    application = Path(__file__).resolve().parent
    log = application / 'runtime/launcher.log'
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open('a', encoding='utf-8') as stream:
            stream.write(f'[{datetime.now().isoformat()}] Start; Python={sys.executable}\n')
        if not (application / 'server.py').is_file():
            raise FileNotFoundError('Missing server.py in management centre.')
        if not (application / 'web/dist/index.html').is_file():
            raise RuntimeError('Missing web/dist/index.html; run the root dependency installer.')
        sys.path.insert(0, str(application))
        sys.argv[0] = str(application / 'server.py')
        runpy.run_path(sys.argv[0], run_name='__main__')
        return 0
    except KeyboardInterrupt:
        return 0
    except Exception:
        detail = traceback.format_exc()
        with log.open('a', encoding='utf-8') as stream:
            stream.write(detail + '\n')
        print(detail, file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
