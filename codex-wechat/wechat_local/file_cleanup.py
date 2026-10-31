"""Age-based cleanup confined to generated outgoing snapshots, never downloads."""
import hashlib
import json
import logging
from pathlib import Path
import re
import shutil
import time


def redirected(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def protected_events(state, spool, now):
    protected = set()
    for row in state.db.execute('SELECT id,status,file_result FROM jobs'):
        if row['status'] in ('pending', 'running', 'generated', 'not_sent', 'uncertain'):
            protected.add(row['id'])
    # UI uncertainty survives even when the host job has expired or failed.
    for (identifier,) in spool.db.execute("SELECT id FROM ui_actions WHERE status IN ('uncertain','executing')"):
        if identifier.startswith('file:'):
            original = identifier[5:]
            protected.add(original)
            if original.rpartition(':')[2].isdigit():
                protected.add(original.rpartition(':')[0])
    for (data,) in state.db.execute('SELECT payload FROM file_selections'):
        selection = json.loads(data)
        if selection.get('kind') == 'pending_send' and now - selection.get('created', 0) < 900:
            if selection.get('staging_event_id'):
                protected.add(selection['staging_event_id'])
            else:
                # Compatibility for pending selections written before this patch.
                ids = {entry['id'] for entry in selection.get('candidates', [])}
                for row in state.db.execute('SELECT id,file_result FROM jobs WHERE file_result IS NOT NULL'):
                    remaining = json.loads(row['file_result']).get('remaining', [])
                    if any(entry['id'] in ids for entry in remaining):
                        protected.add(row['id'])
    return {hashlib.sha256(identifier.encode()).hexdigest() for identifier in protected}


def remove_old_snapshots(workspaces, protected, cutoff):
    root = Path(workspaces).absolute()
    removed, skipped = 0, 0
    if not root.is_dir() or redirected(root):
        return removed, skipped
    resolved_root = root.resolve()
    for workspace in root.iterdir():
        if not re.fullmatch(r'[a-f0-9]{24}', workspace.name) or redirected(workspace):
            continue
        outgoing = workspace / 'outgoing_files'
        if not outgoing.is_dir() or redirected(outgoing):
            continue
        outgoing_root = outgoing.resolve()
        if not outgoing_root.is_relative_to(resolved_root):
            continue
        for candidate in outgoing.iterdir():
            if not re.fullmatch(r'[a-f0-9]{64}', candidate.name) or candidate.name in protected:
                continue
            try:
                # Resolve and verify the absolute recursive-delete target first.
                if redirected(candidate) or not candidate.is_dir():
                    continue
                target = candidate.resolve()
                if target.parent != outgoing_root or not target.is_relative_to(resolved_root):
                    continue
                stack, safe = [candidate], True
                while stack and safe:
                    path = stack.pop()
                    if (redirected(path) or not path.resolve().is_relative_to(target)
                            or path.stat().st_mtime > cutoff):
                        safe = False
                    elif path.is_dir():
                        stack.extend(path.iterdir())
                if not safe:
                    continue
                shutil.rmtree(target)
                removed += 1
            except OSError:
                skipped += 1
    return removed, skipped


def run_if_due(config, state, spool, now=None):
    days = config.file_send_cleanup_days
    if not days:
        return None
    now = time.time() if now is None else now
    state.db.execute('CREATE TABLE IF NOT EXISTS file_cleanup_schedule(name TEXT PRIMARY KEY,last_run REAL)')
    row = state.db.execute("SELECT last_run FROM file_cleanup_schedule WHERE name='outgoing'").fetchone()
    interval = days * 86400
    if row and now - row[0] < interval:
        return None
    result = remove_old_snapshots(config.workspaces, protected_events(state, spool, now), now - interval)
    with state.db:
        state.db.execute("INSERT OR REPLACE INTO file_cleanup_schedule VALUES('outgoing',?)", (now,))
    logging.getLogger(__name__).info('Outgoing snapshot cleanup: removed=%s, skipped=%s', *result)
    return result
