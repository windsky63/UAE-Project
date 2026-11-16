"""Install the shared environment from management and all discovered tools."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_layout import PROJECT_ROOT, tool_directories


def isolated_python(python, *args):
    return [str(python), '-X', 'utf8', '-I', *args]


def installation_plan(root=PROJECT_ROOT):
    root = Path(root).resolve()
    projects = [root / '管理中心', *tool_directories(root)]
    manager_requirements = root / '管理中心/tools/requirements.txt'
    requirements = ([manager_requirements] if manager_requirements.is_file() else [])
    requirements += [p / 'requirements.txt' for p in projects if (p / 'requirements.txt').is_file()]
    editable = [p for p in projects if (p / 'pyproject.toml').is_file() and not (p / 'requirements.txt').exists()]
    node = []
    for project in projects:
        for folder in (project, project / 'web'):
            if (folder / 'package.json').is_file():
                node.append(folder)
    return {'tools': [p.name for p in projects[1:]], 'requirements': requirements, 'editable': editable, 'node': node}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--test-dependencies', action='store_true')
    args = parser.parse_args(argv)
    plan = installation_plan()
    if args.plan:
        print(json.dumps({k: [str(p) for p in v] for k, v in plan.items()}, ensure_ascii=False, indent=2))
        return 0
    if sys.version_info < (3, 12):
        raise RuntimeError('请安装 Python 3.12 或更新版本。')
    python = PROJECT_ROOT / '.venv-analysis/Scripts/python.exe'
    if not python.is_file():
        subprocess.run([sys.executable, '-m', 'venv', str(python.parents[1])], check=True)
    command = isolated_python(python, '-m', 'pip', 'install')
    for path in plan['requirements']:
        command += ['-r', str(path)]
    if args.test_dependencies:
        command += ['-r', str(PROJECT_ROOT / '管理中心/tools/requirements-test.txt')]
    if plan['requirements'] or args.test_dependencies:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    for path in plan['editable']:
        subprocess.run(isolated_python(python, '-m', 'pip', 'install', '-e', str(path)), check=True)
    for path in plan['node']:
        npm = shutil.which('npm.cmd') or shutil.which('npm')
        if not npm:
            raise RuntimeError('检测到前端依赖，请安装 Node.js（包含 npm）后重新运行安装依赖。')
        subprocess.run([npm, 'ci' if (path / 'package-lock.json').exists() else 'install'], cwd=path, check=True)
        if 'build' in json.loads((path / 'package.json').read_text(encoding='utf-8')).get('scripts', {}):
            subprocess.run([npm, 'run', 'build'], cwd=path, check=True)
    print('管理中心及已检测工具的依赖安装完成。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
