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
echo Environment installation complete. Read the deployment guide before migrating existing data.
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
    guide = '''# 含环境的 Windows x64 部署包

本包包含 Python 3.14.3 x64、Tcl/Tk、一个共享虚拟环境及其完整运行依赖，无需服务器创建虚拟环境、安装系统 Python 或通过 pip 下载依赖。

使用方法：
1. 解压到独立暂存目录，不直接覆盖服务器现有目录。
2. 双击“安装便携环境.cmd”。它只校正随包环境的基础解释器路径并执行离线自检，不调用 venv 或 pip install。
3. 按“部署说明-给Codex.md”完成服务器数据备份与程序合并。该文档第3节的创建环境/安装依赖命令由本包环境安装入口替代；第4节 Codex CLI 安装和账号登录仍需按实际情况处理。
4. 将 python-runtime、.venv-analysis 和 portable_setup.py 一并放到最终根目录，再双击“安装便携环境.cmd”校正最终路径。
5. 双击“打开管理中心.cmd”，它也会自动校正环境路径。建议通过管理中心运行工具，独立工具的旧 CMD 可能仍选择系统 Python。命令行操作请显式使用随包虚拟环境 Python；pip 请使用 python.exe -m pip。

不要把本包中的共享环境与服务器旧虚拟环境混合覆盖；旧环境保留为备份，当前共享环境必须使用本包提供的完整目录。安装脚本不修改服务器下载文件、台账、上传配置、既有微信配置或运行开关；微信配置缺失时会创建关闭自动回复及发送的默认文件。包内 codex-wechat/wechat.codex.json 是安全初始配置，合并升级时必须保留服务器已有同名配置，不能用默认文件覆盖它。

支持范围：Windows x64，操作系统需支持 Python 3.14。不是 Linux、Windows ARM64 或旧版 Windows 的通用安装包。仅做当前 Windows 系统上的隔离依赖、换目录测试，服务器微信 UI 兼容性和真实网络服务仍需现场验证。

Codex CLI、微信客户端、账号登录、聊天名单及 NAS 访问权限属于服务器外部环境，不包含在 Python 虚拟环境内。包内经用户授权包含同步工具现有登录凭据，须私下传递。
'''
    entries['先读我-含环境安装.md'] = guide.encode('utf-8')
    old_guide = entries['部署说明-给Codex.md'].decode('utf-8')
    entries['部署说明-给Codex.md'] = ('> 本包为含环境版：优先阅读“先读我-含环境安装.md”。已提供完整环境；不要执行下文第3节创建环境或pip安装命令，改运行“安装便携环境.cmd”。\n\n' + old_guide).encode('utf-8')
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
