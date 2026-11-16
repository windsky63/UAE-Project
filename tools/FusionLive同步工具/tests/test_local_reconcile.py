"""Real disk/SQLite conflict recovery; no server data or network access."""
import hashlib
import time
import unittest
from unittest.mock import patch

import file_layout as layout
from local_reconcile import DiskReconciler
from tests import test_file_layout as fixture


class LocalReconcileTests(unittest.TestCase):
    def setUp(self):
        self.case = fixture.LayoutTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.ledger, self.base = self.case.ledger, self.case.base
        self.source = self.case.add('one',data=b'new')
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                SELECT project,id,'old-version',path,3,?,'2026-01-01' FROM files''',
                (hashlib.sha256(b'old').hexdigest(),))

    def test_moved_old_reference_recovers_from_recorded_copy_without_tree_scan(self):
        old = self.base/'history/recorded.pdf'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'old')
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                SELECT project,file_id,fingerprint,'history/recorded.pdf',bytes,sha256,'2026-01-02' FROM file_history''')
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        with patch('local_reconcile.os.walk',side_effect=AssertionError('known paths should not scan')):
            self.assertEqual(service.tick(5),1)
        self.assertEqual(self.ledger.one('SELECT path FROM file_history ORDER BY id')[0],'history/recorded.pdf')
        self.assertEqual(self.source.read_bytes(),b'new')
        self.assertEqual(old.read_bytes(),b'old')
        self.assertEqual(self.ledger.one('SELECT count(*) FROM local_reconcile_audit')[0],1)
        self.assertTrue(list((self.base/'runtime/backups').glob('before-local-reconcile-*')))

    def test_unregistered_renamed_old_copy_is_found_only_in_idle_scan(self):
        old = self.base/'history/deep/renamed.bin'
        old.parent.mkdir(parents=True)
        old.write_bytes(b'old')
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        with patch('local_reconcile.os.walk',side_effect=AssertionError('fast path must not scan')):
            self.assertEqual(service.tick(5),0)
        self.assertEqual(service.tick(5,scan=True),1)
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],'history/deep/renamed.bin')
        self.assertEqual(self.source.read_bytes(),b'new')

    def test_same_size_wrong_content_is_not_adopted(self):
        wrong = self.base/'history/wrong.pdf'
        wrong.parent.mkdir(parents=True)
        wrong.write_bytes(b'bad')
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        self.assertEqual(service.tick(5),0)
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],self.source.relative_to(self.base).as_posix())
        self.assertFalse(list((self.base/'runtime').glob('backups/before-local-reconcile-*')))
        self.assertEqual(service.tick(5,scan=True),1)
        self.assertIsNone(self.ledger.one('SELECT path FROM file_history')[0])
        self.assertEqual(self.source.read_bytes(),b'new')

    def test_pending_exact_version_is_recovered_from_history(self):
        historical = self.base/'history/exact.pdf'
        historical.parent.mkdir(parents=True)
        historical.write_bytes(b'old')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE file_history SET path='history/exact.pdf'")
            self.ledger.db.execute("UPDATE files SET fingerprint='old-version',status='pending',path=NULL,sha256=NULL,bytes=0")
        service = DiskReconciler(self.ledger)
        service.schedule(keys=(('p','one'),))
        self.assertEqual(service.tick(5),1)
        row = self.ledger.one('SELECT * FROM files')
        self.assertEqual(row['status'],'done')
        self.assertEqual(row['path'],'history/exact.pdf')
        self.assertEqual(row['sha256'],hashlib.sha256(b'old').hexdigest())

    def test_no_conflicts_does_not_walk_or_hash(self):
        service = DiskReconciler(self.ledger)
        with patch('local_reconcile.os.walk',side_effect=AssertionError('normal download scanned')), \
             patch.object(service,'verify',side_effect=AssertionError('normal download hashed')):
            self.assertEqual(service.tick(5,scan=True),0)

    def test_pending_migration_is_not_rewritten(self):
        (self.base/'runtime/layout.pending').write_text('in progress')
        service = DiskReconciler(self.ledger)
        service.schedule(keys=(('p','one'),))
        self.assertEqual(service.tick(5,scan=True),0)
        self.assertEqual((self.base/'runtime/layout.pending').read_text(),'in progress')

    def test_inactive_conflict_repairs_path_without_reactivating_version(self):
        old = self.case.add('obsolete',data=b'old',active=0)
        original = dict(self.ledger.one("SELECT * FROM files WHERE id='obsolete'"))
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path=? WHERE id='obsolete'",
                                   (self.source.relative_to(self.base).as_posix(),))
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        service.tick(5,scan=True)
        row = dict(self.ledger.one("SELECT * FROM files WHERE id='obsolete'"))
        self.assertEqual(row,original)
        self.assertEqual(old.read_bytes(),b'old')
        self.assertEqual(self.source.read_bytes(),b'new')

    def test_missing_inactive_content_clears_only_wrong_reference(self):
        self.case.add('obsolete',data=b'old',active=0)
        original = dict(self.ledger.one("SELECT * FROM files WHERE id='obsolete'"))
        (self.base/original['path']).unlink()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path=? WHERE id='obsolete'",
                                   (self.source.relative_to(self.base).as_posix(),))
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        service.tick(5,scan=True)
        row = dict(self.ledger.one("SELECT * FROM files WHERE id='obsolete'"))
        self.assertEqual(row,{**original,'path':None})
        self.assertEqual(self.source.read_bytes(),b'new')

    def test_automatic_planning_conflict_schedules_and_repairs_history(self):
        # Two historical fingerprints claimed the same old path; one has a
        # separately recorded verified copy. The planner must pass that path
        # into the real reconciler rather than silently schedule zero jobs.
        case = fixture.LayoutTests()
        case.setUp()
        self.addCleanup(case.doCleanups)
        old, staged = case.prepare_incremental_fixture()
        ledger, base = case.ledger, case.base
        recovered = base/'history/recovered.pdf'
        recovered.parent.mkdir(parents=True)
        recovered.write_bytes(b'bad')
        digest = hashlib.sha256(b'bad').hexdigest()
        with ledger.db:
            ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                SELECT project,file_id,'other-version',path,bytes,?,'fixture-2' FROM file_history''',(digest,))
            ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                SELECT project,file_id,fingerprint,'history/recovered.pdf',bytes,sha256,'fixture-3'
                FROM file_history WHERE sha256=?''',(digest,))
        self.assertTrue(layout.prepare_downloads_or_defer(ledger))
        self.assertEqual(old.read_bytes(),b'new')
        self.assertFalse(staged.exists())
        rows = ledger.all('SELECT path,sha256 FROM file_history')
        self.assertTrue(all(hashlib.sha256((base/r['path']).read_bytes()).hexdigest()==r['sha256'] for r in rows))
        self.assertTrue(ledger.one('SELECT 1 FROM local_reconcile_audit'))
        self.assertTrue(list((base/'runtime/backups').glob('before-local-reconcile-*')))

    def test_large_hash_resumes_across_budget_slices(self):
        data = b'x'*(3*1024*1024)
        path = self.base/'history/large.bin'
        path.parent.mkdir(parents=True)
        path.write_bytes(data)
        clock = [0.0]
        def check():
            clock[0] += .1
        service = DiskReconciler(self.ledger,check)
        digest = hashlib.sha256(data).hexdigest()
        with patch('local_reconcile.time.monotonic',side_effect=lambda:clock[0]):
            self.assertIsNone(service.verify('history/large.bin',len(data),digest,.25))
            for _ in range(10):
                result = service.verify('history/large.bin',len(data),digest,clock[0]+.25)
                if result is not None:
                    break
        self.assertTrue(result)

    def test_retained_alias_follows_current_physical_move(self):
        with self.ledger.db:
            self.ledger.db.execute('UPDATE file_history SET sha256=?',(hashlib.sha256(b'new').hexdigest(),))
            self.ledger.db.execute("UPDATE files SET status='pending'")
            self.ledger.db.execute("INSERT INTO refresh_cycles VALUES('p',1,'complete','2026-01-01')")
        layout.prepare_downloads(self.ledger,reconcile=True)
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],self.ledger.one('SELECT path FROM files')[0])
        self.assertFalse(self.source.exists())

    def test_current_content_not_found_after_scan_is_requeued_without_deleting_bytes(self):
        self.source.write_bytes(b'bad')
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        self.assertEqual(service.tick(5,scan=True),2)
        current = self.ledger.one('SELECT * FROM files')
        self.assertEqual(current['status'],'pending')
        self.assertIsNone(current['path'])
        self.assertEqual(current['sha256'],hashlib.sha256(b'new').hexdigest())
        self.assertEqual(self.source.read_bytes(),b'bad')

    def test_scan_permission_error_does_not_prove_absence(self):
        service = DiskReconciler(self.ledger)
        service.schedule(paths=(self.source.relative_to(self.base).as_posix(),))
        def denied(root,*,onerror,**kwargs):
            onerror(PermissionError('fixture denied directory'))
            return iter(())
        with patch('local_reconcile.os.walk',side_effect=denied):
            self.assertEqual(service.tick(5,scan=True),0)
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],self.source.relative_to(self.base).as_posix())

    def test_idle_scan_continues_from_checkpoint_after_process_restart(self):
        folder = self.base/'history'
        folder.mkdir()
        for index in range(20):
            (folder/('%02d.txt'%index)).write_bytes(b'irrelevant')
        old = folder/'zz-old-copy.pdf'
        old.write_bytes(b'old')
        service = DiskReconciler(self.ledger)
        path = self.source.relative_to(self.base).as_posix()
        service.schedule(paths=(path,))
        service.tick(5)  # Settle known paths before testing a bounded tree slice.
        clock = [0.0]
        def check():
            clock[0] += .15
        service.check = check
        with patch('local_reconcile.time.monotonic',side_effect=lambda:clock[0]):
            service.tick(.6,scan=True)
        self.assertIsNotNone(self.ledger.one('SELECT payload FROM local_reconcile_scan_state'))
        restarted = DiskReconciler(self.ledger)
        restarted.resume()
        self.assertTrue(restarted.visited)
        restarted.tick(5,scan=True)
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],'history/zz-old-copy.pdf')


if __name__ == '__main__':
    unittest.main()
