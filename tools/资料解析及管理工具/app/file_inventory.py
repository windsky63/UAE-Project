"""Read-only physical inventory shared by the dashboard and daily report.

Only directory entries and sizes are read. No hashes, migrations or durable
cache: every call observes the current files rather than old version records.
"""
from datetime import datetime, timezone, timedelta
import os
import json
import hashlib
from pathlib import Path, PureWindowsPath
import stat
from time import perf_counter


def _reparse(status):
    return stat.S_ISLNK(status.st_mode) or bool(getattr(status,'st_file_attributes',0) & 0x400)


def scan_files(base, scope):
    root = Path(base)/scope
    try:
        status = root.lstat()
    except FileNotFoundError:
        return {}
    if _reparse(status) or not stat.S_ISDIR(status.st_mode):
        return {}
    files = {}
    folders = [(root,scope)]
    while folders:
        folder, relative = folders.pop()
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        status = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue  # A concurrent publisher may have moved it.
                    if _reparse(status):
                        continue
                    name = relative+'/'+entry.name
                    if stat.S_ISDIR(status.st_mode):
                        folders.append((Path(entry.path),name))
                    elif stat.S_ISREG(status.st_mode):
                        files[name.casefold()] = status.st_size
        except FileNotFoundError:
            continue
    return files


def relative_key(value):
    text = str(value or '').replace('\\','/')
    if PureWindowsPath(text).is_absolute() or text.startswith('/'):
        parts = text.split('/')
        start = next((i for i,p in enumerate(parts) if p.casefold() in ('downloads','history','runtime')),None)
        if start is None:
            return None
        text = '/'.join(parts[start:])
    if not text or any(part in ('','.', '..') for part in text.split('/')):
        return None
    return text.casefold()


def totals(files):
    return dict(files=len(files),bytes=sum(files.values()))


def collect_inventory(base, db=None, *, include_history=True):
    started = perf_counter()
    base = Path(base)
    downloads = scan_files(base,'downloads')
    history = scan_files(base,'history') if include_history else {}
    pending = {}
    if db is not None:
        for row in db.execute("SELECT path,bytes FROM files WHERE active=1 AND status='done' AND sha256 IS NOT NULL AND path IS NOT NULL"):
            path, size = row[0],row[1]
            key = relative_key(path)
            if not key or not key.startswith('runtime/download_ready/'):
                continue
            candidate = base/key
            try:
                status = candidate.lstat()
                if (not _reparse(status) and stat.S_ISREG(status.st_mode) and status.st_size==size
                        and candidate.resolve().is_relative_to(base.resolve())):
                    pending[key]=size
            except FileNotFoundError:
                continue
    return dict(total=totals(downloads | history),downloads=totals(downloads),history=totals(history),
                pending_publication=totals(pending),
                changing=(base/'runtime/layout.pending').exists(),
                scanned_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
                scan_ms=round((perf_counter()-started)*1000,2),
                paths=dict(downloads=downloads,history=history))


def summary(inventory):
    return {key:value for key,value in inventory.items() if key!='paths'}


def read_daily_snapshot(base, day=None, *, require_ready=False):
    """One small JSON is the authority for both dashboard and message text."""
    logs=Path(base)/'logs'
    paths=[logs/day/'日报快照.json'] if day else sorted(logs.glob('????-??-??/日报快照.json'),reverse=True)[:1]
    if not paths:
        raise ValueError('尚未生成日报快照')
    path=paths[0]
    if path.stat().st_size>2*1024*1024:
        raise ValueError('日报快照过大')
    data=json.loads(path.read_text(encoding='utf-8'))
    text=data['text']
    if (data.get('snapshot_format')!='daily-file-snapshot-v1' or data.get('date')!=path.parent.name
            or not isinstance(text,str) or hashlib.sha256(text.encode('utf-8')).hexdigest()!=data.get('body_sha256')):
        raise ValueError('日报快照校验失败')
    if require_ready and not data.get('ready'):
        raise ValueError('日报快照尚未就绪')
    return data
