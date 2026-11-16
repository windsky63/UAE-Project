"""Managed daily handoffs. All dates and deadlines use Beijing time."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

BEIJING = timezone(timedelta(hours=8))


def current(at=None):
    return (at or datetime.now(BEIJING)).astimezone(BEIJING)


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def analysis_due(base, at=None):
    at = current(at)
    # The download deadline is a fallback even if the downloader still owns its lock.
    if at.hour * 60 + at.minute >= 630:
        return True
    database = Path(base) / 'runtime/ledger.sqlite3'
    if not database.is_file():
        return False
    try:
        with closing(sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            row = db.execute("SELECT ended FROM runs WHERE action IN ('auto','sync','download') "
                             "AND status='done' AND logout_confirmed=1 ORDER BY id DESC LIMIT 1").fetchone()
        return bool(row and row[0] and datetime.fromisoformat(row[0]).astimezone(BEIJING).date() == at.date())
    except (sqlite3.Error, ValueError):
        return False


def analysis_finished(base, counts, at=None):
    at = current(at)
    path = Path(base) / 'runtime/pipeline_analysis.json'
    if read(path).get('date') != at.date().isoformat():
        save(path, dict(date=at.date().isoformat(), ended=at.isoformat(), counts=counts))


def upload_due(settings, at=None):
    at = current(at)
    state = read(settings.base / 'runtime/pipeline_upload.json')
    if state.get('date') == at.date().isoformat() and (state.get('complete') or state.get('stopped')):
        return False
    finished = read(settings.ledger.parent / 'pipeline_analysis.json')
    return finished.get('date') == at.date().isoformat() or at.hour >= 12


def upload_finished(settings, started):
    # A round crossing midnight completes its originating day's work.
    save(settings.base / 'runtime/pipeline_upload.json',
         dict(date=current(started).date().isoformat(), complete=True, ended=current().isoformat()))


def mirror_ready(settings):
    """Incomplete downloads may upload verified bytes but cannot retire NAS files."""
    with closing(sqlite3.connect(settings.ledger.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'refresh_cycles' in tables and db.execute("SELECT 1 FROM refresh_cycles WHERE status!='complete' LIMIT 1").fetchone():
            return False
        return not db.execute("SELECT 1 FROM files WHERE active=1 AND status!='done' LIMIT 1").fetchone()
