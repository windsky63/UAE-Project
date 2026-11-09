"""Catalogue snapshots, resumable association checks and change reconciliation."""

from paths import HOME, CODE
import hashlib
from contextlib import closing
import json
import sqlite3
from pathlib import Path
from fusion_client import now
from paths import relative_path


def fingerprint(data, fields=None):
    if fields is None:
        data = {k: v for k, v in data.items() if k not in ('isOverdue',)}
    else:
        data = {k: data[k] for k in fields if k in data}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()


CONTENT_FIELDS = ('id', 'filename', 'fileName', 'contentSize', 'revision', 'versionLabel', 'versionLable',
                  'creationDate', 'uploadDate', 'revisionDate', 'modifiedDate', 'updatedDate',
                  'lastModified', 'lastModifiedDate', 'modificationDate', 'updatedAt', 'modifiedAt',
                  'contentId', 'contentVersionId', 'contentChecksum', 'checksum', 'md5', 'sha256', 'etag', 'eTag', 'hasContent')


def same_linked_version(before, after):
    """A virtual link's creation time describes its entry, not new file bytes.

    Require an explicit, identical remote version and a different link context.
    All other content evidence must agree; timestamp changes within one entry
    and actual version/size/checksum changes still invalidate downloaded bytes.
    """
    if any(item.get('isVirtualLink') != 'true' for item in (before, after)):
        return False
    if any(not before.get(key) or before.get(key) != after.get(key)
           for key in ('id', 'firstVersionId', 'versionLabel')):
        return False
    context = ('virtualLinkId', 'ownerId', 'isSourceDocument')
    if all(before.get(key) == after.get(key) for key in context):
        return False
    return all(before.get(key) == after.get(key) for key in CONTENT_FIELDS if key != 'creationDate')


def source_file_identity(item):
    """Stable remote version family; never merge different attachment members."""
    item = dict(item)
    data = json.loads(item.get('metadata') or '{}')
    first = data.get('firstVersionId')
    if not first:
        return None
    relation = item.get('relation') or data.get('relation') or ''
    member = '' if relation == 'Document' else str(item.get('filename') or data.get('filename') or '').casefold()
    return json.dumps([item['project'], str(first), relation, member], ensure_ascii=False, separators=(',', ':'))


class InventoryMixin:
    def upgrade_schema(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version < 2 and self.one('SELECT COUNT(*) n FROM documents')['n']:
            folder = self.base_dir / 'runtime' / 'backups'
            folder.mkdir(parents=True, exist_ok=True)
            backup = folder / ('ledger-before-v2-' + now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
            with closing(sqlite3.connect(backup)) as target:
                self.db.backup(target)
        additions = {
            'folders': {'snapshot': 'INTEGER NOT NULL DEFAULT 0', 'active': 'INTEGER NOT NULL DEFAULT 1'},
            'documents': {'snapshot': 'INTEGER NOT NULL DEFAULT 0', 'active': 'INTEGER NOT NULL DEFAULT 1',
                          'fingerprint': 'TEXT', 'content_fingerprint': 'TEXT', 'content_changed': 'INTEGER NOT NULL DEFAULT 0',
                          'checked_cycle': 'INTEGER NOT NULL DEFAULT 0', 'change_type': "TEXT NOT NULL DEFAULT 'existing'"},
            'files': {'fingerprint': 'TEXT', 'active': 'INTEGER NOT NULL DEFAULT 1', 'change_type': "TEXT NOT NULL DEFAULT 'existing'",
                      'retryable': 'INTEGER NOT NULL DEFAULT 1'},
        }
        for table, fields in additions.items():
            columns = {row['name'] for row in self.all(f'PRAGMA table_info({table})')}
            for column, definition in fields.items():
                if column not in columns:
                    self.db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {definition}')
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS snapshots(
          id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,started TEXT NOT NULL,
          finished TEXT,status TEXT NOT NULL DEFAULT 'scanning');
        CREATE TABLE IF NOT EXISTS refresh_cycles(
          project TEXT PRIMARY KEY,cycle INTEGER NOT NULL,status TEXT NOT NULL,started TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS document_files(
          project TEXT NOT NULL,document_id TEXT NOT NULL,folder_id TEXT NOT NULL,file_id TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(project,document_id,folder_id,file_id));
        CREATE TABLE IF NOT EXISTS changes(
          id INTEGER PRIMARY KEY AUTOINCREMENT,snapshot INTEGER,project TEXT NOT NULL,entity TEXT NOT NULL,
          entity_id TEXT NOT NULL,folder_id TEXT,kind TEXT NOT NULL,details TEXT,created TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS file_history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,project TEXT NOT NULL,file_id TEXT NOT NULL,fingerprint TEXT,
          path TEXT,bytes INTEGER,sha256 TEXT,archived TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS document_files_content ON document_files(project,file_id,active);
        CREATE INDEX IF NOT EXISTS documents_refresh ON documents(project,active,related_state,checked_cycle);
        CREATE INDEX IF NOT EXISTS changes_snapshot ON changes(snapshot);
        ''')
        with self.db:
            for row in self.all('SELECT project,id,folder_id,metadata FROM documents WHERE fingerprint IS NULL'):
                data = json.loads(row['metadata'])
                self.db.execute('UPDATE documents SET fingerprint=?,content_fingerprint=? WHERE project=? AND id=? AND folder_id=?',
                                (fingerprint(data), fingerprint(data, CONTENT_FIELDS), row['project'], row['id'], row['folder_id']))
            for row in self.all('SELECT project,id,metadata FROM files WHERE fingerprint IS NULL'):
                self.db.execute('UPDATE files SET fingerprint=? WHERE project=? AND id=?',
                                (fingerprint(json.loads(row['metadata']), CONTENT_FIELDS), row['project'], row['id']))
            if version < 2:
                self.db.execute('''INSERT OR IGNORE INTO document_files(project,document_id,folder_id,file_id)
                  SELECT project,document_id,folder_id,id FROM files''')
            for table in ('files', 'file_history'):
                for row in self.all(f'SELECT rowid AS record_id,path FROM {table} WHERE path IS NOT NULL'):
                    path = relative_path(row['path'], self.base_dir)
                    if path != row['path']:
                        self.db.execute(f'UPDATE {table} SET path=? WHERE rowid=?', (path, row['record_id']))
            self.db.execute('PRAGMA user_version=2')
        latest = self.one('SELECT * FROM snapshots ORDER BY id DESC LIMIT 1')
        cycle = self.one('SELECT cycle FROM refresh_cycles WHERE project=?', (latest['project'],)) if latest else None
        self.snapshot_id = latest['id'] if latest else None
        self.cycle = cycle['cycle'] if cycle else None

    def change(self, project, entity, identifier, folder_id, kind, details=None):
        self.db.execute('INSERT INTO changes(snapshot,project,entity,entity_id,folder_id,kind,details,created) VALUES(?,?,?,?,?,?,?,?)',
                        (self.snapshot_id, project, entity, identifier, folder_id, kind,
                         json.dumps(details, ensure_ascii=False) if details else None, now().isoformat()))

    def root(self, client, rescan=False, checkpoint=None, new_cycle=False):
        project = client.project_id
        with self.db:
            if rescan or self.snapshot_id is None:
                self.snapshot_id = self.db.execute('INSERT INTO snapshots(project,started) VALUES(?,?)',
                                                   (project, now().isoformat())).lastrowid
                previous = self.one('SELECT * FROM refresh_cycles WHERE project=?', (project,))
                self.cycle = (previous['cycle'] + 1 if previous['status'] == 'complete' or new_cycle else previous['cycle']) if previous else 1
                self.db.execute('INSERT OR REPLACE INTO refresh_cycles VALUES(?,?,?,?)',
                                (project, self.cycle, 'incomplete', now().isoformat()))
                self.db.execute('DELETE FROM scan_pages WHERE project=?', (project,))
            self.db.execute('''INSERT INTO folders(project,id,parent,name,path,list_state,snapshot,active)
              VALUES(?,?,?,?,?,'done',?,1) ON CONFLICT(project,id) DO UPDATE SET name=excluded.name,
              path=excluded.path,tree_state='pending',list_state='done',next_start=0,snapshot=excluded.snapshot,active=1''',
                            (project, client.cabinet_id, None, client.project_name, client.project_name, self.snapshot_id))
            if checkpoint:
                checkpoint(self.snapshot_id, self.cycle)

    def save_children(self, project, folder, children):
        identifiers = [child['id'] for child in children]
        if len(identifiers) != len(set(identifiers)):
            raise RuntimeError('目录响应包含重复 ID。')
        with self.db:
            for child in children:
                ancestor = folder
                visited = set()
                while ancestor:
                    if ancestor['id'] == child['id'] or ancestor['id'] in visited:
                        raise RuntimeError('目录响应存在循环。')
                    visited.add(ancestor['id'])
                    ancestor = self.one('SELECT * FROM folders WHERE project=? AND id=?', (project, ancestor['parent'])) if ancestor['parent'] else None
                old = self.one('SELECT * FROM folders WHERE project=? AND id=?', (project, child['id']))
                if old and old['snapshot'] == self.snapshot_id and old['parent'] != folder['id']:
                    raise RuntimeError('同一轮扫描的目录出现多个父目录，停止检查。')
                if not old:
                    self.change(project, 'folder', child['id'], folder['id'], 'added', {'name': child['text']})
                elif old['parent'] != folder['id'] or old['name'] != child['text']:
                    self.change(project, 'folder', child['id'], folder['id'], 'moved_or_renamed', {'old_path': old['path']})
                self.db.execute('''INSERT INTO folders(project,id,parent,name,path,snapshot,active) VALUES(?,?,?,?,?,?,1)
                  ON CONFLICT(project,id) DO UPDATE SET parent=excluded.parent,name=excluded.name,path=excluded.path,
                  snapshot=excluded.snapshot,active=1,tree_state='pending',list_state='pending',next_start=0,error=NULL''',
                                (project, child['id'], folder['id'], child['text'], folder['path'] + '/' + child['text'], self.snapshot_id))
            self.db.execute("UPDATE folders SET tree_state='done',error=NULL WHERE project=? AND id=?", (project, folder['id']))

    def save_page(self, project, folder_id, total, records, next_start, done, *, transaction=True):
        from contextlib import nullcontext
        with self.db if transaction else nullcontext():
            start = next_start - len(records)
            ids = [item.get('id') for item in records]
            if start < 0 or next_start > total or any(not identifier for identifier in ids) or len(ids) != len(set(ids)):
                raise RuntimeError('分页位置、总数或文件 ID 无效。')
            earlier = self.all('SELECT ids FROM scan_pages WHERE project=? AND folder_id=? AND start<?', (project, folder_id, start))
            seen = {identifier for page in earlier for identifier in json.loads(page['ids'])}
            if seen.intersection(ids):
                raise RuntimeError('本轮扫描的分页包含重复文件 ID。')
            for item in records:
                old = self.one('SELECT * FROM documents WHERE project=? AND id=? AND folder_id=?', (project, item['id'], folder_id))
                signature = fingerprint(item)
                content_signature = fingerprint(item, CONTENT_FIELDS)
                kind = 'existing' if old and old['fingerprint'] == signature else 'updated' if old else 'added'
                changed = bool(old and (old['content_changed'] or old['content_fingerprint'] != content_signature))
                pending = not old or kind != 'existing' or not old['active'] or old['checked_cycle'] < self.cycle or old['related_state'] == 'failed'
                related = 'pending' if pending else old['related_state']
                if kind != 'existing':
                    self.change(project, 'document', item['id'], folder_id, kind,
                                {'reference': item.get('reference'), 'revision': item.get('revision'),
                                 'was_placeholder': bool(old and json.loads(old['metadata']).get('hasContent') == 'false')})
                self.db.execute('''INSERT INTO documents(project,id,folder_id,first_id,reference,filename,revision,title,size,metadata,
                  snapshot,active,fingerprint,content_fingerprint,content_changed,checked_cycle,change_type,related_state)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?) ON CONFLICT(project,id,folder_id) DO UPDATE SET
                  first_id=excluded.first_id,reference=excluded.reference,filename=excluded.filename,revision=excluded.revision,
                  title=excluded.title,size=excluded.size,metadata=excluded.metadata,snapshot=excluded.snapshot,active=1,
                  fingerprint=excluded.fingerprint,content_fingerprint=excluded.content_fingerprint,
                  content_changed=excluded.content_changed,change_type=excluded.change_type,related_state=excluded.related_state,error=NULL''',
                                (project, item['id'], folder_id, item.get('firstVersionId'), item.get('reference'), item.get('fileName'),
                                 item.get('revision'), item.get('name'), int(item.get('contentSize') or 0), json.dumps(item, ensure_ascii=False),
                                 self.snapshot_id, signature, content_signature, int(changed), 0, kind, related))
            self.db.execute('UPDATE folders SET total=?,next_start=?,list_state=?,error=NULL WHERE project=? AND id=?',
                            (total, next_start, 'done' if done else 'pending', project, folder_id))
            self.db.execute('INSERT OR REPLACE INTO scan_pages VALUES(?,?,?,?)', (project, folder_id, start, json.dumps(ids)))

    def replace_folder_records(self, project, folder_id, records):
        """Commit a fully fetched folder atomically; retain unchanged checks/files."""
        visible = {row['id'] for row in self.all('SELECT id FROM documents WHERE project=? AND folder_id=? AND active=1',
                                                (project, folder_id))}
        received = {record['id'] for record in records}
        with self.db:
            self.db.execute('DELETE FROM scan_pages WHERE project=? AND folder_id=?', (project, folder_id))
            self.save_page(project, folder_id, len(records), records, len(records), True, transaction=False)
            for identifier in sorted(visible - received):
                self.change(project, 'document', identifier, folder_id, 'not_visible')
                self.db.execute('UPDATE documents SET active=0 WHERE project=? AND folder_id=? AND id=?',
                                (project, folder_id, identifier))
            self.update_file_visibility(project)

    def finish_catalogue(self, project):
        pending = self.one("SELECT COUNT(*) n FROM folders WHERE project=? AND snapshot=? AND (tree_state!='done' OR list_state!='done')", (project, self.snapshot_id))['n']
        if pending:
            return False
        for folder in self.all('SELECT * FROM folders WHERE project=? AND snapshot=? AND parent IS NOT NULL', (project, self.snapshot_id)):
            count = self.one('SELECT COUNT(*) n FROM documents WHERE project=? AND folder_id=? AND snapshot=? AND active=1', (project, folder['id'], self.snapshot_id))['n']
            if count != folder['total']:
                raise RuntimeError('目录清单条数与平台总数不一致，停止而不删除旧任务。')
        with self.db:
            for entity, table in [('folder', 'folders'), ('document', 'documents')]:
                for row in self.all(f'SELECT * FROM {table} WHERE project=? AND active=1 AND snapshot!=?', (project, self.snapshot_id)):
                    self.change(project, entity, row['id'], row['folder_id'] if entity == 'document' else row['parent'], 'not_visible')
                self.db.execute(f'UPDATE {table} SET active=0 WHERE project=? AND snapshot!=?', (project, self.snapshot_id))
            self.db.execute("UPDATE snapshots SET status='catalogued' WHERE id=?", (self.snapshot_id,))
            self.update_file_visibility(project)
        return True

    def update_file_visibility(self, project, file_ids=None):
        sql = '''UPDATE files SET active=EXISTS(SELECT 1 FROM document_files x JOIN documents d
          ON d.project=x.project AND d.id=x.document_id AND d.folder_id=x.folder_id
          WHERE x.project=files.project AND x.file_id=files.id AND x.active=1 AND d.active=1)
          WHERE project=?'''
        if file_ids is None:
            self.db.execute(sql, (project,))
        else:
            self.db.executemany(sql+' AND id=?', ((project, identifier) for identifier in file_ids))

    def save_related(self, document, items, placeholder=False):
        if not placeholder and not items and json.loads(document['metadata']).get('hasContent') == 'true':
            raise RuntimeError('文档有内容但关联接口未返回文件，保留为待核对。')
        project = document['project']
        keys = (project, document['id'], document['folder_id'])
        with self.db:
            previous_ids = {row['file_id'] for row in self.all('SELECT file_id FROM document_files WHERE project=? AND document_id=? AND folder_id=? AND active=1', keys)}
            current = set()
            for item in items:
                if not item.get('id') or not item.get('filename'):
                    raise RuntimeError('关联文件缺少 ID 或文件名。')
                current.add(item['id'])
                old = self.one('SELECT * FROM files WHERE project=? AND id=?', (project, item['id']))
                raw_signature = fingerprint(item, CONTENT_FIELDS)
                old_metadata = json.loads(old['metadata']) if old else {}
                old_raw_signature = fingerprint(old_metadata, CONTENT_FIELDS) if old else None
                if old and (raw_signature == old_raw_signature or same_linked_version(old_metadata, item)):
                    signature = (fingerprint({'content': raw_signature, 'parent': document['content_fingerprint']})
                                 if document['content_changed'] and item['id'] == document['id'] else old['fingerprint'])
                else:
                    signature = raw_signature
                changed = bool(old and old['fingerprint'] != signature)
                if changed and old['path'] and old['sha256']:
                    self.db.execute('INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived) VALUES(?,?,?,?,?,?,?)',
                                    (project, item['id'], old['fingerprint'], old['path'], old['bytes'], old['sha256'], now().isoformat()))
                available = item.get('readAccessStatus') == 'Available'
                if item.get('updateAccessStatus') == 'IsDeleted' and item.get('isSourceDocument') == 'false':
                    available = False
                if not available:
                    status = 'done' if old and old['status'] == 'done' and not changed else 'restricted'
                else:
                    status = 'pending' if not old or changed or old['status'] == 'restricted' else old['status']
                if not old or changed:
                    prior_metadata = json.loads(old['metadata']) if old else {}
                    self.change(project, 'file', item['id'], document['folder_id'], 'updated' if old else 'added',
                        {'filename': item['filename'], 'parent_document_id': document['id'],
                         'previous_fingerprint': old['fingerprint'] if old else None,
                         'fingerprint': signature,
                         'changed_fields': [k for k in CONTENT_FIELDS if prior_metadata.get(k) != item.get(k)],
                         'parent_content_changed': bool(document['content_changed'] and item['id']==document['id'])})
                if item['id'] not in previous_ids:
                    self.change(project, 'association', item['id'], document['folder_id'], 'added', {'document_id': document['id']})
                self.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,revision,relation,size,metadata,status,
                  fingerprint,active,change_type,updated) VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                  ON CONFLICT(project,id) DO UPDATE SET metadata=excluded.metadata,filename=excluded.filename,
                  revision=excluded.revision,size=excluded.size,relation=excluded.relation,status=excluded.status,
                  fingerprint=excluded.fingerprint,active=1,change_type=excluded.change_type,updated=excluded.updated,
                  path=CASE WHEN ? THEN NULL ELSE files.path END,sha256=CASE WHEN ? THEN NULL ELSE files.sha256 END,
                  bytes=CASE WHEN ? THEN 0 ELSE files.bytes END,attempts=CASE WHEN ? THEN 0 ELSE files.attempts END,
                  error=CASE WHEN ? THEN NULL ELSE files.error END''',
                                (project, item['id'], document['id'], document['folder_id'], item['filename'], item.get('revision'),
                                 item.get('relationshipType') or 'Document', int(item.get('contentSize') or 0), json.dumps(item, ensure_ascii=False),
                                 status, signature, 'updated' if changed else 'added' if not old else 'existing', now().isoformat(),
                                 changed, changed, changed, changed or bool(old and old['status'] == 'restricted'), changed))
                self.db.execute('''INSERT INTO document_files VALUES(?,?,?,?,1)
                  ON CONFLICT(project,document_id,folder_id,file_id) DO UPDATE SET active=1
                  WHERE document_files.active!=1''', (*keys, item['id']))
                if changed:
                    self.db.execute('UPDATE files SET downloaded_at=NULL,download_date_source=NULL,target_path=NULL,retryable=1 WHERE project=? AND id=?', (project, item['id']))
            for removed in previous_ids - current:
                self.db.execute('''UPDATE document_files SET active=0
                  WHERE project=? AND document_id=? AND folder_id=? AND file_id=?''', (*keys, removed))
                self.change(project, 'association', removed, document['folder_id'], 'not_visible', {'document_id': document['id']})
            self.db.execute('UPDATE documents SET related_state=?,checked_cycle=?,content_changed=0,error=NULL WHERE project=? AND id=? AND folder_id=?',
                            ('no_content' if placeholder else 'done', self.cycle, *keys))
            self.update_file_visibility(project, previous_ids | current)

    def finish_refresh(self, project):
        pending = self.one("SELECT COUNT(*) n FROM documents WHERE project=? AND active=1 AND (checked_cycle<? OR related_state IN ('pending','failed'))", (project, self.cycle))['n']
        if pending:
            return False
        with self.db:
            self.db.execute("UPDATE refresh_cycles SET status='complete' WHERE project=?", (project,))
            self.db.execute("UPDATE snapshots SET status='complete',finished=? WHERE id=?", (now().isoformat(), self.snapshot_id))
        return True

    def changes_summary(self):
        return {row['entity'] + ':' + row['kind']: row['n'] for row in self.all('SELECT entity,kind,COUNT(*) n FROM changes WHERE snapshot=? GROUP BY entity,kind', (self.snapshot_id,))}
