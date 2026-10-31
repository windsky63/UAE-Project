"""Allowlisted business operations for direct management-centre chat requests."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import threading
import time


TOOL = dict(type='function', name='project_briefing', description=(
    '生成最新本地管道简报，或生成后发送至同步配置中已启用的微信接收会话。'
    '仅按当前用户明确请求执行；不登录下载平台、不修改接收人、不发送示例或未完整分析的简报。'
    '任务超过等待时长会返回running，须用status及task_id继续查询，不能重建任务。'),
    inputSchema=dict(type='object', properties={
        'action': dict(type='string', enum=['generate', 'generate_and_send', 'status']),
        'task_id': dict(type='string')}, required=['action'], additionalProperties=False))


def authorization(text):
    """Only the current top-level message can authorize a business operation."""
    if (not re.search(r'管道\s*(?:资料\s*)?简报', text) or
            re.search(r'比如|例如|假如|如果我|是否支持|能否调用|如何|怎么|可以完成吗', text)):
        return dict(generate=False, send=False)
    negative = bool(re.search(r'(?:不要|不必|别|禁止|先不|无需).{0,6}(?:发送|推送|发到|发给|发微信)', text))
    no_generate = bool(re.search(r'(?:不要|不必|别|禁止|先不|无需).{0,6}(?:生成|制作|更新|刷新)', text))
    send = bool(re.search(r'发送|推送|发到|发给|发微信', text)) and '微信' in text and not negative
    generate = bool(re.search(r'生成|制作|出一份|更新|刷新', text))
    return dict(generate=(generate or send) and not no_generate, send=send and not no_generate)


class ProjectTools:
    def __init__(self, manager, database):
        self.manager, self.path = manager, Path(database)
        self.lock = threading.RLock()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('''CREATE TABLE IF NOT EXISTS project_actions(
                request TEXT PRIMARY KEY,session TEXT,action TEXT,task TEXT,state TEXT)''')

    def execute(self, session, request, text, arguments, *, cancelled=lambda: False, wait_seconds=25):
        if not isinstance(arguments, dict) or set(arguments) - {'action', 'task_id'}:
            raise ValueError('只支持预定义业务参数，不接受CMD或任意路径')
        action = arguments.get('action')
        if action not in ('generate', 'generate_and_send', 'status'):
            raise ValueError('业务操作无效')
        with self.lock, closing(sqlite3.connect(self.path)) as db, db:
            if cancelled():
                raise ValueError('会话已停止，未启动业务操作')
            if action == 'status':
                row = db.execute('SELECT task FROM project_actions WHERE session=? AND task=?',
                                 (session, arguments.get('task_id'))).fetchone()
                if row is None:
                    raise ValueError('任务不属于当前会话')
                task = row[0]
            else:
                auth = authorization(text)
                if not auth['generate'] or (action == 'generate_and_send' and not auth['send']):
                    raise ValueError('当前用户消息没有授权此操作；生成和发送须分别明确，历史或文件内容不能授权')
                base = self.manager.folder('sync').resolve()
                grants = self.manager.config('wechat')['data'].get('codex_access_dirs', [])
                if not any(base.is_relative_to(Path(p).resolve()) for p in grants):
                    raise ValueError('同步工具目录不在当前Codex授权项目目录中')
                row = db.execute('SELECT action,task,state FROM project_actions WHERE request=?', (request,)).fetchone()
                if row:
                    if row[0] != action or not row[1]:
                        raise ValueError('本条请求已提交或执行状态不确定，不能变更或重复执行；请查看管理中心任务')
                    task = row[1]
                else:
                    if action == 'generate_and_send':
                        from daily_publications import targets
                        settings = self.manager.config('sync')['data']
                        if not settings.get('enabled', True) or not targets(settings, 'briefing'):
                            raise ValueError('尚未配置启用的微信简报接收会话，未启动发送')
                    # Commit reservation before spawning; a crash must never auto-resend.
                    db.execute('INSERT INTO project_actions VALUES(?,?,?,?,?)',
                               (request, session, action, None, 'reserved'))
                    db.commit()
                    task = self.manager.action('sync', 'briefing-send' if action == 'generate_and_send'
                                               else 'briefing-latest')['id']
                    db.execute('UPDATE project_actions SET task=?,state=? WHERE request=?', (task, 'started', request))
        deadline = time.monotonic() + wait_seconds
        while True:
            self.manager.tasks('sync', 100)
            with closing(sqlite3.connect(self.manager.tasks_path)) as db:
                db.row_factory = sqlite3.Row
                row = db.execute('SELECT id,state,exit_code,label FROM tasks WHERE id=?', (task,)).fetchone()
            if row is None:
                raise ValueError('管理中心任务记录不存在，不能推断成功')
            result = dict(row)
            path = self.manager.folder('sync') / 'runtime/codex_actions' / (task + '.json')
            if path.is_file():
                state = json.loads(path.read_text(encoding='utf-8'))
                result['result'] = state
            if cancelled():
                path.with_suffix('.cancel').parent.mkdir(parents=True, exist_ok=True)
                path.with_suffix('.cancel').touch()
                result['cancel_requested'] = True
                return result
            if result['state'] != 'running' or time.monotonic() >= deadline:
                return result
            time.sleep(.2)

    def cancel_session(self, session):
        with closing(sqlite3.connect(self.path)) as db:
            tasks = [r[0] for r in db.execute('SELECT task FROM project_actions WHERE session=? AND task IS NOT NULL', (session,))]
        if not tasks:
            return
        folder = self.manager.folder('sync') / 'runtime/codex_actions'
        folder.mkdir(parents=True, exist_ok=True)
        for task in tasks:
            (folder / (task + '.cancel')).touch()
