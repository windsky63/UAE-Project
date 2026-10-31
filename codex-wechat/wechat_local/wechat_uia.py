"""Our text sender: strict UIA controls, no vendor sender and no OCR fallback."""
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import time

from wechat_local.delivery import DeliveryNotSent, mention_parts


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


def ui_error_detail(exc):
    """Keep API/error codes, never arbitrary window text or clipboard content."""
    parts = [type(exc).__name__]
    for key in ('winerror', 'hresult', 'errno'):
        value = getattr(exc, key, None)
        if isinstance(value, int):
            parts.append(f'{key}={value}')
    args = getattr(exc, 'args', ())
    if args and isinstance(args[0], int) and not any('=' in item for item in parts):
        parts.append(f'code={args[0]}')
    function = getattr(exc, 'funcname', None)
    if not function and len(args) > 1:
        function = args[1]
    if isinstance(function, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.:]{0,79}', function):
        parts.append('API=' + function)
    return ', '.join(parts)


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

    def activate(self, hwnd):
        import win32gui
        self._stage = '激活微信窗口'
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, 9)
        if win32gui.GetForegroundWindow() != hwnd:
            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                # Windows may report failure even though the requested window became active.
                if win32gui.GetForegroundWindow() != hwnd:
                    raise
        time.sleep(.3)

    def open_chat(self, name):
        hwnd = self.window().NativeWindowHandle
        self.activate(hwnd)
        self._stage = '定位聊天界面'
        root = self.root()
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
            self._stage = '搜索目标会话'
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
        self._stage = '核对目标会话'
        time.sleep(.5)
        edit = self.input(root)
        if edit is None or chat_name(edit.Name) != name:
            raise DeliveryNotSent('Current conversation does not match')
        return root, edit

    @staticmethod
    def list_items(root):
        """Use native UIA list item roles, retaining identity across the popup opening."""
        found = {}
        pending = [(root, 0)]
        while pending:
            node, depth = pending.pop()
            try:
                # Message bubbles are unrelated to the @ selector and change during capture.
                identity = node.AutomationId or ''
                if any(part in identity for part in ('message_list', 'chat_message')):
                    continue
                if node.ControlTypeName == 'ListItemControl' and not node.IsOffscreen:
                    found[tuple(node.GetRuntimeId())] = node
            except Exception:
                continue  # Qt popup controls can disappear between UIA property reads.
            if depth < 30:
                try:
                    pending.extend((child, depth + 1) for child in node.GetChildren())
                except Exception:
                    continue
        return found

    def mention_items(self, root):
        found = self.list_items(root)
        for node in self.auto.GetRootControl().GetChildren():
            try:
                if node.ProcessId == root.ProcessId:
                    found.update(self.list_items(node))
            except Exception:
                continue
        return found

    @staticmethod
    def mention_matches(node, mention):
        try:
            if node.Name == mention:
                return True
            # Some Qt versions expose the name in a child label, not the list row.
            pending = [(child, 0) for child in node.GetChildren()]
            while pending:
                child, depth = pending.pop()
                if child.ControlTypeName == 'TextControl' and child.Name == mention:
                    return True
                if depth < 2:
                    pending.extend((item, depth + 1) for item in child.GetChildren())
        except Exception:
            return False
        return False

    def remember_partial(self, edit, expected):
        context = getattr(self, '_draft_context', None)
        value = read_value(edit)
        if context and value in expected and value:
            self._partial_draft = (*context, value)

    def clear_draft(self, root, edit, name):
        self._stage = '发送前清空目标会话草稿'
        current = self.input(root)
        if current is None or chat_name(current.Name) != name or chat_name(edit.Name) != name:
            raise DeliveryNotSent('清空草稿前目标会话发生变化，未操作输入框')
        pattern = edit.GetValuePattern()
        if pattern is None or pattern.IsReadOnly:
            raise DeliveryNotSent('目标会话草稿无法清空，未提交发送')
        pattern.SetValue('')
        if read_value(edit) != '':
            raise DeliveryNotSent('目标会话草稿未清空，未提交发送')
        self._partial_draft = None
        self._pending_draft = None

    def select_mention(self, root, edit, mention, leading=''):
        self._stage = '采集@候选基线'
        existing = set(self.mention_items(root))
        self._stage = '输入@触发成员选择'
        edit.SendKeys('@', charMode=True, waitTime=.1)
        self.remember_partial(edit, {leading + '@'})
        time.sleep(.2)
        # SendKeys interprets braces even in charMode; member names are data.
        literal = ''.join({'{': '{{}', '}': '{}}'}.get(c, c) for c in mention)
        self._stage = '输入@成员检索名'
        # Control.SendKeys calls SetFocus() each time. Do not move focus back to
        # the chat editor after @ has opened a native member-selection popup.
        current = self.input(root)
        focused = self.auto.GetFocusedControl()
        if current is None or chat_name(current.Name) != chat_name(edit.Name) or focused is None or focused.ProcessId != root.ProcessId:
            raise DeliveryNotSent('输入 @ 名称前焦点或会话已变化，未提交发送')
        self.auto.SendKeys(literal, charMode=True, waitTime=.1)
        self.remember_partial(edit, {leading + '@', leading + '@' + mention})
        for _ in range(10):
            time.sleep(.2)
            current = self.input(root)
            if current is None or chat_name(current.Name) != chat_name(edit.Name):
                raise DeliveryNotSent('选择 @ 成员时当前会话发生变化')
            self._stage = '枚举@成员候选'
            candidates = [node for identity, node in self.mention_items(root).items()
                          if identity not in existing and self.mention_matches(node, mention)]
            if len(candidates) > 1:
                raise DeliveryNotSent('@ 候选成员不唯一')
            if len(candidates) == 1:
                self._stage = '选择@成员'
                candidates[0].Click()
                time.sleep(.2)
                self._stage = '校验原生@结果'
                prefix = read_value(edit)
                # Qt may expose a native mention as an embedded rich-text
                # object (U+FFFC), rather than its rendered @display name.
                # Accept it only here, after clicking the unique exact member
                # candidate, and only at the expected insertion position.
                if prefix and (re.fullmatch(re.escape(leading + '@' + mention) + r'\s+', prefix)
                               or re.fullmatch(re.escape(leading + '\ufffc') + r'\s*', prefix)):
                    self.remember_partial(edit, {prefix})
                    return prefix
                raise DeliveryNotSent('未能确认微信原生 @ 成员选择结果')
        raise DeliveryNotSent('未找到微信原生 @ 成员候选项，请检查成员名称或微信版本')

    def send(self, name, text, mention=None):
        if getattr(self, '_protected', False) and not getattr(self, '_desktop_worker', False):
            from .desktop_guard import protected_send
            return protected_send(self, 'send', name, text, mention=mention, not_sent=DeliveryNotSent)
        submitted = False
        self._stage = '打开目标会话'
        def mark_submitted():
            nonlocal submitted
            callback = getattr(self, '_before_submit', None)
            if callback:
                callback()
            submitted = True
        try:
            if mention:
                return self._send(name, text, mark_submitted, mention)
            return self._send(name, text, mark_submitted)
        except Exception as exc:
            if not submitted:
                if isinstance(exc, DeliveryNotSent):
                    raise DeliveryNotSent(str(exc) + ' [阶段：' + self._stage + ']') from None
                raise DeliveryNotSent('WeChat UI interrupted before Send: ' + ui_error_detail(exc)
                                      + ' [阶段：' + self._stage + ']') from None
            raise

    def _send(self, name, text, mark_submitted, mention=None):
        if not text or len(text) > 16000: raise DeliveryNotSent('Invalid reply text')
        root, edit = self.open_chat(name)
        self._stage = '检查输入框草稿'
        draft = read_value(edit)
        pending = getattr(self, '_pending_draft', None)
        own_draft = bool(pending and pending[:3] == (name, text, mention) and draft == pending[3])
        if draft != '' and not own_draft:
            # The operator explicitly authorized clearing pre-existing drafts
            # in the verified target chat, including edits left by interference.
            self.clear_draft(root, edit, name)
            draft = ''
        self._draft_context = (name, text, mention)
        import pyperclip
        self._stage = '读取剪贴板'
        previous = pyperclip.paste()
        expected = draft if own_draft else text
        clipboard_text = None
        try:
            self._stage = '聚焦聊天输入框'
            edit.Click()
            if not own_draft:
                if mention:
                    leading, trailing = mention_parts(text, mention)
                    if leading:
                        self._stage = '填入@之前的任务信息'
                        clipboard_text = leading
                        pyperclip.copy(clipboard_text)
                        self._partial_draft = (name, text, mention, leading)
                        edit.SendKeys('{Ctrl}v', waitTime=.1)
                        pasted = read_value(edit)
                        if pasted is None or pasted.replace('\r\n', '\n') != leading.replace('\r\n', '\n'):
                            raise DeliveryNotSent('@之前的任务信息未能正确填入')
                        leading = pasted
                        self._partial_draft = (name, text, mention, leading)
                        prefix = self.select_mention(root, edit, mention, leading)
                    else:
                        prefix = self.select_mention(root, edit, mention)
                    expected = prefix + trailing
                else:
                    trailing = text
                self._stage = '填入回复正文'
                clipboard_text = trailing
                pyperclip.copy(clipboard_text)
                self._pending_draft = (name, text, mention, expected)
                edit.SendKeys('{Ctrl}v', waitTime=.1)
            time.sleep(.2)
            self._stage = '发送前核对会话与正文'
            current = self.input(root)
            got = read_value(edit)
            if current is None or chat_name(current.Name) != name or got is None or got.replace('\r\n','\n') != expected.replace('\r\n','\n'):
                raise DeliveryNotSent('Target or input content changed before sending')
            # Any error from here is an uncertain send, never safe to auto-retry.
            mark_submitted()
            self._pending_draft = None
            self._partial_draft = None
            self._stage = '提交发送'
            edit.SendKeys('{Enter}', waitTime=.1)
        finally:
            try:
                if clipboard_text is not None and pyperclip.paste() == clipboard_text:
                    pyperclip.copy(previous)
            except Exception as exc:
                logging.getLogger(__name__).warning('Clipboard restoration failed: %s', ui_error_detail(exc))

    def send_file(self, name, path, expected_sha=None):
        if getattr(self, '_protected', False) and not getattr(self, '_desktop_worker', False):
            from .desktop_guard import protected_send
            return protected_send(self, 'send_file', name, path, expected_sha=expected_sha, not_sent=DeliveryNotSent)
        submitted = False
        def mark():
            nonlocal submitted
            callback = getattr(self, '_before_submit', None)
            if callback:
                callback()
            submitted = True
        try:
            return self._send_file(name, Path(path), expected_sha, mark)
        except Exception as exc:
            if not submitted:
                raise DeliveryNotSent('文件尚未提交：' + ui_error_detail(exc)) from None
            raise

    @staticmethod
    def dropfiles(path):
        import struct
        return struct.pack('<IiiII', 20, 0, 0, 0, 1) + (str(path) + '\0\0').encode('utf-16-le')

    def file_confirmation(self, root, filename, recipient):
        dialogs = []
        for node in self.auto.GetRootControl().GetChildren():
            if node.ProcessId != root.ProcessId or node.ClassName in ('mmui::MainWindow', 'Qt51514QWindowIcon'):
                continue
            if not find_control(node, lambda n: (n.Name or '') == filename, max_depth=12):
                continue
            if not find_control(node, lambda n: (n.Name or '') in (
                    recipient, '发送给：' + recipient, '发送给: ' + recipient, '发送给 ' + recipient), max_depth=12):
                continue
            button = find_control(node, lambda n: n.ControlTypeName == 'ButtonControl' and n.Name in ('发送', '发送(S)', 'Send'), max_depth=12)
            if button:
                dialogs.append(button)
        if len(dialogs) != 1:
            raise RuntimeError('无法唯一确认包含此文件的发送确认窗口；请核对微信，不自动重发')
        return dialogs[0]

    def _send_file(self, name, path, expected_sha, mark_submitted):
        from .file_transfer import verify_staged
        if not expected_sha or not path.is_file() or path.is_symlink():
            raise DeliveryNotSent('待发送文件缺失或没有SHA256')
        verify_staged(path, expected_sha)
        root, edit = self.open_chat(name)
        # A file request never clears or merges an existing user draft.
        if read_value(edit) != '':
            raise DeliveryNotSent('当前会话有草稿，文件未提交')
        import win32clipboard as clipboard
        formats = (13, 8, 15)  # Unicode text, DIB, HDROP.
        previous = {}
        clipboard.OpenClipboard()
        try:
            for fmt in formats:
                if clipboard.IsClipboardFormatAvailable(fmt):
                    previous[fmt] = clipboard.GetClipboardData(fmt)
            clipboard.EmptyClipboard()
            clipboard.SetClipboardData(15, self.dropfiles(path.resolve()))
        finally:
            clipboard.CloseClipboard()
        try:
            edit.Click()
            current = self.input(root)
            if current is None or chat_name(current.Name) != name or read_value(current) != '':
                raise DeliveryNotSent('发送前目标会话或草稿已变化')
            # Pasting can itself submit on some client versions. From here any
            # failure is uncertain and must never be automatically retried.
            mark_submitted()
            edit.SendKeys('{Ctrl}v', waitTime=.3)
            time.sleep(.3)
            self.submit_pasted_file(root, path.name, name)
        finally:
            clipboard.OpenClipboard()
            try:
                if clipboard.IsClipboardFormatAvailable(15) and tuple(clipboard.GetClipboardData(15)) == (str(path.resolve()),):
                    clipboard.EmptyClipboard()
                    for fmt, value in previous.items():
                        if fmt == 15:
                            import struct
                            value = struct.pack('<IiiII', 20, 0, 0, 0, 1) + ('\0'.join(value) + '\0\0').encode('utf-16-le')
                        clipboard.SetClipboardData(fmt, value)
            finally:
                clipboard.CloseClipboard()

    def submit_pasted_file(self, root, filename, recipient):
        """Finish only our paste into a previously empty, protected editor.

        Weixin 4.x can insert a single attachment object directly into the
        editor instead of opening a separate recipient confirmation dialog.
        The caller must hold input protection and have checked the empty draft
        before pasting; an existing user draft must never enter this path.
        """
        current = self.input(root)
        if current is None or chat_name(current.Name) != recipient:
            raise RuntimeError('文件确认时目标会话已变化')
        try:
            button = self.file_confirmation(root, filename, recipient)
        except RuntimeError:
            # One U+FFFC is the client's single inline attachment object. No
            # text, multiple objects, or unknown editor state may be submitted.
            if read_value(current) != '\ufffc':
                raise
            current.SendKeys('{Enter}', waitTime=.3)
        else:
            button.Click()
