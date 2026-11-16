from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import pytest

from ima_manager.daily import JOURNAL_NAME, record, record_batch, record_collection
from ima_manager.sync import scan


def test_activity_log_merges_successes_preserves_last_path_and_flushes_before_error():
    from ima_manager.watch import ActivityLog
    messages=[]
    activity=ActivityLog(messages.append,interval=60)
    try:
        activity.phase('资料提取')
        activity.notify('资料提取：first.pdf — copied')
        activity.notify('资料提取：second.pdf — copied')
        activity.notify('资料提取：last.pdf — copied')
        activity.notify('资料提取：broken.pdf — error：无法读取')
        merged=next(message for message in messages if '合并 2 条成功结果' in message)
        assert 'last.pdf' in merged and 'broken.pdf' not in merged
        errors=[message for message in messages if '[ERROR]' in message]
        assert len(errors)==1 and 'broken.pdf' in errors[0]
        assert messages.index(merged)<messages.index(errors[0])
        assert all('+08:00' in message for message in messages)
    finally:activity.close()
    assert not activity.worker.is_alive()


def test_activity_log_never_labels_zero_failure_counts_as_error():
    from ima_manager.watch import ActivityLog
    messages=[]
    activity=ActivityLog(messages.append,interval=60)
    try:
        activity.notify('资料提取完成：失败 0 项，冲突 0 项')
        assert '[INFO]' in messages[-1]
        activity.notify('资料提取：broken.pdf — failed')
        activity.notify('资料提取：broken.pdf — failed')
        assert sum('[ERROR]' in message for message in messages)==2
    finally:activity.close()


def test_activity_log_redactor_failure_does_not_abort_business_or_echo_original():
    from ima_manager.watch import ActivityLog
    messages=[]
    activity=ActivityLog(messages.append,redact=lambda text:object(),interval=60)
    try:
        activity.notify('password=must-not-leak')
        assert messages and all('must-not-leak' not in message for message in messages)
        assert '原文未输出' in messages[-1]
    finally:activity.close()


def test_daily_schedule_persists_and_uses_beijing_date(tmp_path):
    from ima_manager.watch import claim_daily_sync
    before = datetime(2026, 11, 6, 4, 59, tzinfo=timezone.utc)
    assert not claim_daily_sync(tmp_path, scheduled=True, at=before)
    assert not (tmp_path / 'daily-sync.sqlite3').exists()
    due = before + timedelta(minutes=1)
    assert claim_daily_sync(tmp_path, scheduled=True, at=due)
    assert not claim_daily_sync(Path(str(tmp_path)), at=due + timedelta(hours=10))
    assert not claim_daily_sync(tmp_path, at=due - timedelta(days=1))
    assert not claim_daily_sync(tmp_path, scheduled=True, at=due + timedelta(hours=12))
    assert claim_daily_sync(tmp_path, scheduled=True, at=due + timedelta(days=1))


def test_automatic_sync_concurrent_claims_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from ima_manager.watch import claim_daily_sync
    at = datetime(2026, 11, 6, 0, 0, tzinfo=timezone.utc)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: claim_daily_sync(tmp_path, scheduled=True, sync_time='00:00', at=at), range(8)))
    assert sum(results) == 1
    assert not claim_daily_sync(tmp_path, scheduled=True, at=at + timedelta(hours=5))


def test_watch_rechecks_persisted_switch_before_first_iteration(tmp_path, monkeypatch):
    import threading
    from ima_manager import config
    from ima_manager.api import ImaError
    from ima_manager.watch import watch_session
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    config.save_settings(dict(config.load_settings(), auto_sync_enabled=False))
    with pytest.raises(ImaError, match='自动同步已关闭'):
        with watch_session(tmp_path, threading.Event()):
            pytest.fail('disabled watch must not enter its first iteration')


def test_manual_sync_stop_monitor_works_while_automatic_sync_is_disabled(tmp_path, monkeypatch):
    import threading
    from ima_manager import config
    from ima_manager.watch import watch_session
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    config.save_settings(dict(config.load_settings(), auto_sync_enabled=False))
    stopped = threading.Event()
    with watch_session(tmp_path, stopped, automatic=False):
        assert not stopped.is_set()
        (tmp_path / 'watch-stop.request').write_text('stop', encoding='ascii')
        assert stopped.wait(3)


def test_journal_is_compact_daily_and_excluded_from_uploads(tmp_path):
    at = datetime(2026, 11, 3, 16, 30, tzinfo=timezone(timedelta(hours=8)))
    assert record(tmp_path, '云端更新', '项目管理规定/a.pdf', '成功', old_version='a'*64,
                  version='b'*64, target='kb/根目录', at=at)
    assert not record(tmp_path, '云端更新', '项目管理规定/a.pdf', '成功', old_version='a'*64,
                      version='b'*64, target='kb/根目录', at=at)
    assert record(tmp_path, '云端更新', '项目管理规定/a.pdf', '成功', old_version='a'*64,
                  version='b'*64, target='kb/根目录', at=at+timedelta(days=1))
    text = (tmp_path / JOURNAL_NAME).read_text(encoding='utf-8')
    assert text.count('云端更新') == 2
    assert '2026-11-03 16:30:00' in text and 'aaaaaaaaaaaa→bbbbbbbbbbbb' in text
    assert '目标=kb/根目录' in text
    assert scan(tmp_path) == []


def test_collection_journal_keeps_failures_but_not_unchanged_files(tmp_path):
    report = {'files': [{'name': 'a.pdf', 'status': 'unchanged'},
                        {'name': 'b.pdf', 'status': 'error', 'reason': '找不到原文件'}]}
    record_collection(tmp_path, report)
    text = (tmp_path / JOURNAL_NAME).read_text(encoding='utf-8')
    assert 'b.pdf' in text and '失败' in text and '找不到原文件' in text
    assert 'a.pdf' not in text


def test_collection_preview_never_creates_a_daily_file(tmp_path):
    source = tmp_path / 'source'
    record_collection(source, {'dry_run': True, 'files': []})
    assert not source.exists()


def test_journal_keeps_repeated_failure_after_recovery(tmp_path):
    assert record(tmp_path, '上传', 'a.pdf', '失败', detail='网络错误')
    assert not record(tmp_path, '上传', 'a.pdf', '失败', detail='网络错误')
    assert record(tmp_path, '上传', 'a.pdf', '成功')
    assert record(tmp_path, '上传', 'a.pdf', '失败', detail='网络错误')
    text = (tmp_path / JOURNAL_NAME).read_text(encoding='utf-8')
    assert text.count('网络错误') == 2


def test_batch_journal_preserves_order_versions_and_failure_after_recovery(tmp_path):
    at = datetime(2026, 11, 11, 12, tzinfo=timezone(timedelta(hours=8)))
    events = [dict(operation='提取新增', filename=f'file-{i}.pdf', result='成功',
                   version='b'*64, old_version='a'*64, at=at) for i in range(260)]
    failed = dict(operation='上传', filename='失败.pdf', result='失败', detail='网络错误', at=at)
    events += [failed, failed, dict(failed, result='成功', detail=''), failed]
    assert record_batch(tmp_path, events) == 263
    lines = (tmp_path / JOURNAL_NAME).read_text(encoding='utf-8').splitlines()[2:]
    assert len(lines) == 263
    assert 'file-0.pdf' in lines[0] and 'file-259.pdf' in lines[259]
    assert all('aaaaaaaaaaaa→bbbbbbbbbbbb' in line for line in lines[:260])
    assert '失败' in lines[-3] and '成功' in lines[-2] and '网络错误' in lines[-1]
    assert record_batch(tmp_path, [failed]) == 0


@pytest.mark.parametrize('written', ['none', 'partial', 'all'])
def test_real_process_journal_interruption_replays_without_losing_or_duplicating_lines(tmp_path, written):
    script = '''
import os,sys
from pathlib import Path
from ima_manager import daily
original=daily._flush_pending
def interrupted(db,stream):
    pending=db.execute('SELECT offset,payload FROM pending_journal WHERE id=1').fetchone()
    if pending:
        offset,payload=pending
        size=0 if sys.argv[2]=='none' else len(payload)//2 if sys.argv[2]=='partial' else len(payload)
        stream.seek(offset); stream.write(payload[:size]); stream.flush(); os.fsync(stream.fileno())
        os._exit(23)
    return original(db,stream)
daily._flush_pending=interrupted
daily.record_batch(Path(sys.argv[1]),[dict(operation='collect',filename=f'file-{i}.pdf',result='ok') for i in range(64)])
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path), written],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=30)
    assert result.returncode == 23, result.stderr
    record_batch(tmp_path, [])
    lines = (tmp_path / JOURNAL_NAME).read_text(encoding='utf-8').splitlines()[2:]
    assert len(lines) == 64
    assert [line.split(' | ')[2] for line in lines] == [f'file-{i}.pdf' for i in range(64)]
    assert record_batch(tmp_path, [dict(operation='collect', filename=f'file-{i}.pdf', result='ok') for i in range(64)]) == 0


def test_real_processes_share_batched_journal_without_duplicate_lines(tmp_path):
    script = '''
import sys
from ima_manager.daily import record_batch
record_batch(sys.argv[1],[dict(operation='collect',filename=f'file-{i}.pdf',result='ok') for i in range(80)],batch_size=16)
'''
    processes = [subprocess.Popen([sys.executable, '-c', script, str(tmp_path)],
                                 cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                 for _ in range(2)]
    try:
        for process in processes:
            _, error = process.communicate(timeout=30)
            assert process.returncode == 0, error
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)
    lines = (tmp_path / JOURNAL_NAME).read_text(encoding='utf-8').splitlines()[2:]
    assert len(lines) == 80 and len(set(line.split(' | ')[2] for line in lines)) == 80


def test_failed_collection_task_is_recorded_without_fabricating_file_updates(tmp_path, monkeypatch):
    from ima_manager import __main__ as cli
    source = tmp_path / 'source'
    settings = {'source': str(source), 'recursive': True, 'ledger_path': str(tmp_path / 'missing.xlsx'),
                'downloads_path': str(tmp_path / 'downloads')}
    monkeypatch.setattr(cli, 'load_settings', lambda: settings)
    assert cli.main(['collect', '--ledger', settings['ledger_path']]) == 1
    text = (source / JOURNAL_NAME).read_text(encoding='utf-8')
    assert '（任务）' in text and '失败' in text and '文件台账不存在' in text


def test_single_sync_still_reports_forbidden_as_failure(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from ima_manager import __main__ as cli
    monkeypatch.setattr(cli, 'RUNTIME', tmp_path / 'runtime')
    monkeypatch.setattr(cli, 'load_settings', lambda: {'source': str(tmp_path / 'source'), 'kb': 'test-kb',
                                                     'collect_enabled': False})
    monkeypatch.setattr(cli, 'load_credentials', lambda: ('fake-client', 'fake-key'))
    monkeypatch.setattr(cli, 'ImaClient', lambda *_: SimpleNamespace(client_id='fake-client'))
    from ima_manager import plans
    source = tmp_path / 'source'
    source.mkdir()
    settings = cli.load_settings()
    engine = cli.Engine(cli.ImaClient(), cli.Store(cli.RUNTIME / 'state.sqlite3'))
    revision = (plans.receipt_revision(engine, 'test-kb'), plans.layout_revision(source))
    plans.save(cli.RUNTIME, engine, settings, source, 'test-kb', '', True, {'files': []}, revision)
    def forbidden(*args, **kwargs):
        raise cli.ImaError('ima HTTP 403 test')
    monkeypatch.setattr(cli.Engine, 'execute', forbidden)
    assert cli.main(['sync', '--execute']) == 1
    error = capsys.readouterr().err
    assert 'ima HTTP 403 test' in error and '秒后重试' not in error
    result = json.loads((tmp_path / 'runtime/latest-plan.json').read_text(encoding='utf-8'))
    assert result['aborted'] is True and result['executed'] is True


@pytest.mark.parametrize(('stop_method', 'fail_first'), [
    ('request', ''), ('settings', ''), ('request', 'cli.ImaError'),
    ('request', 'OSError'), ('request', 'cli.sqlite3.OperationalError')])
def test_watch_uses_real_process_and_cooperative_stop_without_external_services(tmp_path, stop_method, fail_first):
    runtime, source = tmp_path / 'runtime', tmp_path / 'source'
    source.mkdir()
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    settings = {'source': str(source), 'kb': 'fake-kb', 'folder': '', 'recursive': True, 'collect_enabled': False, 'sync_time': '00:00'}
    script = ('from pathlib import Path; from types import SimpleNamespace; '
              'from ima_manager import __main__ as cli, config; '
              f'cli.RUNTIME=Path({str(runtime)!r}); '
              'config.RUNTIME=cli.RUNTIME; '
              f'cli.load_settings=lambda: {settings!r}; '
              'cli.load_credentials=lambda: ("fake-client","fake-key"); '
              'cli.ImaClient=lambda *_: SimpleNamespace(client_id="fake-client"); '
              '')
    if fail_first:
        script += ('\noriginal = cli.Engine.plan\nattempts = 0\n'
                   'def flaky_plan(self, *args, **kwargs):\n'
                   '    global attempts\n    attempts += 1\n'
                   f'    if attempts == 1: raise {fail_first}("ima HTTP 403 test")\n'
                   '    return original(self, *args, **kwargs)\n'
                   'cli.Engine.plan = flaky_plan\n')
    script += '\nraise SystemExit(cli.main(["watch","--execute","--interval","10"]))'
    process = subprocess.Popen([sys.executable, '-c', script],
                               cwd=Path(__file__).resolve().parents[1], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8')
    try:
        deadline = time.monotonic() + 15
        marker = source / JOURNAL_NAME if fail_first else runtime / 'latest-plan.json'
        while time.monotonic() < deadline and not marker.exists() and process.poll() is None:
            time.sleep(.05)
        assert marker.is_file()
        assert (source / JOURNAL_NAME).is_file()
        stop = ('from pathlib import Path; from ima_manager import __main__ as cli, config; '
                f'cli.RUNTIME=Path({str(runtime)!r}); config.RUNTIME=cli.RUNTIME; ')
        stop += ('raise SystemExit(cli.main(["stop"]))' if stop_method == 'request' else
                 'config.save_settings(dict(config.load_settings(), auto_sync_enabled=False))')
        requested = subprocess.run([sys.executable, '-c', stop],
            cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True, text=True, encoding='utf-8', timeout=15)
        assert requested.returncode == 0, requested.stderr
        out, error = process.communicate(timeout=10)
        assert process.returncode == 0, error
        if not fail_first:
            assert '"files": []' in out
        if fail_first:
            assert 'ima HTTP 403 test' in error and '今日次数已使用，次日定时再试' in error
            assert not (runtime / 'latest-plan.json').exists()
            assert '生成同步计划失败' in (source / JOURNAL_NAME).read_text(encoding='utf-8')
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=10)
