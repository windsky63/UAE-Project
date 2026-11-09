"""Durable notification retries without a real model or desktop."""
from concurrent.futures import Future
from datetime import datetime, timezone
import queue
from types import SimpleNamespace

import pytest

from wechat_local.config import LocalConfig, WechatChat
from wechat_local.delivery import DeliveryNotSent
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State
from wechat_local.codex_settings import CodexServiceError


@pytest.fixture
def notice_rig(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr('wechat_local.spool.time.time', lambda: clock[0])
    config = LocalConfig(chats=[WechatChat(id='group', name='Group', kind='group', reply=True)],
                         codex_enabled=True, allow_send=True)
    class Driver:
        account_id, account_name = 'robot', 'bot'
        failure = DeliveryNotSent('mouse interference before send')
        confirmed = False
        def __init__(self):
            self.sent = []
        def send(self, chat_id, text, **kwargs):
            self.sent.append((chat_id, text, kwargs))
            if self.failure:
                raise self.failure
        def delivery_confirmed(self, *args, **kwargs):
            return self.confirmed
    driver = Driver()
    state = State(tmp_path / 'state.db')
    spool_path = tmp_path / 'spool.db'
    listener = Listener(config, driver, DesktopSpool(str(spool_path)), state,
                        SimpleNamespace(progress=queue.Queue()))
    event = dict(id='job', chat_id='group', sender='Alice', sender_id='alice',
                 direction='incoming', content_type='text', text='@bot query',
                 historical=False, observed_at=datetime.now(timezone.utc).isoformat())
    state.enqueue(event)
    try:
        yield listener, driver, state, event, clock, spool_path
    finally:
        listener.close()
        listener.spool.db.close()
        state.close()


def start_notice(rig, stage):
    listener, _, state, event, _, _ = rig
    if stage == 'queued':
        listener.queue_notice(event, state.job(event['id']))
    else:
        state.set_job(event['id'], 'running')
        listener.active = (event['id'], Future())
        if stage == 'thinking':
            listener.acknowledge(event, state.job(event['id']))
        else:
            listener.runner.progress.put((event['id'], 'querying'))
            listener.report_progress()


@pytest.mark.parametrize('stage', ['queued', 'thinking', 'querying'])
def test_notice_retries_exact_payload_after_restart_and_backoff(notice_rig, stage):
    listener, driver, state, _, clock, path = notice_rig
    start_notice(notice_rig, stage)
    original = driver.sent[0]
    assert len(driver.sent) == 1
    listener.spool.db.close()
    listener.spool = DesktopSpool(str(path))
    driver.failure = None
    listener.retry_notices()
    assert len(driver.sent) == 1
    clock[0] += listener.config.send_retry_seconds
    listener.retry_notices()
    listener.retry_notices()
    assert driver.sent == [original, original]
    assert not listener.spool.pending_notices()
    notices = state.db.execute("SELECT * FROM archive WHERE direction='outgoing'").fetchall()
    assert len(notices) == 1 and notices[0]['text'] == original[1]
    assert original[2] == ({} if stage == 'querying' else {'mention_user': 'alice'})


@pytest.mark.parametrize('stage', ['queued', 'thinking', 'querying'])
def test_notice_has_bounded_attempts(notice_rig, stage):
    listener, driver, _, _, clock, _ = notice_rig
    start_notice(notice_rig, stage)
    for _ in range(8):
        clock[0] += listener.config.send_retry_seconds
        listener.retry_notices()
    assert len(driver.sent) == listener.config.send_max_attempts == 3


@pytest.mark.parametrize('stage', ['queued', 'thinking', 'querying'])
def test_stale_notice_is_not_sent_after_task_advances(notice_rig, stage):
    listener, driver, state, event, clock, _ = notice_rig
    start_notice(notice_rig, stage)
    state.set_job(event['id'], 'running' if stage == 'queued' else 'generated', reply='answer')
    driver.failure = None
    clock[0] += listener.config.send_retry_seconds
    listener.retry_notices()
    assert len(driver.sent) == 1 and not listener.spool.pending_notices()


def test_uncertain_notice_is_reconciled_without_resend(notice_rig):
    listener, driver, state, _, clock, _ = notice_rig
    driver.failure = RuntimeError('submitted; confirmation delayed')
    start_notice(notice_rig, 'thinking')
    clock[0] += 20
    listener.retry_notices()
    assert len(driver.sent) == 1
    driver.confirmed = True
    listener.retry_notices()
    assert len(driver.sent) == 1 and not listener.spool.pending_notices()
    assert state.db.execute('SELECT count(*) FROM archive').fetchone()[0] == 1


def test_notice_retries_respect_send_switch(notice_rig):
    listener, driver, _, _, clock, _ = notice_rig
    start_notice(notice_rig, 'thinking')
    driver.failure = None
    listener.config.allow_send = False
    clock[0] += 10
    listener.retry_notices()
    assert len(driver.sent) == 1
    listener.config.allow_send = True
    listener.retry_notices()
    assert len(driver.sent) == 2


@pytest.mark.parametrize('stage', ['queued', 'thinking', 'querying'])
def test_emergency_pause_does_not_consume_notice_retry_budget(notice_rig, monkeypatch, stage):
    listener, driver, _, _, clock, _ = notice_rig
    start_notice(notice_rig, stage)
    monkeypatch.setattr('wechat_local.listener.desktop_paused', lambda: True)
    driver.failure = None
    for _ in range(6):
        clock[0] += 10
        listener.retry_notices()
    assert len(driver.sent) == 1
    assert listener.spool.db.execute('SELECT attempts FROM ui_actions').fetchone()[0] == 1
    monkeypatch.setattr('wechat_local.listener.desktop_paused', lambda: False)
    listener.retry_notices()
    assert len(driver.sent) == 2


def test_emergency_pause_does_not_consume_reply_retry_budget(notice_rig, monkeypatch):
    listener, driver, state, event, _, _ = notice_rig
    state.set_job(event['id'], 'running')
    state.set_job(event['id'], 'generated', reply='answer')
    monkeypatch.setattr('wechat_local.listener.desktop_paused', lambda: True)
    listener.deliver()
    assert not driver.sent and state.job(event['id'])['status'] == 'generated'
    assert listener.spool.db.execute('SELECT count(*) FROM ui_actions').fetchone()[0] == 0


def test_failed_connection_notice_retries_after_restart_without_replaying_model(notice_rig):
    listener, driver, state, event, clock, path = notice_rig
    state.set_job(event['id'], 'running')
    state.set_job(event['id'], 'failed', error='sanitized account failure')
    listener.failure_notice(state.job(event['id']), CodexServiceError('account'))
    original = driver.sent[0]
    assert listener.spool.pending_notices()
    listener.spool.db.close()
    listener.spool = DesktopSpool(str(path))
    driver.failure = None
    clock[0] += listener.config.send_retry_seconds
    listener.retry_notices()
    listener.retry_notices()
    assert driver.sent == [original, original]
    assert not listener.spool.pending_notices()
    assert state.job(event['id'])['status'] == 'failed'


@pytest.mark.parametrize('stage', ['queued', 'thinking', 'querying'])
def test_emergency_pause_does_not_consume_notice_retry_budget(notice_rig, monkeypatch, stage):
    listener, driver, _, _, clock, _ = notice_rig
    start_notice(notice_rig, stage)
    monkeypatch.setattr('wechat_local.listener.desktop_paused', lambda: True)
    driver.failure = None
    for _ in range(6):
        clock[0] += 10
        listener.retry_notices()
    assert len(driver.sent) == 1
    assert listener.spool.db.execute('SELECT attempts FROM ui_actions').fetchone()[0] == 1
    monkeypatch.setattr('wechat_local.listener.desktop_paused', lambda: False)
    listener.retry_notices()
    assert len(driver.sent) == 2


def test_emergency_pause_does_not_consume_reply_retry_budget(notice_rig, monkeypatch):
    listener, driver, state, event, _, _ = notice_rig
    state.set_job(event['id'], 'running')
    state.set_job(event['id'], 'generated', reply='answer')
    monkeypatch.setattr('wechat_local.listener.desktop_paused', lambda: True)
    listener.deliver()
    assert not driver.sent and state.job(event['id'])['status'] == 'generated'
    assert listener.spool.db.execute('SELECT count(*) FROM ui_actions').fetchone()[0] == 0
