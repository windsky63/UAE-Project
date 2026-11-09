"""Retention verifies content and journals physical deletion before changing refs."""
import hashlib
import json
import unittest
from unittest.mock import patch

import file_layout as layout
from history_retention import HistoryRetention
from tests import test_file_layout as fixture


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.LayoutTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.ledger, self.base = self.case.ledger, self.case.base
        self.latest = self.case.add('one',data=b'latest')
        layout.prepare_downloads(self.ledger)
        self.latest = self.base/self.ledger.one('SELECT path FROM files')[0]
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO refresh_cycles VALUES('p',1,'complete','2026-01-01')")
        self.paths = [self.add_history(i,('version-'+str(i)).encode()) for i in range(5)]

    def add_history(self,index,data,name=None):
        relative = 'history/'+(name or ('v'+str(index)+'.pdf'))
        path = self.base/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                VALUES('p','one',?,?,?,?,?)''',
                ('fp-'+str(index),relative,len(data),hashlib.sha256(data).hexdigest(),'2026-01-%02d'%(index+1)))
        return path

    def test_keeps_first_and_two_latest_old_contents_and_removes_duplicates(self):
        duplicate = self.add_history(5,b'version-0','first-duplicate.pdf')
        latest_copy = self.add_history(6,b'latest','latest-duplicate.pdf')
        service = HistoryRetention(self.ledger,lambda:None)
        self.assertEqual(service.tick(10),4)
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])
        self.assertFalse(duplicate.exists())
        self.assertFalse(latest_copy.exists())
        self.assertEqual(self.latest.read_bytes(),b'latest')
        self.assertEqual(self.ledger.one('SELECT count(*) FROM file_history')[0],7)
        self.assertEqual(self.ledger.one('SELECT count(*) FROM file_history WHERE path IS NOT NULL')[0],3)
        self.assertEqual(self.ledger.one("SELECT count(*) FROM history_retention_jobs WHERE state='done'")[0],4)
        self.assertTrue(list((self.base/'runtime/backups').glob('before-history-retention-*')))

    def test_missing_or_corrupt_latest_never_authorizes_deletion(self):
        self.latest.write_bytes(b'broken')
        self.assertEqual(HistoryRetention(self.ledger,lambda:None).tick(10),0)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_live_reference_and_recovery_queries_use_indexes(self):
        HistoryRetention(self.ledger,lambda:None).tick(10)
        queries = [
            ('SELECT 1 FROM files WHERE active=1 AND path=?',('history/v1.pdf',)),
            ('SELECT 1 FROM file_history WHERE path=? AND (sha256 IS NOT ? OR bytes IS NOT ?)',
             ('history/v1.pdf','digest',9)),
            ('UPDATE file_history SET path=NULL WHERE path=? AND sha256=? AND bytes=?',
             ('history/v1.pdf','digest',9)),
            ('UPDATE files SET path=NULL,target_path=NULL WHERE active=0 AND path=? AND sha256=? AND bytes=?',
             ('history/v1.pdf','digest',9)),
            ("SELECT payload FROM history_retention_jobs WHERE state='pending'",()),
        ]
        for sql,params in queries:
            with self.subTest(sql=sql):
                plan = [r[3] for r in self.ledger.all('EXPLAIN QUERY PLAN '+sql,params)]
                self.assertTrue(any('SEARCH ' in item and 'INDEX' in item for item in plan),plan)
                self.assertFalse(any('SCAN ' in item for item in plan),plan)

    def test_many_deletions_do_not_repeatedly_sweep_recovery_journal(self):
        duplicates = [self.add_history(i,b'latest') for i in range(5,45)]
        statements = []
        self.ledger.db.set_trace_callback(statements.append)
        try:
            service = HistoryRetention(self.ledger,lambda:None)
            service.tick(10)
        finally:
            self.ledger.db.set_trace_callback(None)
        sweeps = [sql for sql in statements if sql ==
                  "SELECT payload FROM history_retention_jobs WHERE state='pending'"]
        self.assertEqual(len(sweeps),1)
        self.assertEqual(service.deleted_files,42)
        self.assertTrue(all(not p.exists() for p in duplicates))
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])
        self.assertEqual(self.ledger.one('SELECT count(*) FROM file_history')[0],45)

    def test_incomplete_inventory_defers_cleanup(self):
        with self.ledger.db:
            self.ledger.db.execute("UPDATE refresh_cycles SET status='running'")
        self.assertEqual(HistoryRetention(self.ledger,lambda:None).tick(10),0)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_unknown_file_is_not_deleted(self):
        unknown = self.base/'history/unregistered.txt'
        unknown.write_text('unknown')
        HistoryRetention(self.ledger,lambda:None).tick(10)
        self.assertEqual(unknown.read_text(),'unknown')

    def test_active_reference_pins_old_history_copy(self):
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,
                size,bytes,path,sha256,active) SELECT project,'pinned',document_id,folder_id,filename,metadata,'done',
                9,9,'history/v1.pdf',?,1 FROM files WHERE id='one' ''',
                (hashlib.sha256(b'version-1').hexdigest(),))
        HistoryRetention(self.ledger,lambda:None).tick(10)
        self.assertTrue(self.paths[1].exists())

    def test_delete_interruption_recovers_without_losing_version_evidence(self):
        service = HistoryRetention(self.ledger,lambda:None)
        with patch.object(service,'_finish_delete',side_effect=OSError('crash after unlink')):
            with self.assertRaises(OSError):
                service.tick(10)
        self.assertEqual(self.ledger.one("SELECT count(*) FROM history_retention_jobs WHERE state='pending'")[0],1)
        HistoryRetention(self.ledger,lambda:None).tick(10)
        self.assertEqual(self.ledger.one("SELECT count(*) FROM history_retention_jobs WHERE state='pending'")[0],0)
        self.assertEqual(self.ledger.one('SELECT count(*) FROM file_history')[0],5)
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])

    def test_changed_platform_ids_share_own_document_lineage(self):
        old = self.case.add('old',filename='older-name.pdf',source='history/id-old.pdf',data=b'old-id',active=0)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE documents SET first_id='family',active=CASE WHEN id='old' THEN 0 ELSE 1 END")
            self.ledger.db.execute("UPDATE files SET downloaded_at='2026-01-02',updated='2026-01-02' WHERE id='old'")
        HistoryRetention(self.ledger,lambda:None).tick(10)
        self.assertFalse(old.exists())
        self.assertIsNone(self.ledger.one("SELECT path FROM files WHERE id='old'")[0])
        self.assertTrue(self.paths[0].exists())

    def test_busy_reader_defers_cleanup(self):
        from analysis_worker import worker_lock
        with worker_lock(self.base/'runtime/file_analysis') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(HistoryRetention(self.ledger,lambda:None).tick(10),0)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_initial_content_reused_as_latest_does_not_keep_extra_old_version(self):
        self.latest.write_bytes(b'version-0')
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET bytes=9,size=9,sha256=? WHERE id=?',
                                  (hashlib.sha256(b'version-0').hexdigest(),'one'))
        HistoryRetention(self.ledger,lambda:None).tick(10)
        self.assertEqual([p.exists() for p in self.paths],[False,False,False,True,True])
        self.assertEqual(self.latest.read_bytes(),b'version-0')

    def test_pending_analysis_reference_pins_history_file(self):
        import sqlite3
        from contextlib import closing
        folder = self.base/'runtime/file_analysis'
        folder.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(folder/'queue.sqlite3')) as db:
            db.execute('CREATE TABLE jobs(path TEXT,origin TEXT,state TEXT,active INTEGER)')
            db.execute("INSERT INTO jobs VALUES(?,?,'pending',1)",(str(self.paths[1]),str(self.paths[1])))
            db.commit()
        HistoryRetention(self.ledger,lambda:None).tick(10)
        self.assertTrue(self.paths[1].exists())

    def test_slow_metadata_planning_still_gets_a_bounded_deletion_slice(self):
        service = HistoryRetention(self.ledger,lambda:None)
        original = service._plan
        clock = [0.0]
        def slow_plan():
            original()
            clock[0] += 1.2  # Real server copy needs more than the .5s work slice.
        with patch.object(service,'_plan',side_effect=slow_plan), \
             patch('history_retention.time.monotonic',side_effect=lambda:clock[0]):
            self.assertEqual(service.tick(.5),2)
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])

    def test_automatic_local_reference_maintenance_never_deletes_history(self):
        from sync import maintain_local_references
        with patch('history_retention.prune_history',side_effect=AssertionError('automatic cleanup')):
            maintain_local_references(self.ledger)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_manual_executor_keeps_progress_in_memory(self):
        from history_retention import run_manual
        updates=[]
        self.assertEqual(run_manual(self.base,'manual-test',progress=updates.append),0)
        status=updates[-1]
        self.assertEqual(status['state'],'succeeded')
        self.assertEqual(status['deleted_files'],2)
        self.assertEqual(status['deleted_bytes'],18)
        self.assertTrue(status['manual_only'])
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])
        self.assertFalse(list((self.base/'runtime').glob('history_retention*.json*')))

    def test_obsolete_locked_status_file_is_never_touched(self):
        from history_retention import run_manual
        path=self.base/'runtime/history_retention_status.json'
        path.write_text('obsolete status',encoding='utf-8')
        with path.open('rb'):
            self.assertEqual(run_manual(self.base,'no-status-file'),0)
        self.assertEqual(path.read_text(encoding='utf-8'),'obsolete status')

    def test_progress_channel_failure_does_not_abort_cleanup(self):
        from history_retention import run_manual
        with patch('history_retention.os.replace',side_effect=AssertionError('status replacement')):
            def unavailable(data):
                raise BrokenPipeError('manager disconnected')
            self.assertEqual(run_manual(self.base,'no-display',progress=unavailable),0)
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])

    def test_failed_cleanup_still_reports_failure_in_memory(self):
        from history_retention import run_manual
        updates=[]
        with self.ledger.db:
            self.ledger.db.execute("UPDATE refresh_cycles SET status='running'")
        self.assertEqual(run_manual(self.base,'failure-test',progress=updates.append),2)
        self.assertEqual(updates[-1]['state'],'failed')
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_manual_cleanup_can_stop_before_deleting(self):
        from history_retention import run_manual
        updates=[]
        self.assertEqual(run_manual(self.base,'stop-test',progress=updates.append,cancelled=lambda:True),0)
        self.assertEqual(updates[-1]['state'],'stopped')
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_manual_cleanup_rejects_live_download(self):
        from history_retention import run_manual
        from analysis_worker import worker_lock
        with worker_lock(self.base/'runtime','sync.lock') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(run_manual(self.base,'busy-test'),2)
        self.assertTrue(all(p.exists() for p in self.paths))

    def test_second_manual_process_reports_its_own_failure(self):
        from history_retention import run_manual
        from analysis_worker import worker_lock
        updates=[]
        with worker_lock(self.base/'runtime','history_retention.lock') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(run_manual(self.base,'duplicate-test',progress=updates.append),2)
        self.assertEqual(updates[-1]['job_id'],'duplicate-test')
        self.assertEqual(updates[-1]['state'],'failed')

    def test_real_manual_cli_has_no_download_time_gate_or_status_file(self):
        import subprocess,sys
        from pathlib import Path
        script = Path(__file__).resolve().parents[1]/'app/history_retention.py'
        result = subprocess.run([sys.executable,str(script),'--base-dir',str(self.base),'--execute','--job-id','cli-test'],
                                capture_output=True,timeout=60)
        self.assertEqual(result.returncode,0,result.stderr.decode('utf-8',errors='replace'))
        self.assertEqual([p.exists() for p in self.paths],[True,False,False,True,True])
        self.assertFalse(list((self.base/'runtime').glob('history_retention*.json*')))

    def test_real_cleanup_streams_to_manager_memory_and_logs_final_result(self):
        import sys
        from pathlib import Path
        sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'管理中心'))
        from manager import Manager
        manager=Manager(self.base,self.base/'admin-runtime')
        script=Path(__file__).resolve().parents[1]/'app/history_retention.py'
        command=[sys.executable,'-u',str(script),'--base-dir',str(self.base),'--execute','--progress-pipe']
        with patch.object(manager,'command',return_value=(command,self.base)):
            task=manager.action('sync','clean-history')
        process=manager.children[task['id']]
        try:
            process.wait(timeout=60)
            status=manager.history_cleanup_status()
            self.assertEqual(status['state'],'succeeded')
            self.assertEqual(status['deleted_files'],2)
            self.assertEqual(status['deleted_bytes'],18)
            self.assertFalse(list((self.base/'runtime').glob('history_retention*.json*')))
            log=next((self.base/'logs').rglob('*.log')).read_text(encoding='utf-8')
            self.assertIn('"state": "succeeded"',log)
            self.assertNotIn('"state": "running"',log)
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=60)
            manager.tasks()


if __name__ == '__main__':
    unittest.main()
