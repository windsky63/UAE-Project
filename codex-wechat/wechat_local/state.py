"""Persistent local archive and reply jobs; no remote server or vector model."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .memory import clip, write_markdown, NOTE_CHARS


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class State:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS archive (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, sender TEXT NOT NULL,
                direction TEXT NOT NULL, content_type TEXT NOT NULL, text TEXT NOT NULL,
                observed_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS archive_chat ON archive(chat_id,observed_at,id);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, chat_id TEXT NOT NULL, event TEXT NOT NULL,
                status TEXT NOT NULL, reply TEXT, usage TEXT, error TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_chat_status ON jobs(chat_id,status,created_at,id);
            CREATE TABLE IF NOT EXISTS task_numbers (
                number INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS qa_memory (
                job_id TEXT PRIMARY KEY,chat_id TEXT NOT NULL,sender TEXT NOT NULL,
                question TEXT NOT NULL,answer TEXT NOT NULL,note TEXT NOT NULL,answered_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS qa_memory_chat ON qa_memory(chat_id,answered_at,job_id);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(jobs)')}
        for name in ('memory_note', 'started_at', 'finished_at', 'queue_position', 'file_result'):
            if name not in columns:
                kind = 'INTEGER' if name == 'queue_position' else 'TEXT'
                self.db.execute(f'ALTER TABLE jobs ADD COLUMN {name} {kind}')
        self.db.commit()
        self.db.execute('CREATE TABLE IF NOT EXISTS file_selections(chat_id TEXT,sender TEXT,payload TEXT,PRIMARY KEY(chat_id,sender))')
        self.db.commit()
        # Legacy jobs retain their payloads, including pending jobs whose queue
        # notice may already have been sent. New jobs receive sequential numbers.
    def recover_interrupted(self):
        with self.db:
            # Never automatically repeat an interrupted model call or uncertain UI action.
            self.db.execute("UPDATE jobs SET status='failed',error='Interrupted generation; review before retry' WHERE status='running'")

    def bind_account(self, bot_id, account_id):
        with self.db:
            self.db.execute('CREATE TABLE IF NOT EXISTS identity (id INTEGER PRIMARY KEY CHECK(id=1),bot_id TEXT,account_id TEXT)')
            row = self.db.execute('SELECT bot_id,account_id FROM identity WHERE id=1').fetchone()
            if row and tuple(row) != (bot_id, account_id):
                raise ValueError('Account changed; use new local state and workspace directories')
            self.db.execute('INSERT OR IGNORE INTO identity VALUES (1,?,?)', (bot_id, account_id))

    def archive(self, event):
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO archive VALUES (?,?,?,?,?,?,?)', (
                event['id'], event['chat_id'], event['sender'], event['direction'],
                event['content_type'], event['text'], event['observed_at']))

    def enqueue(self, event):
        with self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO jobs(id,chat_id,event,status,reply,usage,error,created_at,updated_at) VALUES (?,?,?,\'pending\',NULL,NULL,NULL,?,?)',
                            (event['id'], event['chat_id'], json.dumps(event, ensure_ascii=False), utcnow(), utcnow()))
            if inserted.rowcount:
                self.db.execute('INSERT INTO task_numbers(job_id) VALUES (?)', (event['id'],))

    def task_number(self, identifier):
        row = self.db.execute('SELECT number FROM task_numbers WHERE job_id=?', (identifier,)).fetchone()
        return str(row[0]) if row else identifier

    def next_job(self):
        return self.db.execute("SELECT * FROM jobs WHERE status='pending' ORDER BY created_at,id LIMIT 1").fetchone()

    def set_job(self, identifier, status, reply=None, usage=None, error=None, memory_note=None, file_result=None):
        with self.db:
            at = utcnow()
            self.db.execute('''UPDATE jobs SET status=?,reply=coalesce(?,reply),usage=coalesce(?,usage),error=?,
                updated_at=?,memory_note=coalesce(?,memory_note),started_at=coalesce(started_at,?),
                finished_at=coalesce(finished_at,?) WHERE id=?''',
                (status, reply, json.dumps(usage) if usage is not None else None, error, at, memory_note,
                 at if status == 'running' else None, at if status in ('generated', 'failed') else None, identifier))
            if file_result is not None:
                self.db.execute('UPDATE jobs SET file_result=? WHERE id=?', (json.dumps(file_result, ensure_ascii=False), identifier))

    def file_selection(self, chat_id, sender):
        row = self.db.execute('SELECT payload FROM file_selections WHERE chat_id=? AND sender=?', (chat_id, sender)).fetchone()
        return json.loads(row[0]) if row else None

    def save_file_selection(self, chat_id, sender, payload):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO file_selections VALUES(?,?,?)', (chat_id, sender, json.dumps(payload, ensure_ascii=False)))

    def job(self, identifier):
        return self.db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()

    def queue_position(self, identifier):
        job = self.job(identifier)
        if job['queue_position'] is None:
            position = self.db.execute('''SELECT count(*) FROM jobs WHERE status='pending'
                AND (created_at < ? OR (created_at = ? AND id <= ?))''',
                (job['created_at'], job['created_at'], identifier)).fetchone()[0]
            with self.db:
                self.db.execute('UPDATE jobs SET queue_position=? WHERE id=?', (position, identifier))
        return int(self.job(identifier)['queue_position'])

    def remember(self, chat_id):
        """Idempotently import only confirmed answers, including pre-upgrade jobs."""
        rows = self.db.execute('''SELECT j.* FROM jobs j LEFT JOIN qa_memory m ON m.job_id=j.id
            WHERE j.chat_id=? AND j.status='confirmed' AND j.reply IS NOT NULL AND m.job_id IS NULL
            ORDER BY j.created_at DESC,j.id DESC LIMIT 500''', (chat_id,)).fetchall()
        entries = []
        for row in rows:
            event = json.loads(row['event'])
            note = (row['memory_note'] if row['memory_note'] is not None
                    else '原回复摘录：' + clip(row['reply'], NOTE_CHARS - 6))
            entries.append((row['id'], chat_id, event.get('sender_id') or event['sender'],
                            event['text'], row['reply'], clip(note, NOTE_CHARS), row['updated_at']))
        with self.db:
            self.db.executemany('INSERT OR IGNORE INTO qa_memory VALUES(?,?,?,?,?,?,?)', entries)

    def memory_rows(self, chat_id):
        return self.db.execute("SELECT * FROM qa_memory WHERE chat_id=? AND note<>'' ORDER BY answered_at DESC,job_id DESC LIMIT 500",
                               (chat_id,)).fetchall()

    def export_memory(self, chat_id, workspace):
        self.remember(chat_id)
        write_markdown(workspace, self.memory_rows(chat_id))

    def generated(self):
        return self.db.execute("SELECT * FROM jobs WHERE status IN ('generated','not_sent','uncertain') ORDER BY created_at,id").fetchall()

    def export_chat(self, chat_id, target, include_memory=True):
        """Codex receives a snapshot containing only the selected chat."""
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if include_memory:
            self.remember(chat_id)
        snapshot = sqlite3.connect(target)
        try:
            with snapshot:
                snapshot.execute('CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY,sender TEXT,direction TEXT,content_type TEXT,text TEXT,observed_at TEXT)')
                snapshot.execute('CREATE TABLE IF NOT EXISTS checkpoint (id INTEGER PRIMARY KEY CHECK(id=1),source_rowid INTEGER NOT NULL)')
                snapshot.execute('''CREATE TABLE IF NOT EXISTS qa_memory(
                    job_id TEXT PRIMARY KEY,sender TEXT,question TEXT,answer TEXT,note TEXT,answered_at TEXT)''')
                snapshot.execute('DELETE FROM qa_memory')
                snapshot.executemany('INSERT INTO qa_memory VALUES(?,?,?,?,?,?)',
                                     [(r['job_id'], r['sender'], clip(r['question'], 300), '', r['note'], r['answered_at'])
                                      for r in (self.memory_rows(chat_id) if include_memory else [])])
                row = snapshot.execute('SELECT source_rowid FROM checkpoint WHERE id=1').fetchone()
                last = row[0] if row else 0
                rows = self.db.execute('SELECT rowid,id,sender,direction,content_type,text,observed_at FROM archive WHERE chat_id=? AND rowid>? ORDER BY rowid', (chat_id, last)).fetchall()
                snapshot.executemany('INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?,?)', [tuple(r)[1:] for r in rows])
                if rows:
                    snapshot.execute('INSERT INTO checkpoint VALUES (1,?) ON CONFLICT(id) DO UPDATE SET source_rowid=excluded.source_rowid', (rows[-1][0],))
        finally:
            snapshot.close()

    def recent(self, chat_id, limit, exclude_id):
        rows = self.db.execute("""SELECT id,sender,direction,text,observed_at FROM archive WHERE chat_id=? AND id<>?
            AND NOT (direction='outgoing' AND (id LIKE 'thinking:%' OR id LIKE 'querying:%' OR id LIKE 'queued:%'
                OR text IN ('正在思考……','正在查询……') OR (instr(text,'任务编号：')>0 AND
                    (text LIKE '%正在思考中……' OR text LIKE '%正在查询……' OR text LIKE '%正在排队中……'))))
            ORDER BY observed_at DESC,id DESC LIMIT ?""",
                               (chat_id, exclude_id, limit)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def close(self):
        self.db.close()
