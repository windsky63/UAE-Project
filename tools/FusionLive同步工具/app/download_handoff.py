"""Durable download handoff; never starts a parser."""
from contextlib import contextmanager
from pathlib import Path
import os
import json
import time

@contextmanager
def worker_lock(cache, name='worker.lock'):
    cache = Path(cache)
    cache.mkdir(parents=True, exist_ok=True)
    stream = (cache / name).open('a+b')
    acquired = False
    try:
        try:
            if stream.seek(0, os.SEEK_END) == 0:
                stream.write(b'0')
                stream.flush()
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            pass
        yield acquired
    finally:
        if acquired:
            stream.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_UN)
        stream.close()

def notify_download_completed(base_dir, database):
    folder = Path(base_dir) / 'runtime'
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / ('download_completed.' + str(os.getpid()) + '.tmp')
    try:
        temporary.write_text(json.dumps(dict(database=str(database), updated=time.time())), encoding='utf-8')
        os.replace(temporary, folder / 'download_completed.json')
    except OSError:
        temporary.unlink(missing_ok=True)

def in_download_window(at=None):
    from fusion_client import now
    at = at or now()
    return 120 <= at.hour * 60 + at.minute < 630
