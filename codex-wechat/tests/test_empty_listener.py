"""Empty-chat startup uses isolated files and must never initialize capture or Codex."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from wechat_local.config import LocalConfig


def arguments(tmp_path):
    path = tmp_path/'wechat.json'
    path.write_text(json.dumps(dict(chats=[], codex_command=['missing-codex-fixture'], poll_seconds=.5,
        database_cache=str(tmp_path/'cache'), state_path=str(tmp_path/'state/listener.sqlite3'),
        workspaces=str(tmp_path/'workspaces'))), encoding='utf-8')
    return [sys.executable, '-B', '-m', 'wechat_local', '--config', str(path)]


def test_empty_chats_once_succeeds_without_cli_database_or_state(tmp_path):
    result = subprocess.run(arguments(tmp_path)+['--once'],cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, encoding='utf-8', timeout=10,
        env={**os.environ,'PYTHONIOENCODING':'utf-8'})
    assert result.returncode == 0, result.stderr
    assert '待配置' in result.stdout
    assert not (tmp_path/'state').exists()
    assert not (tmp_path/'workspaces').exists()
    assert not (tmp_path/'cache/desktop-binding.local.json').exists()


def test_empty_chat_process_remains_idle_and_stops_gracefully(tmp_path):
    args = arguments(tmp_path)
    directory = Path(__file__).resolve().parents[1]
    env = {**os.environ,'PYTHONIOENCODING':'utf-8'}
    child = subprocess.Popen(args,cwd=directory,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
        text=True,encoding='utf-8',env=env,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    try:
        deadline = time.monotonic()+8
        while not (tmp_path/'cache/bridge.lock').exists() and child.poll() is None and time.monotonic()<deadline:
            time.sleep(.02)
        assert child.poll() is None
        assert (tmp_path/'cache/bridge.lock').exists()
        assert not (tmp_path/'state').exists()
        request = subprocess.run(args+['--stop'],cwd=directory,capture_output=True,text=True,encoding='utf-8',env=env,timeout=5)
        assert request.returncode == 0, request.stderr
        stdout, stderr = child.communicate(timeout=6)
        assert child.returncode == 0, stderr
        assert '待配置' in stdout
    finally:
        if child.poll() is None:
            (tmp_path/'cache/listener.stop.request').write_text('test cleanup',encoding='utf-8')
            try:
                child.communicate(timeout=6)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate()


def test_requested_defaults_and_explicit_existing_settings_are_preserved():
    config = LocalConfig()
    assert config.chats == []
    assert config.codex_enabled and config.allow_send
    assert config.database_root == 'D:/xwechat_files'
    assert config.database_account == ''
    existing = LocalConfig(codex_enabled=False, allow_send=False, database_root='C:/custom', database_account='custom')
    assert not existing.codex_enabled and not existing.allow_send
    assert (existing.database_root,existing.database_account)==('C:/custom','custom')
