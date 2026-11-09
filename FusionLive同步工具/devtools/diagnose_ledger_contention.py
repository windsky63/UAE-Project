"""Offline process-level contention probes on a private SQLite backup only."""
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

HOME = Path(__file__).resolve().parents[1]
APP = HOME / 'app'
sys.path.insert(0, str(APP))

PROBE = r'''
import json,sqlite3,sys,time
from pathlib import Path
started=time.monotonic()
try:
    db=sqlite3.connect(Path(sys.argv[1]).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)
    result=db.execute('PRAGMA quick_check').fetchall()
    db.close()
    print(json.dumps(dict(ok=result==[('ok',)],seconds=round(time.monotonic()-started,4))))
except Exception as exc:
    print(json.dumps(dict(ok=False,error=type(exc).__name__,detail=str(exc),seconds=round(time.monotonic()-started,4))))
'''
READER = r'''
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[2])
import local_analysis
original=Path.is_file
announced=False
def slow_stat(path):
    global announced
    if not announced:
        print('ready',flush=True)
        announced=True
    time.sleep(.02)  # Model slow file metadata I/O inside the real cursor loop.
    return original(path)
Path.is_file=slow_stat
local_analysis.ledger_sources(sys.argv[1],Path(sys.argv[1]).parent)
'''
WRITER = r'''
import sqlite3,sys,time
db=sqlite3.connect(sys.argv[1])
db.execute('BEGIN IMMEDIATE')
db.execute('UPDATE diagnostic_probe SET value=value+1')
print('ready',flush=True)
time.sleep(30)
db.rollback()
'''


def probe(database):
    child = subprocess.run([sys.executable, '-B', '-c', PROBE, str(database)],
                           capture_output=True, text=True, timeout=20)
    return json.loads(child.stdout)


def write_probe(database):
    started = time.monotonic()
    try:
        with closing(sqlite3.connect(database, timeout=.2)) as db:
            db.execute('UPDATE diagnostic_probe SET value=value+1')
            db.commit()  # Only this disposable backup is ever written.
        return dict(ok=True, seconds=round(time.monotonic()-started, 4))
    except sqlite3.Error as exc:
        return dict(ok=False, error=type(exc).__name__, detail=str(exc),
                    seconds=round(time.monotonic()-started, 4))


def with_competitor(database, code):
    child = subprocess.Popen([sys.executable, '-B', '-c', code, str(database), str(APP)],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        if child.stdout.readline().strip() != 'ready':
            raise RuntimeError('Competitor did not reach its open transaction')
        checks = [probe(database) for _ in range(3)]
        return dict(competitor_alive=child.poll() is None, quick_checks=checks,
                    write_probe=write_probe(database))
    finally:
        if child.poll() is None:
            child.terminate()  # Only the temporary offline fixture created above.
        child.communicate(timeout=10)


def cleanup_probe(inherited):
    from preflight import check_before_login
    messages = []
    extra = ("import subprocess; subprocess.Popen([sys.executable,'-c','import time;time.sleep(3)'],stdout=sys.stdout,stderr=sys.stderr); "
             if inherited else '')
    code = 'import sys,time; ' + extra + "print('fixture check running',flush=True); time.sleep(20)"
    try:
        check_before_login(log=messages.append, timeout=.3,
                           _command=[sys.executable, '-u', '-c', code])
        return dict(error=None)
    except Exception as exc:
        return dict(error=type(exc).__name__, detail=str(exc),
                    phase=messages[-1] if messages else None)


def main():
    source = HOME / 'runtime/ledger.sqlite3'
    output = HOME / 'runtime/ledger-contention-diagnosis.json'
    results = dict(platform=sys.platform, sqlite=sqlite3.sqlite_version,
                   source_bytes=source.stat().st_size, production_database_modified=False)
    with tempfile.TemporaryDirectory(prefix='fusion-ledger-contention-') as temporary:
        database = Path(temporary) / 'ledger.sqlite3'
        with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as original:
            with closing(sqlite3.connect(database)) as copied:
                original.backup(copied)
                copied.execute('CREATE TABLE diagnostic_probe(value INTEGER NOT NULL)')
                copied.execute('INSERT INTO diagnostic_probe VALUES(0)')
                copied.commit()
                copied.execute('PRAGMA journal_mode=WAL')
        results['baseline_wal'] = [probe(database) for _ in range(3)]
        results['real_ledger_scan_wal'] = with_competitor(database, READER)
        results['write_transaction_wal'] = with_competitor(database, WRITER)
        with closing(sqlite3.connect(database)) as copied:
            copied.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            copied.execute('PRAGMA journal_mode=DELETE')
        results['real_ledger_scan_delete'] = with_competitor(database, READER)
    results['startup_timeout_plain'] = cleanup_probe(False)
    results['startup_timeout_inherited_log_handle'] = cleanup_probe(True)
    output.write_text(json.dumps(results, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
