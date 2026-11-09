"""Persist inventory -> initial bulk download -> daily maintenance in SQLite."""

from paths import HOME, CODE
import json
import os
from fusion_client import now
from reporting import volume
from paths import resolve_path, exports_dir

STAGES = {'inventory': '阶段一：全量清单与关联盘点',
          'scope_review': '全量盘点已完成，等待人工确认施工范围',
          'bulk_download': '阶段二：集中下载',
          'maintenance': '阶段三：每日盘点与增量补下载'}


class Workflow:
    def __init__(self, ledger, client):
        self.ledger, self.client = ledger, client
        self.project = client.project_id
        row = ledger.one('SELECT * FROM workflows WHERE project=?', (self.project,))
        if row is None:
            snapshot = ledger.one('SELECT * FROM snapshots WHERE project=? ORDER BY id DESC LIMIT 1', (self.project,))
            cycle = ledger.one('SELECT cycle FROM refresh_cycles WHERE project=?', (self.project,))
            complete = snapshot and snapshot['status'] == 'complete'
            approved = self.has_approved_scope()
            phase = ('download' if approved else 'review') if complete else 'associations' if snapshot and snapshot['status'] == 'catalogued' else 'catalogue'
            with ledger.db:
                ledger.db.execute('INSERT INTO workflows(project,stage,phase,snapshot_id,cycle,updated) VALUES(?,?,?,?,?,?)',
                    (self.project, ('bulk_download' if approved else 'scope_review') if complete else 'inventory', phase,
                     snapshot['id'] if snapshot else None, cycle['cycle'] if cycle else None, now().isoformat()))
        self.reload()
        from reporting import establish_report_baseline
        establish_report_baseline(ledger, at=now(), include_local=False)
        self.apply_download_plan()
        # Older releases downloaded the whole project without a scope checkpoint.
        # After this update they must stop for review, retaining every existing file.
        if self.state['stage'] in ('bulk_download', 'maintenance') and not self.has_approved_scope():
            if self.inventory_ready():
                self.save(stage='scope_review', phase='review')
            else:
                snapshot = ledger.one('SELECT status FROM snapshots WHERE id=?', (self.state['snapshot_id'],))
                self.save(stage='inventory', phase='catalogue' if not snapshot or snapshot['status'] == 'scanning' else 'associations')
        elif self.state['stage'] == 'scope_review' and self.has_approved_scope():
            self.save(stage='bulk_download', phase='download')

    def has_approved_scope(self):
        row = self.ledger.one('SELECT * FROM download_scopes WHERE project=?', (self.project,))
        if not row or not row['confirmed_at']:
            return False
        # Only an explicitly approved whole-project scope is supported.
        return json.loads(row['definition']).get('mode') == 'all'

    def cached_queue_ready(self, definition):
        """Validate the approved baseline; never disguise deferred failures as complete."""
        if (definition.get('snapshot_id'), definition.get('cycle')) != (self.state['snapshot_id'], self.state['cycle']):
            return False
        snapshot = self.ledger.one('SELECT status FROM snapshots WHERE project=? AND id=?',
                                   (self.project, self.state['snapshot_id']))
        cycle = self.ledger.one('SELECT cycle FROM refresh_cycles WHERE project=?', (self.project,))
        if not snapshot or snapshot['status'] not in ('catalogued', 'complete') or not cycle or cycle['cycle'] != self.state['cycle']:
            return False
        folders = self.ledger.all('SELECT tree_state,list_state FROM folders WHERE project=? AND active=1', (self.project,))
        docs = self.ledger.all('SELECT id,reference,related_state,checked_cycle FROM documents WHERE project=? AND active=1', (self.project,))
        counts = {'folders': len(folders), 'documents': len(docs),
                  'content_files': self.ledger.one('SELECT COUNT(*) n FROM files WHERE project=? AND active=1', (self.project,))['n']}
        if counts != definition.get('expected_counts') or not all(counts.values()):
            return False
        if any(r['tree_state'] != 'done' or r['list_state'] != 'done' for r in folders):
            return False
        accepted = {(r['version_id'], r['reference']) for r in definition.get('deferred_associations', [])}
        for doc in docs:
            if doc['related_state'] in ('done', 'no_content') and doc['checked_cycle'] >= self.state['cycle']:
                continue
            if doc['related_state'] != 'failed' or (doc['id'], doc['reference']) not in accepted:
                return False
        # Every known content must have a checked parent; do not create an unselectable queue.
        orphan = self.ledger.one('''SELECT COUNT(*) n FROM files i WHERE i.project=? AND i.active=1 AND NOT EXISTS(
          SELECT 1 FROM document_files x JOIN documents d ON d.project=x.project AND d.id=x.document_id
          AND d.folder_id=x.folder_id WHERE x.project=i.project AND x.file_id=i.id AND x.active=1
          AND d.active=1 AND d.related_state='done' AND d.checked_cycle=?)''', (self.project, self.state['cycle']))['n']
        return not orphan

    def apply_download_plan(self):
        """One-time, offline import of the operator-approved plan on the running PC."""
        target = self.ledger.base_dir / 'approved_download_plan.json'
        if not target.exists() or self.has_approved_scope():
            return
        plan = json.loads(target.read_text(encoding='utf-8'))
        if plan.get('project') != self.project:
            return
        if plan.get('mode') != 'all' or plan.get('approved') is not True:
            raise RuntimeError('下载计划无效：需要已批准的全项目下载范围。')
        definition = {**plan, 'snapshot_id': self.state['snapshot_id'], 'cycle': self.state['cycle']}
        if not self.cached_queue_ready(definition):
            raise RuntimeError('第二阶段基线检查未通过：需要运行电脑已完成的380目录/6179文档/15421内容清单，且仅允许计划中指定的关联异常。请保留并核对运行电脑原 runtime；不会登录或自动重新盘点。')
        with self.ledger.db:
            self.ledger.db.execute('INSERT OR REPLACE INTO download_scopes(project,definition,confirmed_at) VALUES(?,?,?)',
                                   (self.project, json.dumps(definition, ensure_ascii=False), now().isoformat()))
            self.ledger.db.execute("UPDATE workflows SET stage='bulk_download',phase='download',updated=? WHERE project=?",
                                   (now().isoformat(), self.project))
        self.reload()
        print('已应用人工批准的全项目下载计划：仅下载现有清单；指定的1条关联异常保留，第三阶段再核对。', flush=True)

    def download_ready(self):
        if self.inventory_ready():
            return True
        row = self.ledger.one('SELECT definition FROM download_scopes WHERE project=?', (self.project,))
        return bool(self.state['stage'] == 'bulk_download' and row and
                    self.cached_queue_ready(json.loads(row['definition'])))

    def reload(self):
        self.state = dict(self.ledger.one('SELECT * FROM workflows WHERE project=?', (self.project,)))
        self.ledger.snapshot_id = self.state['snapshot_id']
        self.ledger.cycle = self.state['cycle']

    def save(self, **values):
        values['updated'] = now().isoformat()
        with self.ledger.db:
            self.ledger.db.execute('UPDATE workflows SET ' + ','.join(k + '=?' for k in values) + ' WHERE project=?',
                                   (*values.values(), self.project))
        self.reload()

    def daily_inventory_enabled(self):
        target = self.ledger.base_dir / 'approved_download_plan.json'
        if not target.exists():
            return False
        plan = json.loads(target.read_text(encoding='utf-8'))
        return plan.get('project') == self.project and plan.get('daily_inventory_before_download') is True

    def daily_inventory_due(self):
        return self.daily_inventory_enabled() and self.state['last_inventory_day'] != now().date().isoformat()

    def begin_inventory(self, reset_initial=False, new_cycle=False):
        # One transaction binds the new snapshot to the durable workflow checkpoint.
        def checkpoint(snapshot, cycle):
            self.ledger.db.execute("UPDATE workflows SET stage=?,phase='catalogue',snapshot_id=?,cycle=?,updated=? WHERE project=?",
                ('inventory' if reset_initial else self.state['stage'], snapshot, cycle, now().isoformat(), self.project))
        options = {'new_cycle':True} if new_cycle else {}
        self.ledger.root(self.client, rescan=True, checkpoint=checkpoint, **options)
        self.reload()

    def refresh_failed_folders(self, check=lambda: None):
        """Refresh only failing folders, at most once per folder per 30 minutes."""
        with self.ledger.db:
            self.ledger.db.execute('''CREATE TABLE IF NOT EXISTS association_folder_refreshes(
                project TEXT,folder_id TEXT,checked_at TEXT,PRIMARY KEY(project,folder_id))''')
        folders = self.ledger.all('''SELECT DISTINCT d.folder_id,r.checked_at FROM documents d
            LEFT JOIN association_folder_refreshes r ON r.project=d.project AND r.folder_id=d.folder_id
            WHERE d.project=? AND d.active=1 AND d.related_state='failed' ORDER BY d.folder_id''', (self.project,))
        from datetime import datetime
        for folder in folders:
            at = now()
            if folder['checked_at'] and 0 <= (at-datetime.fromisoformat(folder['checked_at'])).total_seconds() < 1800:
                continue
            check()
            records, seen, expected, start = [], set(), None, 0
            while True:
                check()
                total, page = self.client.document_page(folder['folder_id'], start, 100)
                if (not isinstance(total, int) or total < 0 or (expected is not None and total != expected)
                        or start + len(page) > total or (start < total and not page)):
                    raise RuntimeError('失败目录刷新分页不完整或总数变化，保留原清单并稍后重试。')
                identifiers = [record.get('id') for record in page]
                if any(not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers) or seen.intersection(identifiers):
                    raise RuntimeError('失败目录刷新包含重复或缺失的版本ID，保留原清单。')
                records.extend(page)
                seen.update(identifiers)
                expected, start = total, start + len(page)
                if start == total:
                    break
            check()
            self.ledger.replace_folder_records(self.project, folder['folder_id'], records)
            with self.ledger.db:
                self.ledger.db.execute('INSERT OR REPLACE INTO association_folder_refreshes VALUES(?,?,?)',
                                       (self.project, folder['folder_id'], at.isoformat()))
            print(f'失败目录版本清单已刷新：{folder["folder_id"]}，{len(records)}条；旧版本停止重试，未变化的核对进度保留。', flush=True)

    def inventory_ready(self):
        snapshot = self.ledger.one('SELECT status FROM snapshots WHERE id=? AND project=?', (self.state['snapshot_id'], self.project))
        cycle = self.ledger.one('SELECT * FROM refresh_cycles WHERE project=?', (self.project,))
        if not snapshot or snapshot['status'] != 'complete' or not cycle or cycle['status'] != 'complete' or cycle['cycle'] != self.state['cycle']:
            return False
        pending = self.ledger.one("SELECT COUNT(*) n FROM documents WHERE project=? AND active=1 AND (checked_cycle<? OR related_state NOT IN ('done','no_content'))",
                                  (self.project, self.state['cycle']))['n']
        return not pending

    def complete_inventory(self):
        if not self.inventory_ready():
            raise RuntimeError('完整目录或关联清单尚未完成，不能进入只下载阶段。')
        from reporting import record_owner_inventory
        record_owner_inventory(self.ledger,self.project,self.state['snapshot_id'],at=now())
        report = {'captured_at': now().isoformat(), 'project': self.project,
                  'snapshot': self.state['snapshot_id'], 'volume': volume(self.ledger, self.project),
                  'content_files': self.queue_summary()}
        target = exports_dir(self.ledger.base_dir) / f'项目文件体量-{self.project}-清单{self.state["snapshot_id"]}.json'
        temp = target.with_suffix('.json.tmp')
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, target)
        approved = self.has_approved_scope()
        self.save(stage=('bulk_download' if self.state['stage'] == 'inventory' else self.state['stage']) if approved else 'scope_review',
                  phase='download' if approved else 'review', last_inventory_day=now().date().isoformat())
        from scope_review import export_scope_review
        export_scope_review(self.ledger, self.project)
        print('完整原件/附件清单：', report['volume']['content_files']['n'], '个内容 ID；合计', report['volume']['content_GB'],
              'GB（含已识别的受限内容）；报告：', target, flush=True)

    def queue_summary(self):
        return {r['status']: {'files': r['n'], 'bytes': r['size']} for r in self.ledger.all('''
          SELECT status,COUNT(*) n,COALESCE(SUM(size),0) size FROM files WHERE project=? AND active=1 GROUP BY status''', (self.project,))}

    def unfinished(self):
        return self.ledger.one("SELECT COUNT(*) n FROM files WHERE project=? AND active=1 AND status NOT IN ('done','restricted') AND NOT(status='failed' AND retryable=0)", (self.project,))['n']

    def repair_missing_downloads(self):
        from file_layout import has_pending_publications, prepare_downloads_or_defer
        if ((self.ledger.base_dir/'runtime/layout.pending').exists()
                or has_pending_publications(self.ledger)):
            if not prepare_downloads_or_defer(self.ledger):
                # The migration journal owns paths that may currently be staged.
                # Do not reset those done records while independent HTTP continues.
                return 0
        # A cheap existence/size check; full SHA-256 verification remains an offline command.
        from reporting import local_presence
        checks = {}
        local_presence(self.ledger, now(), checks=checks)
        missing = []
        # Probe the filesystem before opening a write transaction. A missing
        # early file must not hold the SQLite writer lock for the entire scan.
        for item in self.ledger.all("SELECT * FROM files WHERE project=? AND active=1 AND status='done'", (self.project,)):
            key = (item['project'], item['id'], item['path'], item['sha256'], item['bytes'])
            if key in checks:
                if not checks[key]:
                    missing.append((self.project, item['id'], item['path'], item['sha256']))
                continue
            path = resolve_path(item['path'], self.ledger.base_dir) if item['path'] else None
            if not path or not item['sha256'] or not path.is_file() or path.stat().st_size != item['bytes']:
                missing.append((self.project, item['id'], item['path'], item['sha256']))
        with self.ledger.db:
            count = self.ledger.db.executemany("UPDATE files SET status='pending',attempts=0,error='本地文件缺失或大小不符',downloaded_at=NULL,download_date_source=NULL WHERE project=? AND id=? AND status='done' AND path IS ? AND sha256 IS ?", missing).rowcount
        if self.state['phase'] == 'idle' and self.unfinished():
            self.save(phase='download')
        return count

    def finish_download(self):
        from file_layout import has_pending_publications
        if ((self.ledger.base_dir/'runtime/layout.pending').exists()
                or has_pending_publications(self.ledger)):
            self.save(phase='download')
            print('已校验文件仍待发布，保留下载阶段；下轮恢复，不重复下载。', flush=True)
            return False
        remaining = self.unfinished()
        if remaining:
            self.save(phase='download')
            print('本轮仍有', remaining, '个可下载文件未完成（含失败或重试耗尽），保留下载阶段。', flush=True)
            return False
        values = dict(stage='maintenance', phase='idle')
        if self.state['stage'] == 'bulk_download':
            values['initial_download_completed'] = now().isoformat()
        self.save(**values)
        restricted = self.ledger.one("SELECT COUNT(*) n FROM files WHERE project=? AND active=1 AND status='restricted'", (self.project,))['n']
        permanent = self.ledger.one("SELECT COUNT(*) n FROM files WHERE project=? AND active=1 AND status='failed' AND retryable=0", (self.project,))['n']
        print('当前清单中可自动下载文件已完成；受限文件', restricted, '个；永久性失败', permanent,
              '个（均不标记为下载成功）。后续进入每日维护。', flush=True)
        return True

    def maintenance_due(self):
        today = now().date().isoformat()
        return self.state['last_inventory_day'] != today and (self.state['initial_download_completed'] or '')[:10] != today
