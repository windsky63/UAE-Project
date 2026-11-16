import json
from pathlib import Path
import subprocess
import sys

import pytest

from ima_manager import control
from ima_manager.api import ImaError


def test_scan_and_local_archive_need_no_ima_credentials(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    file = source / 'a.txt'
    file.write_bytes(b'test')
    monkeypatch.setattr(control, 'RUNTIME', tmp_path / 'runtime')
    monkeypatch.setattr(control, 'load_settings', lambda: {'source': str(source), 'kb': '', 'folder': '', 'recursive': True})
    def no_credentials():
        raise AssertionError('local action must not load account credentials')
    monkeypatch.setattr(control, 'load_credentials', no_credentials)
    result = control.run({'operation': 'scan'})
    assert result['items'][0]['name'] == 'a.txt'
    assert control.run({'operation': 'move-local', 'data': {'path': 'a.txt'}})['saved']
    assert not file.exists()
    assert (source / '删除/a.txt').read_bytes() == b'test'


def test_local_control_rejects_source_external_files(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    source.mkdir()
    outside = tmp_path / 'outside.txt'
    outside.write_bytes(b'keep')
    monkeypatch.setattr(control, 'RUNTIME', tmp_path / 'runtime')
    monkeypatch.setattr(control, 'load_settings', lambda: {'source': str(source), 'kb': '', 'folder': '', 'recursive': True})
    with pytest.raises(ImaError, match='不在当前'):
        control.run({'operation': 'move-local', 'data': {'path': '../outside.txt'}})
    assert outside.read_bytes() == b'keep'


def test_management_worker_uses_structured_stdin_and_real_process(tmp_path):
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'a.txt').write_text('test', encoding='utf-8')
    settings = {'source': str(source), 'kb': '', 'folder': '', 'recursive': True}
    script = ('from pathlib import Path; from ima_manager import config; '
              f'config.RUNTIME=Path({str(tmp_path / "runtime")!r}); '
              f'config.load_settings=lambda: {settings!r}; '
              'from ima_manager.__main__ import main; raise SystemExit(main(["control"]))')
    result = subprocess.run([sys.executable, '-X', 'utf8', '-c', script],
                            cwd=Path(__file__).resolve().parents[1], input='{"operation":"scan"}',
                            capture_output=True, text=True, encoding='utf-8', timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['items'][0]['name'] == 'a.txt'
