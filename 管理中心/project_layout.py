"""The management centre owns the project layout and discovers tool folders."""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ADMIN_ROOT = PROJECT_ROOT / '管理中心'
TOOLS_ROOT = PROJECT_ROOT / 'tools'
RELEASES_ROOT = PROJECT_ROOT / 'releases'
BACKUPS_ROOT = PROJECT_ROOT / 'backups'


def tool_directories(root=PROJECT_ROOT):
    base = Path(root) / 'tools'
    if not base.is_dir():
        return []
    return sorted((p for p in base.iterdir() if p.is_dir() and not p.name.startswith('.')
                   and not p.is_symlink() and not p.is_junction()), key=lambda p: p.name.casefold())


def tool_path(name, root=PROJECT_ROOT):
    if not name or Path(name).name != name or '\\' in name or name in {'.', '..'}:
        raise ValueError('Invalid tool directory name')
    return Path(root) / 'tools' / name
