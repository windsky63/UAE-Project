import os
import json
from pathlib import Path
import subprocess
import sys

import pytest

from ima_manager import config
from ima_manager.api import ImaError


def test_collect_summary_keeps_complete_report_on_disk(tmp_path, monkeypatch, capsys):
    from ima_manager import __main__ as cli, collect
    from ima_manager.config import atomic_json
    report = {'files': [{'name': f'file-{i}.pdf', 'status': 'copied'} for i in range(1000)],
              'counts': {'copied': 1000}, 'migrations': [], 'timings_seconds': {'daily_journal': 0.5}}
    runtime = tmp_path / 'runtime'
    monkeypatch.setattr(cli, 'RUNTIME', runtime)
    monkeypatch.setattr(cli, 'load_settings', lambda: {'source': str(tmp_path / 'source')})
    def prepare(*args, **kwargs):
        atomic_json(runtime / 'collection-report.json', report)
        return report
    monkeypatch.setattr(collect, 'prepare_documents', prepare)
    assert cli.main(['collect', '--summary']) == 0
    text = capsys.readouterr().out
    summary = json.loads(text)
    assert len(text) < 1000 and summary['file_count'] == 1000
    assert summary['counts'] == {'copied': 1000} and 'files' not in summary
    assert len(json.loads(Path(summary['report_path']).read_text(encoding='utf-8'))['files']) == 1000


@pytest.fixture
def manual_plan_case(tmp_path, monkeypatch):
    from unittest.mock import Mock
    from ima_manager import __main__ as cli, collect
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'reviewed.txt').write_bytes(b'reviewed')
    runtime = tmp_path / 'runtime'
    settings = dict(source=str(source), kb='kb', folder='', recursive=True, collect_enabled=True)
    client = Mock()
    client.client_id = 'reviewed-plan-account'
    client.listing.return_value = []
    client.repeated_many.side_effect = lambda kb, folder, params: {item['name']: False for item in params}
    client.repeated.return_value = False
    client.call.side_effect = lambda endpoint, body: ({'media_id': 'created', 'cos_credential': {'cos_key': 'key'}}
        if endpoint == 'create_media' else {'media_id': 'added'})
    uploads = []
    client.upload_object.side_effect = lambda stream, *args: uploads.append(stream.read())
    monkeypatch.setattr(cli, 'RUNTIME', runtime)
    monkeypatch.setattr(cli, 'load_settings', lambda: dict(settings))
    monkeypatch.setattr(cli, 'load_credentials', lambda: ('client', 'key'))
    monkeypatch.setattr(cli, 'ImaClient', lambda *_: client)
    def no_collection(*args, **kwargs):
        raise AssertionError('manual preview/execution must not collect documents again')
    monkeypatch.setattr(collect, 'prepare_documents', no_collection)
    return cli, source, runtime, settings, client, uploads


def test_manual_executes_saved_preview_without_planning_or_adding_new_files(manual_plan_case, monkeypatch):
    cli, source, runtime, settings, client, uploads = manual_plan_case
    assert cli.main(['plan', '--summary']) == 0
    preview = json.loads((runtime / 'latest-plan.json').read_text(encoding='utf-8'))
    original_manifest = (runtime / 'preview-plan.json').read_bytes()
    (source / 'later.txt').write_bytes(b'not reviewed')
    def no_plan(*args, **kwargs):
        raise AssertionError('must execute the saved preview')
    monkeypatch.setattr(cli.Engine, 'plan', no_plan)
    assert cli.main(['sync', '--execute', '--summary']) == 0
    result = json.loads((runtime / 'latest-plan.json').read_text(encoding='utf-8'))
    assert result['plan_id'] == preview['plan_id']
    assert [row['name'] for row in result['files']] == ['reviewed.txt']
    assert uploads == [b'reviewed']
    assert json.loads((runtime / 'preview-state.json').read_text(encoding='utf-8'))['state'] == 'executed'
    assert (runtime / 'preview-plan.json').read_bytes() == original_manifest
    assert cli.main(['sync', '--execute']) == 1
    assert uploads == [b'reviewed']


@pytest.mark.parametrize('automatic_used', [False, True])
@pytest.mark.parametrize('fail_first', [False, True])
def test_manual_sync_can_repeat_without_consuming_automatic_run(manual_plan_case, monkeypatch, automatic_used, fail_first):
    from ima_manager.watch import claim_daily_sync
    cli, source, runtime, settings, client, uploads = manual_plan_case
    daily_db = runtime / 'daily-sync.sqlite3'
    if automatic_used:
        assert claim_daily_sync(runtime, scheduled=True, sync_time='00:00')
    previous = daily_db.read_bytes() if automatic_used else None
    execute = cli.Engine.execute
    attempts = []
    def execute_attempt(self, *args, **kwargs):
        attempts.append(True)
        if fail_first and len(attempts) == 1:
            raise ImaError('simulated manual sync failure')
        return execute(self, *args, **kwargs)
    monkeypatch.setattr(cli.Engine, 'execute', execute_attempt)
    for attempt in range(2):
        assert cli.main(['plan', '--summary']) == 0
        assert cli.main(['sync', '--execute', '--summary']) == int(fail_first and attempt == 0)
        assert (daily_db.read_bytes() if daily_db.exists() else None) == previous
    assert len(attempts) == 2
    assert uploads == [b'reviewed']
    assert claim_daily_sync(runtime, scheduled=True, sync_time='00:00') is (not automatic_used)


def test_manual_missing_preview_does_not_consume_daily_run(manual_plan_case, capsys):
    cli, source, runtime, settings, client, uploads = manual_plan_case
    assert cli.main(['sync', '--execute']) == 1
    assert '预览' in capsys.readouterr().err
    assert not (runtime / 'daily-sync.sqlite3').exists()
    client.call.assert_not_called()


@pytest.mark.parametrize('change', ['file', 'removed', 'kb', 'account', 'recursive', 'receipts', 'layout', 'checksum'])
def test_manual_stale_preview_is_blocked_before_daily_run_and_cloud_writes(manual_plan_case, change):
    from ima_manager import plans
    cli, source, runtime, settings, client, uploads = manual_plan_case
    assert cli.main(['plan']) == 0
    if change == 'file':
        (source / 'reviewed.txt').write_bytes(b'changed')
    elif change == 'removed':
        (source / 'reviewed.txt').unlink()
    elif change == 'kb':
        settings['kb'] = 'different'
    elif change == 'account':
        client.client_id = 'different-account'
    elif change == 'recursive':
        settings['recursive'] = False
    elif change == 'receipts':
        engine = cli.Engine(client, cli.Store(runtime / 'state.sqlite3'))
        row = {'path': str(source / 'other.txt'), 'sha256': 'other'}
        engine.store.put(engine.account, 'kb', '', row, 'done', 'other-media')
    elif change == 'layout':
        config.atomic_json(source / '.ima-state/layout-migrations.json', [])
    else:
        path = runtime / 'preview-plan.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        data['files'][0]['name'] = 'tampered.txt'
        config.atomic_json(path, data)
    assert cli.main(['sync', '--execute']) == 1
    assert not (runtime / 'daily-sync.sqlite3').exists()
    client.call.assert_not_called()
    assert uploads == []


def test_failed_new_preview_invalidates_previous_preview(manual_plan_case, monkeypatch):
    cli, source, runtime, settings, client, uploads = manual_plan_case
    assert cli.main(['plan']) == 0
    def failure(*args, **kwargs):
        raise ImaError('simulated preview failure')
    monkeypatch.setattr(cli.Engine, 'plan', failure)
    assert cli.main(['plan']) == 1
    assert cli.main(['sync', '--execute']) == 1
    client.call.assert_not_called()
    assert not (runtime / 'daily-sync.sqlite3').exists()


def test_replaced_preview_cannot_execute_another_plan_id(manual_plan_case):
    cli, source, runtime, settings, client, uploads = manual_plan_case
    assert cli.main(['plan']) == 0
    previous = json.loads((runtime / 'latest-plan.json').read_text(encoding='utf-8'))['plan_id']
    assert cli.main(['plan']) == 0
    current = json.loads((runtime / 'latest-plan.json').read_text(encoding='utf-8'))['plan_id']
    assert current != previous
    assert cli.main(['sync', '--execute', '--plan-id', previous]) == 1
    assert not (runtime / 'daily-sync.sqlite3').exists()
    client.call.assert_not_called()
    assert cli.main(['sync', '--execute', '--plan-id', current]) == 0
    assert uploads == [b'reviewed']


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
