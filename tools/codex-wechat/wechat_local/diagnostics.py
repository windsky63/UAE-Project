"""Read-only preflight checks. Never instantiate a database reader or UI sender."""
import importlib.util
from importlib import metadata
import json
import os
from pathlib import Path
import shutil
import sys


DEPENDENCIES = {
    'pydantic': 'pydantic', 'cryptography': 'cryptography',
    'pyperclip': 'pyperclip', 'psutil': 'psutil',
    'zstandard': 'zstandard', 'colorama': 'colorama',
    'uiautomation': 'uiautomation', 'pywin32': 'win32api',
}


def dependency_status():
    result = {}
    for package, module in DEPENDENCIES.items():
        try:
            available = importlib.util.find_spec(module) is not None
            version = metadata.version(package) if available else None
        except (ImportError, ValueError, metadata.PackageNotFoundError):
            available, version = False, None
        result[package] = {'available': available, 'version': version}
    return result


def check_environment(config):
    dependencies = dependency_status()
    report = {
        'config_valid': True, 'chats': len(config.chats), 'idle_mode': not bool(config.chats),
        'codex_enabled': config.codex_enabled, 'allow_send': config.allow_send,
        'python': {'executable': sys.executable, 'version': sys.version.split()[0],
                   'virtual_environment': sys.prefix != sys.base_prefix},
        'dependencies': dependencies,
        'errors': [], 'warnings': [],
        'unverified': [
            '微信数据库能否解密、账号身份和聊天名单是否正确：需实际采集验证。',
            'Codex 登录及模型可用性：本检查不调用 Codex 或模型。',
            '微信 UI 控件、DLL 配置与真实发送：本检查不操作微信。',
        ],
        'requires_server': False, 'requires_embedding_model': False,
    }
    errors, warnings = report['errors'], report['warnings']
    capture_issues = errors if config.chats else warnings
    if sys.version_info < (3, 11):
        errors.append('需要 Python 3.11 或以上版本。')
    if os.name != 'nt':
        capture_issues.append('微信采集和发送仅支持 Windows。')
    missing = [name for name, info in dependencies.items() if not info['available']]
    if missing:
        capture_issues.append('缺少运行依赖：' + ', '.join(missing) + '；请运行安装单机版.cmd。')
    if not report['python']['virtual_environment']:
        warnings.append('当前未使用虚拟环境；建议运行安装单机版.cmd 后使用 .venv\\Scripts\\python.exe。')
    if not config.chats:
        warnings.append('未配置监听会话，可启动待配置模式；保存会话后重启监听以生效。')
    if any(not chat.username for chat in config.chats):
        warnings.append('部分聊天未填写 username，存在同名聊天无法唯一识别的风险。')
    report['sync_sources'] = []
    for source in config.sync_sources:
        root = Path(source.root)
        ready = (root / 'runtime/ledger.sqlite3').is_file() and (root / 'downloads').is_dir()
        report['sync_sources'].append(dict(id=source.id, projects=source.projects, local_files_ready=ready,
            granted_chats=[chat.id for chat in config.chats if source.id in chat.sync_sources]))
        if not ready:
            warnings.append('同步文件源 ' + source.id + ' 缺少本地台账或 downloads 目录；当前只能报告数据不可用。')

    executable = shutil.which(config.codex_command[0])
    report['codex_found'] = executable is not None
    report['codex_command_ready'] = executable is not None
    if executable is None:
        (errors if config.codex_enabled and config.chats else warnings).append('未找到 Codex CLI；请安装或设置 codex_command 的完整路径。')
    elif os.name == 'nt' and executable.lower().endswith(('.cmd', '.bat')):
        entry = Path(executable).parent / 'node_modules/@openai/codex/bin/codex.js'
        if not entry.is_file() or shutil.which('node') is None:
            report['codex_command_ready'] = False
            (errors if config.codex_enabled and config.chats else warnings).append('Codex npm 启动器缺少 Node 或 codex.js；请修复安装或配置原生 codex.exe。')
    report['windows_driver_available'] = all(dependencies[name]['available'] for name in ('uiautomation', 'pywin32', 'pyperclip', 'psutil'))

    # Only discover directories and inspect encrypted source file permissions.
    # Constructing LocalDatabaseReader would extract keys and write caches.
    report['database'] = {'directory_readable': False, 'files_readable': False}
    if not config.chats:
        report['database']['skipped'] = '待配置模式不读取微信数据库。'
    elif dependencies['cryptography']['available']:
        try:
            from .wechat_capture import load_backend, resolve_database_account
            root, account = resolve_database_account(config, load_backend())
            storage = Path(root) / account / 'db_storage'
            files = list(storage.rglob('*.db'))
            readable = os.access(storage, os.R_OK) and bool(files) and all(os.access(path, os.R_OK) for path in files)
            report['database'] = {'root': str(root), 'account': account,
                                  'directory_readable': os.access(storage, os.R_OK),
                                  'files_readable': readable, 'encrypted_db_files': len(files)}
            if not readable:
                errors.append('微信 db_storage 中没有可读的数据库文件，请检查存储位置及权限。')
        except Exception as exc:
            # These helpers only inspect directories; do not include key extraction diagnostics.
            errors.append('微信数据库目录检查失败：' + str(exc))
    else:
        warnings.append('缺少 cryptography，暂未检查数据库目录。')

    for key in ('database_cache', 'workspaces', 'state_path'):
        path = Path(getattr(config, key))
        if key == 'state_path':
            if path.exists() and (not path.is_file() or not os.access(path, os.W_OK)):
                errors.append('state_path 必须是可写的数据库文件路径。')
            path = path.parent
        elif path.exists() and not path.is_dir():
            errors.append(key + ' 必须指向目录。')
        while not path.exists() and path.parent != path:
            path = path.parent
        if not path.is_dir() or not os.access(path, os.W_OK):
            errors.append(key + ' 的父目录不可写。')

    profile = Path(config.uia_profile)
    try:
        data = json.loads(profile.read_text(encoding='utf-8'))
        digest = data['dll_sha256']
        if (not isinstance(digest, str) or len(digest) != 64
                or any(c not in '0123456789abcdefABCDEF' for c in digest)
                or type(data['gate_rva']) is not int or data['gate_rva'] < 0):
            raise ValueError('Invalid UIA profile')
        report['uia_profile_valid'] = True
    except (OSError, ValueError, KeyError, TypeError):
        report['uia_profile_valid'] = False
        warnings.append('UIA 配置缺失或无效；若微信未直接提供 UIA 控件，发送将被阻止。')
    report['ready_for_listener'] = not errors
    report['ready_for_capture_attempt'] = bool(config.chats) and not errors
    report['live_validation_complete'] = False
    return report
