"""Explicit filehelper-only live attachment verification; artifacts stay isolated."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wechat_local.wechat_uia import WindowsUIASender, find_control, read_value, chat_name


def save(report, output):
    (output / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')


def observe(sender, filename, output, label):
    root = sender.root()
    edit = sender.input(root)
    found = find_control(root, lambda n: filename in (n.Name or ''), max_depth=30)
    result = dict(target=chat_name(edit.Name) if edit else None,
                  editor_empty=read_value(edit) == '' if edit else False,
                  filename_observed=found is not None)
    from PIL import ImageGrab
    ImageGrab.grab().save(output / (label + '.png'))
    return result


def codex_test(output, report, retry_failed=False):
    import sqlite3
    from wechat_local.config import load_config, WechatChat, SyncSource
    from wechat_local.wechat_capture import LocalDatabaseDriver
    from wechat_local.codex_runner import CodexRunner
    from wechat_local.listener import Listener
    from wechat_local.state import State
    from wechat_local.spool import DesktopSpool

    config = load_config(ROOT / 'wechat.codex.json')
    fixture = output / 'test-project'
    (fixture / 'downloads').mkdir(parents=True, exist_ok=True)
    (fixture / 'runtime').mkdir(exist_ok=True)
    path = fixture / 'downloads/Codex聊天确认发送测试.txt'
    if not path.exists():
        path.write_text('本文件用于真实 Codex 判断用户确认后发送附件的本机测试。\n'
                        '目标仅限文件传输助手。\n' + datetime.now().isoformat(), encoding='utf-8')
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(fixture / 'runtime/ledger.sqlite3') as db:
        db.execute('CREATE TABLE IF NOT EXISTS files(project,id,filename,path,bytes,sha256,status,active)')
        db.execute('DELETE FROM files')
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                   ('live-test', 'attachment-one', path.name, 'downloads/' + path.name,
                    path.stat().st_size, sha, 'done', 1))
    chat = WechatChat(id='attachment-live', name='文件传输助手', username='filehelper',
                      kind='private', reply=True, sync_sources=['attachment-fixture'])
    config.chats = [chat]
    config.sync_sources = [SyncSource(id='attachment-fixture', root=str(fixture), projects=['live-test'])]
    config.codex_auto_access_dirs = False
    config.codex_access_dirs = [str(fixture)]
    config.database_cache = str(output / 'db-cache')
    config.workspaces = str(output / 'chats')
    config.state_path = str(output / 'listener.sqlite3')
    config.query_progress_enabled = False
    config.allow_send = config.file_send_enabled = config.codex_enabled = True
    driver = LocalDatabaseDriver(config)
    sender = WindowsUIASender(ROOT / 'data/wechat-uia-profile.json')
    driver.sender = sender
    runner = CodexRunner(config)
    state = State(config.state_path)
    spool = DesktopSpool(str(output / 'spool.sqlite3'))
    listener = Listener(config, driver, spool, state, runner)
    report['confirmation_test_boundary'] = (
        '真实微信文件传输助手自聊文字先经本地消息库确认，再在隔离测试库中作为 incoming 用户事件接入；'
        '真实 CodexRunner、模型、文件选择、校验、Listener、DesktopSpool、LocalDatabaseDriver 和微信UI发送均未模拟。'
        '未验证另一个微信账号的入站采集。')
    try:
        for index, text in enumerate([
            '【附件实发测试】请先帮我查找 Codex聊天确认发送测试.txt，告诉我找到的文件，先不要发送。',
            '【附件实发测试】确认，请把刚才找到的那一份文件现在发送到当前聊天。',
        ], 1):
            entry = dict(stage='codex_search' if index == 1 else 'codex_confirm_send', user_text=text)
            report['stages'].append(entry)
            save(report, output)
            print('USER_MESSAGE ' + entry['stage'], flush=True)
            identifier = 'attachment-live-user-' + str(index)
            previous = state.job(identifier)
            if previous is not None and previous['status'] == 'confirmed':
                entry['already_confirmed_no_replay'] = True
                save(report, output)
                continue
            if previous is not None and (not retry_failed or previous['status'] != 'failed'):
                raise RuntimeError('已有测试任务；必须先检查报告，禁止重复提交')
            began = time.time()
            if previous is None:
                try:
                    driver.send(chat.id, text)
                except RuntimeError:
                    # Submission may precede the database write. Never resubmit.
                    pass
            else:
                began = datetime.fromisoformat(previous['created_at']).timestamp() - 60
                entry['explicit_failed_generation_retry'] = True
            for _ in range(30):
                if driver.delivery_confirmed(chat.id, text, began):
                    entry['user_text_db_confirmed'] = True
                    break
                time.sleep(1)
            else:
                raise RuntimeError('测试用户文字尚未在微信数据库确认；不启动模型')
            event = dict(id=identifier, chat_id=chat.id,
                         sender=driver.account_id, sender_id=driver.account_id, text=text,
                         direction='incoming', content_type='text', historical=False,
                         observed_at=datetime.now(timezone.utc).isoformat())
            state.archive(event)
            state.enqueue(event)
            if previous is not None:
                state.set_job(identifier, 'pending')
            listener.start_next()
            future = listener.active[1]
            started = time.monotonic()
            last_notice = started
            while listener.active:
                if time.monotonic() - last_notice >= 20:
                    print('MODEL_WAIT ' + json.dumps(dict(stage=entry['stage'],
                          elapsed=round(time.monotonic()-started)), ensure_ascii=False), flush=True)
                    last_notice = time.monotonic()
                listener.finish_active()
                time.sleep(.2)
            row = state.job(event['id'])
            if future.exception() is not None:
                import traceback
                exc = future.exception()
                entry['exception_detail'] = str(exc)[:1500]
                entry['exception_locations'] = [dict(file=Path(f.filename).name, line=f.lineno, function=f.name)
                    for f in traceback.extract_tb(exc.__traceback__)]
            entry.update(model_status=row['status'], error=row['error'], reply=row['reply'],
                         model_seconds=round(time.monotonic()-started, 3))
            if row['status'] != 'generated':
                raise RuntimeError('Codex生成失败：' + str(row['error']))
            payload = json.loads(row['file_result'] or '{}')
            entry['file_result_kind'] = payload.get('kind')
            expected = 'search' if index == 1 else 'send'
            if payload.get('kind') != expected:
                raise RuntimeError('真实模型未提出预期操作：' + str(payload.get('kind')))
            if index == 1 and len(payload.get('candidates', [])) != 1:
                raise RuntimeError('模型未返回唯一有效文件候选')
            for _ in range(30):
                listener.deliver()
                row = state.job(event['id'])
                if row['status'] == 'confirmed':
                    break
                time.sleep(1)
            entry['delivery_status'] = row['status']
            entry['delivery_detail'] = row['error']
            if row['status'] != 'confirmed':
                raise RuntimeError('发送未确认：' + str(row['status']))
            if index == 1:
                selection = state.file_selection(chat.id, driver.account_id)
                entry['saved_selection_count'] = len((selection or {}).get('candidates', []))
                if entry['saved_selection_count'] != 1:
                    raise RuntimeError('未保存唯一候选，不能继续确认发送')
            else:
                entry.update(filename=path.name, sha256=sha, bytes=path.stat().st_size,
                             file_db_confirmed=driver.file_delivery_confirmed(chat.id, path.name, path.stat().st_size, began),
                             observation=observe(sender, path.name, output, 'codex-confirm-after'))
                # A second deliver call verifies deduplication without a second UI submission.
                listener.deliver()
                entry['send_attempts'] = spool.db.execute(
                    'SELECT attempts FROM ui_actions WHERE id=?', ('file:' + event['id'],)).fetchone()[0]
            save(report, output)
            print('STAGE_DONE ' + json.dumps(entry, ensure_ascii=False), flush=True)
        report.pop('codex_error', None)
    except Exception as exc:
        report['codex_error'] = dict(type=type(exc).__name__, detail=str(exc))
        save(report, output)
        raise
    finally:
        listener.close()
        state.close()
        spool.db.close()
        save(report, output)


def verify_results(output, report):
    import xml.etree.ElementTree as ET
    from wechat_local.config import load_config, WechatChat
    from wechat_local.wechat_capture import LocalDatabaseDriver
    config = load_config(ROOT / 'wechat.codex.json')
    config.database_cache = str(output / 'db-cache')
    config.chats = [WechatChat(id='attachment-live', name='文件传输助手', username='filehelper', kind='private', reply=True)]
    driver = LocalDatabaseDriver(config)
    sender = WindowsUIASender(ROOT / 'data/wechat-uia-profile.json')
    rows = driver.reader.recent('filehelper')
    result = []
    for path in [output / '普通附件发送测试.txt', output / 'test-project/downloads/Codex聊天确认发送测试.txt']:
        matches = [row for row in rows if row.get('sender_username') == driver.account_id
                   and driver.file_matches(row.get('content'), path.name, path.stat().st_size)]
        uploaded = []
        for row in matches:
            app = ET.fromstring(row['content']).find('.//appmsg')
            uploaded.append(bool(app.findtext('appattach/cdnattachurl') or app.findtext('appattach/attachid')))
        result.append(dict(filename=path.name, bytes=path.stat().st_size,
                           matching_local_messages=len(matches), uploaded_metadata_present=uploaded))
    report['final_db_verification'] = result
    report['final_verification_time'] = datetime.now(timezone.utc).isoformat()
    report['final_observation'] = observe(sender, 'Codex聊天确认发送测试.txt', output, 'codex-confirm-final')
    report['attachments_verified'] = all(r['matching_local_messages'] == 1 and all(r['uploaded_metadata_present']) for r in result)
    if report['attachments_verified'] and 'codex_error' in report:
        report['prior_codex_error'] = report.pop('codex_error')
    save(report, output)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--send', action='store_true', required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--resume-ordinary', action='store_true')
    parser.add_argument('--codex', action='store_true')
    parser.add_argument('--retry-failed', action='store_true')
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    output = args.output or ROOT / 'data/manual-tests' / (datetime.now().strftime('%Y%m%d-%H%M%S') + '-attachments')
    output.mkdir(parents=True, exist_ok=True)
    report = json.loads((output / 'result.json').read_text(encoding='utf-8')) if args.resume_ordinary or args.codex or args.verify_only else dict(target='文件传输助手', output=str(output), stages=[])
    if args.verify_only:
        verify_results(output.resolve(), report)
        return
    if args.codex:
        codex_test(output.resolve(), report, args.retry_failed)
        print('REPORT ' + str(output / 'result.json'), flush=True)
        return
    save(report, output)
    path = output / '普通附件发送测试.txt'
    if not args.resume_ordinary:
        path.write_text('本文件用于本地微信普通附件实发测试。\n' + datetime.now().isoformat(), encoding='utf-8')
    sender = WindowsUIASender(ROOT / 'data/wechat-uia-profile.json')
    entry = dict(stage='ordinary_attachment', filename=path.name,
                 sha256=hashlib.sha256(path.read_bytes()).hexdigest(), bytes=path.stat().st_size)
    report['stages'].append(entry)
    try:
        if args.resume_ordinary:
            # The previous report and screenshot identify this exact test draft.
            # Resume it once; never repaste an uncertain attachment.
            from wechat_local.desktop_guard import DesktopMutex, InputProtection
            with DesktopMutex(timeout=5):
                with InputProtection('普通附件测试：提交已核实的测试草稿'):
                    root = sender.root()
                    edit = sender.input(root)
                    if chat_name(edit.Name) != '文件传输助手' or read_value(edit) != '\ufffc':
                        raise RuntimeError('测试草稿已变化，停止提交')
                    sender.submit_pasted_file(root, path.name, '文件传输助手')
        else:
            sender.send_file('文件传输助手', path, entry['sha256'])
        time.sleep(1)
        entry['send_returned'] = True
    except Exception as exc:
        entry['error_type'] = type(exc).__name__
        entry['error'] = str(exc)
    finally:
        entry['desktop_operation'] = getattr(sender, 'last_desktop_operation', None)
        entry['observation'] = observe(sender, path.name, output, 'ordinary-after')
        save(report, output)
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
