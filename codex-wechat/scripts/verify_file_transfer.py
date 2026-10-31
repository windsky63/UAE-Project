"""Explicit local smoke test for 文件传输助手 or RPA测试群 with --send."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wechat_local.listener import Listener
from wechat_local.delivery import mentioned_text
from wechat_local.state import State
from wechat_local.wechat_uia import WindowsUIASender, chat_name, read_value
from wechat_local.wechat_capture import LocalDatabaseDriver


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true')
    parser.add_argument('--target', choices=['文件传输助手', 'RPA测试群'], default='文件传输助手')
    args = parser.parse_args()
    if not args.send:
        parser.error('--send is required to send the three test messages')
    target = args.target
    mention = '智能机器人' if target == 'RPA测试群' else None
    root = Path(__file__).resolve().parents[1]
    output = root / 'data' / 'manual-tests' / datetime.now().strftime('%Y%m%d-%H%M%S')
    output.mkdir(parents=True)
    state = State(output / 'state.sqlite3')
    event = dict(id='desktop-smoke', chat_id='smoke-test', sender=mention or target,
                 text='本机发送流程测试', observed_at=datetime.now(timezone.utc).isoformat())
    state.enqueue(event)
    listener = object.__new__(Listener)
    listener.state = state
    queued = listener.task_identity(state.job(event['id']), event) + '任务排序：第1位\n正在排队中……\n（codex-wechat 本机测试）'
    state.set_job(event['id'], 'running')
    thinking = listener.task_header(state.job(event['id']), event) + '正在思考中……\n（codex-wechat 本机测试）'
    state.set_job(event['id'], 'generated', reply='本机发送流程测试完成。')
    note = 'codex-wechat 群聊原生 @ 本机测试' if mention else '此会话仅验证文字 @ 位置；群原生 @ 需在群聊验证'
    answer = listener.task_header(state.job(event['id']), event, finished=True) + '回答如下：\n本机发送流程测试完成。\n（' + note + '）'
    report = {'target': target, 'mention': mention, 'native_mention_tested': False, 'messages': []}
    sender = WindowsUIASender(root / 'data' / 'wechat-uia-profile.json')
    try:
        for stage, text in [('queued', queued), ('thinking', thinking), ('reply', answer)]:
            started = time.monotonic()
            sender.send(target, text, mention=mention)
            expected = mentioned_text(text, mention) if mention else text
            ui_root = sender.root()
            edit = sender.input(ui_root)
            if edit is None or chat_name(edit.Name) != target or read_value(edit) != '':
                raise RuntimeError('Target or cleared editor could not be verified')
            # Observe the sent bubble, not just an accepted Enter key.
            found = False
            pending = [(ui_root, 0)]
            seen = 0
            while pending and seen < 1500:
                node, depth = pending.pop()
                seen += 1
                label = node.Name or ''
                if LocalDatabaseDriver.message_matches(label, expected):
                    found = True
                    break
                if depth < 25:
                    pending.extend((child, depth + 1) for child in node.GetChildren())
            entry = dict(stage=stage, text=text, seconds=round(time.monotonic() - started, 3),
                         editor_cleared=True, bubble_observed=found)
            report['messages'].append(entry)
            report['native_mention_tested'] = bool(mention)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    finally:
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        state.close()
        print('REPORT ' + str(output / 'result.json'), flush=True)


if __name__ == '__main__':
    main()
