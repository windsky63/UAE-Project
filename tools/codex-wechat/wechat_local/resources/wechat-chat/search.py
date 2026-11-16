"""Bounded, read-only search in a task's chat snapshot and text attachments."""
import argparse
import json
from pathlib import Path
import sqlite3


TEXT_TYPES = {'.txt', '.md', '.csv', '.json', '.log'}


def search_history(root, query='', offset=0, limit=8, after='', before='', around=''):
    db_path = (Path(root) / 'memory.sqlite3').resolve()
    conn = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        where, args = ['1=1'], []
        if around:
            row = conn.execute('SELECT observed_at FROM messages WHERE id=?', (around,)).fetchone()
            if not row:
                return {'items': [], 'total': 0}
            # Return a small chronological window preceding and following the match.
            rows = conn.execute('''SELECT * FROM (
                SELECT * FROM (SELECT * FROM messages WHERE observed_at<=? ORDER BY observed_at DESC,id DESC LIMIT ?)
                UNION SELECT * FROM (SELECT * FROM messages WHERE observed_at>? ORDER BY observed_at,id LIMIT ?))
                ORDER BY observed_at,id''', (row[0], limit, row[0], limit)).fetchall()
            return {'items': [{**dict(r), 'text': r['text'][:1000]} for r in rows]}
        if query:
            where.append('instr(lower(text),lower(?))>0')
            args.append(query)
        if after:
            where.append('observed_at>=?')
            args.append(after)
        if before:
            where.append('observed_at<?')
            args.append(before)
        condition = ' AND '.join(where)
        total = conn.execute('SELECT count(*) FROM messages WHERE ' + condition, args).fetchone()[0]
        rows = conn.execute('SELECT * FROM messages WHERE ' + condition + ' ORDER BY observed_at DESC,id DESC LIMIT ? OFFSET ?',
                            (*args, limit, offset)).fetchall()
        return {'items': [{**dict(r), 'text': r['text'][:1000]} for r in rows], 'total': total, 'offset': offset, 'limit': limit}
    finally:
        conn.close()


def attachment(root, name):
    base = Path(root).resolve() / 'files'
    supplied = base / name
    path = supplied.resolve()
    if not path.is_relative_to(base) or supplied.is_symlink() or path.suffix.lower() not in TEXT_TYPES:
        raise ValueError('Only text files within this chat files/ directory are supported')
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('File is larger than 2 MiB')
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['history', 'around', 'files', 'read'])
    parser.add_argument('--query', default='')
    parser.add_argument('--id', default='')
    parser.add_argument('--after', default='')
    parser.add_argument('--before', default='')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int, default=None)
    parser.add_argument('--path', default='')
    args = parser.parse_args()
    limit = args.limit if args.limit is not None else (4000 if args.action == 'read' else 8)
    if args.offset < 0 or not 1 <= limit <= (4000 if args.action == 'read' else 20):
        parser.error('Invalid page size or offset')
    root = Path.cwd()
    if args.action in ('history', 'around'):
        result = search_history(root, args.query, args.offset, limit, args.after, args.before, args.id if args.action == 'around' else '')
    elif args.action == 'read':
        content = attachment(root, args.path).read_text(encoding='utf-8-sig', errors='replace')
        result = {'path': args.path, 'text': content[args.offset:args.offset + limit], 'total_chars': len(content)}
    else:
        matches, inspected = [], 0
        for path in (root / 'files').rglob('*'):
            if not path.is_file() or path.suffix.lower() not in TEXT_TYPES:
                continue
            inspected += 1
            if inspected > 500:
                break
            name = path.relative_to(root / 'files').as_posix()
            try:
                content = attachment(root, name).read_text(encoding='utf-8-sig', errors='replace')
            except ValueError:
                continue
            position = content.lower().find(args.query.lower()) if args.query else 0
            if position >= 0:
                matches.append({'path': name, 'snippet': content[max(0, position - 80):position + 400]})
        result = {'items': matches[args.offset:args.offset + limit], 'total': len(matches), 'scan_limit': 500}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
