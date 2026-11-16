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
    output = ROOT / 'data/manual-tests' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-hotkey')
    output.mkdir(parents=True)
    report = dict(hotkey=guard.EMERGENCY_HOTKEY, wechat_messages_sent=0)
    process = subprocess.Popen([sys.executable, '-u', str(Path(guard.__file__).resolve()), '--demo'],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                               env=dict(guard.os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8'),
                               creationflags=subprocess.CREATE_NO_WINDOW)
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
        report.update(cancelled=True, exit_code=process.returncode,
                      seconds_after_hotkey=round(time.monotonic() - started, 3),
                      paused=guard.desktop_paused())
        assert not report['paused'] and process.returncode == 125
        with guard.DesktopMutex(timeout=1):
            report['mutex_released'] = True
        guard.require_unpaused()
        report['next_send_allowed'] = True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(report, ensure_ascii=False), flush=True)
        print('REPORT ' + str(output / 'result.json'), flush=True)


if __name__ == '__main__':
    main()
