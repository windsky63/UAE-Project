"""Shared Windows desktop protocol; identical copy ships with wechat_push.

One session-wide mutex serializes both senders. A disposable worker owns input
protection, clipboard and UIA. A separate watchdog kills a stuck worker, which
also releases Windows input hooks and the abandoned mutex.
"""
import ctypes
from ctypes import wintypes
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import tempfile

MUTEX_NAME = r'Local\ProjectTools.WeChatDesktop.v1'
LOCK_SECONDS = 60
OPERATION_SECONDS = 35
BANNER_WIDTH, BANNER_HEIGHT = 760, 88
EMERGENCY_HOTKEY = 'Ctrl + Shift + F12'
PREFIX = '@@WECHAT_DESKTOP '
ALLOWED_SENDERS = {'wechat_local.wechat_uia', 'wechat_push.uia'}


class DesktopBusy(RuntimeError):
    pass


class DesktopPaused(RuntimeError):
    pass


def pause_file():
    # One durable user-wide latch shared by both independently installed tools.
    base = Path(os.environ.get('LOCALAPPDATA') or tempfile.gettempdir())
    return base / 'ProjectTools' / 'WechatDesktop' / 'paused.json'


def desktop_paused():
    return pause_file().exists()


def pause_desktop():
    path = pause_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'paused-{os.getpid()}.tmp')
    temporary.write_text(json.dumps(dict(reason='emergency-hotkey', pid=os.getpid(), at=time.time())), encoding='utf-8')
    os.replace(temporary, path)


def resume_desktop():
    # Wait for the cancelled worker to exit before accepting sends again.
    with DesktopMutex():
        pause_file().unlink(missing_ok=True)


def require_unpaused():
    if desktop_paused():
        raise DesktopPaused('微信发送已被紧急快捷键暂停；请运行“恢复微信发送.cmd”后再发送')


class EmergencyShortcut:
    """Track only Ctrl/Shift modifiers, since suppressed keys don't update OS state."""
    CTRL = {0x11, 0xA2, 0xA3}
    SHIFT = {0x10, 0xA0, 0xA1}

    def __init__(self, ctrl=False, shift=False):
        self.keys = {False: set(), True: set()}
        if ctrl:
            self.keys[False].add(0x11)
        if shift:
            self.keys[False].add(0x10)

    def feed(self, key, down, injected=False):
        # Automation's Ctrl+V release must not erase a physically held Ctrl.
        keys = self.keys[bool(injected)]
        if key in self.CTRL | self.SHIFT:
            if down:
                keys.add(key)
            else:
                keys.discard(key)
                keys.discard(0x11 if key in self.CTRL else 0x10)
        held = self.keys[False] | self.keys[True]
        return bool(down and key == 0x7B and held & self.CTRL and held & self.SHIFT)


def emergency_cancel():
    # Emergency exit must work even if persisting the pause latch fails.
    try:
        pause_desktop()
    finally:
        try:
            emit('emergency_cancelled', at=time.time(), hotkey=EMERGENCY_HOTKEY, pid=os.getpid())
        finally:
            os._exit(125)


class DesktopMutex:
    def __init__(self, timeout=LOCK_SECONDS):
        self.timeout = timeout
        self.handle = None

    def __enter__(self):
        self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.WaitForSingleObject.restype = wintypes.DWORD
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateMutexW(None, False, MUTEX_NAME)
        if not self.handle:
            raise DesktopBusy('无法创建微信桌面互斥锁')
        result = self.kernel.WaitForSingleObject(self.handle, int(self.timeout * 1000))
        if result not in (0, 0x80):  # WAIT_OBJECT_0 / WAIT_ABANDONED
            self.kernel.CloseHandle(self.handle)
            self.handle = None
            raise DesktopBusy('其他微信任务正在发送，本次等待超时，未操作微信')
        return self

    def __exit__(self, *args):
        if self.handle:
            try:
                self.kernel.ReleaseMutex(self.handle)
            finally:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


def emit(state, **values):
    print(PREFIX + json.dumps(dict(state=state, **values), ensure_ascii=False), flush=True)


class InputProtection:
    def __init__(self, label):
        self.label = label
        self.stop = threading.Event()
        self.ready = threading.Event()
        self.error = None
        self.watchdog = None
        self.thread = None

    def _display_and_hooks(self):
        """Own the banner and hook message pump; never record input content."""
        import win32api
        import win32con
        import win32gui
        hooks, hwnd, font = [], None, None
        user = ctypes.WinDLL('user32', use_last_error=True)
        proc_type = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
        class Keyboard(ctypes.Structure):
            _fields_ = [('vk', wintypes.DWORD), ('scan', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
        class Mouse(ctypes.Structure):
            _fields_ = [('point', wintypes.POINT), ('data', wintypes.DWORD), ('flags', wintypes.DWORD),
                        ('time', wintypes.DWORD), ('extra', ctypes.c_size_t)]
        user.SetWindowsHookExW.argtypes = [ctypes.c_int, proc_type, wintypes.HINSTANCE, wintypes.DWORD]
        user.SetWindowsHookExW.restype = wintypes.HANDLE
        user.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
        user.CallNextHookEx.restype = ctypes.c_ssize_t
        user.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        shortcut = EmergencyShortcut(ctrl=bool(user.GetAsyncKeyState(0x11) & 0x8000),
                                     shift=bool(user.GetAsyncKeyState(0x10) & 0x8000))
        @proc_type
        def keyboard(code, wp, lp):
            if code >= 0:
                event = ctypes.cast(lp, ctypes.POINTER(Keyboard)).contents
                if wp in (0x100, 0x104, 0x101, 0x105) and shortcut.feed(
                        event.vk, wp in (0x100, 0x104), injected=bool(event.flags & 0x10)):
                    emergency_cancel()
                if not event.flags & 0x10:
                    return 1  # Suppress physical input; permit injected automation.
            return user.CallNextHookEx(None, code, wp, lp)
        @proc_type
        def mouse(code, wp, lp):
            if code >= 0 and not ctypes.cast(lp, ctypes.POINTER(Mouse)).contents.flags & 1:
                return 1
            return user.CallNextHookEx(None, code, wp, lp)
        try:
            for kind, callback in ((13, keyboard), (14, mouse)):
                hook = user.SetWindowsHookExW(kind, callback, None, 0)
                if not hook:
                    raise OSError('无法启用键鼠输入保护，未操作微信')
                hooks.append(hook)
            text = ('微信自动发送中 · ' + self.label + '\n'
                    f'{EMERGENCY_HOTKEY}：紧急解除并暂停发送\n'
                    f'完成自动恢复；看门狗最长 {OPERATION_SECONDS} 秒')
            width = min(BANNER_WIDTH, win32api.GetSystemMetrics(0))
            hwnd = win32gui.CreateWindowEx(
                win32con.WS_EX_TOPMOST | win32con.WS_EX_NOACTIVATE | win32con.WS_EX_TOOLWINDOW,
                'STATIC', text, win32con.WS_POPUP | win32con.WS_BORDER | win32con.SS_CENTER,
                (win32api.GetSystemMetrics(0) - width) // 2, 0, width, BANNER_HEIGHT, 0, 0, 0, None)
            font_spec = win32gui.LOGFONT()
            font_spec.lfFaceName, font_spec.lfHeight, font_spec.lfWeight = 'Microsoft YaHei UI', -20, 600
            font = win32gui.CreateFontIndirect(font_spec)
            win32gui.SendMessage(hwnd, win32con.WM_SETFONT, font, True)
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
            win32gui.UpdateWindow(hwnd)
            self.ready.set()
            while not self.stop.wait(.01):
                win32gui.PumpWaitingMessages()
        except BaseException as exc:
            self.error = exc
            self.ready.set()
        finally:
            for hook in hooks:
                user.UnhookWindowsHookEx(hook)
            if hwnd:
                win32gui.DestroyWindow(hwnd)
            if font:
                win32gui.DeleteObject(font)

    def __enter__(self):
        try:
            require_unpaused()
            # Workers are disposable processes; set coordinates before creating
            # a banner so high-DPI desktops do not scale it outside the screen.
            ctypes.windll.user32.SetProcessDPIAware()
            # Independent process, not a timer thread: even if UIA or this
            # interpreter stalls, terminating the worker releases all its hooks.
            self.watchdog = subprocess.Popen(
                [sys.executable, '-u', str(Path(__file__).resolve()), '--watchdog', str(os.getpid()), str(OPERATION_SECONDS)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
            if self.watchdog.stdout.readline().strip() != b'ready':
                raise RuntimeError('微信输入保护看门狗未启动')
            # Always use hooks: BlockInput can swallow an emergency hotkey
            # before our callback sees it, including under administrator tokens.
            self.thread = threading.Thread(target=self._display_and_hooks, daemon=True)
            self.thread.start()
            if not self.ready.wait(3) or self.error:
                raise RuntimeError('无法启用微信操作提示及键鼠保护，未操作微信')
            self.mode = 'physical-input-hooks'
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *args):
        # Ask the hook thread to release input before watchdog cleanup.
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.watchdog:
            if self.watchdog.poll() is None:
                self.watchdog.terminate()
            self.watchdog.wait(timeout=3)
            self.watchdog.stdout.close()


def watchdog(pid, seconds):
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | PROCESS_TERMINATE
    if not handle:
        return
    try:
        print('ready', flush=True)
        if kernel.WaitForSingleObject(handle, int(seconds * 1000)) == 0x102:
            kernel.TerminateProcess(handle, 124)
    finally:
        kernel.CloseHandle(handle)


def parse_output(output):
    if isinstance(output, bytes):
        output = output.decode('utf-8', errors='replace')
    records = []
    for line in (output or '').splitlines():
        if line.startswith(PREFIX):
            try:
                records.append(json.loads(line[len(PREFIX):]))
            except ValueError:
                pass
    return records


def protected_send(sender, operation, name, value, *, mention=None, expected_sha=None, not_sent=RuntimeError, uncertain=RuntimeError):
    if desktop_paused():
        raise not_sent('微信发送已紧急暂停；请运行“恢复微信发送.cmd”后再发送')
    request = dict(module=type(sender).__module__, profile=str(sender.profile_path.resolve()),
                   operation=operation, name=name, value=str(value), mention=mention, expected_sha=expected_sha)
    environment = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
    try:
        process = subprocess.Popen([sys.executable, '-u', str(Path(__file__).resolve()), '--worker'],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, encoding='utf-8', env=environment, creationflags=subprocess.CREATE_NO_WINDOW)
    except OSError as exc:
        raise not_sent('无法启动受保护的微信发送进程：' + type(exc).__name__) from None
    timed_out = False
    try:
        try:
            output, _ = process.communicate(json.dumps(request, ensure_ascii=False), timeout=LOCK_SECONDS + OPERATION_SECONDS + 8)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            output, _ = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
    records = parse_output(output)
    sender.last_desktop_operation = dict(records=records, exit_code=process.returncode, timed_out=timed_out)
    submitted = any(r.get('state') == 'submitting' for r in records)
    emergency = any(r.get('state') == 'emergency_cancelled' for r in records)
    result = next((r for r in reversed(records) if r.get('state') in ('success', 'not_sent', 'uncertain')), None)
    if not timed_out and process.returncode == 0 and result and result['state'] == 'success':
        return result.get('result')
    if emergency:
        # Also persist in the parent, in case the worker could not write its latch.
        pause_desktop()
    detail = ('用户按下紧急快捷键，已解除键鼠保护并暂停微信发送' if emergency else
              result.get('detail') if result else '微信发送进程超时或退出；输入保护已由系统释放')
    if submitted or (result and result['state'] == 'uncertain'):
        raise uncertain(detail + '；已进入提交阶段，请核实聊天记录，不自动重发')
    raise not_sent(detail)


def worker():
    submitted = False
    def before_submit():
        nonlocal submitted
        emit('submitting', at=time.time())
        submitted = True
    try:
        request = json.load(sys.stdin)
        if request['module'] not in ALLOWED_SENDERS or request['operation'] not in ('send', 'send_image', 'send_file'):
            raise ValueError('Unsupported desktop operation')
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        module = importlib.import_module(request['module'])
        with DesktopMutex():
            require_unpaused()
            emit('acquired', at=time.time(), pid=os.getpid())
            with InputProtection('Codex 回复' if request['module'].startswith('wechat_local') else '推送文 / 简报') as protection:
                emit('protected', mode=protection.mode, at=time.time())
                sender = module.WindowsUIASender(request['profile'])
                sender._desktop_worker = True
                sender._before_submit = before_submit
                try:
                    kwargs = {'mention': request['mention']} if request.get('mention') else {}
                    if request['operation'] == 'send_file':
                        kwargs = {'expected_sha': request.get('expected_sha')}
                    result = getattr(sender, request['operation'])(request['name'], request['value'], **kwargs)
                finally:
                    close = getattr(sender, 'close', None)
                    if close:
                        close()
            emit('released', at=time.time())
        emit('success', result=result)
    except BaseException as exc:
        detail = str(exc) if type(exc).__name__ in ('DeliveryNotSent', 'DeliveryUncertain', 'DesktopBusy', 'DesktopPaused', 'RuntimeError') else type(exc).__name__
        emit('uncertain' if submitted else 'not_sent', detail=detail)


if __name__ == '__main__':
    if sys.argv[1:2] == ['--watchdog']:
        watchdog(int(sys.argv[2]), float(sys.argv[3]))
    elif sys.argv[1:] == ['--worker']:
        worker()
    elif sys.argv[1:] == ['--resume']:
        resume_desktop()
        print('微信发送暂停已解除。符合有效期及重试条件的任务可继续；不确定的消息仍需核实。')
    elif sys.argv[1:] == ['--status']:
        print(json.dumps(dict(paused=desktop_paused(), hotkey=EMERGENCY_HOTKEY), ensure_ascii=False))
    elif sys.argv[1:] == ['--demo']:
        with DesktopMutex(timeout=1), InputProtection('紧急快捷键演示'):
            print('DEMO_READY ' + EMERGENCY_HOTKEY, flush=True)
            time.sleep(OPERATION_SECONDS + 20)
