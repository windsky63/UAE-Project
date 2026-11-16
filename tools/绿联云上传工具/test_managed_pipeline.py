from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]/'管理中心'))
import pipeline
import main
from nas_sync import BEIJING


def test_success_persists_across_scheduler_restart(tmp_path, monkeypatch):
    settings = SimpleNamespace(base=tmp_path, ledger=tmp_path/'sync/runtime/ledger.sqlite3')
    monkeypatch.setattr(main, '_managed_pipeline', pipeline)
    monkeypatch.setattr(main, 'now', lambda: datetime(2026, 11, 16, 12, tzinfo=BEIJING))
    monkeypatch.setattr(main, 'log', Mock())
    upload = Mock(return_value=0)
    monkeypatch.setattr(main, 'scheduled_upload', upload)
    for _ in range(2):
        exiting = Mock()
        exiting.is_set.side_effect = [False, True]
        main.managed_schedule(settings, exiting)
    assert upload.call_count == 1


def test_analysis_completion_starts_upload_before_noon(tmp_path, monkeypatch):
    settings = SimpleNamespace(base=tmp_path, ledger=tmp_path/'sync/runtime/ledger.sqlite3')
    at = datetime(2026, 11, 16, 8, tzinfo=BEIJING)
    pipeline.analysis_finished(settings.ledger.parent.parent, {'done': 1}, at)
    monkeypatch.setattr(main, '_managed_pipeline', pipeline)
    monkeypatch.setattr(main, 'now', lambda: at)
    monkeypatch.setattr(main, 'log', Mock())
    upload = Mock(return_value=0)
    monkeypatch.setattr(main, 'scheduled_upload', upload)
    exiting = Mock()
    exiting.is_set.side_effect = [False, True]
    main.managed_schedule(settings, exiting)
    upload.assert_called_once()
