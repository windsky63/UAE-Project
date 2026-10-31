"""Shared, stdlib-only project evidence setup for web and WeChat conversations."""
from pathlib import Path
import shutil
import sys

try:
    from .sync_files import prepare_catalog
except ImportError:  # Loaded directly by the management center without WeChat dependencies.
    import importlib.util
    spec = importlib.util.spec_from_file_location('project_sync_files', Path(__file__).with_name('sync_files.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prepare_catalog = module.prepare_catalog

ROOT = Path(__file__).resolve().parents[1]


def prepare_project_context(config, chat, workspace, access_dirs):
    workspace = Path(workspace).resolve()
    skill = workspace / '.agents/skills/wechat-chat'
    skill.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ROOT / '.agents/skills/wechat-chat/project-qa.md', skill / 'project-qa.md')
    shutil.copyfile(ROOT / 'wechat_local/sync_files.py', skill / 'sync_files.py')
    catalog = prepare_catalog(config, chat, workspace, access_dirs=access_dirs)
    # Absolute workspace selection keeps queries correct for both frontends' different cwd values.
    def powershell_literal(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = (f'& {powershell_literal(sys.executable)} -X utf8 '
               f'{powershell_literal(skill / "sync_files.py")} summary '
               f'--workspace {powershell_literal(workspace)}')
    instructions = ('Shared project evidence instructions (web and WeChat):\n'
                    + (skill / 'project-qa.md').read_text(encoding='utf-8')
                    + '\nUse the following absolute PowerShell command for the current snapshot; '
                    'replace summary with search, read, members or analysis as needed. '
                    'Keep --workspace for all commands. Do not recursively scan downloads or logs '
                    'or start fresh parsing to answer questions about existing results.\n'
                    + command)
    return dict(catalog=catalog, instructions=instructions)
