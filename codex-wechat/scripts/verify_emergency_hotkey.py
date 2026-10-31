"""Verify Ctrl+Shift+F12 against a real protected demo; never send WeChat."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wechat_local import desktop_guard as guard


def main():
    if guard.desktop_paused():
        raise RuntimeError('Desktop is already paused; do not clear an existing operator pause for this test')
    output = ROOT / 'data/manual-tests' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-hotkey')
    output.mkdir(parents=True)
    report = dict(hotkey=guard.EMERGENCY_HOTKEY, wechat_messages_sent=0)
    process = subprocess.Popen([sys.executable, '-u', str(Path(guard.__file__).resolve()), '--demo'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                               env=dict(guard.os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
                               creationflags=subprocess.CREATE_NO_WINDOW)
    cancelled_pid = None
    try:
        first = process.stdout.readline().strip()
        if not first.startswith('DEMO_READY '):
            raise RuntimeError('Demo protection did not start')
        import ctypes
        import win32api
        import win32con
        from PIL import ImageGrab
        ctypes.windll.user32.SetProcessDPIAware()
        width = min(guard.BANNER_WIDTH, win32api.GetSystemMetrics(0))
        x = (win32api.GetSystemMetrics(0) - width) // 2
        time.sleep(.2)
        ImageGrab.grab(bbox=(x, 0, x + width, guard.BANNER_HEIGHT)).save(output / 'hotkey-banner.png')
        started = time.monotonic()
        try:
            for key in (0x11, 0x10, 0x7B):
                win32api.keybd_event(key, 0, 0, 0)
                time.sleep(.04)
        finally:
            for key in (0x7B, 0x10, 0x11):
                win32api.keybd_event(key, 0, win32con.KEYEVENTF_KEYUP, 0)
        stdout, stderr = process.communicate(timeout=8)
        records = guard.parse_output(stdout)
        event = next(r for r in records if r['state'] == 'emergency_cancelled')
        cancelled_pid = event['pid']
        report.update(cancelled=True, exit_code=process.returncode,
                      seconds_after_hotkey=round(time.monotonic() - started, 3),
                      paused=guard.desktop_paused())
        assert report['paused'] and process.returncode == 125
        with guard.DesktopMutex(timeout=1):
            report['mutex_released'] = True
        try:
            guard.require_unpaused()
        except guard.DesktopPaused:
            report['next_send_prevented'] = True
        assert report.get('next_send_prevented')
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        # Only undo the exact pause created by this automated test.
        if cancelled_pid and guard.desktop_paused():
            marker = json.loads(guard.pause_file().read_text(encoding='utf-8'))
            if marker.get('pid') == cancelled_pid:
                guard.resume_desktop()
                report['test_pause_cleared'] = not guard.desktop_paused()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        print('REPORT ' + str(output / 'result.json'), flush=True)


if __name__ == '__main__':
    main()
