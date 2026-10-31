"""Relocate bundled Windows environments without creating environments or pip installs."""
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent


def main():
    if sys.platform != 'win32' or platform.machine().lower() not in {'amd64', 'x86_64'}:
        raise RuntimeError('This bundle requires Windows x64.')
    base = ROOT / 'python-runtime'
    env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV'):
        env.pop(key, None)
    env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1', PYTHONUTF8='1',
               PYTHONIOENCODING='utf-8')
    probes = {
        '.venv-analysis': 'import requests,py7zr,openpyxl,PIL,pandas,numpy,uiautomation,win32clipboard,win32gui,psutil,pyperclip,pydantic,cryptography,zstandard,colorama; import tkinter; tkinter.Tcl(); from PIL import Image; Image.new("RGB",(1,1)); import lzma,ssl,sqlite3; from cryptography.hazmat.primitives.ciphers import Cipher,algorithms,modes; Cipher(algorithms.AES(bytes(32)),modes.CBC(bytes(16))).encryptor(); pydantic.create_model("Probe",value=(int,...))(value=1)',
    }
    for name, probe in probes.items():
        folder = ROOT / name
        config = f'home = {base}\ninclude-system-site-packages = false\nversion = 3.14.3\nexecutable = {base / "python.exe"}\n'
        (folder / 'pyvenv.cfg').write_text(config, encoding='utf-8')
        python = folder / 'Scripts/python.exe'
        # Isolated mode plus prefix checks prevents an installed system Python
        # from accidentally making an incomplete bundle appear healthy.
        check = ('import sys,pathlib; '
                 f'assert pathlib.Path(sys.base_prefix).resolve()==pathlib.Path({str(base)!r}).resolve(); '
                 f'assert pathlib.Path(sys.prefix).resolve()==pathlib.Path({str(folder)!r}).resolve(); '
                 'assert not sys.flags.no_site; ' + probe)
        subprocess.run([str(python), '-I', '-B', '-c', check], cwd=ROOT, env=env, check=True)
        subprocess.run([str(python), '-I', '-B', '-m', 'pip', 'check'], cwd=ROOT, env=env, check=True)
        print(f'OK: {name}', flush=True)
    config = ROOT / 'codex-wechat/wechat.codex.json'
    if not config.exists():
        defaults = json.loads(config.with_name('wechat.codex.example.json').read_text(encoding='utf-8-sig'))
        try:
            with config.open('x', encoding='utf-8') as output:
                output.write(json.dumps(defaults, ensure_ascii=False, indent=2) + '\n')
            print('Created initial WeChat config from defaults; empty chat list starts in idle mode.', flush=True)
        except FileExistsError:
            pass
    print('Bundled environments ready. No venv creation or package download performed.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Portable setup failed: {exc}', file=sys.stderr)
        raise SystemExit(1)
