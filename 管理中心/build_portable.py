"""Package a private complete project with an isolated Python runtime and dependencies."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

import build_deployment

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / '管理中心/releases/Project_Tools_2026.10.27-windows-x64-offline-unified-env.5.zip'

COLLECT = r'''
import importlib.metadata as md,json,pathlib,sys
from packaging.requirements import Requirement
todo=json.loads(sys.argv[1]); seen={}; files={}
while todo:
    req=Requirement(todo.pop())
    if req.marker and not req.marker.evaluate({'extra':''}): continue
    dist=md.distribution(req.name)
    assert not req.specifier or dist.version in req.specifier,(req.name,dist.version,str(req.specifier))
    key=dist.metadata['Name'].lower().replace('_','-')
    if key in seen: continue
    seen[key]=dist.version
    todo.extend(dist.requires or [])
    site=pathlib.Path(dist.locate_file('')).resolve()
    for rel in dist.files or []:
        source=pathlib.Path(dist.locate_file(rel)).resolve()
        if not source.is_relative_to(site) or not source.is_file(): continue
        relative=source.relative_to(site)
        if '__pycache__' in relative.parts or source.suffix=='.pyc': continue
        files[relative.as_posix()]=str(source)
print(json.dumps({'base':sys.base_prefix,'packages':seen,'files':files}))
'''


def main():
    build_deployment.build()
    source = ROOT / '管理中心/releases' / f'Project_Tools_{build_deployment.RELEASE}.zip'
    with zipfile.ZipFile(source) as archive:
        entries = {name: archive.read(name) for name in archive.namelist() if name != 'deployment-manifest.json'}
    entries['管理中心/portable_setup.py'] = (ROOT / '管理中心/portable_setup.py').read_bytes()
    environments = {}
    base = None
    for name, requirements in (
        ('.venv-analysis', ('FusionLive同步工具/requirements.txt', '绿联云上传工具/requirements.txt', 'codex-wechat/requirements.txt')),
    ):
        roots = [line.strip() for filename in requirements
                 for line in (ROOT / filename).read_text(encoding='utf-8').splitlines()
                 if line.strip() and not line.lstrip().startswith('#')]
        roots += ['pip', 'packaging']
        python = ROOT / name / 'Scripts/python.exe'
        result = subprocess.run([str(python), '-c', COLLECT, json.dumps(roots)],
                                capture_output=True, text=True, encoding='utf-8', check=True)
        inventory = json.loads(result.stdout)
        runtime = Path(inventory['base'])
        if base is not None:
            assert runtime == base, 'Environments use different base interpreters'
        base = runtime
        for relative, location in inventory['files'].items():
            entries[f'{name}/Lib/site-packages/{relative}'] = Path(location).read_bytes()
        for exe in ('python.exe', 'pythonw.exe'):
            entries[f'{name}/Scripts/{exe}'] = (ROOT / name / 'Scripts' / exe).read_bytes()
        entries[f'{name}/pyvenv.cfg'] = b'include-system-site-packages = false\n'
        environments[name] = inventory['packages']

    # Include the base stdlib, Tcl/Tk and native DLLs, but no machine-global packages.
    for path in base.rglob('*'):
        if not path.is_file():
            continue
        relative = path.relative_to(base)
        if relative.parts[0] not in {'Lib', 'DLLs', 'tcl'} and len(relative.parts) != 1:
            continue
        if any(part.lower() in {'site-packages', '__pycache__', 'test', 'tests', 'idlelib'} for part in relative.parts):
            continue
        if path.suffix in {'.pyc', '.pyo'} or path.name == 'NEWS.txt':
            continue
        entries['python-runtime/' + relative.as_posix()] = path.read_bytes()

    install = '''@echo off
setlocal
chcp 65001 >nul
set "PYTHONHOME="
set "PYTHONPATH="
set "PYTHONUTF8=1"
"%~dp0python-runtime\\python.exe" -I -B "%~dp0管理中心\\portable_setup.py"
if errorlevel 1 goto failed
echo Environment installation complete. Copy program files into the server project and preserve existing data and configuration.
pause
exit /b 0
:failed
echo Installation failed. Keep the error message above.
pause
exit /b 1
'''
    entries['安装便携环境.cmd'] = install.replace('\n', '\r\n').encode('utf-8')
    launcher = entries['打开管理中心.cmd'].decode('ascii')
    marker = 'if exist ".venv-analysis\\Scripts\\python.exe" goto use_venv'
    bootstrap = ('set "PYTHONHOME="\r\nset "PYTHONPATH="\r\n'
                 'set "PATH=%~dp0.venv-analysis\\Scripts;%~dp0python-runtime;%PATH%"\r\n'
                 '"%~dp0python-runtime\\python.exe" -I -B "%~dp0' + '\u7ba1\u7406\u4e2d\u5fc3' + '\\portable_setup.py"\r\n'
                 'if errorlevel 1 (\r\n  set "ADMIN_EXIT_CODE=1"\r\n  goto cleanup\r\n)\r\n')
    # Keep the launcher ASCII-only, finding the setup script via a glob is unnecessary:
    # place its bootstrap at the root with an ASCII name.
    entries['portable_setup.py'] = entries['管理中心/portable_setup.py'].replace(
        b'ROOT = Path(__file__).resolve().parent.parent', b'ROOT = Path(__file__).resolve().parent')
    bootstrap = bootstrap.replace('管理中心\\portable_setup.py', 'portable_setup.py')
    assert marker in launcher
    entries['打开管理中心.cmd'] = launcher.replace(marker, bootstrap + marker).encode('ascii')
    lan_launcher = entries['打开管理中心-局域网.cmd'].decode('ascii')
    assert marker in lan_launcher
    entries['打开管理中心-局域网.cmd'] = lan_launcher.replace(marker, bootstrap + marker).encode('ascii')
    manifest = {'release': '2026.10.27-windows-x64-offline-unified-env.5', 'python': '3.14.3',
                'environments': environments, 'contains_fusionlive_login_credentials': True,
                'runtime_data_included': False,
                'files': {name: {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
                          for name, data in sorted(entries.items())}}
    entries['deployment-manifest.json'] = json.dumps(manifest, ensure_ascii=False, indent=2).encode('utf-8')
    with zipfile.ZipFile(OUTPUT, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(entries.items()):
            archive.writestr(name, data)
    with zipfile.ZipFile(OUTPUT) as archive:
        assert archive.testzip() is None
        for name, info in manifest['files'].items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == info['sha256'], name
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    OUTPUT.with_suffix('.zip.sha256').write_text(digest + '  ' + OUTPUT.name + '\n', encoding='utf-8')
    print(json.dumps({'file': OUTPUT.name, 'MiB': round(OUTPUT.stat().st_size / 1024**2, 2),
                      'files': len(entries), 'sha256': digest}, ensure_ascii=False))


if __name__ == '__main__':
    main()
