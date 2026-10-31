"""Load the shared stdlib-only Codex settings used by the WeChat tool."""
import importlib.util
from pathlib import Path

_path = Path(__file__).resolve().parent.parent / 'codex-wechat/wechat_local/codex_settings.py'
_spec = importlib.util.spec_from_file_location('project_codex_settings', _path)
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
transport_overrides = _module.transport_overrides
access_directories = _module.access_directories
permission_arguments = _module.permission_arguments

_context_spec = importlib.util.spec_from_file_location('project_context', _path.with_name('project_context.py'))
_context = importlib.util.module_from_spec(_context_spec)
_context_spec.loader.exec_module(_context)
prepare_project_context = _context.prepare_project_context

# Both adapters import the exact same transport and process pool implementation.
import sys
_root = str(_path.parents[1])
if _root not in sys.path:
    sys.path.insert(0, _root)
from wechat_local.shared_backend import SharedConnection, config_signature
