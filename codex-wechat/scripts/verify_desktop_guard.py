"""Explicit concurrent desktop smoke test; sends only marked RPA/filehelper samples."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import threading

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'codex-wechat'))
sys.path.insert(0, str(ROOT / 'FusionLive同步工具'))

from wechat_local.desktop_guard import DesktopMutex, BANNER_WIDTH, BANNER_HEIGHT
from wechat_local.delivery import mentioned_text
from wechat_local.wechat_capture import LocalDatabaseDriver
from wechat_local.wechat_uia import WindowsUIASender
from wechat_push.uia import WindowsUIASender as PushSender


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true')
    if not parser.parse_args().send:
        parser.error('--send is required')
    output = ROOT / 'codex-wechat/data/manual-tests' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-guard')
    output.mkdir(parents=True)
    import ctypes
    ctypes.windll.user32.SetProcessDPIAware()
    report = dict(watchdog_released_input=False, operations=[])
    # The probe deliberately hangs while protected. The independent watchdog
    # must kill it in two seconds, releasing both hooks and the named mutex.
    probe = ("import time; from wechat_local import desktop_guard as g; g.OPERATION_SECONDS=2; "
             "m=g.DesktopMutex(); m.__enter__(); p=g.InputProtection('看门狗恢复测试（2秒）'); "
             "p.__enter__(); print('PROTECTED '+p.mode,flush=True); time.sleep(20)")
    process = subprocess.Popen([sys.executable, '-u', '-c', probe], cwd=ROOT / 'codex-wechat',
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                               creationflags=subprocess.CREATE_NO_WINDOW)
    try:
        line = process.stdout.readline().strip()
        if not line.startswith('PROTECTED '):
            raise RuntimeError('Protection probe did not start: ' + line)
        from PIL import ImageGrab
        import win32api
        width = min(BANNER_WIDTH, win32api.GetSystemMetrics(0))
        x = (win32api.GetSystemMetrics(0) - width) // 2
        ImageGrab.grab(bbox=(x, 0, x + width, BANNER_HEIGHT)).save(output / 'input-protection-banner.png')
        _, error = process.communicate(timeout=8)
        if process.returncode != 124:
            raise RuntimeError('Watchdog did not terminate stuck worker: ' + error)
        with DesktopMutex(timeout=1):
            report['watchdog_released_input'] = True
            report['watchdog_exit_code'] = process.returncode
        print('WATCHDOG released input and mutex', flush=True)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=3)
    from PIL import Image, ImageDraw
    picture = output / 'briefing-test.png'
    image = Image.new('RGB', (700, 200), '#f2f7fb')
    draw = ImageDraw.Draw(image)
    draw.text((24, 35), 'CODEX-WECHAT DESKTOP MUTEX TEST', fill='#172f49', font_size=28)
    draw.text((24, 95), 'Briefing image sample - not a production report', fill='#172f49', font_size=20)
    image.save(picture)
    stamp = datetime.now().strftime('%H:%M:%S')
    codex_text = f'任务编号：测试1\n回复对象：@智能机器人\n回答如下：\n统一桌面锁与键鼠保护并发测试（{stamp}）。'
    push_text = f'【推送文并发测试 {stamp}】\n验证推送文、简报图片与 Codex 回复依次操作微信。此消息为本机测试。'
    barrier = threading.Barrier(2)
    def codex():
        sender = WindowsUIASender(ROOT / 'codex-wechat/data/wechat-uia-profile.json')
        barrier.wait(timeout=10)
        sender.send('RPA测试群', codex_text, mention='智能机器人')
        return [dict(source='codex', kind='text', target='RPA测试群', **sender.last_desktop_operation)]
    def publication():
        sender = PushSender(ROOT / 'FusionLive同步工具/wechat_push/uia_profile.json')
        barrier.wait(timeout=10)
        operations = []
        try:
            sender.send('文件传输助手', push_text)
            operations.append(dict(source='publication', kind='text', target='文件传输助手', **sender.last_desktop_operation))
            sender.send_image('文件传输助手', picture)
            operations.append(dict(source='publication', kind='image', target='文件传输助手', **sender.last_desktop_operation))
            return operations
        finally:
            sender.close()
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(codex), pool.submit(publication)]
            for future in futures:
                report['operations'].extend(future.result(timeout=150))
        intervals = []
        for operation in report['operations']:
            records = operation['records']
            start = next(r['at'] for r in records if r['state'] == 'acquired')
            end = next(r['at'] for r in records if r['state'] == 'released')
            intervals.append((start, end, operation['source'], operation['kind']))
        intervals.sort()
        report['mutex_intervals'] = intervals
        report['no_overlap'] = all(a[1] <= b[0] for a, b in zip(intervals, intervals[1:]))
        assert report['no_overlap']
        observer = WindowsUIASender(ROOT / 'codex-wechat/data/wechat-uia-profile.json')
        observed = []
        for target, expected in [('RPA测试群', mentioned_text(codex_text, '智能机器人')), ('文件传输助手', push_text)]:
            with DesktopMutex(timeout=10):
                ui_root, edit = observer.open_chat(target)
                pending = [(ui_root, 0)]
                labels = []
                while pending:
                    node, depth = pending.pop()
                    if 'chat_bubble_item_view' in (node.AutomationId or ''):
                        labels.append(node.Name or '')
                    if depth < 25:
                        pending.extend((child, depth + 1) for child in node.GetChildren())
                observed.append(dict(target=target, text_bubble_observed=any(
                    LocalDatabaseDriver.message_matches(label, expected) for label in labels)))
                if target == '文件传输助手':
                    from PIL import ImageGrab
                    b = observer.window().BoundingRectangle
                    ImageGrab.grab(bbox=(b.left, b.top, b.right, b.bottom)).save(output / 'filehelper.png')
        report['observations'] = observed
        assert all(entry['text_bubble_observed'] for entry in observed)
        print(json.dumps(dict(no_overlap=True, operations=len(intervals), observations=observed), ensure_ascii=False), flush=True)
    finally:
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('REPORT ' + str(output / 'result.json'), flush=True)


if __name__ == '__main__':
    main()
