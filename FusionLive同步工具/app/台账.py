"""Native desktop ledger. Viewing, searching and exporting work offline."""

from paths import HOME, CODE
import os
import json
from datetime import date
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox

from fusion_client import HOME, RUNTIME, now, check_window, WindowClosed
from ledger import DB_PATH, Ledger
from paths import resolve_path
from local_credentials import get_credentials
from workflow import STAGES
from login_test import load_window, save_window, check_test_window
from business_type import classify, file_business, ledger_file_contexts

STATES = {'done': '已完成', 'pending': '待处理', 'failed': '失败', 'downloading': '下载中',
          'no_content': '未上传内容（占位记录）',
          'restricted': '无读取权限', 'not_enumerated': '附件待枚举', 'running': '运行中',
          'stopped': '已停止', 'interrupted': '异常中断', 'logout_unconfirmed': '注销未确认'}
STATES.update({'inventory_incomplete': '盘点未完成', 'scanning': '目录盘点中', 'catalogued': '目录完成，关联核对中', 'complete': '盘点与关联核对完成'})
STATES['download_pending'] = '下载未完成，等待继续'
STATES['awaiting_scope'] = '等待人工确认施工范围'


class App:
    def __init__(self, root):
        ledger = Ledger()
        ledger.close()
        self.root = root
        root.title('FusionLive 文件台账')
        root.geometry('1380x840')
        root.minsize(1000, 650)
        root.option_add('*Font', ('Microsoft YaHei UI', 10))
        self.process = None
        self.process_action = None
        self.log_handle = None
        self.closing = False
        self.folder = None
        self.folder_keys = ()
        self.file_paths = {}
        self.search = tk.StringVar()
        self.filter = tk.StringVar(value='全部')
        self.download_since = tk.StringVar()
        username, password = get_credentials()
        self.account = tk.StringVar(value=username)
        self.password = tk.StringVar(value=password)
        self.limit = tk.StringVar(value='0')
        self.summary = tk.StringVar(value='本地台账；查看和搜索不连接业主平台。')
        self.session = tk.StringVar()
        outer = ttk.Frame(root, padding=14)
        outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='FusionLive 文件台账', font=('Microsoft YaHei UI', 19, 'bold')).pack(anchor='w')
        ttk.Label(outer, textvariable=self.summary).pack(anchor='w', pady=(8, 3))
        ttk.Label(outer, textvariable=self.session).pack(anchor='w', pady=(0, 12))
        controls = ttk.LabelFrame(outer, text='同步任务 · 手动任务 01:00—10:30；常驻入口 02:00—10:30，到点停止并注销', padding=10)
        controls.pack(fill='x')
        for text, variable, width, show in [('账号', self.account, 22, ''), ('密码', self.password, 18, '*'), ('文件上限(0=持续)', self.limit, 6, '')]:
            ttk.Label(controls, text=text).pack(side='left', padx=(0, 5))
            ttk.Entry(controls, textvariable=variable, width=width, show=show).pack(side='left', padx=(0, 12))
        ttk.Button(controls, text='强制重新盘点', command=lambda: self.start('scan')).pack(side='left', padx=3)
        ttk.Button(controls, text='按阶段继续', command=lambda: self.start('auto')).pack(side='left', padx=3)
        ttk.Button(controls, text='停止并注销', command=self.stop).pack(side='left', padx=3)
        test_controls = ttk.Frame(outer, padding=(0, 8))
        test_controls.pack(fill='x')
        try:
            test_start, test_end = load_window()
        except (OSError, ValueError, KeyError, TypeError):
            test_start, test_end = '', ''
        self.test_start = tk.StringVar(value=test_start)
        self.test_end = tk.StringVar(value=test_end)
        ttk.Label(test_controls, text='登录测试时段（北京时间）').pack(side='left')
        ttk.Entry(test_controls, textvariable=self.test_start, width=7).pack(side='left', padx=5)
        ttk.Label(test_controls, text='至').pack(side='left')
        ttk.Entry(test_controls, textvariable=self.test_end, width=7).pack(side='left', padx=5)
        ttk.Button(test_controls, text='保存测试时段', command=self.save_test_window).pack(side='left', padx=5)
        self.test_button = ttk.Button(test_controls, text='登录测试并下载一个文件', command=lambda: self.start('login-test'))
        self.test_button.pack(side='left', padx=5)
        self.test_notice = tk.StringVar()
        ttk.Label(test_controls, textvariable=self.test_notice).pack(side='left', padx=5)
        toolbar = ttk.Frame(outer, padding=(0, 12))
        toolbar.pack(fill='x')
        ttk.Label(toolbar, text='搜索编号、文件名或标题').pack(side='left')
        entry = ttk.Entry(toolbar, textvariable=self.search, width=36)
        entry.pack(side='left', padx=8)
        entry.bind('<Return>', lambda _: self.refresh_rows())
        box = ttk.Combobox(toolbar, textvariable=self.filter, width=15, state='readonly', values=['全部', '未下载', '已完成', '失败', '附件待枚举', '未上传内容', '本轮新增或变更'])
        box.pack(side='left', padx=4)
        box.bind('<<ComboboxSelected>>', lambda _: self.refresh_rows())
        ttk.Label(toolbar, text='下载自 YYYY-MM-DD').pack(side='left', padx=4)
        ttk.Entry(toolbar, textvariable=self.download_since, width=12).pack(side='left')
        ttk.Button(toolbar, text='查询', command=self.refresh_rows).pack(side='left', padx=4)
        ttk.Button(toolbar, text='全部目录', command=self.clear_folder).pack(side='left', padx=4)
        ttk.Button(toolbar, text='导出 Excel', command=lambda: self.start('export')).pack(side='right')
        ttk.Button(toolbar, text='校验本地', command=lambda: self.start('verify')).pack(side='right', padx=4)
        ttk.Button(toolbar, text='打开日志', command=self.open_log).pack(side='right', padx=6)
        ttk.Button(toolbar, text='生成简报', command=self.briefing).pack(side='right', padx=4)
        panes = ttk.Panedwindow(outer, orient='horizontal')
        panes.pack(fill='both', expand=True)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=1)
        panes.add(right, weight=4)
        ttk.Label(left, text='平台目录 · 选择后包含子目录').pack(anchor='w', pady=(0, 8))
        self.tree = ttk.Treeview(left, show='tree', selectmode='browse')
        self.tree.column('#0', width=280)
        scroll = ttk.Scrollbar(left, orient='vertical', command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.tree.pack(fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self.select_folder)
        tabs = ttk.Notebook(right)
        tabs.pack(fill='both', expand=True)
        doc_frame = ttk.Frame(tabs)
        file_frame = ttk.Frame(tabs)
        run_frame = ttk.Frame(tabs)
        changes_frame = ttk.Frame(tabs)
        tabs.add(doc_frame, text='文档清单')
        tabs.add(file_frame, text='原件和附件')
        tabs.add(run_frame, text='任务与注销记录')
        tabs.add(changes_frame, text='盘点变更')
        self.docs = self.table(doc_frame, [('reference', '文档编号', 230), ('revision', '修订', 55), ('filename', '文件名', 280), ('state', '平台内容/附件状态', 190), ('size', '字节', 100), ('title', '标题', 500), ('path', '平台目录', 550)])
        self.files = self.table(file_frame, [('filename', '文件名', 290), ('revision', '修订', 55), ('relation', '类型', 100), ('state', '下载状态', 100), ('size', '字节', 100), ('path', '本地路径', 550), ('error', '错误', 450), ('downloaded_at', '下载日期（北京）', 220), ('date_source', '日期来源', 180)])
        for table in (self.docs, self.files):
            existing = tuple(table['columns'])
            table.configure(columns=existing + ('business_type', 'discipline', 'business_status', 'business_source', 'business_reason'))
            table.configure(displaycolumns=(existing[0], 'business_type', 'discipline', 'business_status',
                                            *existing[1:], 'business_source', 'business_reason'))
            for key, title, width in (('business_type','业务类型',220),('discipline','业务专业',100),
                                      ('business_status','业务类型识别状态',130),('business_source','业务类型来源',180),
                                      ('business_reason','业务类型待核对原因',350)):
                table.heading(key,text=title)
                table.column(key,width=width,minwidth=70,stretch=False)
        self.runs = self.table(run_frame, [('started', '开始时间（北京）', 230), ('ended', '结束时间', 230), ('action', '操作', 100), ('state', '任务状态', 110), ('logout', '注销确认', 100), ('error', '错误', 450)])
        self.changes = self.table(changes_frame, [('time', '北京时间', 220), ('entity', '对象', 100), ('kind', '变更', 150), ('id', '对象ID', 130), ('details', '详情', 650)])
        self.files.bind('<Double-1>', self.open_file)
        self.foot = ttk.Label(outer, text='每个表最多显示 1000 条；可搜索或选择目录缩小范围。已填入本机内置账号。')
        self.foot.pack(anchor='w', pady=(10, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        if not DB_PATH.exists():
            ledger = Ledger()
            ledger.close()
        self.refresh()

    def table(self, parent, columns):
        frame = ttk.Frame(parent)
        frame.pack(fill='both', expand=True)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show='headings', selectmode='browse')
        for key, title, width in columns:
            tree.heading(key, text=title)
            tree.column(key, width=width, minwidth=50, stretch=False)
        y = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
        x = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview)
        tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        y.pack(side='right', fill='y')
        x.pack(side='bottom', fill='x')
        tree.pack(fill='both', expand=True)
        return tree

    @contextmanager
    def connection(self):
        db = sqlite3.connect(DB_PATH.as_uri() + '?mode=ro', uri=True, timeout=3)
        try:
            yield db
        finally:
            db.close()

    def refresh(self):
        try:
            with self.connection() as db:
                folders = db.execute('SELECT project,id,parent,name,path FROM folders WHERE active=1 ORDER BY length(path),rowid').fetchall()
                doc_count = db.execute('SELECT COUNT(*) FROM documents WHERE active=1').fetchone()[0]
                file_count, done, size = db.execute("SELECT COUNT(*),COALESCE(SUM(status='done'),0),COALESCE(SUM(CASE WHEN status='done' THEN bytes ELSE 0 END),0) FROM files WHERE active=1").fetchone()
                complete = db.execute("SELECT COUNT(*) FROM folders WHERE list_state='done' AND active=1").fetchone()[0]
            self.summary.set(f'目录 {len(folders)} · 已盘点目录 {complete} · 文档记录 {doc_count:,} · 已枚举原件/附件 {file_count:,} · 已下载 {done:,} · {size / 1024 / 1024:.1f} MB')
            keys = tuple((p, i, parent, name) for p, i, parent, name, path in folders)
            if keys != self.folder_keys:
                self.tree.delete(*self.tree.get_children())
                for project, identifier, parent, name, path in folders:
                    key = project + ':' + identifier
                    parent_key = project + ':' + parent if parent else ''
                    self.tree.insert(parent_key, 'end', iid=key, text=name, values=(path,), open=parent is None)
                self.folder_keys = keys
            self.refresh_rows()
            if self.process and self.process.poll() is not None:
                code = self.process.returncode
                action = self.process_action
                self.process = None
                self.log_handle.close()
                self.log_handle = None
                self.foot.configure(text=f'任务已结束，退出码 {code}。请查看任务与注销记录。')
                if action == 'login-test':
                    self.show_test_result(code)
            if self.closing and not self.process:
                self.root.destroy()
                return
        except (sqlite3.Error, OSError) as exc:
            self.foot.configure(text='刷新失败：' + str(exc))
        self.refresh_test_state()
        self.root.after(2500, self.refresh)

    def save_test_window(self):
        try:
            save_window(self.test_start.get().strip(), self.test_end.get().strip())
        except (ValueError, OSError) as exc:
            messagebox.showerror('测试时段', str(exc))
            return False
        self.refresh_test_state()
        return True

    def refresh_test_state(self):
        self.test_button.configure(state='disabled' if self.process or self.closing else 'normal')
        try:
            check_test_window(check_stop=False)
            self.test_notice.set('允许时段内；测试完成后注销')
        except (WindowClosed, OSError, ValueError, KeyError, TypeError) as exc:
            self.test_notice.set(str(exc))

    def show_test_result(self, code):
        if code:
            messagebox.showerror('登录测试未完成', f'测试失败或已停止（退出码 {code}）。请查看 runtime/sync.log 和任务与注销记录。')
            return
        try:
            result = json.loads((RUNTIME / 'login_test_result.json').read_text(encoding='utf-8'))
            messagebox.showinfo('登录测试成功', f"已下载一个文件并确认注销。\n文件：{result['path']}\n大小：{result['bytes']} 字节\nSHA256：{result['sha256']}")
        except (OSError, ValueError, KeyError) as exc:
            messagebox.showerror('读取测试结果失败', str(exc))

    def refresh_rows(self):
        where, args = ['d.active=1 AND f.active=1'], []
        if self.folder:
            project, identifier = self.folder.split(':', 1)
            where.append('d.project=? AND f.id IN (WITH RECURSIVE sub(id) AS (SELECT ? UNION SELECT x.id FROM folders x JOIN sub ON x.parent=sub.id WHERE x.project=?) SELECT id FROM sub)')
            args.extend([project, identifier, project])
        term = self.search.get().strip()
        if term:
            where.append('(d.reference LIKE ? OR d.filename LIKE ? OR d.title LIKE ?)')
            args.extend(['%' + term + '%'] * 3)
        clause = ' AND '.join(where)
        status = self.filter.get()
        related_file = '''SELECT 1 FROM document_files ix JOIN files it ON it.project=ix.project AND it.id=ix.file_id
          WHERE ix.project=d.project AND ix.document_id=d.id AND ix.folder_id=d.folder_id AND ix.active=1 AND it.active=1'''
        doc_filter = {
            '未下载': " AND (d.related_state='pending' OR EXISTS(" + related_file + " AND it.status!='done'))",
            '已完成': ' AND EXISTS(' + related_file + " AND it.status='done')",
            '失败': ' AND EXISTS(' + related_file + " AND it.status='failed')",
            '附件待枚举': " AND d.related_state='pending'",
            '未上传内容': " AND json_extract(d.metadata,'$.hasContent')='false'",
            '本轮新增或变更': " AND EXISTS(SELECT 1 FROM changes c WHERE c.snapshot=(SELECT MAX(id) FROM snapshots) AND c.project=d.project AND c.kind IN ('added','updated','moved_or_renamed') AND ((c.entity='document' AND c.entity_id=d.id AND c.folder_id=d.folder_id) OR (c.entity='file' AND c.entity_id IN (SELECT file_id FROM document_files WHERE project=d.project AND document_id=d.id AND folder_id=d.folder_id AND active=1))))"}.get(status, '')
        since = self.download_since.get().strip()
        if since:
            try:
                date.fromisoformat(since)
            except ValueError:
                messagebox.showinfo('日期格式', '请按 YYYY-MM-DD 输入下载日期。')
                return
            doc_filter += " AND EXISTS(" + related_file + " AND it.downloaded_at>=?)"
        doc_args = args + ([since] if since else [])
        with self.connection() as db:
            contexts = ledger_file_contexts(db)
            self.docs.delete(*self.docs.get_children())
            for row in db.execute("""SELECT d.reference,d.revision,d.filename,CASE WHEN json_extract(d.metadata,'$.hasContent')='false' THEN CASE WHEN json_extract(d.metadata,'$.hasAttachments')='true' THEN '未上传主文件（有附件）' ELSE 'no_content' END ELSE d.related_state END,d.size,d.title,f.path FROM documents d JOIN folders f ON f.project=d.project AND f.id=d.folder_id WHERE """ + clause + doc_filter + ' ORDER BY d.reference LIMIT 1000', doc_args):
                row = list(row)
                row[3] = STATES.get(row[3], row[3])
                business = classify(filename=row[2] or '', reference=row[0] or '')
                self.docs.insert('', 'end', values=row + [business[k] for k in ('business_type','discipline','status','source','reason')])
            file_filter = {'未下载': " AND (i.status!='done' OR d.related_state='pending')", '已完成': " AND i.status='done'", '失败': " AND i.status='failed'", '附件待枚举': " AND d.related_state='pending'"}.get(status, '')
            if status in ('未上传内容', '本轮新增或变更'):
                file_filter = doc_filter
                file_args = doc_args
            else:
                file_args = args + ([since] if since else [])
                if since:
                    file_filter += ' AND i.downloaded_at>=?'
            self.files.delete(*self.files.get_children())
            self.file_paths.clear()
            sql = '''SELECT COALESCE(i.filename,d.filename,d.reference),d.revision,i.relation,COALESCE(i.status,CASE WHEN json_extract(d.metadata,'$.hasContent')='false' AND COALESCE(json_extract(d.metadata,'$.hasAttachments'),'false')!='true' THEN 'no_content' WHEN d.related_state='no_content' THEN 'no_content' ELSE 'not_enumerated' END),COALESCE(i.size,d.size),i.path,i.error,i.downloaded_at,i.download_date_source,d.project,i.id,d.reference
              FROM documents d JOIN folders f ON f.project=d.project AND f.id=d.folder_id
              LEFT JOIN document_files x ON x.project=d.project AND x.document_id=d.id AND x.folder_id=d.folder_id AND x.active=1
              LEFT JOIN files i ON i.project=x.project AND i.id=x.file_id AND i.active=1 WHERE ''' + clause + file_filter + ' ORDER BY d.reference LIMIT 1000'
            for row in db.execute(sql, file_args):
                values = list(row[:9])
                values[3] = STATES.get(row[3], row[3])
                business = (file_business(dict(filename=row[0],path=row[5]),contexts.get((row[9],row[10]),()))
                            if row[10] else classify(filename=row[0] or '',reference=row[11] or ''))
                values += [business[k] for k in ('business_type','discipline','status','source','reason')]
                iid = self.files.insert('', 'end', values=[v if v is not None else '' for v in values])
                if row[3] == 'done' and row[5]:
                    self.file_paths[iid] = row[5]
            self.runs.delete(*self.runs.get_children())
            for row in db.execute('SELECT started,ended,action,status,logout_confirmed,error FROM runs ORDER BY id DESC LIMIT 100'):
                values = list(row)
                values[3] = STATES.get(row[3], row[3])
                values[4] = '已确认' if row[4] else ('进行中' if row[3] == 'running' else '未确认/未登录')
                self.runs.insert('', 'end', values=[v if v is not None else '' for v in values])
            last = db.execute('SELECT status,logout_confirmed FROM runs ORDER BY id DESC LIMIT 1').fetchone()
            self.session.set('最近任务：' + (('运行中；结束时执行注销' if last[0] == 'running' else '已注销并确认' if last[1] else '注销未确认或未建立登录，请查看记录') if last else '尚未运行'))
            snapshot = db.execute('SELECT status FROM snapshots ORDER BY id DESC LIMIT 1').fetchone()
            if snapshot:
                self.session.set(self.session.get() + ' · 盘点：' + STATES.get(snapshot[0], snapshot[0]))
            stage = db.execute('SELECT stage,phase FROM workflows ORDER BY updated DESC LIMIT 1').fetchone()
            if stage:
                self.session.set(self.session.get() + ' · ' + STAGES.get(stage[0], stage[0]) + ' / ' + stage[1])
            self.changes.delete(*self.changes.get_children())
            kinds = {'added': '新增', 'updated': '更新', 'not_visible': '当前不可见', 'moved_or_renamed': '移动或重命名'}
            for row in db.execute('SELECT created,entity,kind,entity_id,details FROM changes ORDER BY id DESC LIMIT 1000'):
                values = list(row)
                values[2] = kinds.get(values[2], values[2])
                self.changes.insert('', 'end', values=[v or '' for v in values])

    def select_folder(self, _):
        selected = self.tree.selection()
        self.folder = selected[0] if selected else None
        self.refresh_rows()

    def clear_folder(self):
        self.folder = None
        self.tree.selection_remove(*self.tree.selection())
        self.refresh_rows()

    def briefing(self):
        if getattr(self, 'briefing_process', None) and self.briefing_process.poll() is None:
            messagebox.showinfo('简报生成中', '正在读取后台分析结果并生成简报，请等待当前任务完成。')
            return
        day = now().date().isoformat()
        folder = HOME / 'logs' / day / '管道简报'
        try:
            log = (RUNTIME / 'briefing.log').open('w', encoding='utf-8')
            try:
                process = subprocess.Popen([sys.executable, str(CODE / 'briefing.py'), '--date', day], cwd=HOME,
                    stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            except OSError:
                log.close()
                raise
            self.briefing_process = process
            self.foot.configure(text='正在读取已缓存的后台分析结果并生成简报；不会重复解压、登录或下载。')
            def poll():
                if self.closing:
                    log.close()
                    return
                if process.poll() is None:
                    self.root.after(500, poll)
                    return
                log.close()
                try:
                    if process.returncode:
                        self.foot.configure(text='简报生成失败；详见 runtime/briefing.log。')
                        messagebox.showerror('简报生成失败', (RUNTIME / 'briefing.log').read_text(encoding='utf-8')[-1500:])
                        return
                    state = json.loads((folder / '简报推送状态.json').read_text(encoding='utf-8'))
                    notice = '离线简报已生成，尚未发送。' if state['ready'] else '简报预览已生成；后台分析尚未完成，当前不可自动推送。'
                    self.foot.configure(text=notice)
                    messagebox.showinfo('管道简报', f"{notice}\nExcel及{len(state['images'])}张图片。\n{folder}")
                    os.startfile(folder)
                except (OSError, ValueError) as exc:
                    messagebox.showerror('简报生成失败', str(exc))
            self.root.after(500, poll)
        except OSError as exc:
            messagebox.showerror('简报生成失败', str(exc))

    def start(self, action):
        if self.process:
            messagebox.showinfo('任务运行中', '请先等待当前任务结束，或点击停止并注销。')
            return
        env = dict(os.environ)
        env['PYTHONIOENCODING'] = 'utf-8'
        args = [sys.executable, '-u', str(CODE / 'sync.py'), action]
        if action == 'login-test':
            if not self.save_test_window():
                return
            if (RUNTIME / 'login.paused').exists():
                messagebox.showinfo('登录已暂停', '账号登录已暂停，不能运行登录测试。')
                return
            try:
                check_test_window(check_stop=False)
            except WindowClosed:
                start, end = load_window()
                if not messagebox.askyesno('时段外人工测试警告', f'当前不在北京时间 {start}—{end} 的测试时段。\n继续将立即登录平台、下载一个文件并注销。\n是否由您人工确认执行本次测试？', default='no'):
                    return
                args.append('--manual-test-outside-window')
        if action in ('scan', 'download', 'auto', 'login-test'):
            try:
                if action != 'login-test':
                    check_window()
            except WindowClosed as exc:
                messagebox.showinfo('访问时间', str(exc))
                return
            if not self.account.get().strip() or not self.password.get():
                messagebox.showinfo('登录信息', '请填写账号和密码，或重新打开台账使用本机内置账号。')
                return
            env['FUSIONLIVE_USERNAME'] = self.account.get().strip()
            env['FUSIONLIVE_PASSWORD'] = self.password.get()
            if action in ('download', 'auto'):
                try:
                    limit = int(self.limit.get())
                    if limit < 0:
                        raise ValueError()
                except ValueError:
                    messagebox.showinfo('下载上限', '请输入非负整数；0 表示持续下载至队列完成或时间收尾。')
                    return
                if limit:
                    args.extend(['--max-files', str(limit)])
                args.append('--retry-failed')
        self.log_handle = (RUNTIME / 'sync.log').open('a', encoding='utf-8')
        self.log_handle.write('\n任务启动 ' + now().isoformat() + ' ' + action + '\n')
        self.log_handle.flush()
        try:
            self.process = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                        stdout=self.log_handle, stderr=subprocess.STDOUT,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except OSError as exc:
            self.log_handle.close()
            self.log_handle = None
            messagebox.showerror('任务启动失败', str(exc))
            return
        self.process_action = action
        self.refresh_test_state()
        if action == 'login-test':
            self.foot.configure(text='正在登录测试：仅下载一个文件，完成后注销；可点击停止并注销。')
            return
        self.foot.configure(text='任务已启动：按数据库保存的阶段继续；集中下载阶段不重新盘点。点击停止并注销可安全收尾。')

    def stop(self):
        (RUNTIME / 'stop.request').write_text(now().isoformat(), encoding='utf-8')
        self.foot.configure(text='已请求停止。等待当前请求结束并执行注销，请勿强制结束进程。')

    def close(self):
        if self.process:
            self.closing = True
            self.stop()
        else:
            self.root.destroy()

    def open_file(self, _):
        selected = self.files.selection()
        path = self.file_paths.get(selected[0]) if selected else None
        if path and resolve_path(path).is_file():
            os.startfile(str(resolve_path(path)))

    def open_log(self):
        log = HOME / 'logs'
        if log.exists():
            os.startfile(log)
        else:
            messagebox.showinfo('日志', '尚未生成界面任务日志。')


if __name__ == '__main__':
    window = tk.Tk()
    App(window)
    window.mainloop()
