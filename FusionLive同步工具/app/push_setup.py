"""Small desktop target selector shown by the normal scheduler launcher."""

from paths import HOME, CODE
import sys
import queue
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from push_settings import HOME, DEFAULT, load_settings, save_settings, clean_title


def main():
    settings = load_settings()
    root = tk.Tk()
    root.title('同步工具 · 微信简报推送')
    root.geometry('680x780')
    root.minsize(680, 780)
    frame = ttk.Frame(root, padding=18)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='管道简报推送设置', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor='w')
    ttk.Label(frame, text='每天北京时间 11:00 按开关发布；10:40 开始准备简报。').pack(anchor='w', pady=(8, 2))
    ttk.Label(frame, text='使用本机已登录的微信。仅发送，不监听或读取聊天消息。').pack(anchor='w')
    ttk.Label(frame, text='管道简报标题（日期自动添加）').pack(anchor='w', pady=(12, 4))
    briefing_title = tk.StringVar(value=settings['briefing_title'])
    ttk.Entry(frame, name='briefing_title', textvariable=briefing_title).pack(fill='x')
    publish_text = tk.BooleanVar(value=settings['publish_text'])
    publish_briefing = tk.BooleanVar(value=settings['publish_briefing'])
    switches = ttk.Frame(frame)
    switches.pack(fill='x', pady=(10, 4))
    ttk.Checkbutton(switches, name='publish_text', text='定时发布微信推送文', variable=publish_text).pack(side='left')
    ttk.Checkbutton(switches, name='publish_briefing', text='定时发布简报图片', variable=publish_briefing).pack(side='left', padx=18)
    ttk.Label(frame, text='上方为全局开关；每个会话可分别选择推送文、简报。关闭后仍照常生成文件。').pack(anchor='w', pady=(0, 10))
    choices = {}
    box = ttk.LabelFrame(frame, text='勾选接收会话（完整名称必须能在微信中唯一匹配）', padding=10)
    box.pack(fill='both', expand=True)
    canvas = tk.Canvas(box, highlightthickness=0)
    scroll = ttk.Scrollbar(box, orient='vertical', command=canvas.yview)
    rows = ttk.Frame(canvas)
    canvas.create_window((0, 0), window=rows, anchor='nw')
    rows.bind('<Configure>', lambda _:canvas.configure(scrollregion=canvas.bbox('all')))
    canvas.configure(yscrollcommand=scroll.set)
    canvas.pack(side='left', fill='both', expand=True)
    scroll.pack(side='right', fill='y')
    def add_name(name, checked=True):
        name = name.strip()
        if not name or len(name) > 200:
            return
        if name in choices:
            if checked:
                choices[name][0].set(True)
            return
        selected = tk.BooleanVar(value=checked)
        flags = settings.get('recipient_publications', {}).get(name, {})
        text = tk.BooleanVar(value=flags.get('publish_text', True))
        briefing = tk.BooleanVar(value=flags.get('publish_briefing', True))
        choices[name] = selected, text, briefing
        row = ttk.Frame(rows)
        row.pack(fill='x', pady=3)
        ttk.Checkbutton(row, text=name, variable=selected, width=34).pack(side='left')
        ttk.Checkbutton(row, text='推送文', variable=text).pack(side='left', padx=6)
        ttk.Checkbutton(row, text='简报', variable=briefing).pack(side='left', padx=6)
    for name in dict.fromkeys([*settings.get('known_chats', []), *settings['recipients']]):
        add_name(name, checked=name in settings['recipients'])
    entry_row = ttk.Frame(frame)
    entry_row.pack(fill='x', pady=10)
    manual = tk.StringVar()
    entry = ttk.Entry(entry_row, name='chat_name', textvariable=manual)
    entry.pack(side='left', fill='x', expand=True)
    def add_manual():
        add_name(manual.get())
        manual.set('')
    ttk.Button(entry_row, text='添加会话', command=add_manual).pack(side='left', padx=6)
    entry.bind('<Return>', lambda _:add_manual())
    status = tk.StringVar(value='请手动添加微信中可唯一匹配的完整会话名称，勾选后保存。')
    ttk.Label(frame, name='status', textvariable=status, wraplength=630).pack(anchor='w')
    test_box = ttk.LabelFrame(frame, text='手动测试发送（不受定时发布开关控制）', padding=10)
    test_box.pack(fill='x', pady=(12, 0))
    ttk.Label(test_box, text='测试聊天对象').pack(anchor='w')
    test_row = ttk.Frame(test_box)
    test_row.pack(fill='x', pady=(4, 6))
    test_recipient = tk.StringVar(value=settings['test_recipient'])
    ttk.Entry(test_row, name='test_recipient', textvariable=test_recipient).pack(side='left', fill='x', expand=True)
    ttk.Label(test_box, text='优先使用最新文件；缺少时使用logs/test_push固定样例。先文后图，测试期间请暂停操作微信。', wraplength=610).pack(anchor='w')
    busy = [False]
    events = queue.Queue()
    disabled_controls = []

    def children(widget):
        for child in widget.winfo_children():
            yield child
            yield from children(child)

    def test_send():
        recipient = test_recipient.get().strip()
        if not recipient or len(recipient) > 200 or any(c in recipient for c in '\r\n\t'):
            status.set('请填写测试聊天对象的完整名称。')
            return
        if busy[0]:
            return
        busy[0] = True
        for widget in children(frame):
            if isinstance(widget, (ttk.Button, ttk.Checkbutton, ttk.Entry)):
                disabled_controls.append((widget, widget.state()))
                widget.state(['disabled'])
        status.set('正在查找最新推送文和简报并发送到：' + recipient)
        root.iconify()

        def work():
            try:
                from wechat_test_send import send_latest_test
                result = send_latest_test(HOME, recipient, HOME / settings['uia_profile'])
                events.put(('ok', f'测试已提交：1条推送文、{result["submitted_images"]}张简报图片 → {recipient}。'))
            except Exception as exc:
                events.put(('error', '测试未全部完成：' + str(exc) + '；请核对微信，程序不会自动重发。'))

        def poll():
            try:
                _, message = events.get_nowait()
            except queue.Empty:
                root.after(100, poll)
                return
            busy[0] = False
            for widget, old_state in disabled_controls:
                widget.state(['!disabled'])
                widget.state(old_state)
            disabled_controls.clear()
            root.deiconify()
            status.set(message)

        threading.Thread(target=work, name='wechat-test-send', daemon=True).start()
        root.after(100, poll)

    ttk.Button(test_row, name='test_send', text='测试发送推送文和简报', command=test_send).pack(side='left', padx=(8, 0))

    def cancel():
        if busy[0]:
            status.set('正在测试发送，请等待完成后再关闭。')
            return
        root.destroy()

    root.protocol('WM_DELETE_WINDOW', cancel)
    buttons = ttk.Frame(frame)
    buttons.pack(fill='x', pady=(12, 0))
    result = [2]
    def save():
        try:
            title = clean_title(briefing_title.get())
        except ValueError as exc:
            messagebox.showinfo('请填写简报标题', str(exc))
            return
        targets = [name for name, (selected, _, _) in choices.items() if selected.get()]
        if (publish_text.get() or publish_briefing.get()) and not targets:
            messagebox.showinfo('请选择接收会话', '请至少添加并勾选一个微信群或联系人，或关闭两个定时发布开关。')
            return
        data = {**settings, 'enabled': publish_text.get() or publish_briefing.get(),
                'publish_text': publish_text.get(), 'publish_briefing': publish_briefing.get(),
                'recipient_publications': {name: {'publish_text': text.get(), 'publish_briefing': briefing.get()}
                                           for name, (_, text, briefing) in choices.items()},
                'recipients': targets, 'known_chats': list(choices), 'briefing_title': title, 'test_recipient': test_recipient.get().strip(),
                'prepare_time_beijing': settings['prepare_time_beijing'], 'send_time_beijing': settings['send_time_beijing']}
        try:
            save_settings(data)
        except (OSError, ValueError) as exc:
            messagebox.showerror('设置未保存', '无法保存启动设置：' + str(exc))
            return
        result[0] = 0
        root.destroy()
    ttk.Button(buttons, text='保存并启动', command=save).pack(side='right')
    ttk.Button(buttons, text='取消', command=cancel).pack(side='right', padx=8)
    root.mainloop()
    return result[0]


if __name__ == '__main__':
    sys.exit(main())
