import hashlib
import os
import subprocess
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from wechat_local.config import LocalConfig
from wechat_local.file_cleanup import run_if_due
from wechat_local.listener import Listener
from wechat_local.spool import DesktopSpool
from wechat_local.state import State


@pytest.fixture
def cleanup(tmp_path):
    config = LocalConfig(workspaces=str(tmp_path / 'chats'))
    state = State(str(tmp_path / 'state.sqlite3'))
    spool = DesktopSpool(str(tmp_path / 'spool.sqlite3'))
    now = time.time()
    def snapshot(identity, age=3):
        folder = Path(config.workspaces) / ('a' * 24) / 'outgoing_files' / hashlib.sha256(identity.encode()).hexdigest()
        (folder / 'archive').mkdir(parents=True)
        (folder / 'archive/files-001.zip').write_bytes(b'generated archive')
        (folder / 'source.txt').write_bytes(b'snapshot')
        for path in [*folder.rglob('*'), folder]:
            os.utime(path, (now - age * 86400, now - age * 86400))
        return folder
    yield config, state, spool, now, snapshot
    state.close()
    spool.db.close()


def test_defaults_delete_only_old_generated_files_and_schedule_survives_restart(cleanup, tmp_path):
    config, state, spool, now, snapshot = cleanup
    old = snapshot('old')
    fresh = snapshot('fresh', 1)
    downloads = tmp_path / 'downloads'
    downloads.mkdir()
    original = downloads / 'original.zip'
    original.write_bytes(b'original source bytes')
    assert config.file_send_cleanup_days == 2
    assert run_if_due(config, state, spool, now) == (1, 0)
    assert not old.exists() and fresh.exists()
    assert original.read_bytes() == b'original source bytes'
    overdue = snapshot('overdue')
    reopened = State(str(tmp_path / 'state.sqlite3'))
    try:
        assert run_if_due(config, reopened, spool, now + 86400) is None
        assert overdue.exists()
        assert run_if_due(config, reopened, spool, now + 2 * 86400) == (2, 0)
        assert not fresh.exists() and not overdue.exists()
    finally:
        reopened.close()


@pytest.mark.parametrize('status', ['pending', 'running', 'generated', 'not_sent', 'uncertain'])
def test_active_or_uncertain_jobs_are_never_removed(cleanup, status):
    config, state, spool, now, snapshot = cleanup
    folder = snapshot('job')
    state.enqueue(dict(id='job', chat_id='chat', sender='alice', text='send'))
    state.set_job('job', status)
    assert run_if_due(config, state, spool, now) == (0, 0)
    assert folder.exists()


def test_expired_job_with_uncertain_ui_submission_is_kept(cleanup):
    config, state, spool, now, snapshot = cleanup
    folder = snapshot('job:with:colons')
    state.enqueue(dict(id='job:with:colons', chat_id='chat', sender='alice', text='send'))
    state.set_job('job:with:colons', 'expired')
    with spool.db:
        spool.db.execute("INSERT INTO ui_actions(id,payload_hash,status) VALUES(?,?,'uncertain')",
                         ('file:job:with:colons:0', 'hash'))
    run_if_due(config, state, spool, now)
    assert folder.exists()


def test_pending_continuation_is_preserved_until_expiry(cleanup):
    config, state, spool, now, snapshot = cleanup
    folder = snapshot('job')
    state.save_file_selection('chat', 'alice', dict(kind='pending_send', created=now,
        staging_event_id='job', candidates=[]))
    run_if_due(config, state, spool, now)
    assert folder.exists()
    run_if_due(config, state, spool, now + 2 * 86400)
    assert not folder.exists()


def test_disabled_cleanup_and_recent_nested_files_are_preserved(cleanup):
    config, state, spool, now, snapshot = cleanup
    old = snapshot('old')
    config.file_send_cleanup_days = 0
    assert run_if_due(config, state, spool, now) is None
    assert old.exists()
    config.file_send_cleanup_days = 2
    os.utime(old / 'source.txt', (now, now))
    run_if_due(config, state, spool, now)
    assert old.exists()


def test_listener_runs_cleanup_even_when_codex_and_sending_are_disabled(cleanup):
    config, state, spool, now, snapshot = cleanup
    config.codex_enabled = False
    config.allow_send = False
    folder = snapshot('old')
    listener = Listener(config, SimpleNamespace(account_id='bot'), spool, state, None)
    try:
        listener.tick()
        assert not folder.exists()
        assert state.db.execute('SELECT last_run FROM file_cleanup_schedule').fetchone() is not None
    finally:
        listener.close()


def test_symlink_inside_snapshot_never_leads_to_source_deletion(cleanup, tmp_path):
    config, state, spool, now, snapshot = cleanup
    folder = snapshot('old')
    original = tmp_path / 'original'
    original.mkdir()
    (original / 'keep.txt').write_bytes(b'untouched')
    try:
        (folder / 'link').symlink_to(original, target_is_directory=True)
    except OSError:
        if os.name != 'nt':
            pytest.skip('Symbolic link creation unavailable')
        result = subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(folder / 'link'), str(original)],
                                capture_output=True)
        if result.returncode:
            pytest.skip('Symbolic link and junction creation unavailable')
    os.utime(folder, (now - 3 * 86400, now - 3 * 86400))
    run_if_due(config, state, spool, now)
    assert folder.exists()
    assert (original / 'keep.txt').read_bytes() == b'untouched'
