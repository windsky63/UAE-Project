from datetime import datetime, timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import analysis_worker as worker
import push_settings


def test_managed_analysis_keeps_publication_protection(tmp_path, monkeypatch):
    due = Mock(return_value=True)
    monkeypatch.setattr(worker, '_managed_pipeline', SimpleNamespace(analysis_due=due))
    monkeypatch.setattr(worker, '_managed_analysis_started', False)
    monkeypatch.setattr(push_settings, 'in_publication_window', lambda *args: True)
    assert worker.analysis_paused_for_schedule(tmp_path)
    due.assert_not_called()


def test_managed_analysis_after_deadline_ignores_download_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, '_managed_pipeline', SimpleNamespace(analysis_due=lambda *args: True))
    monkeypatch.setattr(worker, '_managed_analysis_started', False)
    monkeypatch.setattr(push_settings, 'in_publication_window', lambda *args: False)
    with worker.worker_lock(tmp_path/'runtime', 'sync.lock') as acquired:
        assert acquired
        assert not worker.analysis_paused_for_schedule(tmp_path)


def test_running_analysis_can_cross_midnight_but_still_pauses_for_publication(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, '_managed_pipeline', SimpleNamespace(analysis_due=lambda *args: False))
    monkeypatch.setattr(worker, '_managed_analysis_started', True)
    monkeypatch.setattr(push_settings, 'in_publication_window', lambda *args: False)
    assert not worker.analysis_paused_for_schedule(tmp_path, datetime(2026, 11, 17, 0, 1, tzinfo=timezone(timedelta(hours=8))))
    monkeypatch.setattr(push_settings, 'in_publication_window', lambda *args: True)
    assert worker.analysis_paused_for_schedule(tmp_path)
