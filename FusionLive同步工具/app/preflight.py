"""Offline release integrity and runtime checks, before any FusionLive login."""

from paths import HOME, CODE
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import sys
import tempfile
import time

RELEASE_ID = '2026.11.04-owner-updates.1'
MANIFEST = 'release_manifest.json'
REQUIRED_FILES = {
    'app/path_maintenance_launcher.py',
    'app/path_maintenance.py',
    '整理文件目录.cmd',
    'app/history_repair.py',
    'app/file_layout.py',
    'app/local_reconcile.py',
    'app/history_retention.py',
    'app/publication_worker.py',
    'app/ledger_workbook.py',
    'app/archive_index.py',
    'app/archive_cache.py',
    'app/runtime_logging.py',
    'app/analysis_settings.py',
    'app/equipment_reader.py',
    'app/prefab_rules_260818.py',
    'app/weld_classification.py',
    'app/analysis_cache.json',
    'app/analysis_worker.py',
    'app/briefing.py',
    'app/briefing_schedule.py',
    'app/business_type.py',
    'app/fusion_client.py',
    'app/chunked_download.py',
    'app/gui.py',
    'app/inventory.py',
    'app/ledger.py',
    'app/local_analysis.py',
    'app/local_credentials.py',
    'app/login_test.py',
    'app/logout_watchdog.py',
    'app/parse_idf_to_json.py',
    'app/parse_pcf_to_json.py',
    'app/paths.py',
    'app/preflight.py',
    'app/progress.py',
    'app/push_settings.py',
    'app/push_setup.py',
    'app/reporting.py',
    'app/scheduler.py',
    'app/scope_review.py',
    'app/sync.py',
    'app/wechat_delivery.py',
    'app/wechat_test_send.py',
    'app/workflow.py',
    'app/台账.py',
    'approved_download_plan.json',
    'document_coding_rules.json',
    'main.py',
    'maintenance/wechat_push.example.json',
    'maintenance/启动续读盘点.cmd',
    'maintenance/安装依赖.cmd',
    'maintenance/检查程序完整性.cmd',
    'maintenance/解析本地管道文件.cmd',
    'material_split_rules.json',
    'requirements.txt',
    'wechat_push/__init__.py',
    'wechat_push/errors.py',
    'wechat_push/uia.py',
    'wechat_push/desktop_guard.py',
    'wechat_push/uia_profile.json',
    '启动正式下载.cmd',
    '安全退出.cmd',
    '打开文件台账.cmd',
    '生成管道简报.cmd',
    '恢复微信发送.cmd',
}


def run_checks(home=HOME, *, runtime=True, gui=False, wechat=False, expected_release=RELEASE_ID, progress=None, startup=False):
    progress = progress or (lambda message: None)
    progress('校验发布清单、程序大小和SHA256')
    home = Path(home).resolve()
    report = {'release': expected_release, 'checked_at_beijing': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'files_checked': 0, 'errors': [], 'warnings': [], 'network_accessed': False}
    report['check_mode'] = 'download-startup' if startup else 'full'
    errors = report['errors']
    if sys.version_info < (3, 10):
        errors.append('需要 Python 3.10 或更高版本。')
    try:
        manifest = json.loads((home / MANIFEST).read_text(encoding='utf-8'))
        if not isinstance(manifest, dict):
            raise ValueError('manifest must be a mapping')
        if manifest.get('format') != 1 or manifest.get('release') != expected_release:
            errors.append('版本不一致：校验器与 release_manifest.json 不属于同一发布版本，请重新完整覆盖更新包。')
        files = manifest['files']
        if not isinstance(files, dict):
            raise ValueError('files must be a mapping')
        # Overlay releases retain the server's login file, whose hash cannot be
        # known by the builder. No program module may use this exemption.
        preserved = manifest.get('preserved_local_files', [])
        if not isinstance(preserved, list) or any(name != 'app/local_credentials.py' for name in preserved):
            raise ValueError('invalid preserved local files')
        preserved = set(preserved)
        if preserved.intersection(files):
            raise ValueError('preserved local file also has a release hash')
        for missing in sorted(REQUIRED_FILES - files.keys() - preserved):
            errors.append('校验清单缺少必要项目：' + missing)
        for name in sorted(preserved):
            path = (home / name).resolve()
            if not path.is_relative_to(home):
                errors.append('服务器凭据路径越界：' + name)
                continue
            try:
                data = path.read_bytes()
            except OSError:
                errors.append('服务器凭据缺失或无法读取：' + name + '；请恢复服务器原有凭据文件。')
                continue
            report['files_checked'] += 1
            try:
                compile(data, str(path), 'exec')
            except (SyntaxError, ValueError):
                errors.append('服务器凭据 Python 语法检查失败：' + name)
        report['preserved_local_files'] = sorted(preserved)
        for name, reference in files.items():
            parts = PurePosixPath(name).parts
            allowed = len(parts) == 1 or (len(parts) == 2 and parts[0] in ('maintenance', 'wechat_push', 'app'))
            if (not allowed or '/'.join(parts) != name or ':' in name or '\\' in name
                    or '..' in parts or Path(name).is_absolute() or name == MANIFEST):
                errors.append('校验清单含非法程序路径：' + name)
                continue
            path = (home / name).resolve()
            if not path.is_relative_to(home):
                errors.append('校验清单含越界程序路径：' + name)
                continue
            try:
                data = path.read_bytes()
            except OSError:
                errors.append('文件缺失或无法读取：' + name)
                continue
            report['files_checked'] += 1
            actual = hashlib.sha256(data).hexdigest()
            if not isinstance(reference, dict) or reference.get('sha256') != actual or reference.get('bytes') != len(data):
                errors.append('SHA-256 / 大小不匹配：' + name + '（可能修改、损坏或混用版本）')
            if name.endswith('.py'):
                try:
                    compile(data, str(path), 'exec')
                except (SyntaxError, ValueError) as exc:
                    errors.append('Python 语法检查失败：' + name + ' / ' + type(exc).__name__)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        errors.append('无法读取有效的 ' + MANIFEST + '：' + type(exc).__name__ + '。请重新完整解压更新包。')

    if runtime:
        report['python'] = sys.version.split()[0]
        progress('检查requests依赖')
        try:
            importlib.import_module('requests')
            installed = metadata.version('requests')
            major, minor = (int(part) for part in installed.split('.')[:2])
            if not ((major, minor) >= (2, 31) and major < 3):
                errors.append('requests 版本不满足 requirements.txt，请运行“maintenance/安装依赖.cmd”。')
            report['requests'] = installed
        except (ImportError, metadata.PackageNotFoundError, ValueError):
            errors.append('requests 或其依赖不可用，请运行“maintenance/安装依赖.cmd”。')
        packages = () if startup else (('openpyxl', 'openpyxl'), ('Pillow', 'PIL'), ('py7zr', 'py7zr'), ('pandas', 'pandas'), ('numpy', 'numpy'))
        for package, module in packages:
            progress('加载依赖：'+package)
            try:
                importlib.import_module(module)
                report[package] = metadata.version(package)
                if package == 'py7zr' and not (1, 1, 4) <= tuple(map(int, report[package].split('.')[:3])) < (2, 0, 0):
                    errors.append('py7zr版本不满足requirements.txt，请运行maintenance/安装依赖.cmd。')
            except (ImportError, metadata.PackageNotFoundError, ValueError):
                errors.append(package + ' 简报依赖不可用，请运行“maintenance/安装依赖.cmd”。')
        report['sqlite'] = sqlite3.sqlite_version
        if not startup:
            progress('检查Tcl/Tk及可选桌面组件')
            try:
                tkinter = importlib.import_module('tkinter')
                report['tcl'] = tkinter.Tcl().call('info', 'patchlevel')
            except Exception as exc:
                message = '台账组件 Tcl/Tk 不可用：' + type(exc).__name__
                (errors if gui else report['warnings']).append(message)
        if os.name == 'nt' and not startup:
            for module in ('uiautomation', 'win32clipboard', 'win32gui', 'win32api', 'win32process', 'psutil', 'pyperclip'):
                if importlib.util.find_spec(module) is None:
                    (errors if wechat else report['warnings']).append('微信发送依赖不可用：' + module + '；请运行maintenance/安装依赖.cmd。')
        progress('检查工作目录写入权限')
        for folder in [home] + [home / name for name in ('runtime', 'logs') if (home / name).exists()]:
            try:
                with tempfile.TemporaryFile(dir=folder) as stream:
                    stream.write(b'FusionLive offline startup check\n')
                    stream.flush()
                    os.fsync(stream.fileno())
            except OSError:
                errors.append('目录不可写：' + str(folder))
        database = home / 'runtime' / 'ledger.sqlite3'
        if database.exists():
            progress('只读探测下载台账及表访问' if startup else '只读检查下载台账完整性')
            try:
                with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                    if startup:
                        db.execute('PRAGMA schema_version').fetchone()
                        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        for table in ('files', 'documents', 'folders'):
                            if table in tables:
                                db.execute('SELECT 1 FROM '+table+' LIMIT 1').fetchall()
                        report['database'] = '下载启动只读探测通过；未执行全库完整性扫描'
                    else:
                        result = db.execute('PRAGMA quick_check').fetchall()
                        if result != [('ok',)]:
                            errors.append('SQLite 完整性检查失败，请保留数据库并检查备份。')
                        else:
                            report['database'] = 'quick_check: ok（只读检查）'
            except sqlite3.Error as exc:
                errors.append('数据库只读检查失败：' + type(exc).__name__)
        else:
            report['database'] = '尚无数据库；首次任务会创建。若为迁移，请确认已复制 runtime。'
    report['ok'] = not errors
    return report


class CheckCancelled(RuntimeError):
    pass


def check_before_login(*, cancelled=lambda: False, log=print, timeout=120, home=HOME, _command=None):
    """Load native dependencies on a separate process's main thread, before login.

    A blocked DLL import must never trap the scheduler or prevent safe shutdown.
    Child output uses a file so a stuck reader thread or a full pipe cannot block exit.
    """
    if cancelled():
        raise CheckCancelled('启动检查已取消，本次未登录。')
    command = _command or [sys.executable, '-u', str(Path(home)/'main.py'), 'preflight', '--startup']
    env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    log('开始独立进程启动检查；检查期间不登录平台，最长等待'+str(timeout)+'秒。')
    with tempfile.TemporaryDirectory(prefix='fusion-preflight-') as directory:
        output = Path(directory)/'stdout.log'
        with output.open('wb') as writer:
            child = subprocess.Popen(command, cwd=home, env=env, stdin=subprocess.DEVNULL,
                                     stdout=writer, stderr=subprocess.STDOUT,
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            started = last_heartbeat = time.monotonic()
            pending, stage = b'', '启动检查进程'
            with output.open('rb') as reader:
                def drain(final=False):
                    nonlocal pending, stage
                    pending += reader.read()
                    lines = pending.split(b'\n')
                    pending = b'' if final else lines.pop()
                    for raw in lines:
                        line = raw.decode('utf-8', errors='replace').strip()
                        if line:
                            stage = line
                            log('启动检查：'+line)
                try:
                    while True:
                        drain()
                        if cancelled():
                            raise CheckCancelled('启动检查已取消，本次未登录。')
                        elapsed = time.monotonic()-started
                        code = child.poll()
                        if code is not None:
                            drain(final=True)
                            if code:
                                raise RuntimeError(f'独立启动检查失败，退出码{code}；本次未登录。详情见上述启动检查日志及runtime/startup_check.json。')
                            log('独立启动检查通过，准备启动同步任务。')
                            return
                        if elapsed >= timeout:
                            raise RuntimeError(f'独立启动检查超时（{timeout}秒），本次未登录；最后输出：{stage}')
                        if time.monotonic()-last_heartbeat >= 10:
                            log(f'启动检查进行中，已等待{elapsed:.0f}秒；最近输出：{stage}')
                            last_heartbeat = time.monotonic()
                        time.sleep(.1)
                finally:
                    if child.poll() is None:
                        # This process performs offline checks only; it holds no platform session.
                        # Windows venv python.exe can be a redirector. Stopping
                        # only it leaves the actual interpreter holding stdout.
                        descendants = []
                        if os.name == 'nt':
                            try:
                                import psutil
                                descendants = psutil.Process(child.pid).children(recursive=True)
                            except ImportError:
                                pass
                            except psutil.Error:
                                pass
                        for process in reversed(descendants):
                            try:
                                process.terminate()
                            except psutil.Error:
                                pass
                        child.terminate()
                        try:
                            child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=5)
                        if descendants:
                            _, alive = psutil.wait_procs(descendants, timeout=5)
                            for process in alive:
                                try:
                                    process.kill()
                                except psutil.Error:
                                    pass
                            psutil.wait_procs(alive, timeout=5)
                    drain(final=True)


def save_report(report, home=HOME):
    try:
        folder = Path(home) / 'runtime'
        folder.mkdir(exist_ok=True)
        target = folder / 'startup_check.json'
        temporary = target.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, target)
    except OSError:
        print('检查结果无法写入 runtime/startup_check.json，请查看上方输出。', flush=True)


def print_report(report):
    if report.get('check_mode') == 'download-startup':
        print('下载启动检查：全部程序校验、下载依赖及台账只读探测；完整数据库和解析/桌面诊断请运行默认preflight。', flush=True)
    print('FusionLive 发布版本：' + report['release'], flush=True)
    print('程序 SHA-256 / 语法检查：' + str(report['files_checked']) + ' 个文件；' + ('通过' if report['ok'] else '未通过'), flush=True)
    if 'python' in report:
        print('Python ' + report['python'] + '；requests ' + str(report.get('requests', '不可用')) + '；SQLite ' + report['sqlite'], flush=True)
    if 'database' in report:
        print('数据库：' + report['database'], flush=True)
    for message in report['warnings']:
        print('提示：' + message, flush=True)
    for message in report['errors']:
        print('错误：' + message, flush=True)
    print('离线检查，不连接或登录平台。', flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线校验程序完整度、发布版本、SHA-256、运行环境和数据库')
    parser.add_argument('--gui', action='store_true', help='同时要求台账 Tcl/Tk 组件可用')
    parser.add_argument('--wechat', action='store_true', help='同时要求微信发送端依赖可用，不启动微信或发送消息')
    parser.add_argument('--startup', action='store_true', help='下载启动检查：校验全部程序，仅加载下载依赖并探测台账；完整诊断仍用默认入口')
    args = parser.parse_args(argv)
    if args.startup and (args.gui or args.wechat):
        parser.error('--startup不能与完整桌面检查组合')
    report = run_checks(gui=args.gui, wechat=args.wechat, startup=args.startup,
                        progress=lambda message: print('检查阶段：'+message, flush=True))
    print_report(report)
    save_report(report)
    return 0 if report['ok'] else 4


if __name__ == '__main__':
    sys.exit(main())
