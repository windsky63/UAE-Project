"""Send two RPA测试群 notices, each after a simulated failure and draft cleanup."""
import argparse
from concurrent.futures import Future
from datetime import datetime, timezone
import json
from pathlib import Path
import queue
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from wechat_local.config import LocalConfig, WechatChat
from wechat_local.delivery import DeliveryNotSent, mentioned_text
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State
from wechat_local.wechat_capture import LocalDatabaseDriver
from wechat_local.wechat_uia import WindowsUIASender, chat_name, read_value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true')
    if not parser.parse_args().send:
        parser.error('--send is required')
    root = Path(__file__).resolve().parents[1]
    output = root / 'data/manual-tests' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-retry')
    output.mkdir(parents=True)
    target, mention = 'RPA测试群', '智能机器人'
    sender = WindowsUIASender(root / 'data/wechat-uia-profile.json')
    report = dict(target=target, mention=mention, simulated_pre_send_failure=True, stages=[])
    class Driver:
        account_id, account_name = 'manual-desktop-test', 'test-bot'
        attempts = 0
        inject_failure = True
        def send(self, chat_id, text, mention_user=None):
            assert chat_id == 'rpa-test' and mention_user == mention
            self.attempts += 1
            if self.inject_failure:
                ui_root, edit = sender.open_chat(target)
                if read_value(edit) != '':
                    raise RuntimeError('Test requires an initially empty editor')
                edit.GetValuePattern().SetValue('codex-wechat 草稿清理与补发测试：此草稿不应发出')
                self.inject_failure = False
                raise DeliveryNotSent('Controlled test: interrupted before send, draft left behind')
            sender.send(target, text, mention=mention)
            ui_root = sender.root()
            edit = sender.input(ui_root)
            assert chat_name(edit.Name) == target and read_value(edit) == ''
            expected = mentioned_text(text, mention)
            found = []
            pending = [(ui_root, 0)]
            while pending:
                node, depth = pending.pop()
                if 'chat_bubble_item_view' in (node.AutomationId or ''):
                    found.append(node.Name or '')
                if depth < 25:
                    pending.extend((child, depth + 1) for child in node.GetChildren())
            if not any(LocalDatabaseDriver.message_matches(label, expected) for label in found):
                raise RuntimeError('Send submitted but test bubble not observed')
    config = LocalConfig(codex_enabled=True, allow_send=True,
                         chats=[WechatChat(id='rpa-test', name=target, kind='group', reply=True)])
    state = State(output / 'state.sqlite3')
    driver = Driver()
    listener = Listener(config, driver, DesktopSpool(str(output / 'spool.sqlite3')), state,
                        SimpleNamespace(progress=queue.Queue()))
    event = dict(id='draft-retry-test', chat_id='rpa-test', sender=mention, sender_id=mention,
                 direction='incoming', content_type='text', historical=False,
                 text='@test-bot 草稿清理与补发测试', observed_at=datetime.now(timezone.utc).isoformat())
    state.enqueue(event)
    try:
        for stage in ('queued', 'thinking'):
            if stage == 'thinking':
                state.set_job(event['id'], 'running')
                listener.active = (event['id'], Future())
            driver.inject_failure = True
            driver.attempts = 0
            job = state.job(event['id'])
            text = (listener.task_identity(job, event) + '任务排序：第1位\n正在排队中……'
                    if stage == 'queued' else listener.task_header(job, event) + '正在思考中……')
            text += '\n（codex-wechat 草稿清理与补发测试）'
            command = dict(id=stage + ':' + event['id'], chat_id=event['chat_id'], text=text, mention_user=mention)
            listener.send_notice(job, stage, command)
            assert driver.attempts == 1 and listener.spool.pending_notices()
            # Reopen the durable spool to verify replay after process-state loss.
            listener.spool.db.close()
            listener.spool = DesktopSpool(str(output / 'spool.sqlite3'))
            time.sleep(config.send_retry_seconds)
            listener.retry_notices()
            assert driver.attempts == 2 and not listener.spool.pending_notices()
            entry = dict(stage=stage, attempts=driver.attempts, draft_cleared=True, bubble_observed=True)
            report['stages'].append(entry)
            print(json.dumps(entry, ensure_ascii=False), flush=True)
    finally:
        listener.close()
        listener.spool.db.close()
        state.close()
        (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('REPORT ' + str(output / 'result.json'), flush=True)


if __name__ == '__main__':
    main()
