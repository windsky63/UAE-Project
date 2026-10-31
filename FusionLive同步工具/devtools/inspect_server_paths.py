"""Read-only metadata analysis; simulation assumes recorded files exist unchanged."""
import argparse
from collections import defaultdict
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'app'))
import file_layout as layout
import history_repair as history
import path_maintenance as maintenance


def inspect(database, base, output):
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro&immutable=1', uri=True)) as db:
        db.row_factory = sqlite3.Row
        ledger = layout.ReadLedger(db, base)
        paths = {}
        conflicts = []
        for table in ('files', 'file_history'):
            for row in db.execute(f'SELECT path,bytes,sha256 FROM {table} WHERE path IS NOT NULL'):
                if row['path'] in paths and dict(row) != paths[row['path']]:
                    conflicts.append(row['path'])
                paths[row['path']] = dict(row)
        class RecordedPath:
            def __init__(self, relative):
                self.relative = relative
            def exists(self):
                return self.relative in paths
            def is_file(self):
                return self.exists()
            def stat(self):
                return SimpleNamespace(st_size=paths[self.relative]['bytes'])
        def confined(base, relative):
            return RecordedPath(str(relative).replace('\\', '/'))
        parents = defaultdict(list)
        for path in paths:
            if path.startswith('history/'):
                parents[(base/Path(path).parent).as_posix()].append(Path(path).name)
        def walk(root, **kwargs):
            # No actual data directories are read: only copied ledger paths.
            if Path(root).name == 'history':
                return iter((parent, [], names) for parent, names in parents.items())
            return iter(())
        with patch.object(layout, 'confined', confined), patch.object(history, 'confined', confined), patch.object(history.os, 'walk', walk):
            proposal = maintenance.plan(ledger)
        statuses = [dict(r) for r in db.execute('SELECT active,status,count(*) n FROM files GROUP BY active,status')]
        proposal['inventory'] = dict(statuses=statuses, integrity=db.execute('PRAGMA quick_check').fetchone()[0],
            database_sha256=before, recorded_paths=len(paths), conflicting_paths=sorted(set(conflicts)),
            limitation='Metadata simulation only: actual file existence and SHA256 must be checked on the server.')
    if hashlib.sha256(database.read_bytes()).hexdigest() != before:
        raise RuntimeError('Database changed during read-only inspection')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(proposal, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(dict(report=str(output), moves=len(proposal['moves']), restored=proposal.get('restored'),
                         history_files=proposal.get('history_files'), missing=len(proposal['missing']),
                         warnings=proposal['warnings'], conflicts=len(conflicts)), ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--base-dir', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--output', type=Path, default=Path('runtime/server_path_inventory.json'))
    args = parser.parse_args()
    inspect(args.database.resolve(), args.base_dir.resolve(), args.output.resolve())
