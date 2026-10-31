"""Local adapters for the three project tools. No platform/NAS/UI connection on reads."""
from contextlib import closing, contextmanager
from datetime import datetime, timezone, timedelta
import hashlib
import codecs
import json
import os
from pathlib import Path, PureWindowsPath
import re
import sqlite3
import subprocess
import sys
import threading
import uuid
from codex_runtime import access_directories
from log_tools import log_files

HOME = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
TOOLS = {
    'sync': dict(name='FusionLive 同步工具', folder='FusionLive同步工具', icon='mdi-cloud-download-outline',
                 schedule='每天 02:00—10:30', description='平台资料下载、后台解析与管道简报'),
    'nas': dict(name='绿联云上传工具', folder='绿联云上传工具', icon='mdi-cloud-upload-outline',
                schedule='每天 12:00—次日 00:00', description='将已确认下载的文件同步到 NAS'),
    'wechat': dict(name='微信与 Codex', folder='codex-wechat', icon='mdi-wechat',
                   schedule='监听启动后持续运行', description='会话采集、文件检索与 Codex 回复'),
}
LABELS = {'start': '启动调度', 'stop': '安全停止', 'check': '本地检查',
          'generate-text': '生成推送文',
          'briefing-latest': '生成最新管道简报', 'briefing-send': '生成最新管道简报并发送微信',
          'briefing': '生成管道简报', 'analysis': '恢复后台解析', 'stop-analysis': '停止后台解析',
          'simulate': '模拟完整流程', 'push-text': '手动推送日报',
          'push-briefing': '手动推送管道简报', 'push-all': '手动推送日报及管道简报'}
SYNC_DEFAULT = dict(analysis_poll_seconds=5, analysis_idle_log_seconds=300,
                    analysis_idle_exit_seconds=0, analysis_auto_idle_exit_seconds=120,
                    enabled=True, recipients=[], prepare_time_beijing='10:40', send_time_beijing='11:00',
                    send_window_minutes=5, briefing_title='管道资料同步简报', publish_text=True,
                    publish_briefing=True, test_recipient='文件传输助手')
NAS_DEFAULT = dict(source_root='../FusionLive同步工具/downloads/HABSHANDBN',
                   download_ledger='../FusionLive同步工具/runtime/ledger.sqlite3',
                   target_root=r'\\nas.example.test\工程项目文件\阿联酋项目FusionLive平台资料', start_hour_beijing=12, stop_hour_beijing=0,
                   chunk_bytes=1048576, retry_attempts=2, preserve_previous_versions=True)


def now():
    return datetime.now(BEIJING).isoformat(timespec='seconds')


@contextmanager
def writing(path):
    with closing(sqlite3.connect(path)) as db, db:
        yield db


def read_json(path, default=None):
    return json.loads(Path(path).read_text(encoding='utf-8-sig')) if Path(path).is_file() else default


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=3)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def counts(path, table, column, where=''):
    if not Path(path).is_file():
        return None
    try:
        with closing(readonly(path)) as db:
            return {str(r[0]): r[1] for r in db.execute(f'SELECT {column},count(*) FROM {table} {where} GROUP BY {column}')}
    except sqlite3.Error:
        return None


def locked(path):
    if not Path(path).is_file():
        return False
    try:
        with Path(path).open('r+b') as stream:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(stream, fcntl.LOCK_UN)
        return False
    except OSError:
        return True


def clean_log(value):
    return re.sub(r'(?i)((?:password|api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*)\S+', r'\1[已隐藏]', value)


def tail(path, size=256 * 1024):
    with Path(path).open('rb') as stream:
        prefix=stream.read(1024)
        stream.seek(0, 2)
        start=max(0,stream.tell()-size)
        encoding=unicode_encoding(prefix)
        width=4 if encoding and '32' in encoding else 2 if encoding else 1
        stream.seek(start-start%width)
        data=stream.read(size+width)
        if width>1:data=data[:len(data)-len(data)%width]
        if start and not encoding:
            data=data.partition(b'\n')[2]  # Discard a potentially split first character/line.
        text=decode_text(data,encoding)
        return text.partition('\n')[2] if start and encoding else text


def unicode_encoding(data):
    for marker,name in ((codecs.BOM_UTF32_LE,'utf-32-le'),(codecs.BOM_UTF32_BE,'utf-32-be'),
                        (codecs.BOM_UTF16_LE,'utf-16-le'),(codecs.BOM_UTF16_BE,'utf-16-be')):
        if data.startswith(marker):return name
    # Some Windows writers omit the UTF-16 BOM. NUL placement distinguishes it.
    sample=data[:1000]
    if len(sample)>8:
        if b'\r\x00\n\x00' in sample:return 'utf-16-le'
        if b'\x00\r\x00\n' in sample:return 'utf-16-be'
        if sample.count(b'\n\x00')>=2 and b'\x00\n' not in sample:return 'utf-16-le'
        if sample.count(b'\x00\n')>=2 and b'\n\x00' not in sample:return 'utf-16-be'
        if sample[1::2].count(0)>len(sample[1::2])*.4:return 'utf-16-le'
        if sample[::2].count(0)>len(sample[::2])*.4:return 'utf-16-be'
    return None


def decode_text(data,encoding=None):
    encoding=encoding or unicode_encoding(data)
    if encoding:return data.decode(encoding,errors='replace').lstrip('\ufeff')
    for name in ('utf-8-sig','gb18030'):
        try:return codecs.getincrementaldecoder(name)(errors='strict').decode(data,final=False)
        except UnicodeDecodeError:pass
    return data.decode('utf-8-sig',errors='replace')


class Manager:
    def __init__(self, root=None, runtime=None):
        self.root = Path(root or HOME.parent).resolve()
        self.runtime = Path(runtime or HOME / 'runtime').resolve()
        self.runtime.mkdir(parents=True, exist_ok=True)
        self.mutex = threading.RLock()
        self.children = {}
        self.artifacts = {}
        self.tasks_path = self.runtime / 'tasks.sqlite3'
        with writing(self.tasks_path) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS tasks(
              id TEXT PRIMARY KEY,tool TEXT,action TEXT,label TEXT,state TEXT,pid INTEGER,
              exit_code INTEGER,started TEXT,ended TEXT,log TEXT)''')
            db.execute("UPDATE tasks SET state='detached' WHERE state='running'")

    def folder(self, tool):
        if tool not in TOOLS:
            raise ValueError('未知工具')
        return self.root / TOOLS[tool]['folder']

    def python(self, tool):
        candidate = self.root / '.venv-analysis/Scripts/python.exe'
        if not candidate.is_file():
            raise ValueError('未找到共享环境 .venv-analysis，请在项目根目录运行 install_python312.cmd 安装全部工具依赖')
        return str(candidate)

    def tasks(self, tool='', limit=30):
        with self.mutex, writing(self.tasks_path) as db:
            db.row_factory = sqlite3.Row
            for identity, process in list(self.children.items()):
                code = process.poll()
                if code is not None:
                    db.execute('UPDATE tasks SET state=?,exit_code=?,ended=? WHERE id=?',
                               ('succeeded' if code == 0 else 'failed', code, now(), identity))
                    if process.stdin:
                        process.stdin.close()
                    del self.children[identity]
            rows = db.execute('SELECT * FROM tasks ' + ('WHERE tool=? ' if tool else '')
                              + 'ORDER BY started DESC,rowid DESC LIMIT ?', ((tool, limit) if tool else (limit,))).fetchall()
            return [dict(r) for r in rows]

    def live(self, tool):
        tasks = self.tasks(tool, 100)
        own = any(t['state'] == 'running' and t['action'] == 'start' for t in tasks)
        folder = self.folder(tool)
        if tool == 'wechat':
            config = read_json(folder / 'wechat.codex.json', {})
            lock = folder / config.get('database_cache', 'data/wechat-db') / 'bridge.lock'
        else:
            lock = folder / ('runtime/scheduler.lock' if tool == 'sync' else 'runtime/uploader.lock')
        external = locked(lock)
        return 'running' if own else 'external' if external else 'stopped'

    def overview(self):
        sync = self.folder('sync')
        downloads = counts(sync / 'runtime/ledger.sqlite3', 'files', 'status', 'WHERE active=1')
        parsed = counts(sync / 'runtime/file_analysis/queue.sqlite3', 'jobs', 'state', 'WHERE active=1')
        uploads = counts(self.folder('nas') / 'runtime/upload.sqlite3', 'uploads', 'status')
        tools = [{**meta, 'id': key, 'state': self.live(key), 'installed': self.folder(key).is_dir(),
                  'actions': (['start', 'stop', 'check', 'analysis', 'stop-analysis', 'briefing', 'generate-text'] if key == 'sync'
                              else ['start', 'stop', 'check'] if key == 'nas' else ['start', 'stop', 'check', 'simulate'])}
                 for key, meta in TOOLS.items()]
        wechat = read_json(self.folder('wechat') / 'wechat.codex.json', {})
        return dict(time=now(), tools=tools, downloads=downloads, analysis=parsed, uploads=uploads,
                    chats=len(wechat.get('chats', [])), codex_enabled=wechat.get('codex_enabled', True),
                    allow_send=wechat.get('allow_send', True), tasks=self.tasks(limit=8),
                    local_downloads=(sync / 'downloads').is_dir())

    def command(self, tool, action):
        base, python = self.folder(tool), self.python(tool)
        if tool == 'sync':
            if action in ('briefing-latest', 'briefing-send'):
                return [python, '-u', str(HOME / 'briefing_runner.py'), '--base', str(base),
                        *(['--send'] if action == 'briefing-send' else [])], base
            if action in ('push-text', 'push-briefing', 'push-all'):
                from daily_publications import day_value
                return [python, '-u', str(HOME / 'publication_runner.py'), '--base', str(base),
                        '--date', day_value(), '--kind', action[5:]], base
            commands = {'start': ['scheduler'], 'stop': ['scheduler', '--stop'],
                        'generate-text': ['sync', 'push-report'],
                        'check': ['preflight', '--gui', '--wechat'],
                        'briefing': ['briefing'], 'analysis': ['analysis_worker', '--watch', '--resume'],
                        'stop-analysis': ['analysis_worker', '--stop']}
            if action not in commands:
                raise ValueError('不支持的同步操作')
            return [python, '-u', str(base / 'main.py'), *commands[action]], base
        if tool == 'nas':
            if action not in ('start', 'stop', 'check'):
                raise ValueError('不支持的上传操作')
            # The local check deliberately does not connect to the NAS.
            return [python, '-u', str(base / 'main.py'),
                    {'start': 'schedule', 'stop': 'stop', 'check': 'integrity'}[action]], base
        if action == 'simulate':
            return [python, '-u', str(base / 'scripts/simulate_sync_pipeline.py')], base
        if action not in ('start', 'stop', 'check'):
            raise ValueError('不支持的微信操作')
        return [python, '-u', '-m', 'wechat_local', '--config', str(base / 'wechat.codex.json'),
                *({'start': [], 'stop': ['--stop'], 'check': ['--check']}[action])], base

    def action(self, tool, action):
        command, cwd = self.command(tool, action)
        if not cwd.is_dir():
            raise ValueError('工具目录不存在')
        with self.mutex:
            active = self.tasks(tool, 100)
            if tool == 'sync' and (action.startswith('briefing') or action.startswith('push-')):
                if any(t['state'] == 'running' and t['action'] in ('briefing-latest', 'briefing-send') for t in active):
                    raise ValueError('Codex简报业务任务正在执行，请等待完成或先停止该对话任务')
            if tool == 'sync' and action.startswith('push-'):
                from daily_publications import day_value, prepare_manual
                prepare_manual(cwd, day_value(), action[5:], self.config('sync')['data'])
                if any(t['state'] == 'running' and t['action'].startswith('push-') for t in active):
                    raise ValueError('手动推送正在运行，请先等待当前任务结束')
            if any(t['state'] == 'running' and t['action'] == action for t in active):
                raise ValueError('同一操作正在运行，请先查看任务结果')
            if action == 'start' and self.live(tool) != 'stopped':
                raise ValueError('工具已在运行，无需重复启动')
            if action == 'analysis' and locked(self.folder('sync') / 'runtime/file_analysis/worker.lock'):
                raise ValueError('后台解析已在运行')
            identity = uuid.uuid4().hex
            if tool == 'sync' and action in ('briefing-latest', 'briefing-send'):
                if any(t['state'] == 'running' and (t['action'].startswith('briefing') or
                       t['action'].startswith('push-')) for t in active):
                    raise ValueError('已有简报生成或推送任务，请等待完成')
                command += ['--job-id', identity]
            log = self.runtime / (now()[:10] + '-' + tool + '-' + action + '-' + identity[:8] + '.log')
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
            with log.open('wb') as stream:
                process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=stream,
                    stderr=subprocess.STDOUT, env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.children[identity] = process
            with writing(self.tasks_path) as db:
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (identity, tool, action, LABELS[action], 'running', process.pid, None, now(), None, log.name))
            return dict(id=identity, state='running', label=LABELS[action])

    def bulk_action(self, action):
        if action not in ('start', 'stop'):
            raise ValueError('仅支持全部启动或全部安全停止')
        items = []
        with self.mutex:
            for tool in TOOLS:
                try:
                    if any(row['state'] == 'running' and row['action'] == action
                           for row in self.tasks(tool, 100)):
                        items.append(dict(tool=tool, state='skipped', detail='同一操作已在执行'))
                    elif action == 'start' and self.live(tool) != 'stopped':
                        items.append(dict(tool=tool, state='skipped', detail='工具已在运行'))
                    else:
                        task = self.action(tool, action)
                        items.append(dict(tool=tool, state='submitted', task=task))
                except Exception as exc:
                    items.append(dict(tool=tool, state='failed', detail=str(exc)[:500]))
        return dict(items=items)

    def clear_tasks(self, tool):
        self.folder(tool)  # Validate before touching records or files.
        result = dict(deleted=0, deleted_logs=0, skipped=0, errors=[])
        with self.mutex:
            self.tasks(tool)  # Reap finished child processes first.
            safe_logs = set(log_files(self.runtime))
            with writing(self.tasks_path) as db:
                db.row_factory = sqlite3.Row
                rows = db.execute('SELECT * FROM tasks WHERE tool=?', (tool,)).fetchall()
                for row in rows:
                    if row['state'] == 'running':
                        result['skipped'] += 1
                        continue
                    if row['state'] == 'detached':
                        # Forget the UI record only; a detached process may still use its log.
                        db.execute('DELETE FROM tasks WHERE id=?',(row['id'],))
                        result['deleted'] += 1
                        continue
                    name = row['log']
                    if name:
                        path = self.runtime / name
                        if Path(name).name != name or PureWindowsPath(name).name != name:
                            result['errors'].append(dict(id=row['id'], reason='日志路径无效'))
                            continue
                        try:
                            if path.exists() or path.is_symlink():
                                if path not in safe_logs or path.is_symlink() or path.is_junction() or path.resolve() != path.absolute():
                                    result['errors'].append(dict(id=row['id'], reason='日志路径受保护'))
                                    continue
                                path.unlink()
                                result['deleted_logs'] += 1
                        except OSError as exc:
                            result['errors'].append(dict(id=row['id'], reason=str(exc)))
                            continue
                    db.execute('DELETE FROM tasks WHERE id=?', (row['id'],))
                    result['deleted'] += 1
        return result

    def publications(self, day=''):
        from daily_publications import snapshot, safe_file
        result = snapshot(self.folder('sync'), day)
        root = self.folder('sync') / 'logs' / result['date']
        def register(raw):
            path = safe_file(raw, root)
            identity = 'artifact-' + hashlib.sha256(str(path).encode()).hexdigest()[:24]
            self.artifacts[identity] = dict(path=path, kind='reports')
            return dict(id=identity, name=path.name, url='/api/file/' + identity)
        for image in result['briefing']['images']:
            image.update(register(image['path']))
        workbook = result['briefing'].get('workbook')
        if workbook:
            try:
                result['briefing']['download'] = register(workbook)
            except (OSError, ValueError):
                pass
        return result

    def push_publications(self, day, kind):
        from daily_publications import day_value
        if day_value(day) != day_value():
            raise ValueError('仅支持当天推送，请刷新页面')
        if kind not in ('text', 'briefing', 'all'):
            raise ValueError('推送类型无效')
        return self.action('sync', 'push-' + kind)

    def config_path(self, tool):
        return self.folder(tool) / {'sync': 'runtime/wechat_push.json', 'nas': 'config.json', 'wechat': 'wechat.codex.json'}[tool]

    def config(self, tool):
        path = self.config_path(tool)
        data = read_json(path, {})
        if not isinstance(data, dict):
            raise ValueError('配置文件必须包含 JSON 对象，请检查配置格式')
        if tool == 'sync':
            data = {**SYNC_DEFAULT, **data}
        elif tool == 'nas':
            template = read_json(path.with_name('config.example.json'), {})
            if not isinstance(template, dict):
                raise ValueError('上传配置示例必须包含 JSON 对象')
            data = {**NAS_DEFAULT, **{k:v for k,v in template.items() if k in NAS_DEFAULT}, **data}
        elif tool == 'wechat':
            defaults = dict(chats=[], sync_sources=[], codex_command=['codex'], codex_model='',
                            codex_transport='auto', codex_auto_access_dirs=True, codex_access_dirs=[],
                            codex_enabled=True, allow_send=True, database_root='D:/xwechat_files', database_account='',
                            account_name='', codex_timeout_seconds=600, reply_max_age_seconds=900,
                            codex_idle_timeout_seconds=300, mention_reply_sender=True, query_progress_enabled=True,
                            chat_memory_enabled=True, reply_target_chars=600, recent_messages=6, codex_backend='app-server',
                            codex_session_rotate_percent=70, codex_session_prewarm=True, codex_session_prewarm_percent=55,
                            codex_context_window_tokens=0, codex_session_unknown_max_turns=20,
                            send_max_attempts=3, send_retry_seconds=5, file_send_cleanup_days=2)
            template = read_json(path.with_name('wechat.codex.example.json'), {})
            if isinstance(template, dict):
                defaults.update(template)
            data = {**defaults, **data}
            if not isinstance(data['chats'], list) or not isinstance(data['sync_sources'], list):
                raise ValueError('微信配置中的 chats 和 sync_sources 必须为列表')
            data['codex_access_dirs'] = access_directories(self.folder('wechat'),
                None if data['codex_auto_access_dirs'] else data['codex_access_dirs'])
        revision = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ''
        result = dict(data=data, revision=revision, exists=path.is_file())
        if tool == 'wechat':
            result['discovered_access_dirs'] = access_directories(self.folder('wechat'))
        return result

    def save_config(self, tool, data, revision):
        if not isinstance(data, dict):
            raise ValueError('配置必须是对象')
        with self.mutex:
            current = self.config(tool)
            if current['revision'] != revision:
                raise ValueError('配置已被其他窗口修改，请刷新后重试')
            if tool == 'sync':
                allowed = set(SYNC_DEFAULT) | {'known_chats', 'recipient_publications', 'uia_profile'}
                if set(data) - allowed - set(current['data']):
                    raise ValueError('配置包含不支持的字段')
                script = ('import json,sys,tempfile\nfrom pathlib import Path\n'
                          'sys.path.insert(0,sys.argv[1])\n'
                          'from push_settings import save_settings,load_settings\n'
                          'data=json.load(sys.stdin)\n'
                          'with tempfile.TemporaryDirectory(prefix="push-validate-") as temporary:\n'
                          '    path=Path(temporary)/"runtime/wechat_push.json"\n'
                          '    path.parent.mkdir()\n'
                          '    path.write_text(json.dumps(data),encoding="utf-8")\n'
                          '    validated=load_settings(temporary)\n'
                          'save_settings(validated)\n')
                # Validate with the original loader in a temporary directory before writing.
                self._validate(tool, script, data, [str(self.folder(tool) / 'app')])
                return self.config(tool)
            if tool == 'wechat':
                data = {**data, 'codex_access_dirs': access_directories(self.folder('wechat'),
                    None if data.get('codex_auto_access_dirs', True) else data.get('codex_access_dirs', []))}
                script = ('import json,sys;from wechat_local.config import LocalConfig;'
                          'data=LocalConfig.model_validate(json.load(sys.stdin));print(data.model_dump_json())')
                data = json.loads(self._validate(tool, script, data))
            else:
                unknown = set(data) - set(NAS_DEFAULT)
                if unknown:
                    raise ValueError('上传配置包含不支持的字段：' + '、'.join(sorted(unknown)))
                data = {**NAS_DEFAULT, **data}
                if not all(isinstance(data[k], str) and data[k].strip() for k in ('source_root','download_ledger','target_root')):
                    raise ValueError('源目录、台账和目标路径不能为空')
                if not data['target_root'].startswith('\\\\') or not PureWindowsPath(data['target_root']).is_absolute():
                    raise ValueError('NAS 目标必须是 UNC 共享路径')
                if (data['start_hour_beijing'], data['stop_hour_beijing']) != (12, 0):
                    raise ValueError('当前上传时段固定为 12:00—次日 00:00')
                if type(data['retry_attempts']) is not int or not 1 <= data['retry_attempts'] <= 5:
                    raise ValueError('重试次数须为1至5')
                if type(data['chunk_bytes']) is not int or not 65536 <= data['chunk_bytes'] <= 8388608:
                    raise ValueError('分块大小超出范围')
                if type(data['preserve_previous_versions']) is not bool:
                    raise ValueError('历史版本开关格式错误')
            path = self.config_path(tool)
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_file():
                backup = self.runtime / ('config-' + tool + '-' + uuid.uuid4().hex[:8] + '.json.bak')
                backup.write_bytes(path.read_bytes())
            temporary = path.with_name(path.name + '.manager.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
            os.replace(temporary, path)
            return self.config(tool)

    def _validate(self, tool, script, data, args=()):
        result = subprocess.run([self.python(tool), '-c', script, *args], cwd=self.folder(tool),
            input=json.dumps(data, ensure_ascii=False), text=True, encoding='utf-8',
            capture_output=True, timeout=15,
            env={**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'},
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        if result.returncode:
            if 'ModuleNotFoundError' in result.stderr:
                raise ValueError('共享环境 .venv-analysis 缺少工具依赖。请在项目根目录执行 '
                                 '.\\.venv-analysis\\Scripts\\python.exe -m pip install -r requirements.txt，'
                                 '或运行 install_python312.cmd。')
            error = clean_log(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else '配置校验失败')
            raise ValueError(error[:500])
        return result.stdout

    def files(self, category='downloads', query='', offset=0, limit=20, filters=None,
              sort='name', direction='asc', facet='', facet_search='', refresh=False):
        from file_catalog import FileCatalog, page_result
        filters = {} if filters is None else filters
        if category == 'downloads':
            with self.mutex:
                if not hasattr(self, '_file_catalog'):
                    self._file_catalog = FileCatalog(self)
            return page_result(self._file_catalog.snapshot(refresh),query,filters,sort,direction,
                               offset,limit,facet,facet_search)
        if category not in ('reports', 'simulation'):
            raise ValueError('未知文件分类')
        roots = ([self.folder('sync') / 'logs', self.folder('sync') / 'exports'] if category == 'reports'
                 else [self.folder('wechat') / 'data/simulations'])
        extensions = {'.xlsx', '.png', '.txt', '.json'} if category == 'reports' else {'.json', '.txt'}
        selected = []
        for base in roots:
            if base.is_dir():
                for path in base.rglob('*'):
                    if path.suffix.lower() not in extensions or not path.is_file() or path.is_symlink():
                        continue
                    if category == 'simulation' and path.name not in ('simulation_result.json', 'reply_preview.txt'):
                        continue
                    if category == 'reports' and not any(k in path.name for k in ('简报', '推送文', '统计', '台账', 'briefing')):
                        continue
                    selected.append((path.stat().st_mtime, path))
        selected.sort(reverse=True)
        items = []
        for modified, path in selected:
            identity = 'artifact-' + hashlib.sha256(str(path).encode()).hexdigest()[:24]
            self.artifacts[identity] = dict(path=path, kind=category)
            items.append(dict(id=identity, name=path.name, project=path.parent.name, bytes=path.stat().st_size,
                              state='ready', kind='简报与导出' if category == 'reports' else '模拟结果',
                              modified=datetime.fromtimestamp(modified, BEIJING).strftime('%m-%d %H:%M')))
        return page_result(dict(items=items, available=True),query,filters,sort,direction,offset,limit,facet,facet_search)

    def artifact(self, identity):
        item = self.artifacts.get(identity)
        if item is None:
            raise ValueError('请从文件列表重新选择文件')
        path = item['path'].resolve()
        if item['kind'] == 'archive_members':
            from file_catalog import archive_index
            member, verified = archive_index(self.folder('sync')).verified_member(self.folder('sync'), item['member_id'])
            if verified != path:
                raise ValueError('解压文件版本已变化，请刷新')
        elif item['kind'] == 'downloads':
            with closing(readonly(self.folder('sync') / 'runtime/ledger.sqlite3')) as db:
                current = db.execute("SELECT path,sha256,bytes FROM files WHERE project=? AND id=? AND active=1 AND status='done'",
                                     (item['project'], item['file_id'])).fetchone()
            if current is None or current['sha256'] != item['sha256'] or current['bytes'] != item['bytes']:
                raise ValueError('文件版本已变化，请刷新')
            expected = (self.folder('sync') / str(current['path']).replace('\\','/')).resolve()
            if expected != path or not path.is_relative_to((self.folder('sync') / 'downloads').resolve()):
                raise ValueError('文件路径不在下载目录内')
        else:
            roots = [self.folder('sync')/'logs',self.folder('sync')/'exports',self.folder('wechat')/'data/simulations']
            if not any(path.is_relative_to(p.resolve()) for p in roots):
                raise ValueError('文件路径超出允许目录')
        if not path.is_file():
            raise ValueError('本机没有此文件；仅复制台账不能读取服务器原件')
        return path

    def preview(self, identity):
        path = self.artifact(identity)
        if path.suffix.lower() == '.png':
            return dict(kind='image', url='/api/file/' + identity, name=path.name)
        if path.suffix.lower() not in ('.txt','.json','.md','.csv','.pcf','.idf','.log'):
            return dict(kind='download',name=path.name, text='此格式请下载原件后查看。')
        with path.open('rb') as stream:
            data = stream.read(128 * 1024)
        return dict(kind='text',name=path.name,text=clean_log(decode_text(data)),
                    truncated=path.stat().st_size > len(data))

    def log_paths(self, tool=''):
        if tool and tool not in TOOLS:
            raise ValueError('未知工具')
        paths = [(p, p.name, next((key for key in TOOLS if ('-' + key + '-') in p.name), ''))
                 for p in log_files(self.runtime) if not tool or ('-' + tool + '-') in p.name]
        for key in ([tool] if tool else TOOLS):
            base = self.folder(key)
            candidates = list(log_files(base / 'logs', True)) + list(log_files(base / 'runtime'))
            if key == 'sync':
                candidates += list(log_files(base / 'runtime/file_analysis'))
            paths += [(p, TOOLS[key]['name'] + ' / ' + str(p.relative_to(base)), key) for p in candidates]
        return paths

    def clear_logs(self, tool='', scope='history'):
        if scope not in ('history', 'all'):
            raise ValueError('日志清理范围无效')
        if tool and tool not in TOOLS:
            raise ValueError('未知工具')
        cutoff = datetime.now(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        result = dict(deleted=0, freed_bytes=0, kept=0, skipped=0, errors=[])
        with self.mutex:
            tasks = self.tasks(limit=10000)
            active = {row['tool'] for row in tasks if row['state'] == 'running'}
            for key in ([tool] if tool else TOOLS):
                if self.live(key) != 'stopped':
                    active.add(key)
                if key == 'sync' and locked(self.folder(key) / 'runtime/file_analysis/worker.lock'):
                    active.add(key)
            for path, label, owner in self.log_paths(tool):
                try:
                    stat = path.stat()
                    if scope == 'history' and stat.st_mtime >= cutoff:
                        result['kept'] += 1
                        continue
                    if owner in active:
                        result['skipped'] += 1
                        continue
                    # Check again immediately before unlink; never delete an alias.
                    if path.is_symlink() or path.is_junction() or path.resolve() != path.absolute():
                        result['skipped'] += 1
                        continue
                    path.unlink()
                    result['deleted'] += 1
                    result['freed_bytes'] += stat.st_size
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    result['errors'].append(dict(source=label, reason=str(exc)))
        return result

    def logs(self, tool='', query='', level='', offset=0, limit=20, compact=True):
        from log_progress import compact as compact_rows, severity, timestamp
        paths = []
        for path, label, _ in self.log_paths(tool):
            try:
                paths.append((path, label, path.stat().st_mtime))
            except OSError:
                continue
        entries = []
        ordered=sorted(paths, key=lambda v: v[2], reverse=True)
        if query:
            named=[entry for entry in ordered if query.casefold() in entry[1].casefold()]
            ordered=named+[entry for entry in ordered if entry not in named]
        for path, label, modified in ordered[:12]:
            try:
                rows=[]
                for index, line in enumerate(tail(path).splitlines()[-1500:]):
                    if not line.strip():
                        continue
                    category=severity(line)
                    if (level and level != category) or (query.casefold() not in line.casefold() and query.casefold() not in label.casefold()):
                        continue
                    rows.append(dict(id=hashlib.sha256(f'{path}:{index}'.encode()).hexdigest()[:20],
                        level=category, created_at=timestamp(line,modified,BEIJING),
                        source=label, message=clean_log(line)))
                entries.extend(reversed(compact_rows(rows) if compact else rows))
            except OSError:
                continue
        entries.sort(key=lambda row:row['created_at'],reverse=True)
        return dict(items=entries[offset:offset + limit], total=len(entries),compact=compact,
                    scope='最近12个日志文件，每个最多读取尾部256KB及1500行')

    def wechat_jobs(self):
        base = self.folder('wechat')
        config = read_json(base / 'wechat.codex.json', {})
        path = base / config.get('state_path', 'data/listener.sqlite3')
        if not path.is_file():
            return []
        try:
            with closing(readonly(path)) as db:
                return [dict(r) for r in db.execute('SELECT id,chat_id,status,reply,usage,error,created_at FROM jobs ORDER BY created_at DESC LIMIT 30')]
        except sqlite3.Error:
            return []
