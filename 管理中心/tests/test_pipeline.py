from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pipeline

TZ = timezone(timedelta(hours=8))


def at(hour, minute=0, day=16):
    return datetime(2026, 11, day, hour, minute, tzinfo=TZ)


def test_analysis_waits_for_successful_logout_or_deadline(tmp_path):
    runtime = tmp_path/'runtime'
    runtime.mkdir()
    with sqlite3.connect(runtime/'ledger.sqlite3') as db:
        db.execute('CREATE TABLE runs(id INTEGER, action TEXT, status TEXT, logout_confirmed INTEGER, ended TEXT)')
        db.execute('INSERT INTO runs VALUES(1,?,?,?,?)', ('download', 'download_pending', 1, at(8).isoformat()))
    assert not pipeline.analysis_due(tmp_path, at(9))
    assert pipeline.analysis_due(tmp_path, at(10, 30))
    with sqlite3.connect(runtime/'ledger.sqlite3') as db:
        db.execute("UPDATE runs SET status='done',logout_confirmed=0")
    assert not pipeline.analysis_due(tmp_path, at(9))
    with sqlite3.connect(runtime/'ledger.sqlite3') as db:
        db.execute('UPDATE runs SET logout_confirmed=1')
    assert pipeline.analysis_due(tmp_path, at(9))
    assert not pipeline.analysis_due(tmp_path, at(9, day=17))


def test_upload_handoff_fallback_restart_and_next_day(tmp_path):
    settings = SimpleNamespace(base=tmp_path/'nas', ledger=tmp_path/'sync/runtime/ledger.sqlite3')
    base = settings.ledger.parent.parent
    assert not pipeline.upload_due(settings, at(11))
    assert pipeline.upload_due(settings, at(12))
    pipeline.analysis_finished(base, {'done': 3}, at(8))
    assert pipeline.upload_due(settings, at(8, 1))
    pipeline.upload_finished(settings, at(8, 1))
    assert not pipeline.upload_due(settings, at(13))
    assert not pipeline.upload_due(settings, at(11, day=17))
    assert pipeline.upload_due(settings, at(12, day=17))


def test_incomplete_manifest_never_runs_destructive_mirror(tmp_path):
    settings = SimpleNamespace(ledger=tmp_path/'ledger.sqlite3')
    with sqlite3.connect(settings.ledger) as db:
        db.execute('CREATE TABLE files(active INTEGER,status TEXT)')
        db.execute("INSERT INTO files VALUES(1,'pending')")
    assert not pipeline.mirror_ready(settings)
    with sqlite3.connect(settings.ledger) as db:
        db.execute("UPDATE files SET status='done'")
    assert pipeline.mirror_ready(settings)
    with sqlite3.connect(settings.ledger) as db:
        db.execute('CREATE TABLE refresh_cycles(status TEXT)')
        db.execute("INSERT INTO refresh_cycles VALUES('running')")
    assert not pipeline.mirror_ready(settings)
