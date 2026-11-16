"""Central regression entry: isolated component processes and durable reports."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_layout import PROJECT_ROOT, ADMIN_ROOT

HOME = Path(__file__).resolve().parent


def selection(args, config):
    selected = {}
    def add(component, patterns):
        base = PROJECT_ROOT / config['components'][component]['directory']
        for pattern in patterns:
            files = sorted(base.glob(pattern))
            if not files:
                raise ValueError('测试文件不存在：' + str(base / pattern))
            selected.setdefault(component, set()).update(str(p.relative_to(base)) for p in files)
    if args.full or (args.component and not args.area and not args.changed):
        for component in args.component or config['components']:
            add(component, config['components'][component]['tests'])
    elif args.changed:
        for name in args.changed:
            path = (PROJECT_ROOT / name).resolve()
            if not path.is_relative_to(PROJECT_ROOT):
                raise ValueError('改动路径超出项目')
            for component, data in config['components'].items():
                base = PROJECT_ROOT / data['directory']
                if path.is_relative_to(base):
                    add(component, [str(path.relative_to(base))] if path.name.startswith('test_')
                        else data['tests'])
                    break
    else:
        for area in args.area or ['quick']:
            if area not in config['areas']:
                raise ValueError('未知测试范围：' + area)
            for component, patterns in config['areas'][area].items():
                add(component, patterns)
    return {key: sorted(value) for key, value in selected.items() if not args.component or key in args.component}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument('--full', action='store_true')
    scope.add_argument('--area', nargs='+')
    scope.add_argument('--changed', nargs='+')
    parser.add_argument('--component', nargs='+')
    parser.add_argument('--plan', action='store_true')
    parser.add_argument('--ui', action='store_true')
    parser.add_argument('--jobs', type=int, default=2)
    parser.add_argument('--timeout', type=int, default=900)
    args = parser.parse_args()
    config = json.loads((HOME / 'test_suites.json').read_text(encoding='utf-8'))
    plan = selection(args, config)
    if args.plan:
        print(json.dumps({'scope': 'full' if args.full else 'scoped', 'components': plan,
                          'ui': config['ui'] if args.ui else []}, ensure_ascii=False, indent=2))
        return 0
    if not plan:
        parser.error('所选范围没有测试')
    report = ADMIN_ROOT / 'runtime/test-reports' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
    report.mkdir(parents=True)
    started = time.monotonic()
    environment = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
    def run(component):
        data = config['components'][component]
        env = dict(environment)
        if data.get('pythonpath'):
            env['PYTHONPATH'] = str(PROJECT_ROOT / data['directory'] / data['pythonpath'])
        command = [sys.executable, '-X', 'utf8', '-m', 'pytest', '-p', 'no:cacheprovider', '-q',
                   '--durations=10', '--junitxml=' + str(report / (component + '.xml')), *plan[component]]
        before = time.monotonic()
        print('START ' + component, flush=True)
        with (report / (component + '.log')).open('w', encoding='utf-8') as output:
            try:
                process = subprocess.run(command, cwd=PROJECT_ROOT / data['directory'], env=env,
                                         stdout=output, stderr=subprocess.STDOUT, timeout=args.timeout)
                code = process.returncode
            except subprocess.TimeoutExpired:
                output.write('\nTEST TIMEOUT\n')
                code = 124
        print(('PASS ' if code == 0 else 'FAIL ') + component, flush=True)
        return dict(component=component, code=code, seconds=round(time.monotonic()-before, 2), command=command)
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        for future in as_completed([pool.submit(run, component) for component in plan]):
            results.append(future.result())
    if args.ui:
        web = ADMIN_ROOT / 'web'
        commands = [[shutil.which('npm.cmd') or shutil.which('npm'), 'run', 'build']]
        commands += [[shutil.which('node') or 'node', name] for name in config['ui']]
        with (report / 'ui.log').open('w', encoding='utf-8') as output:
            for command in commands:
                process = subprocess.run(command, cwd=web, env=environment, stdout=output,
                                         stderr=subprocess.STDOUT, timeout=args.timeout)
                if process.returncode:
                    results.append(dict(component='ui', code=process.returncode, command=command))
                    break
            else:
                results.append(dict(component='ui', code=0))
    summary = dict(scope='full' if args.full else 'scoped', plan=plan, results=results,
                   seconds=round(time.monotonic()-started, 2))
    (report / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print('Report: ' + str(report), flush=True)
    return int(any(item['code'] for item in results))


if __name__ == '__main__':
    raise SystemExit(main())
