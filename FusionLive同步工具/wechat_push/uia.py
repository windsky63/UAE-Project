"""Send-only adapter extracted from the user's language assistant, plus PNG sending."""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .errors import DeliveryNotSent


def find_control(root, predicate, max_depth=30):
    pending = [(root, 0)]
    while pending:
        node, depth = pending.pop(0)
        if predicate(node): return node
        if depth < max_depth:
            pending.extend((child, depth+1) for child in node.GetChildren())
    return None


def read_value(control):
    for pattern in ('GetValuePattern', 'GetLegacyIAccessiblePattern'):
        try:
            result = getattr(control, pattern)()
            if result is not None: return result.Value
        except Exception:
            pass
    return None


def chat_name(value):
    return re.sub(r'(?:(?:长按|按住)鼠标\s*)?语音输入(?:文字)?$', '', value or '').strip()


def aid(control, name):
    return name in (control.AutomationId or '').split('.')


class WindowsUIASender:
    def __init__(self, profile_path):
        if os.name != 'nt': raise RuntimeError('WeChat sending requires Windows')
        import uiautomation
        self.auto = uiautomation
        self.auto.InitializeUIAutomationInCurrentThread()
        self.auto.SetGlobalSearchTimeout(2)
        self.profile_path = Path(profile_path)
        self._protected = True

    def window(self):
        import psutil
        windows = []
        for node in self.auto.GetRootControl().GetChildren():
            if node.ClassName not in ('Qt51514QWindowIcon', 'mmui::MainWindow'): continue
            if not any(label in (node.Name or '') for label in ('微信', 'Weixin')): continue
            if psutil.Process(node.ProcessId).name().lower() != 'weixin.exe': continue
            windows.append(node)
        if len(windows) != 1: raise DeliveryNotSent('Exactly one logged-in WeChat window is required')
        return windows[0]

    def root(self):
        window = self.window()
        root = self.auto.ControlFromHandle(window.NativeWindowHandle)
        if root.ClassName == 'mmui::MainWindow': return root
        child = find_control(root, lambda n:n.ClassName == 'mmui::MainWindow', 2)
        if child: return child
        self.enable_tree(window)
        root = self.auto.ControlFromHandle(window.NativeWindowHandle)
        child = find_control(root, lambda n:n.ClassName == 'mmui::MainWindow', 2)
        if not child: raise DeliveryNotSent('WeChat UIA controls unavailable')
        return child

    def enable_tree(self, window):
        """Only use a locally verified flag address for an identical DLL binary.

        The independent sender does not import replica's accessibility scanner.
        After a client upgrade this deliberately requires revalidation.
        """
        if not self.profile_path.exists():
            raise DeliveryNotSent('Missing verified WeChat UIA profile')
        profile = json.loads(self.profile_path.read_text(encoding='utf-8'))
        import win32api
        import win32con
        import win32process
        access = win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ | win32con.PROCESS_VM_WRITE | win32con.PROCESS_VM_OPERATION
        process = win32api.OpenProcess(access, False, window.ProcessId)
        try:
            modules = [(base, win32process.GetModuleFileNameEx(process, base))
                       for base in win32process.EnumProcessModules(process)]
            dlls = [(base, Path(path)) for base, path in modules if Path(path).name.lower() == 'weixin.dll']
            if len(dlls) != 1: raise DeliveryNotSent('Cannot identify Weixin.dll')
            base, path = dlls[0]
            if hashlib.sha256(path.read_bytes()).hexdigest() != profile['dll_sha256']:
                raise DeliveryNotSent('WeChat changed; UIA profile needs revalidation')
            address = base + int(profile['gate_rva'])
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            signature = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
            kernel.ReadProcessMemory.argtypes = signature
            kernel.WriteProcessMemory.argtypes = signature
            original, transferred = ctypes.c_ubyte(), ctypes.c_size_t()
            if not kernel.ReadProcessMemory(int(process), address, ctypes.byref(original), 1, ctypes.byref(transferred)) or transferred.value != 1:
                raise DeliveryNotSent('Cannot read UIA flag')
            if original.value not in (0, 1): raise DeliveryNotSent('UIA flag does not match the verified profile')
            enabled = ctypes.c_ubyte(1)
            if not kernel.WriteProcessMemory(int(process), address, ctypes.byref(enabled), 1, ctypes.byref(transferred)) or transferred.value != 1:
                raise DeliveryNotSent('Cannot enable UIA controls')
            success = False
            try:
                for _ in range(10):
                    root = self.auto.ControlFromHandle(window.NativeWindowHandle)
                    if find_control(root, lambda n:n.ClassName == 'mmui::MainWindow', 2):
                        success = True
                        return
                    time.sleep(.2)
                raise DeliveryNotSent('UIA flag did not expose controls')
            finally:
                if not success:
                    kernel.WriteProcessMemory(int(process), address, ctypes.byref(original), 1, ctypes.byref(transferred))
        finally:
            win32api.CloseHandle(process)

    def input(self, root):
        return find_control(root, lambda c:aid(c, 'chat_input_field'))

    def list_chat_names(self):
        """Only explicit session-title controls, never message-list text/previews."""
        root = self.root()
        names = set()
        pending = [(root, 0)]
        while pending:
            node, depth = pending.pop(0)
            if any(aid(node, token) for token in ('session_name', 'session_title', 'conversation_name', 'chat_name')):
                if isinstance(node.Name, str) and node.Name.strip():
                    names.add(node.Name.strip())
                continue
            if depth < 15 and not any(aid(node, token) for token in ('message_list', 'chat_message_list', 'chat_input_field')):
                pending.extend((c, depth + 1) for c in node.GetChildren())
        return sorted(names)

    def open_chat(self, name):
        import win32gui
        root = self.root()
        hwnd = self.window().NativeWindowHandle
        if win32gui.IsIconic(hwnd): win32gui.ShowWindow(hwnd, 9)
        try:
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            pass
        time.sleep(.2)
        if win32gui.GetForegroundWindow() != hwnd:
            # Windows may silently deny a background scheduler foreground permission.
            import win32api
            import win32con
            win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
            win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                raise DeliveryNotSent('Cannot bring WeChat to the foreground') from None
            time.sleep(.2)
        if win32gui.GetForegroundWindow() != hwnd:
            import win32api
            import win32process
            current_thread = win32api.GetCurrentThreadId()
            foreground_thread = win32process.GetWindowThreadProcessId(win32gui.GetForegroundWindow())[0]
            attached = False
            try:
                if foreground_thread != current_thread:
                    attached = bool(ctypes.windll.user32.AttachThreadInput(current_thread, foreground_thread, True))
                win32gui.BringWindowToTop(hwnd)
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                pass
            finally:
                if attached:
                    ctypes.windll.user32.AttachThreadInput(current_thread, foreground_thread, False)
            time.sleep(.2)
            if win32gui.GetForegroundWindow() != hwnd:
                raise DeliveryNotSent('WeChat is not the foreground window')
        time.sleep(.3)
        # Return to the chat tab without typing in any chat input field.
        for _ in range(2):
            search = find_control(root, lambda c:c.ClassName == 'mmui::XValidatorTextEdit' and c.Name == '搜索')
            if search: break
            tab = find_control(root, lambda c:c.ClassName == 'mmui::XTabBarItem' and c.Name == '微信')
            if not tab: raise DeliveryNotSent('Chat navigation unavailable')
            tab.Click()
            time.sleep(.5)
        else: raise DeliveryNotSent('Search control unavailable')
        try:
            pattern = search.GetValuePattern()
            if pattern is None or pattern.IsReadOnly: raise ValueError()
            pattern.SetValue(name)
        except Exception:
            raise DeliveryNotSent('Cannot safely set the search control') from None
        time.sleep(.8)
        result_list = find_control(root, lambda c:aid(c, 'search_list'))
        if not result_list: raise DeliveryNotSent('Search results unavailable')
        results = []
        pending = [result_list]
        while pending:
            node = pending.pop()
            if any(part.startswith('search_item_') for part in (node.AutomationId or '').split('.')):
                if node.Name == name: results.append(node)
            else: pending.extend(node.GetChildren())
        if len(results) != 1: raise DeliveryNotSent('Search result is missing or ambiguous')
        results[0].Click()
        time.sleep(.5)
        edit = self.input(root)
        if edit is None or chat_name(edit.Name) != name:
            raise DeliveryNotSent('Current conversation does not match')
        return root, edit

    def send(self, name, text):
        if getattr(self, '_protected', False) and not getattr(self, '_desktop_worker', False):
            from .desktop_guard import protected_send
            from .errors import DeliveryUncertain
            return protected_send(self, 'send', name, text, not_sent=DeliveryNotSent, uncertain=DeliveryUncertain)
        if not text or len(text) > 16000: raise DeliveryNotSent('Invalid reply text')
        root, edit = self.open_chat(name)
        if read_value(edit) != '': raise DeliveryNotSent('Input draft is nonempty or unreadable')
        import pyperclip
        previous = pyperclip.paste()
        try:
            edit.Click()
            pyperclip.copy(text)
            edit.SendKeys('{Ctrl}v', waitTime=.1)
            time.sleep(.2)
            current = self.input(root)
            got = read_value(edit)
            if current is None or chat_name(current.Name) != name or got is None or got.replace('\r\n','\n') != text.replace('\r\n','\n'):
                raise DeliveryNotSent('Target or input content changed before sending')
            # Any error from here is an uncertain send, never safe to auto-retry.
            callback = getattr(self, '_before_submit', None)
            if callback:
                callback()
            edit.SendKeys('{Enter}', waitTime=.1)
        finally:
            if pyperclip.paste() == text: pyperclip.copy(previous)

    def send_image(self, name, path):
        """Paste bitmap data and verify a dialog or single inline image draft."""
        if getattr(self, '_protected', False) and not getattr(self, '_desktop_worker', False):
            from .desktop_guard import protected_send
            from .errors import DeliveryUncertain
            return protected_send(self, 'send_image', name, path, not_sent=DeliveryNotSent, uncertain=DeliveryUncertain)
        import io
        from PIL import Image
        import win32clipboard
        import win32con
        from .errors import DeliveryUncertain
        path = Path(path)
        if not path.is_file() or path.suffix.lower() != '.png':
            raise DeliveryNotSent('Only an existing briefing PNG may be sent')
        with Image.open(path) as picture:
            payload = io.BytesIO()
            picture.convert('RGB').save(payload, format='BMP')
        dib = payload.getvalue()[14:]
        root, edit = self.open_chat(name)
        if read_value(edit) != '':
            raise DeliveryNotSent('WeChat input contains an existing or unreadable draft')
        previous = None
        win32clipboard.OpenClipboard()
        try:
            if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                previous = win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
            win32clipboard.EmptyClipboard()
            win32clipboard.SetClipboardData(win32con.CF_DIB, dib)
        finally:
            win32clipboard.CloseClipboard()
        try:
            edit.Click()
            edit.SendKeys('{Ctrl}v', waitTime=.1)
            time.sleep(.6)
            candidates = []
            inline = False
            for window in self.auto.GetRootControl().GetChildren():
                if window.ProcessId != root.ProcessId:
                    continue
                label = window.Name or ''
                klass = (window.ClassName or '').lower()
                if not ('图片' in label or '发送' in label or 'sendimage' in klass or 'imagepreview' in klass):
                    continue
                image = find_control(window, lambda c:c.ControlTypeName == 'ImageControl', 10)
                button = find_control(window, lambda c:c.Name == '发送' and c.ControlTypeName == 'ButtonControl', 10)
                if image and button:
                    candidates.append(button)
            if len(candidates) != 1:
                # WeChat 4.1 embeds a pasted bitmap in the chat draft instead
                # of opening a dialog. A single object replacement character
                # after our verified empty draft identifies that inline image.
                candidates = []
                if read_value(edit) == '\ufffc':
                    pending = [(root, 0)]
                    while pending:
                        node, depth = pending.pop(0)
                        if node.ControlTypeName == 'ButtonControl' and node.ClassName == 'mmui::XOutlineButton' and node.Name == '发送':
                            candidates.append(node)
                        if depth < 30 and not any(aid(node, token) for token in ('message_list', 'chat_message_list')):
                            pending.extend((child, depth + 1) for child in node.GetChildren())
                if len(candidates) != 1:
                    raise DeliveryUncertain('Image pasted but the preview/send control could not be verified; inspect WeChat manually')
                inline = True
            current = self.input(root)
            if current is None or chat_name(current.Name) != name:
                raise DeliveryUncertain('WeChat target changed after image paste')
            if inline and read_value(current) != '\ufffc':
                raise DeliveryUncertain('Inline image draft changed before submission')
            win32clipboard.OpenClipboard()
            try:
                if not win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB) or win32clipboard.GetClipboardData(win32con.CF_DIB) != dib:
                    raise DeliveryUncertain('Clipboard image changed before submission')
            finally:
                win32clipboard.CloseClipboard()
            callback = getattr(self, '_before_submit', None)
            if callback:
                callback()
            candidates[0].Click()
            time.sleep(1)
            return {'status': 'submitted', 'kind': 'image'}
        finally:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_DIB) and win32clipboard.GetClipboardData(win32con.CF_DIB) == dib:
                    win32clipboard.EmptyClipboard()
                    if previous is not None:
                        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, previous)
            finally:
                win32clipboard.CloseClipboard()

    def close(self):
        cleanup = getattr(self.auto, 'UninitializeUIAutomationInCurrentThread', None)
        if cleanup:
            cleanup()

