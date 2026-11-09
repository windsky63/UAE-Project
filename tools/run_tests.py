"""Scoped regression entry point. No arguments runs quick checks, never full suites."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import fnmatch
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CONFIG = Path(__file__).with_name('test_suites.json')


def load_config():
    return json.loads(CONFIG.read_text(encoding='utf-8'))


def normalize_changed(root, names):
    result = []
    for name in names:
        path = (root / name).resolve()
        if not path.is_relative_to(root.resolve()):
            raise ValueError('Changed path is outside the project: ' + name)
        result.append(path.relative_to(root.resolve()).as_posix())
    return result


def expand_targets(base, patterns):
    selected = set()
    for pattern in patterns:
        filename, separator, node = pattern.partition('::')
        matches = sorted(base.glob(filename)) if any(c in filename for c in '*?[') else [base / filename]
        if not matches or any(not path.is_file() for path in matches):
            raise ValueError('Test target is missing: ' + str(base / filename))
        for path in matches:
            relative = path.relative_to(base).as_posix()
            selected.add(relative + ('::' + node if separator else ''))
    # A whole-file selection already includes any selected nodes from that file.
    return sorted(target for target in selected
                  if '::' not in target or target.split('::', 1)[0] not in selected)


def select_plan(config, *, root=ROOT, areas=None, changed=None, full=False, components=None):
    definitions, suites = config['components'], config['areas']
    requested, notes = {}, []
    chosen = list(areas or ([] if full or changed else ['quick']))
    def add(component, targets):
        requested.setdefault(component, []).extend(targets)
    if full:
        for key, value in definitions.items():
            add(key, value['full'])
    if changed:
        for name in normalize_changed(root, changed):
            owner = next((key for key, value in definitions.items()
                          if value['directory'] != '.' and name.startswith(value['directory'] + '/')), None)
            filename = name.rsplit('/', 1)[-1]
            if filename.startswith('test_') and filename.endswith('.py'):
                key = owner or 'harness'
                relative = (root / name).relative_to(root / definitions[key]['directory']).as_posix()
                if (root / name).is_file():
                    add(key, [relative])
                else:
                    add(key, definitions[key]['full'])
                    notes.append(f'{name}: deleted test; run full {key} coverage')
                continue
            matched = [key for key, value in suites.items()
                       if any(fnmatch.fnmatchcase(name, pattern) for pattern in value.get('sources', []))]
            if matched:
                chosen.extend(matched)
                continue
            if Path(name).suffix.lower() in {'.md', '.png', '.jpg'} or ('/docs/' in name and filename.endswith('.txt')):
                notes.append(name + ': documentation/artifact; no automatic code tests')
            elif owner:
                add(owner, definitions[owner]['full'])
                notes.append(f'{name}: no scoped mapping; run full {owner} coverage')
            else:
                for key, value in definitions.items():
                    add(key, value['full'])
                notes.append(name + ': shared/unknown code; use full coverage')
    chosen = sorted(set(chosen))
    for area in chosen:
        for key, targets in suites[area]['tests'].items():
            add(key, targets)
    allowed = set(components or definitions)
    plan = {key: expand_targets(root / definitions[key]['directory'], targets)
            for key, targets in requested.items() if key in allowed}
    ui_areas = list(suites) if full else chosen
    ui = sorted({script for area in ui_areas for script in suites[area].get('ui', [])})
    return dict(scope='full' if full else 'scoped', areas=chosen, components=plan, ui=ui, notes=notes)


def junit_details(path):
    if not path.is_file():
        return {}
    try:
        root = ET.parse(path).getroot()
        cases = list(root.iter('testcase'))
        slowest = sorted(cases, key=lambda case: float(case.get('time', 0)), reverse=True)[:10]
        return dict(junit_cases=len(cases),
                    slowest=[dict(name=case.get('classname', '') + '::' + case.get('name', ''),
                                  seconds=float(case.get('time', 0))) for case in slowest])
    except (ET.ParseError, ValueError, OSError) as exc:
        return dict(report_error=str(exc))


def execute_component(key, targets, definition, python, output, timeout, *, root=ROOT):
    base = root / definition['directory']
    log, xml = output / (key + '.log'), output / (key + '.xml')
    env = {**os.environ, 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8', 'PYTHONDONTWRITEBYTECODE':'1'}
    if definition.get('pythonpath'):
        env['PYTHONPATH'] = str(base / definition['pythonpath'])
    command = [str(python), '-m', 'pytest', '-q', '-p', 'no:cacheprovider',
               '--durations=10', '--junitxml=' + str(xml), *targets]
    started = time.monotonic()
    try:
        with log.open('w', encoding='utf-8') as stream:
            result = subprocess.run(command, cwd=base, env=env, stdout=stream,
                                    stderr=subprocess.STDOUT, timeout=timeout, check=False)
        code = result.returncode
        error = ''
    except (subprocess.TimeoutExpired, OSError) as exc:
        code, error = 1, f'{type(exc).__name__}: {exc}'
    text = log.read_text(encoding='utf-8', errors='replace') if log.exists() else ''
    summaries = [line for line in text.splitlines() if ' passed' in line or ' failed' in line or 'no tests ran' in line]
    return dict(component=key, exit_code=code, seconds=round(time.monotonic()-started, 3),
                targets=targets, command=command, log=str(log), error=error,
                summary=summaries[-1] if summaries else '', **junit_details(xml))


def run_ui(scripts, output, timeout, *, root=ROOT):
    web = root / load_config()['components']['manager']['directory'] / 'web'
    dist = web / 'dist'
    if dist.resolve() != dist.absolute() or dist.is_symlink():
        raise ValueError('UI build output must stay in the project web/dist directory')
    commands = [['npm.cmd' if os.name == 'nt' else 'npm', 'run', 'build']]
    commands.extend(['node', script] for script in scripts)
    started = time.monotonic()
    code, error = 0, ''
    log = output / 'ui.log'
    with log.open('w', encoding='utf-8') as stream:
        for command in commands:
            try:
                result = subprocess.run(command, cwd=web, stdout=stream, stderr=subprocess.STDOUT,
                                        timeout=timeout, check=False)
                code = result.returncode
            except (subprocess.TimeoutExpired, OSError) as exc:
                code, error = 1, str(exc)
            if code:
                break
    return dict(component='ui',exit_code=code,seconds=round(time.monotonic()-started,3),
                targets=scripts,log=str(log),error=error,summary='UI checks passed' if not code else 'UI checks failed')


def main(argv=None):
    config = load_config()
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--area', nargs='+', choices=sorted(config['areas']))
    selection.add_argument('--changed', nargs='+', help='Explicit current-change paths, relative to project root')
    selection.add_argument('--full', action='store_true', help='All backend suites, including real subprocess integration')
    parser.add_argument('--component', nargs='+', choices=sorted(config['components']))
    parser.add_argument('--list', action='store_true', help='List functional areas without running tests')
    parser.add_argument('--plan', action='store_true', help='Show exact selected files/nodes without running tests')
    parser.add_argument('--ui', action='store_true', help='Also build once and run selected isolated UI checks')
    parser.add_argument('--jobs', type=int, default=2, help='Independent component processes; default 2')
    parser.add_argument('--timeout', type=int, default=300, help='Maximum seconds per component/UI command')
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    args = parser.parse_args(argv)
    if args.jobs < 1 or args.timeout < 1:
        parser.error('--jobs and --timeout must be positive')
    if args.list:
        for key, area in config['areas'].items():
            print(f'{key:12} {area["description"]}')
        return 0
    try:
        plan = select_plan(config, areas=args.area, changed=args.changed, full=args.full, components=args.component)
    except (ValueError, KeyError) as exc:
        parser.error(str(exc))
    if args.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0
    if args.ui and not plan['ui']:
        parser.error('Selected area has no isolated UI checks; use --area overview or catalogue')
    if not plan['components']:
        print('No code tests selected. Use --area or --full for explicit coverage.')
        return 0
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S')
    output = ROOT / 'runtime/test-reports' / (stamp + '-' + uuid.uuid4().hex[:6])
    output.mkdir(parents=True)
    print(f'Coverage: {plan["scope"]}; areas={",".join(plan["areas"])}; jobs={args.jobs}', flush=True)
    for note in plan['notes']:
        print('NOTE: ' + note, flush=True)
    started, results = time.monotonic(), []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        pending = {executor.submit(execute_component, key, targets, config['components'][key],
                                   args.python.resolve(), output, args.timeout): key
                   for key, targets in plan['components'].items()}
        for key, targets in plan['components'].items():
            print(f'START {key}: {len(targets)} test files/nodes', flush=True)
        for future in as_completed(pending):
            result = future.result()
            results.append(result)
            status = 'PASS' if result['exit_code'] == 0 else 'FAIL'
            print(f'{status} {result["component"]}: {result["seconds"]:.2f}s {result["summary"]}', flush=True)
            if result['exit_code']:
                print(result['error'], flush=True)
                print(Path(result['log']).read_text(encoding='utf-8',errors='replace')[-6000:], flush=True)
    if args.ui:
        result = run_ui(plan['ui'], output, args.timeout)
        results.append(result)
        print(f'{"PASS" if not result["exit_code"] else "FAIL"} ui: {result["seconds"]:.2f}s; {result["log"]}', flush=True)
    report = dict(plan=plan,seconds=round(time.monotonic()-started,3),results=results)
    report_path = output / 'summary.json'
    report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'TOTAL {report["seconds"]:.2f}s; report={report_path}', flush=True)
    if plan['scope'] != 'full':
        print('Scoped checks only; full integration coverage was not run.', flush=True)
    return int(any(result['exit_code'] != 0 for result in results))


if __name__ == '__main__':
    raise SystemExit(main())
