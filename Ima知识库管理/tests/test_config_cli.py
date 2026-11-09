import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ima_manager import config
from ima_manager.api import ImaError


def test_environment_pair_required(monkeypatch):
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('IMA_OPENAPI_CLIENTID', 'client')
    with pytest.raises(ImaError, match='同时设置'):
        config.load_credentials()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_dpapi_roundtrip_and_no_plaintext_on_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    config.save_credentials('fake-client-for-test', 'fake-api-key-for-test')
    raw = (tmp_path / 'credentials.dat').read_bytes()
    assert b'fake-api-key-for-test' not in raw
    assert config.load_credentials() == ('fake-client-for-test', 'fake-api-key-for-test')


def test_settings_never_persist_api_key(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    config.save_settings({'source': 'test', 'kb': 'kb', 'api_key': 'never-save-me', 'recursive': True})
    assert 'never-save-me' not in (tmp_path / 'settings.json').read_text(encoding='utf-8')


def test_auto_sync_setting_defaults_to_enabled_and_persists_manual_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    assert config.load_settings()['auto_sync_enabled'] is True
    config.save_settings(dict(config.load_settings(), auto_sync_enabled=False))
    assert config.load_settings()['auto_sync_enabled'] is False


def test_legacy_settings_infer_database_from_saved_downloads_and_preserve_explicit_override(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    downloads = tmp_path / 'server/downloads'
    (tmp_path / 'settings.json').write_text(json.dumps({'downloads_path': str(downloads),
        'ledger_path': str(tmp_path / 'outdated.xlsx')}), encoding='utf-8')
    assert config.load_settings()['database_path'] == str(downloads.parent / 'runtime/ledger.sqlite3')
    value = dict(config.load_settings(), database_path=str(tmp_path / 'custom.sqlite3'))
    config.save_settings(value)
    assert config.load_settings()['database_path'] == str(tmp_path / 'custom.sqlite3')


def test_disabled_watch_is_rejected_before_credentials_or_network(tmp_path, monkeypatch, capsys):
    from ima_manager import __main__ as cli
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    config.save_settings(dict(config.load_settings(), auto_sync_enabled=False))
    def unexpected():
        raise AssertionError('must not read credentials')
    monkeypatch.setattr(cli, 'load_credentials', unexpected)
    assert cli.main(['watch', '--execute']) == 1
    assert '自动同步已关闭' in capsys.readouterr().err


def test_legacy_collection_range_is_ignored_on_load_and_removed_on_save(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    legacy = {'collect_scope': 'project', 'collect_attachments': True, 'collect_guides': True}
    (tmp_path / 'settings.json').write_text(json.dumps(legacy), encoding='utf-8')
    settings = config.load_settings()
    assert not set(legacy) & settings.keys()
    config.save_settings(dict(settings, **legacy))
    assert not set(legacy) & json.loads((tmp_path / 'settings.json').read_text(encoding='utf-8')).keys()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_portable_credentials_import_encrypts_and_removes_plaintext(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path / 'runtime')
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    bootstrap = tmp_path / 'credentials.bootstrap.json'
    bootstrap.write_text(json.dumps({'client_id': 'portable-client', 'api_key': 'portable-key'}), encoding='utf-8')
    assert config.load_credentials() == ('portable-client', 'portable-key')
    assert not bootstrap.exists()
    assert b'portable-key' not in (config.RUNTIME / 'credentials.dat').read_bytes()
    assert config.load_credentials() == ('portable-client', 'portable-key')


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_portable_credentials_preserve_existing_server_account(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path / 'runtime')
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    config.save_credentials('server-client', 'server-key')
    bootstrap = tmp_path / 'credentials.bootstrap.json'
    bootstrap.write_text(json.dumps({'client_id': 'portable-client', 'api_key': 'portable-key'}), encoding='utf-8')
    assert config.load_credentials() == ('server-client', 'server-key')
    assert not bootstrap.exists()


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_authorized_bootstrap_replaces_old_credentials_and_preserves_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path / 'runtime')
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    config.save_credentials('old-client', 'old-key')
    receipts = config.RUNTIME / 'state.sqlite3'
    receipts.write_bytes(b'preserve-upload-receipts')
    bootstrap = tmp_path / 'credentials.bootstrap.json'
    bootstrap.write_text(json.dumps(dict(client_id='new-client', api_key='new-key', replace_existing=True)), encoding='utf-8')
    assert config.load_credentials() == ('new-client', 'new-key')
    assert not bootstrap.exists()
    assert config.load_credentials() == ('new-client', 'new-key')
    assert b'new-key' not in (config.RUNTIME / 'credentials.dat').read_bytes()
    assert receipts.read_bytes() == b'preserve-upload-receipts'


@pytest.mark.skipif(os.name != 'nt', reason='Windows DPAPI')
def test_invalid_replacement_preserves_old_credentials_and_bootstrap(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path / 'runtime')
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    config.save_credentials('old-client', 'old-key')
    original = (config.RUNTIME / 'credentials.dat').read_bytes()
    bootstrap = tmp_path / 'credentials.bootstrap.json'
    bootstrap.write_text(json.dumps(dict(client_id='private-value', api_key=[], replace_existing=True)), encoding='utf-8')
    with pytest.raises(ImaError, match='部署凭证迁移失败') as error:
        config.load_credentials()
    assert 'private-value' not in str(error.value)
    assert (config.RUNTIME / 'credentials.dat').read_bytes() == original
    assert bootstrap.exists()


def test_deployment_credential_preference_requires_local_opt_in(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path)
    assert not config.deployment_credentials_enabled()
    preference = tmp_path / 'deployment.json'
    preference.write_text(json.dumps({'include_credentials': True}), encoding='utf-8')
    assert config.deployment_credentials_enabled()
    preference.write_text(json.dumps({'include_credentials': False}), encoding='utf-8')
    assert not config.deployment_credentials_enabled()
    preference.write_text(json.dumps({'include_credentials': 'true'}), encoding='utf-8')
    with pytest.raises(ImaError, match='部署配置格式错误'):
        config.deployment_credentials_enabled()


def test_invalid_portable_credentials_do_not_leak_or_replace_saved_file(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path / 'runtime')
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    bootstrap = tmp_path / 'credentials.bootstrap.json'
    bootstrap.write_text(json.dumps({'client_id': 'private-value', 'api_key': []}), encoding='utf-8')
    with pytest.raises(ImaError, match='部署凭证迁移失败') as error:
        config.load_credentials()
    assert 'private-value' not in str(error.value)
    assert not (config.RUNTIME / 'credentials.dat').exists()
    assert bootstrap.exists()


def test_portable_credentials_remain_available_when_encryption_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'RUNTIME', tmp_path / 'runtime')
    for name in ('IMA_OPENAPI_CLIENTID', 'IMA_OPENAPI_APIKEY', 'IMA_CLIENT_ID', 'IMA_API_KEY'):
        monkeypatch.delenv(name, raising=False)
    bootstrap = tmp_path / 'credentials.bootstrap.json'
    bootstrap.write_text(json.dumps({'client_id': 'portable-client', 'api_key': 'portable-key'}), encoding='utf-8')
    def fail_protection(raw, decrypt=False):
        raise ImaError('test encryption unavailable')
    monkeypatch.setattr(config, '_protect', fail_protection)
    with pytest.raises(ImaError, match='encryption unavailable'):
        config.load_credentials()
    assert bootstrap.exists()
    assert not (config.RUNTIME / 'credentials.dat').exists()


def test_scan_cli_real_process_works_without_credentials(tmp_path):
    (tmp_path / 'example.txt').write_text('test', encoding='utf-8')
    env = {key: value for key, value in os.environ.items() if not key.startswith('IMA_')}
    env['PYTHONIOENCODING'] = 'utf-8'
    process = subprocess.run([sys.executable, '-m', 'ima_manager', 'scan', '--source', str(tmp_path)],
                             cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                             text=True, encoding='utf-8', timeout=30)
    assert process.returncode == 0, process.stderr
    assert 'example.txt' in process.stdout
    assert 'ready' in process.stdout


def test_legacy_gui_command_points_to_management_center_without_opening_a_window(tmp_path):
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    process = subprocess.run([sys.executable, '-m', 'ima_manager', 'gui'],
                             cwd=Path(__file__).resolve().parents[1], env=env, capture_output=True,
                             text=True, encoding='utf-8', timeout=30)
    assert process.returncode == 0, process.stderr
    assert '管理中心' in process.stdout
