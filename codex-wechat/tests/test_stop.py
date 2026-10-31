import json
from pathlib import Path
import subprocess
import sys


def test_stop_cli_only_writes_request_without_wechat_or_state(tmp_path):
    config=tmp_path/'config.json'
    config.write_text(json.dumps({'database_cache':'cache','state_path':'state.sqlite3'}),encoding='utf-8')
    result=subprocess.run([sys.executable,'-m','wechat_local','--config',str(config),'--stop'],
                          cwd=Path(__file__).resolve().parents[1],capture_output=True,timeout=10)
    assert result.returncode==0
    assert (tmp_path/'cache/listener.stop.request').is_file()
    assert not (tmp_path/'state.sqlite3').exists()
