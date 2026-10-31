"""Explicit local verification; the sole allowed target is File Transfer Assistant."""
import sys
import time
from pathlib import Path

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME/'app'))
from wechat_push.uia import WindowsUIASender, chat_name, read_value

TARGET = '\u6587\u4ef6\u4f20\u8f93\u52a9\u624b'


def main():
    if '--gui-test-send' in sys.argv:
        import tempfile
        import tkinter as tk
        import json
        from datetime import datetime
        from briefing import LOCAL, generate_pending_notice, atomic_json
        from push_settings import DEFAULT, load_settings, save_settings
        import push_setup
        import hashlib
        original_home, original_load = push_setup.HOME, push_setup.load_settings
        original_loop = tk.Tk.mainloop
        errors = []
        now = datetime.now(LOCAL)
        with tempfile.TemporaryDirectory(prefix='fusion-ui-test-send-') as temporary:
            base = Path(temporary)
            day = now.date().isoformat()
            folder = base / 'logs' / day
            generate_pending_notice(folder / '管道简报', day, '同步工具测试 · 按钮功能验证')
            content = '【按钮功能验证】\n验证启动设置中的测试按钮：先发本条推送文，再发简报图片；不含模拟业务数量。'.encode('utf-8-sig')
            (folder / '微信推送文.txt').write_bytes(content)
            atomic_json(folder / '推送状态.json', dict(date=day, generated_at=datetime.now(LOCAL).isoformat(),
                text_file='微信推送文.txt', text_sha256=hashlib.sha256(content).hexdigest(), ready=True))
            save_settings({**DEFAULT, 'publish_text': False, 'publish_briefing': False,
                'test_recipient': TARGET, 'uia_profile': str(HOME / DEFAULT['uia_profile'])}, base)
            push_setup.HOME = base
            push_setup.load_settings = lambda:load_settings(base)
            def verify(root, *args, **kwargs):
                def descendants(widget):
                    yield widget
                    for child in widget.winfo_children():
                        yield from descendants(child)
                controls = list(descendants(root))
                button = next(w for w in controls if w.winfo_name() == 'test_send')
                label = next(w for w in controls if w.winfo_name() == 'status')
                def finish():
                    if 'disabled' in button.state():
                        root.after(250, finish)
                        return
                    message = root.globalgetvar(label.cget('textvariable'))
                    if not message.startswith('测试已提交'):
                        errors.append(message)
                    root.destroy()
                def click():
                    button.invoke()
                    root.after(250, finish)
                root.after(700, click)
                return original_loop(root, *args, **kwargs)
            tk.Tk.mainloop = verify
            try:
                assert push_setup.main() == 2
                assert not errors, errors
                result = json.loads((base / 'runtime/wechat_test/latest_result.json').read_text(encoding='utf-8'))
                assert result['state'] == 'submitted' and result['submitted_text'] and result['submitted_images'] == 1, result
                print('REAL_GUI_BUTTON_SENT_LATEST_TEXT_THEN_IMAGE_TO_FILE_TRANSFER_ASSISTANT_WITH_BOTH_SCHEDULE_SWITCHES_OFF')
            finally:
                tk.Tk.mainloop = original_loop
                push_setup.HOME, push_setup.load_settings = original_home, original_load
        return
    if '--gui-persistence' in sys.argv:
        import tempfile
        import tkinter as tk
        from tkinter import ttk
        import push_setup
        from push_settings import load_settings, save_settings
        original_loop = tk.Tk.mainloop
        original_load, original_save = push_setup.load_settings, push_setup.save_settings
        errors = []
        phase = [0]
        test_title = '\u9879\u76ee\u7ba1\u9053\u7b80\u62a5'
        with tempfile.TemporaryDirectory(prefix='fusion-settings-verify-') as temporary:
            push_setup.load_settings = lambda:load_settings(temporary)
            push_setup.save_settings = lambda data:save_settings(data, temporary)
            def verify(root, *args, **kwargs):
                def check():
                    try:
                        def descendants(widget):
                            yield widget
                            for child in widget.winfo_children():
                                yield from descendants(child)
                        widgets = list(descendants(root))
                        assert not any(isinstance(w, ttk.Button) and w.cget('text') == '加载微信会话' for w in widgets)
                        title = next(w for w in widgets if w.winfo_name() == 'briefing_title')
                        if phase[0] == 0:
                            title.delete(0, 'end')
                            title.insert(0, test_title)
                            entry = next(w for w in widgets if w.winfo_name() == 'chat_name')
                            entry.insert(0, TARGET)
                            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget('text') == '添加会话').invoke()
                            next(w for w in widgets if w.winfo_name() == 'publish_briefing').invoke()
                            next(w for w in widgets if isinstance(w, ttk.Button) and w.cget('text') == '保存并启动').invoke()
                        else:
                            assert title.get() == test_title
                            target = next(w for w in descendants(root) if isinstance(w, ttk.Checkbutton) and w.cget('text') == TARGET)
                            assert 'selected' in target.state()
                            assert 'selected' in next(w for w in widgets if w.winfo_name() == 'publish_text').state()
                            assert 'selected' not in next(w for w in widgets if w.winfo_name() == 'publish_briefing').state()
                            root.destroy()
                    except Exception as exc:
                        errors.append(exc)
                        root.destroy()
                root.after(700, check)
                return original_loop(root, *args, **kwargs)
            tk.Tk.mainloop = verify
            try:
                assert push_setup.main() == 0
                phase[0] = 1
                assert push_setup.main() == 2
                assert not errors, errors
                assert load_settings(temporary)['briefing_title'] == test_title
                assert load_settings(temporary)['publish_text'] is True
                assert load_settings(temporary)['publish_briefing'] is False
                print('REAL_GUI_TITLE_TARGET_AND_TWO_PUBLICATION_SWITCHES_SAVED_AND_RESTORED_NO_TASKS_STARTED')
            finally:
                tk.Tk.mainloop = original_loop
                push_setup.load_settings, push_setup.save_settings = original_load, original_save
        return
    if '--sequence' in sys.argv:
        import tempfile
        import json
        from datetime import datetime, timedelta
        from briefing import LOCAL, generate_pending_notice, atomic_json
        from wechat_delivery import deliver, push_text
        from push_settings import DEFAULT
        now = datetime.now(LOCAL)
        test_at = now + timedelta(minutes=1)
        with tempfile.TemporaryDirectory(prefix='fusion-wechat-order-') as temporary:
            base = Path(temporary)
            folder = base / 'logs' / now.date().isoformat() / '管道简报'
            state = generate_pending_notice(folder, now.date().isoformat())
            state['wechat_push_text'] = '【同步工具测试】\n验证发送顺序：先推送文，后简报图片。\n本次仅测试待分析通知，不含模拟业务数量。\n\n' + push_text(state)
            marker = folder / '简报推送状态.json'
            atomic_json(marker, state)
            settings = {**DEFAULT, 'recipients': [TARGET], 'send_time_beijing': test_at.strftime('%H:%M')}
            result = deliver(base, marker, settings, test_at,
                sender_factory=lambda:WindowsUIASender(HOME / DEFAULT['uia_profile']))
            print(json.dumps(result))
            assert result['complete'], result
        print('REAL_TEXT_THEN_BRIEFING_IMAGE_VERIFIED_TEMP_FILES_REMOVED')
        return
    if '--gui' in sys.argv:
        import tkinter as tk
        from tkinter import ttk
        import push_setup
        original = tk.Tk.mainloop

        def verify(root, *args, **kwargs):
            def check():
                def descendants(widget):
                    yield widget
                    for child in widget.winfo_children():
                        yield from descendants(child)
                widgets = list(descendants(root))
                entry = next(w for w in widgets if isinstance(w, ttk.Entry) and w.winfo_name() == 'chat_name')
                entry.insert(0, TARGET)
                next(w for w in widgets if isinstance(w, ttk.Button) and w.cget('text') == '\u6dfb\u52a0\u4f1a\u8bdd').invoke()
                assert any(isinstance(w, ttk.Checkbutton) and w.cget('text') == TARGET for w in descendants(root))
                print('REAL_GUI_RENDERED_TARGET_ADDED')
                root.after(1200, root.destroy)
            root.after(500, check)
            return original(root, *args, **kwargs)
        tk.Tk.mainloop = verify
        try:
            assert push_setup.main() == 2  # Close without saving or starting tasks.
            print('GUI_CANCELLED_WITHOUT_STARTING_TASKS')
        finally:
            tk.Tk.mainloop = original
        return
    import win32api
    import win32con
    import win32gui
    sender = WindowsUIASender(HOME / 'wechat_push/uia_profile.json')
    try:
        hwnd = sender.window().NativeWindowHandle
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)
        root, edit = sender.open_chat(TARGET)
        print(ascii({'target': chat_name(edit.Name), 'draft_empty': read_value(edit) == ''}))
        if '--text' in sys.argv:
            sender.send(TARGET, '\u3010\u540c\u6b65\u5de5\u5177\u6d4b\u8bd5\u3011\u5fae\u4fe1\u6587\u5b57\u53d1\u9001\u9a8c\u8bc1 ' + time.strftime('%Y-%m-%d %H:%M:%S'))
            print('TEXT_SUBMITTED')
        if '--image' in sys.argv:
            import tempfile
            from PIL import Image, ImageDraw, ImageFont
            with tempfile.TemporaryDirectory(prefix='fusion-wechat-verify-') as folder:
                path = Path(folder) / 'verification.png'
                picture = Image.new('RGB', (1100, 380), '#eff5f8')
                draw = ImageDraw.Draw(picture)
                font = ImageFont.truetype('C:/Windows/Fonts/msyh.ttc', 38)
                draw.rectangle((0, 0, 1100, 95), fill='#104e5d')
                draw.text((35, 20), '\u540c\u6b65\u5de5\u5177\u6d4b\u8bd5', font=font, fill='white')
                draw.text((35, 135), '\u7ba1\u9053\u7b80\u62a5\u56fe\u7247\u53d1\u9001\u9a8c\u8bc1', font=font, fill='#172438')
                draw.text((35, 210), time.strftime('%Y-%m-%d %H:%M:%S'), font=font, fill='#172438')
                draw.text((35, 285), '\u4ec5\u7528\u4e8e\u6587\u4ef6\u4f20\u8f93\u52a9\u624b\u672c\u673a\u6d4b\u8bd5', font=font, fill='#172438')
                picture.save(path)
                try:
                    print(sender.send_image(TARGET, path))
                except Exception:
                    for window in sender.auto.GetRootControl().GetChildren():
                        if window.ProcessId != root.ProcessId:
                            continue
                        pending = [(window, 0)]
                        while pending:
                            node, depth = pending.pop(0)
                            if depth > 7:
                                continue
                            if any(token in (node.AutomationId or '') for token in ('message_list', 'chat_message')):
                                continue
                            print(ascii((depth, node.ControlTypeName, node.ClassName, node.AutomationId, node.Name[:160])))
                            pending.extend((child, depth + 1) for child in node.GetChildren())
                    raise
    finally:
        sender.close()


if __name__ == '__main__':
    main()
