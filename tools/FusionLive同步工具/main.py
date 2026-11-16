"""Stable root launcher; implementation modules live in app/."""
from pathlib import Path
import runpy
import sys

COMMANDS = {'ledger_export', 'scheduler', 'sync', 'gui', 'preflight', 'login_test', 'business_type', 'file_layout', 'history_repair', 'path_maintenance'}


def main():
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='replace')
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print('Usage: python main.py <'+ '|'.join(sorted(COMMANDS))+'> [arguments]')
        return 2
    command = sys.argv.pop(1)
    if command in ('file_layout', 'history_repair'):
        command = 'path_maintenance'
    app = Path(__file__).resolve().parent/'app'
    sys.path.insert(0,str(app))
    sys.argv[0] = str(app/(command+'.py'))
    runpy.run_path(sys.argv[0],run_name='__main__')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
