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
import time
import uuid
from codex_runtime import access_directories
from log_tools import log_files, captured_line, redact
from daily_publications import version_progress

HOME = Path(__file__).resolve().parent
BEIJING = timezone(timedelta(hours=8))
TOOLS = {
    'sync': dict(name='FusionLive 同步工具', folder='FusionLive同步工具', icon='mdi-cloud-download-outline',
                 schedule='每天 02:00—10:30', description='平台资料下载、后台解析与管道简报'),
    'nas': dict(name='绿联云上传工具', folder='绿联云上传工具', icon='mdi-cloud-upload-outline',
                schedule='每天 12:00—次日 00:00', description='将已确认下载的文件同步到 NAS'),
    'wechat': dict(name='微信与 Codex', folder='codex-wechat', icon='mdi-wechat',
                   schedule='监听启动后持续运行', description='会话采集、文件检索与 Codex 回复'),
    'ima': dict(name='ima 知识库', folder='Ima知识库管理', icon='mdi-book-open-page-variant-outline',
                schedule='启动后按配置间隔同步', description='台账资料复制、知识库更新与旧版归档'),
}
LABELS = {'start': '启动调度', 'stop': '安全停止', 'check': '本地检查',
          'hash-check': '检查NAS本机校验连接',
          'clean-analysis-cache': '清理旧解析缓存',
          'clean-history': '手动清理历史文件', 'stop-history-cleanup': '停止历史清理',
          'collect': '提取本地资料', 'plan': '预览上传计划', 'sync-once': '执行一次同步',
          'generate-text': '生成日报快照',
          'briefing-latest': '生成最新管道简报', 'briefing-send': '生成最新管道简报并发送微信',
          'briefing': '生成管道简报', 'analysis': '恢复后台解析', 'stop-analysis': '停止后台解析',
          'simulate': '模拟完整流程', 'push-text': '手动推送日报',
          'push-briefing': '手动推送管道简报', 'push-all': '手动推送日报及管道简报'}
SYNC_DEFAULT = dict(analysis_poll_seconds=5, analysis_idle_log_seconds=300,
                    analysis_idle_exit_seconds=0, analysis_auto_idle_exit_seconds=120,
                    enabled=True, recipients=[], prepare_time_beijing='10:40', send_time_beijing='11:00',
                    text_send_time_beijing=None, briefing_send_time_beijing=None,
                    send_window_minutes=5, briefing_title='管道资料同步简报', publish_text=True,
                    publish_briefing=True, test_recipient='文件传输助手')
NAS_REMOTE_DEFAULT = dict(enabled=True,host='',port=22,username='windsky',root_path='',
                          identity_file='',known_hosts_file='',python_command='python3',
                          timeout_seconds=3600,fallback_to_smb=False)
NAS_DEFAULT = dict(source_root='../FusionLive同步工具/downloads/HABSHANDBN',
                   download_ledger='../FusionLive同步工具/runtime/ledger.sqlite3',
                   target_root=r'\\nas.example.test\工程项目文件\阿联酋项目FusionLive平台资料', start_hour_beijing=12, stop_hour_beijing=0,
                   chunk_bytes=1048576, retry_attempts=2, preserve_previous_versions=True,
                   remote_hash=NAS_REMOTE_DEFAULT,verification_method='ssh')


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
    return redact(value)


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
        self.output_threads = {}
        self.history_lock = threading.Lock()
        self.history_progress = None
        self.history_thread = None
        self.history_job = None
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
                    reader=self.output_threads.pop(identity,None)
                    if reader:
                        reader.join(timeout=1)
                    if identity==self.history_job and self.history_thread:
                        # Reader uses only history_lock, never the task mutex.
                        self.history_thread.join(timeout=1)
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
            lock = folder / ('runtime/watch.lock' if tool == 'ima' else 'runtime/scheduler.lock' if tool == 'sync' else 'runtime/uploader.lock')
        external = locked(lock)
        return 'running' if own else 'external' if external else 'stopped'

    def overview(self):
        sync = self.folder('sync')
        inventory = version_progress(sync / 'runtime/ledger.sqlite3')
        downloads = counts(sync / 'runtime/ledger.sqlite3', 'files', 'status', 'WHERE active=1')
        parsed = counts(sync / 'runtime/file_analysis/queue.sqlite3', 'jobs', 'state', 'WHERE active=1')
        uploads = counts(self.folder('nas') / 'runtime/upload.sqlite3', 'uploads', 'status')
        tools = [{**meta, 'id': key, 'state': self.live(key), 'installed': self.folder(key).is_dir(),
                  'actions': (['start', 'stop', 'check', 'collect', 'plan', 'sync-once'] if key == 'ima' else ['start', 'stop', 'check', 'analysis', 'stop-analysis', 'briefing', 'generate-text','clean-history','stop-history-cleanup','clean-analysis-cache'] if key == 'sync'
                              else ['start', 'stop', 'check'] if key == 'nas' else ['start', 'stop', 'check', 'simulate'])}
                 for key, meta in TOOLS.items()]
        wechat = read_json(self.folder('wechat') / 'wechat.codex.json', {})
        return dict(time=now(), tools=tools, downloads=downloads, analysis=parsed, uploads=uploads,
                    file_inventory=inventory,
                    download_progress=inventory,
                    download_directory=inventory['downloads'] if inventory else None,
                    chats=len(wechat.get('chats', [])), codex_enabled=wechat.get('codex_enabled', True),
                    allow_send=wechat.get('allow_send', True), tasks=self.tasks(limit=8),
                    local_downloads=(sync / 'downloads').is_dir())

    def command(self, tool, action):
        base, python = self.folder(tool), self.python(tool)
        if tool == 'ima':
            commands = {'start': ['watch', '--execute'], 'stop': ['stop'], 'check': ['scan'],
                        'collect': ['collect'], 'plan': ['plan'], 'sync-once': ['sync', '--execute']}
            if action not in commands:
                raise ValueError('不支持的 ima 操作')
            return [python, '-u', '-m', 'ima_manager', *commands[action]], base
        if tool == 'sync':
            if action == 'clean-analysis-cache':
                return [python, '-u', str(base/'app/archive_cache.py'), '--cleanup', '--base-dir', str(base)], base
            if action=='clean-history':
                command = [python,'-u',str(base/'app/history_retention.py'),'--base-dir',str(base),
                           '--execute','--progress-pipe']
                return command,base
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
            if action not in ('start', 'stop', 'check', 'hash-check'):
                raise ValueError('不支持的上传操作')
            # The local check deliberately does not connect to the NAS.
            return [python, '-u', str(base / 'main.py'),
                    {'start': 'schedule', 'stop': 'stop', 'check': 'integrity',
                     'hash-check': 'remote-hash-check'}[action]], base
        if action == 'simulate':
            return [python, '-u', str(base / 'scripts/simulate_sync_pipeline.py')], base
        if action not in ('start', 'stop', 'check'):
            raise ValueError('不支持的微信操作')
        return [python, '-u', '-m', 'wechat_local', '--config', str(base / 'wechat.codex.json'),
                *({'start': [], 'stop': ['--stop'], 'check': ['--check']}[action])], base

    def action(self, tool, action):
        if tool=='sync' and action=='stop-history-cleanup':
            return self.stop_history_cleanup()
        command, cwd = self.command(tool, action)
        if not cwd.is_dir():
            raise ValueError('工具目录不存在')
        with self.mutex:
            active = self.tasks(tool, 100)
            if tool == 'ima' and action == 'start' and self.config('ima')['data']['auto_sync_enabled'] is not True:
                raise ValueError('自动同步已关闭，请在 ima 同步配置中开启并保存，或使用手动同步。')
            if tool == 'ima' and action in ('collect', 'sync-once') and self.live('ima') != 'stopped':
                raise ValueError('ima 自动同步正在运行，请先安全停止再执行单次操作。')
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
            if tool == 'sync' and action == 'clean-analysis-cache':
                if not (cwd/'runtime/file_analysis/queue.sqlite3').is_file():
                    raise ValueError('未找到解析队列，未执行缓存清理。')
                for relative,label in (('runtime/sync.lock','同步下载'),
                                       ('runtime/file_analysis/worker.lock','后台解析'),
                                       ('runtime/wechat_delivery/worker.lock','文件投递')):
                    if locked(cwd/relative):
                        raise ValueError(label+'正在运行，请安全停止后再手动清理缓存。')
            if tool=='sync' and action=='clean-history':
                if not (cwd/'runtime/ledger.sqlite3').is_file():
                    raise ValueError('未找到下载台账，未执行清理。')
                for relative,label in (('runtime/sync.lock','同步下载'),('runtime/file_analysis/worker.lock','后台解析'),
                                       ('runtime/wechat_delivery/worker.lock','文件投递'),('runtime/history_retention.lock','历史清理')):
                    if locked(cwd/relative):
                        raise ValueError(label+'正在运行，请先安全停止相关任务后再手动清理。')
            identity = uuid.uuid4().hex
            if tool=='sync' and action=='clean-history':
                command += ['--job-id',identity]
            if tool == 'sync' and action in ('briefing-latest', 'briefing-send'):
                if any(t['state'] == 'running' and (t['action'].startswith('briefing') or
                       t['action'].startswith('push-')) for t in active):
                    raise ValueError('已有简报生成或推送任务，请等待完成')
                command += ['--job-id', identity]
            log = self.root / 'logs' / now()[:10] / tool / (now()[:10] + '-' + tool + '-' + action + '-' + identity[:8] + '.log')
            log.parent.mkdir(parents=True, exist_ok=True)
            env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONIOENCODING': 'utf-8'}
            env.update(PROJECT_TOOLS_RUN_ID=identity,PROJECT_TOOLS_TOOL=tool,PROJECT_TOOLS_ACTION=action)
            if tool == 'nas':
                env['PROJECT_TOOLS_LOG_CAPTURED'] = '1'
            cleanup = tool=='sync' and action=='clean-history'
            with log.open('wb') as stream:
                process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT, env=env,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.children[identity] = process
            with writing(self.tasks_path) as db:
                db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                           (identity, tool, action, LABELS[action], 'running', process.pid, None, now(), None, log.name))
            if cleanup:
                with self.history_lock:
                    self.history_progress = dict(job_id=identity,state='running',manual_only=True,
                                                 started=now(),phase='启动后台任务')
                self.history_job = identity
                self.history_thread = threading.Thread(target=self.read_history_progress,
                    args=(identity,process,log),daemon=True,name='history-cleanup-progress')
                self.history_thread.start()
            else:
                reader=threading.Thread(target=self.read_task_output,
                    args=(identity,tool,action,process,log),daemon=True,name='task-log-'+identity[:8])
                self.output_threads[identity]=reader
                reader.start()
            return dict(id=identity, state='running', label=LABELS[action])

    def read_task_output(self, identity, tool, action, process, log):
        started=time.monotonic()
        warned=False
        def record(message):
            nonlocal warned
            try:
                captured_line(log,message,now())
            except OSError as exc:
                if not warned:
                    print(f'任务日志写入失败：tool={tool}；run={identity}；path={log}；{type(exc).__name__}：{exc}；继续排空子进程输出。',file=sys.stderr,flush=True)
                    warned=True
        record(f'任务启动：tool={tool}；action={action}；run={identity}；pid={process.pid}；输出={log}')
        try:
            with process.stdout:
                for line in process.stdout:
                    record(decode_text(line))
        except (OSError,ValueError) as exc:
            record(f'日志读取异常：run={identity}；{type(exc).__name__}：{exc}')
        finally:
            code=process.poll()
            # EOF can precede process termination slightly. Wait only for the
            # already-drained process; no business timeout is changed here.
            if code is None:
                try:code=process.wait(timeout=1)
                except subprocess.TimeoutExpired:pass
            record(f'任务输出结束：tool={tool}；action={action}；run={identity}；pid={process.pid}；'
                   f'退出码={code if code is not None else "尚未退出"}；耗时={time.monotonic()-started:.1f}秒')

    def read_history_progress(self, identity, process, log):
        prefix = b'@history-cleanup '
        warned=False
        def record(line):
            nonlocal warned
            try:
                captured_line(log,decode_text(line),now())
            except OSError as exc:
                if not warned:
                    print(f'历史清理日志写入失败：run={identity}；{exc}；继续读取任务进度。',file=sys.stderr,flush=True)
                    warned=True
        record(f'任务启动：tool=sync；action=clean-history；run={identity}；pid={process.pid}'.encode('utf-8'))
        # Drain throughout execution so the child's output cannot fill its pipe.
        try:
            with process.stdout:
                for line in process.stdout:
                    if line.startswith(prefix):
                        try:
                            value=json.loads(line[len(prefix):].decode('utf-8'))
                            if (isinstance(value,dict) and value.get('job_id')==identity
                                    and value.get('state') in ('running','succeeded','stopped','failed')):
                                with self.history_lock:
                                    if self.history_progress and self.history_progress.get('job_id')==identity:
                                        self.history_progress = {**value,'manual_only':True}
                                if value['state']!='running':
                                    record(line)
                                continue
                        except (ValueError,UnicodeError):
                            pass
                    record(line)
        finally:
            record(f'任务输出结束：run={identity}；退出码={process.poll()}'.encode('utf-8'))

    def stop_history_cleanup(self):
        with self.mutex:
            task=next((t for t in self.tasks('sync',100) if t['action']=='clean-history' and t['state']=='running'),None)
            process=self.children.get(task['id']) if task else None
            if not process or not process.stdin:
                raise ValueError('当前没有由本管理中心启动的历史清理任务。')
            try:
                process.stdin.write((json.dumps(dict(command='stop',job_id=task['id']))+'\n').encode('utf-8'))
                process.stdin.flush()
            except (OSError,ValueError) as exc:
                raise ValueError('清理进程已退出或控制连接已关闭，请刷新查看结果。') from exc
            return dict(id=task['id'],state='running',label=LABELS['stop-history-cleanup'])

    def history_cleanup_status(self):
        task = next((r for r in self.tasks('sync',100) if r['action']=='clean-history'),None)
        with self.history_lock:
            value = dict(self.history_progress) if self.history_progress else None
        if value and task and value['job_id']==task['id']:
            if task['state']!='running' and value['state']=='running':
                value.update(state='failed' if task['state']=='failed' else 'interrupted',
                             message='清理进程已退出，未收到最终结果，请查看任务日志。')
            if task['state']=='failed' and value['state'] in ('succeeded','stopped'):
                value.update(state='failed',message='清理进程异常退出，请查看任务日志。')
            return value
        if task:
            return dict(job_id=task['id'],state='running' if task['state']=='running' else
                        'failed' if task['state']=='failed' else 'interrupted',manual_only=True,
                        message='本管理中心没有该任务的内存进度；历史结果请查看清理任务日志。')
        return dict(state='idle',manual_only=True)

    def bulk_action(self, action, tools=None):
        if action not in ('start', 'stop'):
            raise ValueError('仅支持批量启动或批量安全停止')
        if tools is not None and (not isinstance(tools, list) or not tools
                or any(not isinstance(tool, str) or tool not in TOOLS for tool in tools)):
            raise ValueError('请选择至少一个有效工具')
        selected = list(TOOLS) if tools is None else list(dict.fromkeys(tools))
        items = []
        with self.mutex:
            for tool in selected:
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
            safe_logs = set(log_files(self.runtime)) | {path for path, _, _ in self.log_paths(tool)}
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
                        if not path.exists() and not path.is_symlink():
                            path = self.root / 'logs' / row['started'][:10] / tool / name
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
        return self.folder(tool) / {'ima': 'runtime/settings.json', 'sync': 'runtime/wechat_push.json', 'nas': 'config.json', 'wechat': 'wechat.codex.json'}[tool]

    def nas_hash_status(self):
        current = self.config('nas')
        remote = current['data']['remote_hash']
        result = dict(state='unverified' if remote['enabled'] else 'disabled',
                      message='已配置，尚未验证SSH连接' if remote['enabled'] else '当前使用SMB校验')
        if remote['enabled'] and not all(remote.get(k) for k in ('host','username','root_path')):
            result.update(state='unconfigured',message='SSH模式尚未完成首次配置；上传前请运行向导或填写并检查连接')
        stamp = read_json(self.folder('nas')/'runtime/hash-check.json',{})
        if remote['enabled'] and stamp.get('revision') == current['revision']:
            task = next((t for t in self.tasks('nas',100) if t['id']==stamp.get('id')),None)
            if task:
                state = {'running':'checking','succeeded':'ready','failed':'failed'}.get(task['state'],'unverified')
                result.update(state=state,message={'checking':'正在检查SSH认证、Python3和目录映射',
                    'ready':'SSH认证与目录映射检查通过；上传时可使用NAS本机校验',
                    'failed':'连接检查失败，请查看NAS任务日志；首次使用请运行配置向导',
                    'unverified':'连接尚未确认，请重新检查'}[state])
        summaries = sorted((self.folder('nas')/'logs').glob('*/upload-*-summary.json'))
        if summaries:
            latest = read_json(summaries[-1],{})
            result['last_run'] = {k:latest.get(k) for k in ('started','status','remote_hash_files',
                'remote_hash_bytes','remote_hash_fell_back','nas_read_bytes','hash_cache_hits')}
        return result

    def nas_hash_check(self):
        current = self.config('nas')
        if not current['data']['remote_hash']['enabled']:
            raise ValueError('请先完成首次配置或启用并保存NAS本机校验配置。')
        if locked(self.folder('nas')/'runtime/uploader.lock'):
            raise ValueError('请先安全停止上传，再检查SSH配置。')
        result = self.action('nas','hash-check')
        path = self.folder('nas')/'runtime/hash-check.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(dict(id=result['id'],revision=current['revision'])),encoding='utf-8')
        return result

    def nas_setup(self):
        if os.name != 'nt':
            raise ValueError('首次密码配置向导需在Windows上传服务器运行配置NAS本机校验.cmd。')
        with self.mutex:
            if locked(self.folder('nas')/'runtime/uploader.lock'):
                raise ValueError('请先安全停止NAS上传，再运行首次配置向导。')
            previous = getattr(self,'_nas_setup_process',None)
            if previous is not None and previous.poll() is None:
                raise ValueError('服务器上的首次配置向导仍在运行。')
            script = self.folder('nas')/'setup_nas_ssh.py'
            launcher = self.folder('nas')/'配置NAS本机校验.cmd'
            if not script.is_file() or not launcher.is_file():
                raise ValueError('服务器缺少SSH配置向导，请先更新绿联云上传工具。')
            # The batch entry explicitly reconnects CONIN$/CONOUT$. Do not
            # inherit the management service's redirected stdin/log handles.
            self._nas_setup_process = subprocess.Popen(['cmd.exe','/d','/c',launcher.name],
                cwd=script.parent,close_fds=True,
                creationflags=getattr(subprocess,'CREATE_NEW_CONSOLE',16))
        return dict(message='首次配置向导已在服务器桌面打开。请在服务器终端输入用户名及密码，完成后刷新此页。')

    def config(self, tool):
        if tool == 'ima':
            from ima_control import config
            return config(self)
        path = self.config_path(tool)
        data = read_json(path, {})
        if not isinstance(data, dict):
            raise ValueError('配置文件必须包含 JSON 对象，请检查配置格式')
        if tool == 'sync':
            data = {**SYNC_DEFAULT, **data}
            for name in ('text_send_time_beijing','briefing_send_time_beijing'):
                if data[name] is None:
                    data[name] = data['send_time_beijing']
        elif tool == 'nas':
            template = read_json(path.with_name('config.example.json'), {})
            if not isinstance(template, dict):
                raise ValueError('上传配置示例必须包含 JSON 对象')
            data = {**NAS_DEFAULT, **{k:v for k,v in template.items() if k in NAS_DEFAULT}, **data}
            remote = data.get('remote_hash')
            if not isinstance(remote,dict):
                raise ValueError('remote_hash须为配置对象')
            data['remote_hash'] = {**NAS_REMOTE_DEFAULT,**remote}
            data['remote_hash']['enabled'] = data.get('verification_method','ssh')=='ssh'
            data['remote_hash']['fallback_to_smb'] = False
            if not data['remote_hash']['host']:
                data['remote_hash']['host'] = PureWindowsPath(data['target_root']).drive.lstrip('\\').split('\\')[0]
        elif tool == 'wechat':
            defaults = dict(chats=[], sync_sources=[], codex_command=['codex'], codex_model='',
                            codex_transport='auto', codex_auto_access_dirs=True, codex_access_dirs=[],
                            codex_enabled=True, allow_send=True, database_root='D:/xwechat_files', database_account='',
                            account_name='', codex_timeout_seconds=0, reply_max_age_seconds=900,
                            codex_idle_timeout_seconds=0, codex_connect_timeout_seconds=90, mention_reply_sender=True, query_progress_enabled=True,
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
            if tool == 'ima':
                from ima_control import save_config
                return save_config(self, data, revision)
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
                remote = data.get('remote_hash',{})
                if not isinstance(remote,dict) or set(remote)-set(NAS_REMOTE_DEFAULT):
                    raise ValueError('SSH配置包含不支持的字段；密码不能保存在配置中')
                remote = {**NAS_REMOTE_DEFAULT,**remote}
                if data['verification_method'] not in ('ssh','smb'):
                    raise ValueError('文件校验方式须为ssh或smb')
                remote['enabled'] = data['verification_method']=='ssh'
                remote['fallback_to_smb'] = False
                if type(remote['enabled']) is not bool or type(remote['fallback_to_smb']) is not bool:
                    raise ValueError('SSH开关格式错误')
                if type(remote['port']) is not int or not 1<=remote['port']<=65535:
                    raise ValueError('SSH端口无效')
                if type(remote['timeout_seconds']) is not int or not 1<=remote['timeout_seconds']<=86400:
                    raise ValueError('SSH校验超时须为1至86400秒')
                if remote['enabled'] and all(remote.get(k) for k in ('host','username','root_path')):
                    script = ('import json;from pathlib import Path;from nas_remote_hash import RemoteHashSettings;'
                              'RemoteHashSettings.parse(json.load(__import__("sys").stdin),Path.cwd())')
                    self._validate('nas',script,remote)
                data['remote_hash'] = remote
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
        # New operational logs share a single project root. Historical locations
        # remain readable so an update never hides the evidence of earlier faults.
        for path in log_files(self.root / 'logs', True):
            owner = path.parent.name
            if owner in TOOLS and (not tool or tool == owner):
                paths.append((path, str(path.relative_to(self.root)), owner))
        for path in log_files(self.root):
            owner = next((key for key in TOOLS if f'-{key}-' in path.name), '')
            if path.name in ('run.log', 'scheduler.log'):
                owner = 'sync'
            if not tool or tool == owner:
                paths.append((path, '历史根目录 / ' + path.name, owner))
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
        from log_progress import compact as compact_rows, deduplicate, severity, timestamp, events
        with self.mutex:
            for identity,reader in list(self.output_threads.items()):
                process=self.children.get(identity)
                if process and process.poll() is not None:
                    reader.join(timeout=1)
        paths = []
        for path, label, owner in self.log_paths(tool):
            try:
                paths.append((path, label, path.stat().st_mtime, owner))
            except OSError:
                continue
        entries = []
        ordered=sorted(paths, key=lambda v: v[2], reverse=True)
        if query:
            named=[entry for entry in ordered if query.casefold() in entry[1].casefold()]
            ordered=named+[entry for entry in ordered if entry not in named]
        for path, label, modified, owner in ordered:
            try:
                rows=[]
                for index, line in enumerate(events('\n'.join(tail(path).splitlines()[-1500:]))):
                    if not line.strip():
                        continue
                    category=severity(line)
                    if (level and level != category) or (query.casefold() not in line.casefold() and query.casefold() not in label.casefold()):
                        continue
                    rows.append(dict(id=hashlib.sha256(f'{path}:{index}'.encode()).hexdigest()[:20],
                        level=category, created_at=timestamp(line,modified,BEIJING),
                        source=label, tool=owner, message=clean_log(line)))
                entries.extend(rows)
            except OSError:
                continue
        before = len(entries)
        entries = deduplicate(entries)
        duplicates = before - len(entries)
        if compact:
            sources = {}
            for row in entries:
                sources.setdefault(row['source'], []).append(row)
            entries = [row for rows in sources.values() for row in compact_rows(rows)]
        entries.sort(key=lambda row:row['created_at'],reverse=True)
        return dict(items=entries[offset:offset + limit], total=len(entries),compact=compact,
                    duplicates_merged=duplicates, scope='各日志文件最多读取尾部256KB及1500行；相同时间和内容的跨文件副本合并显示')

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
