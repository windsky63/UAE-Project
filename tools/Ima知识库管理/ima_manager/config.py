"""Local settings; persistent credentials use Windows user-bound DPAPI."""
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path

from .api import ImaError

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / 'runtime'
DEFAULT_SOURCE = ROOT / '待同步'


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def load_settings():
    sync = ROOT.parent / 'FusionLive同步工具'
    ledger = sync / 'exports' / '文件台账.xlsx'
    if not ledger.is_file() and (ROOT.parent / '文件台账.xlsx').is_file():
        ledger = ROOT.parent / '文件台账.xlsx'
    defaults = {'source': str(DEFAULT_SOURCE), 'kb': '', 'folder': '', 'recursive': True,
                'collect_enabled': True, 'auto_sync_enabled': True, 'ledger_path': str(ledger),
                'downloads_path': str(sync / 'downloads'), 'database_path': str(sync / 'runtime/ledger.sqlite3'),
                'interval_seconds': 60, 'sync_time': '13:00'}
    path = RUNTIME / 'settings.json'
    if not path.exists():
        return defaults
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        result = dict(defaults, **{k: v for k, v in value.items() if k in defaults})
        if not value.get('database_path'):
            result['database_path'] = str(Path(result['downloads_path']).parent / 'runtime/ledger.sqlite3')
        return result
    except (ValueError, OSError):
        raise ImaError('本机设置文件损坏，请检查 runtime/settings.json。') from None


def save_settings(value):
    atomic_json(RUNTIME / 'settings.json',
                {k: value[k] for k in ('source', 'kb', 'folder', 'recursive', 'collect_enabled', 'auto_sync_enabled',
                                     'ledger_path', 'database_path', 'downloads_path', 'interval_seconds', 'sync_time') if k in value})


def deployment_credentials_enabled():
    """Local authorization for private server packages; contains no secrets."""
    path = RUNTIME / 'deployment.json'
    if not path.is_file():
        return False
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (ValueError, OSError):
        raise ImaError('ima 部署配置无法读取。') from None
    if not isinstance(value, dict) or type(value.get('include_credentials', False)) is not bool:
        raise ImaError('ima 部署配置格式错误。')
    return value.get('include_credentials', False)


class Blob(ctypes.Structure):
    _fields_ = [('cbData', wintypes.DWORD), ('pbData', ctypes.POINTER(ctypes.c_ubyte))]


def _protect(raw, decrypt=False):
    if os.name != 'nt':
        raise ImaError('此系统请使用环境变量配置凭证；加密保存目前仅支持 Windows。')
    buffer = ctypes.create_string_buffer(raw)
    source = Blob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise ImaError('凭证加密或解密失败，请使用原 Windows 用户重新配置。')
    try:
        return ctypes.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(ctypes.cast(output.pbData, ctypes.c_void_p))


def save_credentials(client_id, api_key):
    values = [client_id.strip(), api_key.strip()]
    if not all(values) or any('\n' in v or '\r' in v for v in values):
        raise ImaError('Client ID 和 API Key 不可为空或包含换行。')
    encrypted = _protect(json.dumps(values).encode())
    RUNTIME.mkdir(parents=True, exist_ok=True)
    path = RUNTIME / 'credentials.dat'
    temporary = path.with_suffix('.tmp')
    temporary.write_bytes(encrypted)
    temporary.replace(path)


def load_credentials():
    cid = os.environ.get('IMA_OPENAPI_CLIENTID') or os.environ.get('IMA_CLIENT_ID')
    key = os.environ.get('IMA_OPENAPI_APIKEY') or os.environ.get('IMA_API_KEY')
    if cid or key:
        if not cid or not key:
            raise ImaError('环境变量中的 Client ID 和 API Key 必须同时设置。')
        return cid.strip(), key.strip()
    path = RUNTIME / 'credentials.dat'
    bootstrap = RUNTIME.parent / 'credentials.bootstrap.json'
    if bootstrap.is_file() and path.exists():
        try:
            replacement = json.loads(bootstrap.read_text(encoding='utf-8'))
            if isinstance(replacement, dict) and replacement.get('replace_existing') is True:
                cid, key = replacement['client_id'], replacement['api_key']
                if not isinstance(cid, str) or not isinstance(key, str):
                    raise ValueError()
                save_credentials(cid, key)
                bootstrap.unlink()
                return cid.strip(), key.strip()
        except (ValueError, KeyError, TypeError, OSError):
            raise ImaError('部署凭证迁移失败；请检查迁移文件或在管理中心重新配置。') from None
    if path.exists():
        try:
            cid, key = json.loads(_protect(path.read_bytes(), decrypt=True))
            if not cid or not key:
                raise ValueError()
            bootstrap.unlink(missing_ok=True)
            return cid, key
        except (ValueError, OSError):
            raise ImaError('凭证文件无法读取，请重新配置。') from None
    if bootstrap.is_file():
        try:
            value = json.loads(bootstrap.read_text(encoding='utf-8'))
            cid, key = value['client_id'], value['api_key']
            if not isinstance(cid, str) or not isinstance(key, str):
                raise ValueError()
            save_credentials(cid, key)
            bootstrap.unlink()
            return cid.strip(), key.strip()
        except (ValueError, KeyError, TypeError, OSError):
            raise ImaError('部署凭证迁移失败，请检查迁移文件和目录权限，或在管理中心重新配置。') from None
    raise ImaError('请打开 https://ima.qq.com/agent-interface 获取凭证，再在工具中加密保存。')
