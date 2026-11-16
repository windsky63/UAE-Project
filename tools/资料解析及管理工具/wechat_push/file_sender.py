"""Verified file submission shared with the publication sender."""
from pathlib import Path
import hashlib
import time
from .errors import DeliveryNotSent, DeliveryUncertain
from .uia import find_control, read_value, chat_name

def ui_error_detail(exc):
    return type(exc).__name__

def verify_staged(path, expected):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024), b''):
            digest.update(block)
    if digest.hexdigest()!=expected:
        raise DeliveryNotSent('待发送文件SHA256不符')

class FileSenderMixin:
    def send_file(self, name, path, expected_sha=None):
        if getattr(self, '_protected', False) and not getattr(self, '_desktop_worker', False):
            from .desktop_guard import protected_send
            return protected_send(self, 'send_file', name, path, expected_sha=expected_sha, not_sent=DeliveryNotSent, uncertain=DeliveryUncertain)
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
