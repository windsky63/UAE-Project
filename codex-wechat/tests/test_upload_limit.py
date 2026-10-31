import importlib.util
import json
from pathlib import Path

import pytest

from wechat_local.config import LocalConfig


def test_default_and_example_use_reserved_upload_limit():
    root = Path(__file__).resolve().parents[1]
    assert LocalConfig().file_send_max_mib == 900
    assert json.loads((root / 'wechat.codex.example.json').read_text(encoding='utf-8'))['file_send_max_mib'] == 900
    assert 900 * 1024**2 < 1_000_000_000
    with pytest.raises(ValueError):
        LocalConfig(file_send_max_mib=901)


def test_server_update_preserves_settings_and_backs_up_exact_original(tmp_path):
    source = Path(__file__).resolve().parents[1] / 'scripts/set_upload_limit.py'
    spec = importlib.util.spec_from_file_location('set_upload_limit', source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    config = tmp_path / 'wechat.codex.json'
    original = b'\xef\xbb\xbf' + json.dumps(dict(file_send_max_mib=64, allow_send=False,
        codex_enabled=False, chats=[dict(id='server-chat')], database_account='server-account')).encode()
    config.write_bytes(original)
    backup = module.update_config(config)
    assert backup.read_bytes() == original
    expected = json.loads(original.decode('utf-8-sig'))
    expected['file_send_max_mib'] = 900
    assert json.loads(config.read_text(encoding='utf-8')) == expected
    updated = config.read_bytes()
    assert module.update_config(config) is None
    assert config.read_bytes() == updated
    assert len(list(tmp_path.glob('*.bak'))) == 1
