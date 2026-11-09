"""Local configuration/status and explicit, isolated ima operations."""
import hashlib
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import subprocess

FIELDS = {'source', 'kb', 'folder', 'recursive', 'collect_enabled', 'auto_sync_enabled', 'ledger_path',
          'database_path', 'downloads_path', 'interval_seconds', 'sync_time'}
OPERATIONS = {'credentials', 'scan', 'history', 'move-local', 'bases', 'list', 'search', 'destinations',
              'resolve', 'create-folder', 'rename', 'move'}


def config(manager):
    base = manager.folder('ima')
    sync = manager.folder('sync')
    path = base / 'runtime/settings.json'
    defaults = {'source': str(base / '待同步'), 'kb': '', 'folder': '', 'recursive': True,
                'collect_enabled': True, 'auto_sync_enabled': True, 'ledger_path': str(sync / 'exports/文件台账.xlsx'),
                'downloads_path': str(sync / 'downloads'), 'database_path': str(sync / 'runtime/ledger.sqlite3'),
                'interval_seconds': 60, 'sync_time': '13:00'}
    if not Path(defaults['ledger_path']).is_file() and (manager.root / '文件台账.xlsx').is_file():
        defaults['ledger_path'] = str(manager.root / '文件台账.xlsx')
    if path.is_file():
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict):
            raise ValueError('ima 配置必须为对象')
        defaults.update({k: v for k, v in data.items() if k in FIELDS})
        if not data.get('database_path'):
            defaults['database_path'] = str(Path(defaults['downloads_path']).parent / 'runtime/ledger.sqlite3')
    return {'data': defaults, 'revision': hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else '',
            'exists': path.is_file(), 'credentials_configured': (base / 'runtime/credentials.dat').is_file() or
            (base / 'credentials.bootstrap.json').is_file() or
            bool((os.getenv('IMA_OPENAPI_CLIENTID') or os.getenv('IMA_CLIENT_ID')) and
                 (os.getenv('IMA_OPENAPI_APIKEY') or os.getenv('IMA_API_KEY')))}


def save_config(manager, data, revision):
    current = config(manager)
    if revision != current['revision']:
        raise ValueError('配置已被修改，请刷新后重试')
    if not isinstance(data, dict) or set(data) - FIELDS:
        raise ValueError('ima 配置包含不支持的字段')
    value = {**current['data'], **data}
    for key in ('source', 'database_path', 'downloads_path'):
        if not isinstance(value[key], str) or not value[key].strip():
            raise ValueError('待同步目录、下载数据库和下载目录不能为空')
        path = Path(value[key]).expanduser()
        value[key] = str((path if path.is_absolute() else manager.folder('ima') / path).resolve())
    for key in ('kb', 'folder'):
        if not isinstance(value[key], str):
            raise ValueError('知识库和文件夹标识必须为字符串')
    for key in ('recursive', 'collect_enabled', 'auto_sync_enabled'):
        if type(value[key]) is not bool:
            raise ValueError('ima 开关值必须为布尔值')
    if type(value['interval_seconds']) is not int or not 10 <= value['interval_seconds'] <= 86400:
        raise ValueError('同步间隔须为 10 至 86400 秒')
    import re
    if not isinstance(value['sync_time'], str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value['sync_time']):
        raise ValueError('每日同步时间须为 HH:MM（00:00 至 23:59，北京时间）')
    source, downloads = Path(value['source']), Path(value['downloads_path'])
    if source == downloads or source.is_relative_to(downloads) or downloads.is_relative_to(source):
        raise ValueError('待同步和下载原目录不能相互包含')
    path = manager.config_path('ima')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.manager.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
    if not value['auto_sync_enabled']:
        (path.parent / 'watch-stop.request').write_text('stop', encoding='ascii')
    return config(manager)


def snapshot(manager):
    result = config(manager)
    base = manager.folder('ima')
    for name, filename in [('plan', 'latest-plan.json'), ('collection', 'collection-report.json')]:
        path = base / 'runtime' / filename
        payload = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else None
        if payload:
            if name == 'plan':
                rows = payload.get('files', [])
                payload = {**{k: v for k, v in payload.items() if k not in ('collection', 'files')},
                           'files': rows[:20], 'total_files': len(rows),
                           'counts': dict(Counter(row.get('status', '') for row in rows))}
            else:
                payload = {k: v for k, v in payload.items() if k != 'files'}
        if payload is not None:
            payload['revision'] = f'{path.stat().st_mtime_ns}:{path.stat().st_size}'
            payload['updated_at'] = datetime.fromtimestamp(path.stat().st_mtime,
                timezone(timedelta(hours=8))).isoformat(timespec='seconds')
        result[name] = payload
    journal = Path(result['data']['source']) / '每日更新记录.txt'
    text = ''
    if journal.is_file():
        with journal.open('rb') as stream:
            stream.seek(max(0, journal.stat().st_size - 64 * 1024))
            raw = stream.read()
        text = raw.decode('utf-8', errors='replace')
        if journal.stat().st_size > len(raw):
            text = text.partition('\n')[2]
    result.update(journal=text, journal_path=str(journal), state=manager.live('ima'), tasks=manager.tasks('ima'))
    return result


def records(manager, kind, offset=0, limit=20, search='', status=''):
    """Page complete local records; never connect to ima or launch its CLI."""
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError('分页参数无效')
    if kind in ('tasks', 'history'):
        from manager import readonly
        if kind == 'tasks':
            manager.tasks('ima', 0)  # Reconcile owned process exit codes before querying.
            path, table, status_column = manager.tasks_path, 'tasks', 'state'
            where, values = ['tool=?'], ['ima']
            columns, ordering = ('label', 'log'), 'started DESC,rowid DESC'
        else:
            path = manager.folder('ima') / 'runtime/state.sqlite3'
            table, status_column = 'receipts', 'status'
            where, values = [], []
            columns, ordering = ('path', 'note', 'kb', 'folder'), 'updated DESC,rowid DESC'
        if not path.is_file():
            return dict(items=[], total=0, offset=0, limit=limit)
        if search.strip():
            where.append('(' + ' OR '.join(f'instr(lower({column}),lower(?))>0' for column in columns) + ')')
            values.extend([search.strip()] * len(columns))
        if status:
            where.append(f'{status_column}=?')
            values.append(status)
        clause = ' WHERE ' + ' AND '.join(where) if where else ''
        with closing(readonly(path)) as db:
            total = db.execute(f'SELECT count(*) FROM {table}' + clause, values).fetchone()[0]
            offset = min(offset, max(0, (total - 1) // limit * limit))
            # Explicit columns prevent account/credential fields from reaching the browser.
            selected = '*' if kind == 'tasks' else 'kb,folder,path,status,media_id,note,updated'
            items = [dict(row) for row in db.execute(
                f'SELECT {selected} FROM {table}' + clause + f' ORDER BY {ordering} LIMIT ? OFFSET ?',
                [*values, limit, offset])]
        return dict(items=items, total=total, offset=offset, limit=limit)
    if kind in ('plan', 'collection'):
        filename = 'latest-plan.json' if kind == 'plan' else 'collection-report.json'
        path = manager.folder('ima') / 'runtime' / filename
        data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
        rows = data.get('files', [])
    elif kind == 'journal':
        path = Path(config(manager)['data']['source']) / '每日更新记录.txt'
        rows = []
        if path.is_file():
            with path.open(encoding='utf-8') as stream:
                for line in stream:
                    parts = [part.strip() for part in line.split('|')]
                    if len(parts) < 4 or not parts[0][:4].isdigit():
                        continue
                    rows.append(dict(time=parts[0], operation=parts[1], path=parts[2],
                                     status=parts[3], reason=' | '.join(parts[4:])))
        rows.reverse()
    else:
        raise ValueError('不支持的 ima 记录类型')
    needle = search.strip().casefold()
    rows = [row for row in rows if (not status or row.get('status') == status) and
            (not needle or any(needle in str(value).casefold() for value in row.values()))]
    total = len(rows)
    offset = min(offset, max(0, (total - 1) // limit * limit))
    return dict(items=rows[offset:offset + limit], total=total, offset=offset, limit=limit)


def query(manager, operation, data):
    if operation not in OPERATIONS or not isinstance(data, dict):
        raise ValueError('不支持的 ima 操作')
    result = subprocess.run([manager.python('ima'), '-m', 'ima_manager', 'control'],
        cwd=manager.folder('ima'), input=json.dumps({'operation': operation, 'data': data}, ensure_ascii=False),
        text=True, encoding='utf-8', capture_output=True, timeout=120,
        env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    if result.returncode:
        # Credentials are never returned to the browser, including unexpected validation failures.
        if operation == 'credentials':
            raise ValueError('凭证保存失败，请检查输入并使用服务器当前 Windows 用户重试。')
        raise ValueError((result.stderr or 'ima 操作失败，请检查任务日志。')[-500:])
    return json.loads(result.stdout)
