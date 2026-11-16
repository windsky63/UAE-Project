import hashlib
import json
import sqlite3
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path
from wechat_local.delivery import DeliveryNotSent

class DesktopSpool:
    def __init__(self, path):
        if path != ":memory:": Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS snapshots(chat_id TEXT PRIMARY KEY,epoch TEXT NOT NULL,items TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS captures(id TEXT PRIMARY KEY,body TEXT NOT NULL,uploaded INTEGER NOT NULL DEFAULT 0,seq INTEGER);
            CREATE TABLE IF NOT EXISTS ui_actions(id TEXT PRIMARY KEY,payload_hash TEXT NOT NULL,status TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS db_cursors(source TEXT PRIMARY KEY,cursors TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS notices(
                id TEXT PRIMARY KEY,job_id TEXT NOT NULL,stage TEXT NOT NULL,
                command TEXT NOT NULL,done INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS captures_upload ON captures(uploaded,seq);
        """)
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(ui_actions)')}
        for name, definition in [('attempts', 'INTEGER NOT NULL DEFAULT 1'),
                                 ('attempted_at', 'REAL NOT NULL DEFAULT 0')]:
            if name not in columns:
                self.db.execute(f'ALTER TABLE ui_actions ADD COLUMN {name} {definition}')
        self.db.commit()

    def snapshot(self, chat_id, epoch, messages):
        row = self.db.execute("SELECT epoch,items FROM snapshots WHERE chat_id=?", (chat_id,)).fetchone()
        previous = json.loads(row[1]) if row else []
        signatures = [hashlib.sha256(json.dumps([m['direction'], m['sender'], m['content_type'], m['text']], ensure_ascii=False).encode()).hexdigest() for m in messages]
        current = [{"hash": signature, "ui_id": str(message.get("ui_id", ""))} for signature, message in zip(signatures, messages)]
        same_epoch = bool(row and row[0] == epoch)
        # Runtime IDs disambiguate repeated identical messages within one app instance.
        old = previous if same_epoch else [item['hash'] for item in previous]
        new = current if same_epoch else signatures
        overlap = 0
        for size in range(min(len(old), len(new)), 0, -1):
            if old[-size:] == new[:size]: overlap = size; break
        historical = not same_epoch or (bool(previous) and overlap == 0)
        observed = datetime.now(timezone.utc).isoformat()
        with self.db:
            for message in messages[overlap:]:
                identifier = uuid.uuid4().hex
                event = {key: message[key] for key in ('sender', 'text', 'direction', 'content_type')}
                event.update(id=identifier, chat_id=chat_id, historical=historical, observed_at=observed)
                self.db.execute("INSERT INTO captures(id,body,seq) VALUES (?,?,(SELECT coalesce(max(seq),0)+1 FROM captures))",
                                (identifier, json.dumps(event, ensure_ascii=False)))
            self.db.execute("INSERT INTO snapshots VALUES (?,?,?) ON CONFLICT(chat_id) DO UPDATE SET epoch=excluded.epoch,items=excluded.items",
                            (chat_id, epoch, json.dumps(current)))

    def pending(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT body FROM captures WHERE uploaded=0 ORDER BY seq LIMIT 100")]

    def capture_database(self, source, events, cursors):
        # Advance the source only in the transaction that durably saves its events.
        with self.db:
            for event in events:
                self.db.execute("INSERT OR IGNORE INTO captures(id,body,seq) VALUES (?,?,(SELECT coalesce(max(seq),0)+1 FROM captures))",
                                (event['id'], json.dumps(event, ensure_ascii=False)))
            self.db.execute("INSERT INTO db_cursors VALUES (?,?) ON CONFLICT(source) DO UPDATE SET cursors=excluded.cursors",
                            (source, json.dumps(cursors)))

    def database_cursor(self, source):
        row = self.db.execute("SELECT cursors FROM db_cursors WHERE source=?", (source,)).fetchone()
        return json.loads(row[0]) if row else None

    def uploaded(self, events):
        with self.db: self.db.executemany("UPDATE captures SET uploaded=1 WHERE id=?", [(e['id'],) for e in events])

    def cleanup(self, days):
        """Only acknowledged captures; cursors, snapshots and UI dedup survive forever."""
        if days == 0: return 0
        from datetime import timedelta
        cutoff = (datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
        with self.db:
            count = self.db.execute("DELETE FROM captures WHERE uploaded=1 AND julianday(json_extract(body,'$.observed_at')) < julianday(?)", (cutoff,)).rowcount
        if count:
            self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')
        return count

    def remember_notice(self, job_id, stage, command):
        # Persist the exact payload before the first send, including mention
        # recipient and queue rank; retries must never reconstruct it differently.
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO notices(id,job_id,stage,command) VALUES (?,?,?,?)',
                            (command['id'], job_id, stage, json.dumps(command, ensure_ascii=False)))
        row = self.db.execute('SELECT command FROM notices WHERE id=?', (command['id'],)).fetchone()
        return json.loads(row[0])

    def pending_notices(self):
        return [(job_id, stage, json.loads(command)) for job_id, stage, command in
                self.db.execute('SELECT job_id,stage,command FROM notices WHERE done=0 ORDER BY rowid')]

    def finish_notice(self, identifier):
        with self.db:
            self.db.execute('UPDATE notices SET done=1 WHERE id=?', (identifier,))

    def execute(self, command, driver, max_attempts=1, retry_seconds=5):
        payload_hash = hashlib.sha256(json.dumps(command, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        row = self.db.execute("SELECT payload_hash,status,detail,attempts,attempted_at FROM ui_actions WHERE id=?", (command['id'],)).fetchone()
        if row:
            if row[0] != payload_hash: return {"status": "uncertain", "detail": "Command payload changed"}
            if row[1] == "executing":
                with self.db: self.db.execute("UPDATE ui_actions SET status='uncertain',detail='Process interrupted during UI action' WHERE id=?", (command['id'],))
                return {"status": "uncertain", "detail": "Process interrupted during UI action"}
            if row[1] == 'uncertain':
                confirm = getattr(driver, 'file_delivery_confirmed' if command.get('kind') == 'file' else 'delivery_confirmed', None)
                try:
                    mention = {'mention_user': command['mention_user']} if command.get('mention_user') else {}
                    if command.get('kind') == 'file':
                        confirmed = row[4] > 0 and confirm and confirm(command['chat_id'], command['filename'], command['bytes'], row[4])
                    else:
                        confirmed = row[4] > 0 and confirm and confirm(command['chat_id'], command['text'], row[4], **mention)
                except Exception:
                    confirmed = False
                if confirmed:
                    with self.db:
                        self.db.execute("UPDATE ui_actions SET status='confirmed',detail='Confirmed from local message history' WHERE id=?", (command['id'],))
                    return {'status': 'confirmed', 'detail': 'Confirmed from local message history'}
            if row[1] != 'not_sent' or row[3] >= max_attempts or time.time() - row[4] < retry_seconds:
                return {"status": row[1], "detail": row[2]}
        # Commit BEFORE clicking Send. After a crash never blindly click again.
        with self.db:
            self.db.execute("""INSERT INTO ui_actions(id,payload_hash,status,attempts,attempted_at)
                VALUES (?,?,'executing',1,?) ON CONFLICT(id) DO UPDATE SET
                status='executing',attempts=attempts+1,attempted_at=excluded.attempted_at""",
                (command['id'], payload_hash, time.time()))
        try:
            mention = {'mention_user': command['mention_user']} if command.get('mention_user') else {}
            if command.get('kind') == 'file':
                driver.send_file(command['chat_id'], command['path'], command['sha256'])
            else:
                driver.send(command['chat_id'], command['text'], **mention)
            status, detail = "confirmed", "Desktop UI accepted the send action; recipient receipt is unavailable"
        except DeliveryNotSent as exc:
            status, detail = "not_sent", "No send action was submitted: " + str(exc)
        except Exception as exc:
            import logging
            logging.getLogger(__name__).exception('UI send outcome uncertain: action_id=%s chat_id=%s', command['id'], command['chat_id'])
            status, detail = "uncertain", f"UI send outcome needs review: {type(exc).__name__}: {exc}"
        with self.db: self.db.execute("UPDATE ui_actions SET status=?,detail=? WHERE id=?", (status, detail, command['id']))
        return {"status": status, "detail": detail}


