"""Never archive a predecessor before a verified replacement is available."""
import hashlib
import json
from pathlib import Path
import unittest
import subprocess
import sys
from unittest.mock import patch

import file_layout as layout
from tests import test_download_replacement as replacement
from tests.test_inventory import doc, content


class PredecessorRetentionTests(unittest.TestCase):
    def test_package_role_does_not_merge_main_document_with_same_name_wtr(self):
        case = self.case
        case.old.unlink()
        case.scan([doc('v2', revision='3')], [content('v2', revision='3')])
        document = case.ledger.one("SELECT * FROM documents WHERE id='v2'")
        case.ledger.save_related(document, [content('v2', revision='3'),
            content('wtr2', revision='3', relationshipType='Attachment', reference='DRAWING-001_WTR')])
        with case.ledger.db:
            case.ledger.db.execute("UPDATE files SET folder_id='r' WHERE id='v2'")
            case.ledger.db.execute("UPDATE files SET document_id='package',relation='Attachment',path=NULL,status='pending',sha256=NULL,bytes=0 WHERE id='v1'")
        mapping = layout.targets(case.ledger)
        main_target = mapping[('p','v2')]['path']
        wtr_target = mapping[('p','wtr2')]['path']
        # A previous main version still occupies the future WTR path, while
        # the WTR predecessor has already been archived (the server incident).
        old_main = case.base/wtr_target
        old_main.parent.mkdir(parents=True, exist_ok=True)
        old_main.write_bytes(b'main-v2')
        old_wtr = case.base/'history/wtr-v1.pdf'
        old_wtr.parent.mkdir(parents=True, exist_ok=True)
        old_wtr.write_bytes(b'wtr-v1')
        with case.ledger.db:
            case.ledger.db.execute('INSERT INTO file_history(project,file_id,path,bytes,sha256,archived) VALUES(?,?,?,?,?,?)',
                ('p','v1',wtr_target,7,hashlib.sha256(b'main-v2').hexdigest(),'2026-11-03'))
            row = dict(case.ledger.one("SELECT * FROM files WHERE id='wtr2'"))
            row.update(id='wtr1',document_id='v1',active=0,status='done',path='history/wtr-v1.pdf',
                       bytes=6,sha256=hashlib.sha256(b'wtr-v1').hexdigest(),updated='2026-10-25')
            case.ledger.db.execute('INSERT INTO files ('+','.join(row)+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        files = [dict(r) for r in case.ledger.all('SELECT * FROM files')]
        links = layout.replacement_links(case.ledger, files, mapping, conservative=False)
        self.assertEqual([r['id'] for r in links[('p','v1')]], ['v2'])
        self.assertEqual([r['id'] for r in links[('p','wtr1')]], ['wtr2'])
        proposal = layout.plan(case.ledger, reconcile=True)
        self.assertIn((wtr_target, main_target), [(m['source'],m['target']) for m in proposal['moves']])
        layout.apply(case.ledger, proposal)
        self.assertEqual((case.base/main_target).read_bytes(), b'main-v2')
        self.assertEqual((case.base/wtr_target).read_bytes(), b'wtr-v1')
        self.assertEqual(case.ledger.one("SELECT status FROM files WHERE id='v2'")[0], 'pending')
        self.assertEqual(case.ledger.one("SELECT status FROM files WHERE id='wtr2'")[0], 'pending')
        self.assertFalse(list((case.base/'runtime/path_migration').iterdir()))

    def test_own_association_survives_package_parent_and_relation_change(self):
        case=self.case
        with case.ledger.db:
            case.ledger.db.execute("UPDATE files SET document_id='historical-package',relation='Attachment' WHERE id='v1'")
        case.scan([doc('v2',revision='1')],[content('v2',filename='renamed.pdf',revision='1')])
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        from reporting import system_report_stats
        stats=system_report_stats(case.ledger)
        self.assertEqual(stats['effective_downloads']['retained']['files'],1)
        self.assertTrue(stats['directory_reconciled'])
        case.client.download.side_effect=case.receive
        from sync import download
        download(case.client,case.ledger)
        self.assertTrue(case.ledger.one("SELECT path FROM files WHERE id='v1'")[0].startswith('history/'))

    def test_checkprint_revision_change_keeps_and_counts_one_own_old_member(self):
        case=self.case
        with case.ledger.db:
            case.ledger.db.execute("UPDATE files SET id='a1',filename='drawing_1 Checkprint.zip',relation='Attachment' WHERE id='v1'")
            case.ledger.db.execute("UPDATE document_files SET file_id='a1' WHERE file_id='v1'")
        case.scan([doc('v2',revision='2')],
                  [content('a2',filename='drawing_2 Checkprint.zip',relationshipType='Attachment')])
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        from reporting import system_report_stats
        stats=system_report_stats(case.ledger)
        self.assertEqual(stats['effective_downloads']['total'],dict(files=1,bytes=3))
        self.assertTrue(stats['directory_reconciled'])

    def test_completed_inventory_archives_removed_xlsx_and_keeps_own_pdf(self):
        case=self.case
        original=dict(case.ledger.one("SELECT * FROM files WHERE id='v1'"))
        sheet=case.old.with_suffix('.xlsx')
        case.old.rename(sheet)
        case.old=sheet
        with case.ledger.db:
            case.ledger.db.execute("UPDATE files SET id='sheet',filename='drawing.xlsx',relation='Attachment',path=?,target_path=NULL WHERE id='v1'",
                                   (sheet.relative_to(case.base).as_posix(),))
            case.ledger.db.execute("UPDATE document_files SET file_id='sheet' WHERE file_id='v1'")
            original.update(id='old-pdf',filename='drawing.pdf',relation='Attachment',path='downloads/drawing.pdf')
            columns=','.join(original)
            case.ledger.db.execute('INSERT INTO files ('+columns+') VALUES ('+','.join('?' for _ in original)+')',tuple(original.values()))
            case.ledger.db.execute("INSERT INTO document_files VALUES('p','v1','c','old-pdf',1)")
        pdf=case.base/'downloads/drawing.pdf'
        pdf.write_bytes(b'old')
        case.scan([doc('v2',revision='1')],[content('new-pdf',filename='drawing.pdf',relationshipType='Attachment')])
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertFalse(case.old.exists())
        self.assertEqual(pdf.read_bytes(),b'old')
        self.assertTrue(case.ledger.one("SELECT path FROM files WHERE id='sheet'")[0].startswith('history/'))
        from reporting import system_report_stats
        stats=system_report_stats(case.ledger)
        self.assertEqual(stats['effective_downloads']['total'],stats['download_directory']['total'])
        self.assertTrue(stats['directory_reconciled'])

    def unrelated_package_history(self):
        """An obsolete member of the same package, not a version of v1."""
        case=self.case
        row=dict(case.ledger.one("SELECT * FROM files WHERE id='v1'"))
        row.update(id='other',document_id='v1',filename='unrelated.pdf',active=0,
                   status='pending',path=None,sha256=None,bytes=0)
        columns=','.join(row)
        with case.ledger.db:
            case.ledger.db.execute('INSERT INTO files ('+columns+') VALUES ('+','.join('?' for _ in row)+')',tuple(row.values()))
        path=self.history('history/unrelated.pdf',b'unrelated','2099-01-02')
        with case.ledger.db:
            case.ledger.db.execute("UPDATE file_history SET file_id='other' WHERE path='history/unrelated.pdf'")
        return path

    def test_package_member_cannot_displace_real_predecessor(self):
        case=self.case
        case.updated()
        unrelated=self.unrelated_package_history()
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertEqual(unrelated.read_bytes(),b'unrelated')
        self.assertEqual(case.ledger.one("SELECT status FROM files WHERE id='v1'")[0],'pending')
        self.assertFalse(any(m['source'].endswith('unrelated.pdf') and m['target'].startswith('downloads/')
                             for m in layout.plan(case.ledger,reconcile=True)['moves']))

    def test_restore_only_restores_own_history_and_preserves_other_package_files(self):
        import path_maintenance as maintenance
        case=self.case
        case.updated()
        own=self.history('history/own.pdf',b'old','2099-01-01')
        unrelated=self.unrelated_package_history()
        case.old.unlink()
        before='\n'.join(case.ledger.db.iterdump())
        proposal=maintenance.plan(case.ledger,restore_only=True)
        self.assertEqual(proposal['restored'],1)
        self.assertEqual(proposal['moves'][0]['source'],'history/own.pdf')
        self.assertEqual('\n'.join(case.ledger.db.iterdump()),before)
        maintenance.apply(case.ledger,restore_only=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertEqual(unrelated.read_bytes(),b'unrelated')
        self.assertFalse(own.exists())
        self.assertEqual(case.ledger.one("SELECT status FROM files WHERE id='v1'")[0],'pending')
        self.assertEqual(maintenance.plan(case.ledger,restore_only=True)['moves'],[])
        self.assertEqual(layout.plan(case.ledger,reconcile=True)['moves'],[])

    def test_restore_only_rejects_corrupt_history_and_never_moves_originals(self):
        import path_maintenance as maintenance
        case=self.case
        case.updated()
        own=self.history('history/own.pdf',b'old','2099-01-01')
        case.old.unlink()
        own.write_bytes(b'bad')
        with self.assertRaisesRegex(layout.LayoutError,'SHA256'):
            maintenance.plan(case.ledger,restore_only=True)
        self.assertEqual(own.read_bytes(),b'bad')
        self.assertFalse(case.old.exists())

    def test_restore_only_cli_preview_apply_and_refresh_without_model_or_wechat(self):
        case=self.case
        case.updated()
        self.history('history/own.pdf',b'old','2099-01-01')
        self.unrelated_package_history()
        case.old.unlink()
        command=[sys.executable,str(Path(layout.__file__).resolve().parents[1]/'main.py'),
                 'path_maintenance','--base-dir',str(case.base),'--restore-only']
        preview=subprocess.run(command,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(preview.returncode,0,preview.stdout+preview.stderr)
        self.assertFalse(case.old.exists())
        applied=subprocess.run(command+['--apply','--refresh-report'],capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(applied.returncode,0,applied.stdout+applied.stderr)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertFalse(list((case.base/'logs').glob('*/推送状态.json')))
        from reporting import read_live_push_text
        _,stats=read_live_push_text(case.base)
        self.assertEqual(stats['effective_downloads']['total']['files'],1)
        self.assertEqual(stats['effective_downloads']['latest']['files'],0)
        self.assertEqual(stats['effective_downloads']['retained']['files'],1)

    def test_partial_legacy_archive_is_blocked_before_publication(self):
        case=self.case
        case.updated()
        h=case.ledger.one("SELECT id FROM file_history LIMIT 1")
        move=dict(source=case.old.relative_to(case.base).as_posix(),target='history/unsafe/old.pdf',
                  refs=[['file_history',h['id']]],bytes=3,sha256=hashlib.sha256(b'old').hexdigest(),reconciled=True)
        layout.ensure_schema(case.ledger.db)
        with case.ledger.db:
            case.ledger.db.execute('INSERT INTO layout_moves VALUES(?,?,?)',('unsafe-staged',json.dumps(move),'staged'))
        staging=case.base/'runtime/path_migration/unsafe-staged'/case.old.name
        staging.parent.mkdir(parents=True)
        case.old.rename(staging)
        with self.assertRaisesRegex(layout.LayoutError,'部分移动'):
            layout.apply(case.ledger,recover_only=True)
        self.assertEqual(staging.read_bytes(),b'old')
        self.assertFalse((case.base/'history/unsafe/old.pdf').exists())

    def test_legacy_cross_member_archive_is_replanned_before_moving_bytes(self):
        case=self.case
        case.updated()
        unrelated=self.unrelated_package_history()
        h=case.ledger.one("SELECT id FROM file_history WHERE path=?",(case.old.relative_to(case.base).as_posix(),))
        move=dict(source=case.old.relative_to(case.base).as_posix(),target='history/unsafe/old.pdf',
                  refs=[['file_history',h['id']]],bytes=3,sha256=hashlib.sha256(b'old').hexdigest(),
                  reconciled=True,requires_local=[dict(path='history/unrelated.pdf',bytes=9,sha256=hashlib.sha256(b'unrelated').hexdigest())])
        layout.ensure_schema(case.ledger.db)
        with case.ledger.db:
            case.ledger.db.execute('INSERT INTO layout_moves VALUES(?,?,?)',('unsafe-legacy',json.dumps(move),'pending'))
        (case.base/'runtime/layout.pending').write_text('legacy',encoding='utf-8')
        layout.apply(case.ledger,recover_only=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertEqual(unrelated.read_bytes(),b'unrelated')
        self.assertFalse((case.base/'history/unsafe/old.pdf').exists())
        self.assertTrue(list((case.base/'runtime/backups').glob('before-legacy-replan-*.sqlite3')))

    def test_unified_maintenance_uses_same_single_history_rule(self):
        import path_maintenance as maintenance
        case=self.case
        case.updated()
        self.history('history/latest.pdf',b'newest','2099-01-02')
        self.history('downloads/older.pdf',b'older','2099-01-01')
        proposal=maintenance.plan(case.ledger)
        self.assertEqual(proposal['restored'],1)
        maintenance.apply(case.ledger)
        self.assertEqual([p.read_bytes() for p in (case.base/'downloads').rglob('*') if p.is_file()],[b'newest'])
        self.assertEqual(maintenance.plan(case.ledger)['moves'],[])

    def test_manual_reconcile_preview_then_apply_without_network(self):
        case=self.case
        case.updated()
        self.history('downloads/newest.pdf',b'newest','2099-01-02')
        before='\n'.join(case.ledger.db.iterdump())
        args=['--base-dir',str(case.base)]
        with patch('socket.socket',side_effect=AssertionError('offline operation')):
            self.assertEqual(layout.main(args),0)
            self.assertEqual('\n'.join(case.ledger.db.iterdump()),before)
            self.assertEqual(case.old.read_bytes(),b'old')
            preview=json.loads((case.base/'runtime/path_migration_preview.json').read_text(encoding='utf-8'))
            self.assertTrue(preview['moves'])
            self.assertEqual(layout.main(args+['--apply']),0)
        self.assertEqual([p.read_bytes() for p in (case.base/'downloads').rglob('*') if p.is_file()],[b'newest'])
        self.assertTrue(list((case.base/'runtime/backups').glob('before-path-layout-*.sqlite3')))

    def test_manual_reconcile_refuses_partial_inventory_and_busy_scheduler(self):
        case=self.case
        case.updated()
        args=['--base-dir',str(case.base),'--apply']
        with case.ledger.db:
            case.ledger.db.execute("UPDATE refresh_cycles SET status='associations'")
        with self.assertRaisesRegex(layout.LayoutError,'完整盘点尚未完成'):
            layout.main(args)
        self.assertEqual(case.old.read_bytes(),b'old')
        with case.ledger.db:
            case.ledger.db.execute("UPDATE refresh_cycles SET status='complete'")
        from analysis_worker import worker_lock
        with worker_lock(case.base/'runtime','scheduler.lock') as acquired:
            self.assertTrue(acquired)
            with self.assertRaisesRegex(layout.LayoutError,'请先停止任务'):
                layout.main(args)
        self.assertEqual(case.old.read_bytes(),b'old')

    def test_manual_reconcile_recovers_interrupted_journal_before_new_plan(self):
        case=self.case
        case.updated()
        self.history('history/latest.pdf',b'newest','2099-01-02')
        with patch('file_layout.publish',side_effect=PermissionError('interrupted')):
            with self.assertRaises(PermissionError):
                layout.prepare_downloads(case.ledger,reconcile=True)
        args=['--base-dir',str(case.base)]
        with self.assertRaisesRegex(layout.LayoutError,'有未完成的迁移'):
            layout.main(args)
        with patch('socket.socket',side_effect=AssertionError('offline operation')):
            self.assertEqual(layout.main(args+['--apply']),0)
        self.assertEqual(case.old.read_bytes(),b'newest')
        self.assertFalse((case.base/'runtime/layout.pending').exists())

    def history(self, name, data, archived):
        case = self.case
        path = case.base/name
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        with case.ledger.db:
            case.ledger.db.execute('''INSERT INTO file_history
              (project,file_id,fingerprint,path,bytes,sha256,archived) VALUES(?,?,?,?,?,?,?)''',
              ('p','v1',name,name,len(data),hashlib.sha256(data).hexdigest(),archived))
        return path

    def test_pending_latest_keeps_only_newest_available_history(self):
        case=self.case
        case.updated()
        newest=self.history('downloads/newest.pdf',b'newest','2099-01-02')
        older=self.history('downloads/older.pdf',b'older','2099-01-01')
        unknown=case.base/'downloads/unrelated.txt'
        unknown.write_bytes(b'unrelated')
        layout.prepare_downloads(case.ledger,reconcile=True,reuse=True)
        self.assertEqual([p.read_bytes() for p in (case.base/'downloads').rglob('*') if p.is_file()],[b'newest'])
        self.assertEqual(newest.read_bytes(),b'newest')
        self.assertFalse(older.exists())
        self.assertEqual(case.ledger.one("SELECT status FROM files WHERE active=1")[0],'pending')
        archived={p.read_bytes() for p in (case.base/'history').rglob('*') if p.is_file()}
        self.assertEqual(archived,{b'old',b'older',b'unrelated'})
        with patch('file_layout.plan',side_effect=AssertionError('repeated full scan')):
            self.assertTrue(layout.prepare_downloads(case.ledger,reconcile=True,reuse=True))

    def test_newest_history_is_restored_even_if_older_copy_is_in_downloads(self):
        case=self.case
        case.updated()
        newest=self.history('history/latest.pdf',b'newest','2099-01-02')
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'newest')
        self.assertFalse(newest.exists())
        self.assertEqual(case.ledger.one("SELECT status FROM files WHERE active=1")[0],'pending')
        self.assertIn(b'old',{p.read_bytes() for p in (case.base/'history').rglob('*') if p.is_file()})

    def test_changed_platform_id_keeps_one_history_until_verified_latest_arrives(self):
        case=self.case
        case.scan([doc('v2',revision='1')],[content('v2',revision='1')])
        newest=self.history('downloads/newest.pdf',b'newest','2099-01-02')
        self.history('downloads/older.pdf',b'older','2099-01-01')
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual([p.read_bytes() for p in (case.base/'downloads').rglob('*') if p.is_file()],[b'newest'])
        def receive(item,target,**kwargs):
            self.assertEqual(newest.read_bytes(),b'newest')
            Path(target).write_bytes(b'new')
            return dict(bytes=3,sha256=hashlib.sha256(b'new').hexdigest(),path=str(target))
        case.client.download.side_effect=receive
        from sync import download
        self.assertEqual(download(case.client,case.ledger),1)
        self.assertFalse(newest.exists())
        self.assertEqual([p.read_bytes() for p in (case.base/'downloads').rglob('*') if p.is_file()],[b'new'])
        self.assertEqual({p.read_bytes() for p in (case.base/'history').rglob('*') if p.is_file()},
                         {b'old',b'older',b'newest'})

    def test_corrupt_kept_history_blocks_archiving_all_older_files(self):
        case=self.case
        case.updated()
        newest=self.history('downloads/latest.pdf',b'newest','2099-01-02')
        newest.write_bytes(b'broken')
        with self.assertRaisesRegex(layout.LayoutError,'最新历史副本校验'):
            layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertEqual(newest.read_bytes(),b'broken')

    def test_reconciliation_resume_preserves_newest_after_staging_interruption(self):
        case=self.case
        case.updated()
        self.history('history/latest.pdf',b'newest','2099-01-02')
        with patch('file_layout.publish',side_effect=PermissionError('publication interrupted')):
            with self.assertRaises(PermissionError):
                layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertTrue((case.base/'runtime/layout.pending').exists())
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'newest')
        self.assertFalse((case.base/'runtime/layout.pending').exists())
        self.assertIn(b'old',{p.read_bytes() for p in (case.base/'history').rglob('*') if p.is_file()})

    def test_reconciliation_waits_for_complete_inventory_before_archiving_unmatched(self):
        case=self.case
        case.scan([doc('unmatched',firstVersionId='different',reference='OTHER')],
                  [content('unmatched',filename='unmatched.pdf')])
        with case.ledger.db:
            case.ledger.db.execute("UPDATE refresh_cycles SET status='associations'")
        layout.prepare_downloads(case.ledger,reconcile=True)
        self.assertEqual(case.old.read_bytes(),b'old')
        with case.ledger.db:
            case.ledger.db.execute("UPDATE refresh_cycles SET status='complete'")
        layout.prepare_downloads(case.ledger,reconcile=True,reuse=True)
        self.assertFalse(case.old.exists())
        row=case.ledger.one("SELECT path FROM files WHERE id='v1'")
        self.assertTrue(row['path'].startswith('history/'))
        self.assertEqual((case.base/row['path']).read_bytes(),b'old')

    def test_legacy_archive_journal_is_replanned_before_any_bytes_move(self):
        case=self.case
        case.scan([doc('unmatched',firstVersionId='different',reference='OTHER')],
                  [content('unmatched',filename='unmatched.pdf')])
        layout.ensure_schema(case.ledger.db)
        move=dict(source=case.old.relative_to(case.base).as_posix(),target='history/legacy/old.pdf',
                  refs=[['files','p','v1']],bytes=3,sha256=hashlib.sha256(b'old').hexdigest())
        with case.ledger.db:
            case.ledger.db.execute('INSERT INTO layout_moves VALUES(?,?,?)',('legacy-fixture',json.dumps(move),'pending'))
        (case.base/'runtime/layout.pending').write_text('old plan',encoding='utf-8')
        layout.prepare_downloads(case.ledger)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertFalse((case.base/'history/legacy/old.pdf').exists())
        self.assertFalse((case.base/'runtime/layout.pending').exists())

    def setUp(self):
        self.case=replacement.ReplacementTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)

    def move_new_document(self, identifier):
        ledger=self.case.ledger
        with ledger.db:
            ledger.db.execute("INSERT INTO folders(project,id,name,path,active) VALUES('p','moved','Moved','project/Moved',1)")
            ledger.db.execute("UPDATE documents SET folder_id='moved' WHERE id=?",(identifier,))
            ledger.db.execute("UPDATE files SET folder_id='moved' WHERE active=1")
            ledger.db.execute("UPDATE document_files SET folder_id='moved' WHERE document_id=?",(identifier,))

    def test_directory_and_filename_change_keeps_old_until_new_bytes_arrive(self):
        case=self.case
        case.scan([doc('v2',revision='1')],[content('v2',filename='renamed.pdf',revision='1')])
        self.move_new_document('v2')
        layout.prepare_downloads(case.ledger)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertIn(('p','v2'),layout.local_update_keys(case.ledger))
        case.client.download.side_effect=case.receive
        from sync import download
        download(case.client,case.ledger)
        row=case.ledger.one("SELECT * FROM files WHERE id='v2'")
        self.assertEqual((case.base/row['path']).read_bytes(),b'new')
        old=case.ledger.one("SELECT path FROM files WHERE id='v1'")
        self.assertTrue(old['path'].startswith('history/'))
        self.assertEqual((case.base/old['path']).read_bytes(),b'old')

    def test_renamed_attachment_waits_for_its_document_family(self):
        case=self.case
        with case.ledger.db:
            case.ledger.db.execute("UPDATE documents SET id='parent' WHERE id='v1'")
            case.ledger.db.execute("UPDATE files SET document_id='parent',relation='Attachments' WHERE id='v1'")
            case.ledger.db.execute("UPDATE document_files SET document_id='parent' WHERE document_id='v1'")
        case.scan([doc('parent',revision='1')],
                  [content('a2',filename='renamed-attachment.pdf',relationshipType='Attachments')])
        layout.prepare_downloads(case.ledger)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertNotIn(('p','a2'),layout.local_update_keys(case.ledger))
        self.assertIn(('p','v1'),case.ledger._publication_groups[('p','a2')])

    def test_unmatched_old_file_is_not_assumed_safe_to_archive_while_project_has_pending_files(self):
        case=self.case
        case.scan([doc('unmatched',firstVersionId='different',reference='OTHER')],
                  [content('unmatched',filename='unmatched.pdf')])
        layout.prepare_downloads(case.ledger)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertFalse(any(move['source']==str(case.old.relative_to(case.base))
                             for move in layout.plan(case.ledger)['moves']))

    def test_corrupt_replacement_does_not_move_old_even_when_done_flag_and_size_match(self):
        case=self.case
        case.updated()
        staged=case.base/'runtime/download_ready/new/content'
        staged.parent.mkdir(parents=True,exist_ok=True)
        staged.write_bytes(b'bad')
        with case.ledger.db:
            case.ledger.db.execute("UPDATE files SET status='done',path=?,bytes=3,sha256=? WHERE id='v1'",
                (staged.relative_to(case.base).as_posix(),hashlib.sha256(b'new').hexdigest()))
        with self.assertRaisesRegex(layout.LayoutError,'替代版本SHA256'):
            layout.prepare_downloads(case.ledger)
        self.assertEqual(case.old.read_bytes(),b'old')
        self.assertEqual(staged.read_bytes(),b'bad')
        # The same journal can resume after the required verified bytes exist.
        staged.write_bytes(b'new')
        layout.prepare_downloads(case.ledger)
        self.assertEqual(case.old.read_bytes(),b'new')
