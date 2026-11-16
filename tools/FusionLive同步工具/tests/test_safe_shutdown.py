from pathlib import Path
from unittest.mock import patch
import scheduler

def test_download_stop_does_not_stop_parser(tmp_path):
    runtime=tmp_path/'runtime'
    with patch('scheduler.RUNTIME',runtime):
        scheduler.request_full_stop()
    assert (runtime/'scheduler.exit.request').exists()
    assert (runtime/'stop.request').exists()
    assert not (runtime/'file_analysis/stop.request').exists()
    assert not (runtime/'publication.stop').exists()
