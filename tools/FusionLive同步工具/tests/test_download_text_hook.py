from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
from sync import generate_download_push_text

def test_download_completion_generates_text_once(tmp_path):
    with patch('reporting.generate_daily_snapshot') as generate:
        assert generate_download_push_text(tmp_path)
        generate.assert_called_once_with(tmp_path)

def test_text_failure_keeps_verified_downloads(tmp_path):
    file = tmp_path / 'verified.pdf'
    file.write_bytes(b'verified')
    with patch('reporting.generate_daily_snapshot', side_effect=OSError('locked')):
        assert not generate_download_push_text(tmp_path)
    assert file.read_bytes() == b'verified'

def test_downloader_has_no_old_parser_commands():
    main = Path(__file__).resolve().parents[1] / 'main.py'
    result = subprocess.run([sys.executable, str(main), 'briefing'], capture_output=True)
    assert result.returncode == 2


def test_idle_poll_does_not_regenerate_existing_push_text():
    from sync import download_report_needed
    assert not download_report_needed((3, 'same'), (3, 'same'), True)
    assert download_report_needed((3, 'old'), (4, 'new'), True)
    assert download_report_needed((0, None), (0, None), False)
