"""Install project dependencies using the server's Python 3.12."""
from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def isolated_python(python, *args):
    # -I ignores PYTHONUTF8, so the interpreter option must enable UTF-8 itself.
    return [str(python), '-I', '-X', 'utf8', *args]


def main():
    if sys.version_info[:2] != (3, 12) or sys.platform != 'win32':
        raise RuntimeError('Run this installer with Windows Python 3.12.')
    env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)
    # All tools use one interpreter and one dependency resolution.
    targets = [(ROOT / '.venv-analysis', ROOT / 'requirements.txt')]
    for folder, requirements in targets:
        if not requirements.is_file():
            raise FileNotFoundError(requirements)
        if folder.exists():
            python = folder / 'Scripts/python.exe'
            result = subprocess.run(isolated_python(python, '-c',
                                     'import sys; print("%s.%s" % sys.version_info[:2])'),
                                    capture_output=True, text=True, env=env)
            if result.returncode or result.stdout.strip() != '3.12':
                raise RuntimeError(f'Existing environment is not Python 3.12: {folder}. '
                                   'Stop its tools and rename the environment as a backup, then retry.')
    for folder, requirements in targets:
        if not folder.exists():
            subprocess.run(isolated_python(sys.executable, '-m', 'venv', str(folder)), env=env, check=True)
        python = folder / 'Scripts/python.exe'
        subprocess.run(isolated_python(python, '-m', 'pip', 'install', '-r', str(requirements)),
                       cwd=ROOT, env=env, check=True)
        subprocess.run(isolated_python(python, '-m', 'pip', 'check'), env=env, check=True)
    subprocess.run(isolated_python(targets[0][0] / 'Scripts/python.exe', '-c',
                    'import requests, py7zr, openpyxl, PIL, pandas, numpy, tkinter, '
                    'uiautomation, win32api, pyperclip, psutil'), env=env, check=True)
    subprocess.run(isolated_python(targets[0][0] / 'Scripts/python.exe', '-c',
                    'import pydantic, cryptography, uiautomation, win32api, pyperclip, '
                    'psutil, zstandard, colorama'), env=env, check=True)
    print('Shared .venv-analysis installed; dependency checks passed for all tools.', flush=True)


if __name__ == '__main__':
    main()
