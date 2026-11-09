from contextlib import closing
import hashlib
import itertools
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime

from nas_sync import Settings, Candidate, Uploader, DirectoryMirror, candidates, Stopped, Conflict, BEIJING, plain, load_settings
from main import in_window, next_start, run_upload, main


class UploadTests(unittest.TestCase):
    def test_local_preflight_reports_real_files_bytes_and_phase_elapsed(self):
        item=self.add()
        messages=[];self.u.status=messages.append
        mirror=DirectoryMirror(self.s)
        try:
            mirror.configure(self.u,[item],0)
            value=self.u.phase_snapshot()
            self.assertEqual(value['name'],'本地预检')
            self.assertEqual(value['completed'],1)
            self.assertEqual(value['total'],1)
            self.assertEqual(value['completed_bytes'],item.size)
            self.assertEqual(value['total_bytes'],item.size)
            self.assertEqual(value['current_file'],item.relative)
            self.assertEqual(value['state'],'complete')
            self.assertTrue(any('阶段完成：本地预检' in message for message in messages))
        finally:mirror.close()

    def test_hash_failure_retains_failed_filename_without_claiming_phase_complete(self):
        item=self.add();item.path.write_bytes(b'xxxxxxxxxxxx')
        mirror=DirectoryMirror(self.s)
        try:
            with self.assertRaises(Conflict):mirror.configure(self.u,[item],0)
            value=self.u.phase_snapshot()
            self.assertEqual(value['completed'],0)
            self.assertEqual(value['current_file'],item.relative)
            self.assertEqual(value['state'],'running')
        finally:mirror.close()

    def test_preflight_heartbeat_uses_phase_progress_instead_of_upload_counter(self):
        from main import upload_heartbeat
        self.u.begin_phase('本地预检',2,20)
        self.u.phase_file('first.pdf',10);self.u.phase_advance(10)
        self.u.phase_file('second.pdf',10);self.u.phase_bytes(4)
        signal=threading.Event();messages=[]
        def capture(base,message):
            messages.append(message);signal.set()
        with patch('main.log',side_effect=capture):
            with upload_heartbeat(self.s.base,self.u,lambda:0,2,interval=.01):
                self.assertTrue(signal.wait(2))
        self.assertIn('本阶段完成 1/2',messages[0])
        self.assertIn('已校验 10/20 字节',messages[0])
        self.assertIn('second.pdf',messages[0])
        self.assertIn('本文件已读取 4/10 字节',messages[0])
        self.assertIn('阶段耗时',messages[0])

    def mirror_round(self, **kwargs):
        at=datetime(2026,11,9,13,tzinfo=BEIJING)
        with patch('main.now',return_value=at),patch('main.connect'),patch('main.log'):
            return run_upload(self.s,'once',threading.Event(),**kwargs)

    def archive_bytes(self):
        root=self.s.target.with_name(self.s.target.name+'.nas-archive')
        return [p.read_bytes() for p in root.rglob('*') if p.is_file()]

    def test_mirror_unknown_wrong_directory_and_filename_are_retired(self):
        item=self.add('608200/right/drawing.pdf')
        wrong=self.s.target/'608200/wrong/old-name.pdf'
        wrong.parent.mkdir(parents=True);wrong.write_bytes(item.path.read_bytes())
        (self.s.target/'unused/empty').mkdir(parents=True)
        self.assertEqual(self.mirror_round(),0)
        self.assertEqual((self.s.target/item.relative).read_bytes(),item.path.read_bytes())
        self.assertFalse(wrong.exists());self.assertFalse((self.s.target/'unused').exists())
        self.assertIn(item.path.read_bytes(),self.archive_bytes())
        row=self.u.db.execute('SELECT * FROM mirror_archives').fetchone()
        self.assertEqual(row['source_relative'],'608200/wrong/old-name.pdf')
        self.assertEqual(row['state'],'moved')
        self.assertTrue(Path(row['archive_path']).is_file())

    def test_mirror_replaces_unknown_remote_bytes_even_when_preserve_disabled(self):
        item=self.add();self.s.preserve=False
        target=self.s.target/item.relative
        target.parent.mkdir(parents=True);target.write_bytes(b'NAS manual edit')
        self.assertEqual(self.mirror_round(),0)
        self.assertEqual(target.read_bytes(),item.path.read_bytes())
        self.assertIn(b'NAS manual edit',self.archive_bytes())
        self.assertFalse((self.s.target/'history').exists())

    def test_mirror_identical_file_is_not_uploaded(self):
        item=self.add();self.u.one(item)
        with patch.object(Uploader,'backup_managed',side_effect=AssertionError('unnecessary backup')):
            self.assertEqual(self.mirror_round(),0)
        summary=json.loads(next((self.s.base/'logs/2026-11-09').glob('*summary.json')).read_text(encoding='utf-8'))
        self.assertEqual(summary['sent_bytes'],0)
        self.assertEqual(summary['directory_mirror']['files'],1)

    def test_mirror_creates_registered_empty_folders_and_corrects_case(self):
        item=self.add('608200/Folder/Drawing.PDF')
        wrong=self.s.target/'608200/folder/drawing.pdf'
        wrong.parent.mkdir(parents=True);wrong.write_bytes(item.path.read_bytes())
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('CREATE TABLE folders(project,id,path,active)')
            db.executemany('INSERT INTO folders VALUES(?,?,?,?)',[
                ('p','empty','HABSHANDBN/608200/Empty',1),
                ('p','retired','HABSHANDBN/Retired',0),
                ('p','other','OTHER/Unrelated',1)])
        self.assertEqual(self.mirror_round(),0)
        paths={p.relative_to(self.s.target).as_posix() for p in self.s.target.rglob('*')}
        self.assertEqual(paths,{'608200','608200/Folder','608200/Folder/Drawing.PDF','608200/Empty'})

    def test_mirror_archives_file_and_nonempty_directory_type_blockers(self):
        first=self.add('608200/Folder/drawing.pdf')
        second=self.add('608200/file.pdf',b'second')
        (self.s.target/'608200/file.pdf').mkdir(parents=True)
        (self.s.target/'608200/file.pdf/unknown.bin').write_bytes(b'keep nested')
        (self.s.target/'608200/Folder').write_bytes(b'keep blocker')
        self.assertEqual(self.mirror_round(),0)
        self.assertEqual((self.s.target/first.relative).read_bytes(),first.path.read_bytes())
        self.assertEqual((self.s.target/second.relative).read_bytes(),b'second')
        self.assertEqual(set(self.archive_bytes()),{b'keep nested',b'keep blocker'})

    def test_mirror_corrupt_local_manifest_does_not_displace_remote(self):
        item=self.add();item.path.write_bytes(b'xxxxxxxxxxxx')
        (self.s.target/'extra').mkdir(parents=True)
        unknown=self.s.target/'extra/keep.pdf';unknown.write_bytes(b'keep')
        self.assertEqual(self.mirror_round(),1)
        self.assertEqual(unknown.read_bytes(),b'keep');self.assertEqual(self.archive_bytes(),[])

    def test_mirror_failed_upload_never_retires_unrelated_remote_files(self):
        self.add();self.s.target.mkdir();unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        with patch.object(Uploader,'one',side_effect=Conflict('upload failed')):
            self.assertEqual(self.mirror_round(),2)
        self.assertTrue(unknown.exists());self.assertEqual(self.archive_bytes(),[])

    def test_mirror_changed_ledger_blocks_cleanup(self):
        item=self.add();self.u.one(item)
        unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        mirror=DirectoryMirror(self.s)
        try:
            mirror.configure(self.u,[item],0)
            with closing(sqlite3.connect(self.s.ledger)) as db,db:
                db.execute("UPDATE files SET status='pending'")
            with self.assertRaisesRegex(Stopped,'本轮发生变化'):mirror.reconcile()
            self.assertTrue(unknown.exists())
        finally:mirror.close()

    def test_mirror_registered_pending_file_is_not_treated_as_surplus(self):
        self.add()
        pending=self.s.target/'608200/pending.pdf'
        pending.parent.mkdir(parents=True);pending.write_bytes(b'already on NAS')
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute("INSERT INTO files(path,bytes,sha256,status) VALUES(NULL,0,NULL,'pending')")
        self.assertEqual(self.mirror_round(),2)
        self.assertEqual(pending.read_bytes(),b'already on NAS')
        self.assertEqual(self.archive_bytes(),[])

    def test_mirror_failed_final_hash_does_not_retire_extra_files(self):
        item=self.add();self.u.one(item)
        unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        mirror=DirectoryMirror(self.s)
        try:
            mirror.configure(self.u,[item],0)
            (self.s.target/item.relative).write_bytes(b'broken')
            with self.assertRaisesRegex(Conflict,'正式文件校验失败'):mirror.reconcile()
            self.assertTrue(unknown.exists());self.assertEqual(self.archive_bytes(),[])
        finally:mirror.close()

    def test_mirror_archive_can_be_restored_using_persisted_original_path(self):
        self.add();self.s.target.mkdir();unknown=self.s.target/'old/name.pdf'
        unknown.parent.mkdir();unknown.write_bytes(b'old bytes')
        self.assertEqual(self.mirror_round(),0)
        row=self.u.db.execute('SELECT * FROM mirror_archives').fetchone()
        original=self.s.target/row['source_relative']
        original.parent.mkdir(parents=True,exist_ok=True)
        Path(row['archive_path']).rename(original)
        self.assertEqual(original.read_bytes(),b'old bytes')
        self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(),row['sha256'])

    def test_mirror_rechecks_remote_change_before_archive(self):
        item=self.add();self.u.one(item)
        unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        mirror=DirectoryMirror(self.s)
        try:
            mirror.configure(self.u,[item],0)
            node=mirror.inventory()['keep.pdf']
            unknown.write_bytes(b'concurrent edit')
            with self.assertRaisesRegex(Conflict,'核对后变化'):mirror.archive(node)
            self.assertEqual(unknown.read_bytes(),b'concurrent edit')
        finally:mirror.close()

    def test_mirror_archive_rename_interruption_is_journaled_and_retryable(self):
        item=self.add();self.u.one(item)
        unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        mirror=DirectoryMirror(self.s)
        try:
            mirror.configure(self.u,[item],0)
            with patch.object(Path,'rename',side_effect=OSError('interrupted')):
                with self.assertRaises(OSError):mirror.reconcile()
            self.assertTrue(unknown.exists())
            self.assertEqual(self.u.db.execute('SELECT state FROM mirror_archives').fetchone()[0],'planned')
            mirror.reconcile()
            self.assertFalse(unknown.exists());self.assertIn(b'keep',self.archive_bytes())
        finally:mirror.close()

    def test_mirror_partial_run_does_not_clean_whole_target(self):
        self.add();self.s.target.mkdir();unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        self.assertEqual(self.mirror_round(max_files=1),0)
        self.assertTrue(unknown.exists())

    def test_mirror_empty_or_ignored_manifest_does_not_clean_target(self):
        self.s.target.mkdir();unknown=self.s.target/'keep.pdf';unknown.write_bytes(b'keep')
        self.assertEqual(self.mirror_round(),2)
        self.assertTrue(unknown.exists())
        item=self.add();item.path.unlink()
        self.assertEqual(self.mirror_round(),2)
        self.assertTrue(unknown.exists())

    def test_scope_exclusions_do_not_stop_full_mirror(self):
        item=self.add()
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute("INSERT INTO files VALUES('downloads/OTHER/outside.pdf',10,NULL,'done')")
        self.assertEqual(self.mirror_round(),0)
        self.assertTrue((self.s.target/item.relative).is_file())
        summary=json.loads(next((self.s.base/'logs/2026-11-09').glob('*summary.json')).read_text(encoding='utf-8'))
        report=summary['candidate_diagnostics']
        self.assertEqual(report['blocking_records'],0)
        self.assertEqual(report['excluded_records'],1)
        self.assertEqual(report['records'][0]['path'],'downloads/OTHER/outside.pdf')
        self.assertEqual(report['records'][0]['reason'],'配置源范围外的记录')
        self.assertIn('directory_mirror',summary)

    def test_pruned_history_metadata_does_not_block_mirror_or_recreate_bytes(self):
        item=self.add()
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('ALTER TABLE files ADD COLUMN active INTEGER DEFAULT 1')
            db.execute("INSERT INTO files(path,bytes,sha256,status,active) VALUES(NULL,3,?,'done',0)",('a'*64,))
            db.execute('INSERT INTO file_history(path,bytes,sha256) VALUES(NULL,3,?)',('b'*64,))
        self.assertEqual(self.mirror_round(),0)
        files=[p.relative_to(self.s.target).as_posix() for p in self.s.target.rglob('*') if p.is_file()]
        self.assertEqual(files,[item.relative])
        summary=json.loads(next((self.s.base/'logs/2026-11-09').glob('*summary.json')).read_text(encoding='utf-8'))
        report=summary['candidate_diagnostics']
        self.assertEqual(report['blocking_records'],0)
        self.assertEqual(report['reasons'],{'历史记录无保留路径':2})
        self.assertIn('directory_mirror',summary)

    def test_active_done_record_with_no_path_still_blocks_cleanup(self):
        self.add()
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('ALTER TABLE files ADD COLUMN active INTEGER DEFAULT 1')
            db.execute("INSERT INTO files(path,bytes,sha256,status,active) VALUES(NULL,3,?,'done',1)",('a'*64,))
        details=[]
        _,ignored=candidates(self.s,diagnostics=details)
        self.assertEqual(ignored,1)
        self.assertEqual(details[0]['reason'],'台账缺少本地路径')
        self.assertTrue(details[0]['blocks_mirror'])

    def test_history_with_retained_path_but_missing_bytes_is_not_normal_exclusion(self):
        self.add()
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('INSERT INTO file_history VALUES(?,?,?)',
                       ('history/batch/HABSHANDBN/608200/missing.pdf',3,'a'*64))
        details=[]
        _,ignored=candidates(self.s,diagnostics=details)
        self.assertEqual(ignored,1)
        self.assertEqual(details[0]['reason'],'本地文件不存在')
        self.assertTrue(details[0]['blocks_mirror'])

    def test_missing_record_stops_before_any_upload_or_cleanup(self):
        good=self.add('608200/good.pdf')
        missing=self.add('608200/missing.pdf');missing.path.unlink()
        extra=self.s.target/'keep.pdf';extra.parent.mkdir(parents=True);extra.write_bytes(b'keep')
        with patch.object(Uploader,'one',side_effect=AssertionError('must stop before uploading')):
            self.assertEqual(self.mirror_round(),2)
        self.assertFalse((self.s.target/good.relative).exists())
        self.assertEqual(extra.read_bytes(),b'keep')
        self.assertEqual(self.archive_bytes(),[])
        summary=json.loads(next((self.s.base/'logs/2026-11-09').glob('*summary.json')).read_text(encoding='utf-8'))
        self.assertEqual(summary['files']['uploaded'],0)
        self.assertEqual(summary['status'],'stopped')
        self.assertEqual(summary['sent_bytes'],0)
        self.assertEqual(summary.get('nas_read_bytes',0),0)
        report=summary['candidate_diagnostics']
        self.assertEqual(report['blocking_records'],1)
        self.assertEqual(report['records'][0]['reason'],'本地文件不存在')
        self.assertTrue(report['records'][0]['path'].endswith('missing.pdf'))
        self.assertNotIn('directory_mirror',summary)

    def test_unverifiable_records_stop_even_limited_upload_before_mutation(self):
        good=self.add('608200/good.pdf')
        missing=self.add('608200/missing.pdf');missing.path.unlink()
        with patch.object(Uploader,'one',side_effect=AssertionError('must stop before uploading')):
            self.assertEqual(self.mirror_round(max_files=1),2)
        self.assertFalse((self.s.target/good.relative).exists())

    def test_incomplete_manifest_does_not_replace_unmanaged_target(self):
        good=self.add('608200/good.pdf');missing=self.add('608200/missing.pdf');missing.path.unlink()
        target=self.s.target/good.relative;target.parent.mkdir(parents=True);target.write_bytes(b'unknown NAS bytes')
        self.assertEqual(self.mirror_round(),2)
        self.assertEqual(target.read_bytes(),b'unknown NAS bytes')
        self.assertEqual(self.archive_bytes(),[])

    def test_candidate_diagnostics_distinguish_missing_hash_size_and_temporary(self):
        item=self.add('608200/no-hash.pdf')
        other=self.add('608200/bad-size.pdf');other.path.write_bytes(b'bad')
        self.add('608200/unfinished.part')
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('UPDATE files SET sha256=NULL WHERE path=?',('downloads/HABSHANDBN/'+item.relative,))
        details=[]
        items,ignored=candidates(self.s,diagnostics=details)
        self.assertEqual(items,[]);self.assertEqual(ignored,3)
        self.assertEqual({r['reason']:r['blocks_mirror'] for r in details},
                         {'台账缺少SHA256':True,'本地大小与台账不符':True,'临时文件':False})

    def test_mirror_refuses_symlink_without_touching_external_bytes(self):
        self.add();self.s.target.mkdir();external=self.root/'outside';external.mkdir()
        (external/'keep').write_bytes(b'outside')
        try:(self.s.target/'link').symlink_to(external,target_is_directory=True)
        except OSError:self.skipTest('symlinks unavailable')
        self.assertEqual(self.mirror_round(),1)
        self.assertEqual((external/'keep').read_bytes(),b'outside')

    def test_same_round_hash_cache_skips_content_reads_and_detects_preserved_mtime_change(self):
        import os
        item=self.add()
        target=self.u.remote(item.relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b'old')
        expected=hashlib.sha256(b'old').hexdigest()
        self.assertEqual(self.u.hash_file(target),expected)
        reads=self.u.nas_read_bytes
        self.assertEqual(self.u.hash_file(target),expected)
        self.assertEqual(self.u.nas_read_bytes,reads)
        self.assertEqual(self.u.hash_cache_hits,1)
        stamp=target.stat()
        target.write_bytes(b'new')
        os.utime(target,ns=(stamp.st_atime_ns,stamp.st_mtime_ns))
        self.assertEqual(self.u.hash_file(target),hashlib.sha256(b'new').hexdigest())
        self.assertGreater(self.u.nas_read_bytes,reads)

    def test_rapid_same_size_rewrites_with_restored_mtime_do_not_reuse_old_hash(self):
        import os
        item=self.add()
        target=self.u.remote(item.relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        for number in range(32):
            content=f'{number:04d}'.encode()
            before=target.stat() if target.exists() else None
            target.write_bytes(content)
            if before:os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
            self.assertEqual(self.u.hash_file(target),hashlib.sha256(content).hexdigest())

    def test_force_verification_ignores_same_round_hash_cache(self):
        item=self.add()
        self.u.one(item)
        self.u.one(item,True)
        reads=self.u.nas_read_bytes
        self.u.one(item,True)
        self.assertGreater(self.u.nas_read_bytes,reads)
        self.assertFalse(self.u.force_hash)

    def test_no_reliable_change_stamp_keeps_full_hash_reads(self):
        item=self.add()
        target=self.u.remote(item.relative)
        target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(b'old')
        with patch('nas_sync.hash_stamp',return_value=None):
            self.u.hash_file(target)
            reads=self.u.nas_read_bytes
            self.u.hash_file(target)
        self.assertGreater(self.u.nas_read_bytes,reads)
        self.assertEqual(self.u.hash_cache_hits,0)

    def test_nas_side_hash_avoids_smb_content_reads(self):
        item=self.add()
        self.s.remote_hash=SimpleNamespace(fallback=True)
        with patch('nas_remote_hash.RemoteHasher') as worker:
            worker.return_value.hash.return_value=item.sha256
            self.assertEqual(self.u.one(item),('uploaded',item.size))
            self.assertEqual(self.u.nas_read_bytes,0)
            self.assertEqual(self.u.remote_hash_bytes,item.size)
            self.assertEqual(self.u.remote_hash_files,1)
            self.assertEqual(self.u.one(item),('skipped',0))
            self.assertEqual(worker.return_value.hash.call_count,1)

    def test_ssh_failure_never_falls_back_even_with_legacy_fallback_flag(self):
        from nas_remote_hash import RemoteHashError
        item=self.add()
        self.s.remote_hash=SimpleNamespace(fallback=True)
        with patch('nas_remote_hash.RemoteHasher') as worker:
            worker.return_value.hash.side_effect=RemoteHashError('unavailable')
            with self.assertRaisesRegex(Conflict,'未回退SMB'):self.u.one(item)
            self.assertEqual(self.u.nas_read_bytes,0)
            self.assertEqual(self.u.remote_hash_files,0)
            self.assertFalse((self.s.target/item.relative).exists())

    def test_required_nas_hash_failure_never_publishes_unverified_file(self):
        from nas_remote_hash import RemoteHashError
        item=self.add()
        self.s.remote_hash=SimpleNamespace(fallback=False)
        with patch('nas_remote_hash.RemoteHasher') as worker:
            worker.return_value.hash.side_effect=RemoteHashError('unavailable')
            with self.assertRaises(Conflict):self.u.one(item)
        self.assertFalse((self.s.target/item.relative).exists())
        self.assertNotEqual(self.u.row(item)['status'],'done')

    def test_new_upload_keeps_one_fresh_publication_path_check(self):
        item=self.add()
        with patch.object(self.u,'destination',wraps=self.u.destination) as resolve:
            self.assertEqual(self.u.one(item),('uploaded',item.size))
        self.assertEqual(resolve.call_count,2)
        self.assertEqual((self.s.target/item.relative).read_bytes(),item.path.read_bytes())

    def test_resume_reads_partial_metadata_once_before_transfer(self):
        item=self.add()
        part=self.interrupt_after_chunks(item)
        remaining=item.size-part.stat().st_size
        actual=Path.stat
        calls=[]
        def observe(path,*args,**kwargs):
            result=actual(path,*args,**kwargs)
            if path==part:calls.append(result.st_size)
            return result
        with patch.object(Path,'stat',observe):
            self.assertEqual(self.u.one(item),('uploaded',remaining))
        # One resume length read and one final complete-size read before hashing.
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[-1],item.size)

    def test_migration_keeps_unrelated_scan_cache_and_refreshes_both_paths(self):
        raw,mapped=self.mapped_candidate()
        _,old_part=self.u.destination(raw)
        old_part.parent.mkdir(parents=True,exist_ok=True)
        old_part.write_bytes(b'prefix')
        self.u.save(raw,'uploading',6)
        self.assertFalse(self.u.scan_exists('unrelated/file.pdf'))
        new_part=self.u.partial_relative(mapped.relative)
        self.assertFalse(self.u.scan_exists(new_part))
        self.assertIsNone(self.u.migrate(mapped))
        self.assertIn('unrelated',self.u.scan_directories)
        self.assertTrue(self.u.scan_exists(new_part))
        self.assertFalse(self.u.scan_exists(self.u.partial_relative(raw.relative)))

    def test_ten_migrations_list_unrelated_directory_only_once(self):
        from dataclasses import replace
        import os
        item=self.add()
        actual=os.scandir
        listings=[]
        def observe(path):
            listings.append(plain(path))
            return actual(path)
        with patch('nas_sync.os.scandir',side_effect=observe):
            for index in range(10):
                previous=f'old-{index}/file.pdf'
                target=self.s.target/previous
                target.parent.mkdir(parents=True)
                target.write_bytes(item.path.read_bytes())
                candidate=replace(item,relative=f'new-{index}/file.pdf',legacy_relatives=(previous,))
                self.assertFalse(self.u.scan_exists('stable/missing.pdf'))
                self.assertEqual(self.u.migrate(candidate),('migrated',0))
        self.assertEqual(listings.count(str(self.s.target/'stable')),1)

    def test_no_archivable_alias_does_not_repeat_formal_checks(self):
        from dataclasses import replace
        item=self.add()
        self.u.protected={'protected/file.pdf'}
        item=replace(item,legacy_relatives=(item.relative,'history/old.pdf','protected/file.pdf'))
        with patch.object(self.u,'formal_ready',side_effect=AssertionError('no copy to retire')), \
             patch.object(self.u,'remote',side_effect=AssertionError('no NAS query needed')):
            self.assertEqual(self.u.archive_legacy_copies(item),0)

    def test_optional_metadata_propagates_access_errors(self):
        from nas_sync import optional_signature
        item=self.add()
        with patch.object(Path,'stat',side_effect=PermissionError('NAS denied')):
            with self.assertRaises(PermissionError):optional_signature(item.path)

    def test_remote_path_uses_one_metadata_query_per_component(self):
        from nas_sync import safe_remote
        from pathlib import PurePosixPath
        item=self.add()
        self.u.one(item)
        actual=Path.lstat
        calls=[]
        def observe(path):
            if plain(path).startswith(str(self.s.target)):calls.append(plain(path))
            return actual(path)
        with patch.object(Path,'lstat',observe), \
             patch.object(Path,'is_symlink',side_effect=AssertionError('duplicate SMB stat')), \
             patch.object(Path,'is_junction',side_effect=AssertionError('duplicate SMB stat'),create=True):
            safe_remote(self.s,item.relative)
        self.assertEqual(len(calls),len(PurePosixPath(item.relative).parts)+1)

    def test_target_and_partial_share_checks_only_within_one_resolution(self):
        from pathlib import PurePosixPath
        item=self.add()
        self.u.one(item)
        actual=Path.lstat
        calls=[]
        def observe(path):
            if plain(path).startswith(str(self.s.target)):calls.append(plain(path))
            return actual(path)
        with patch.object(Path,'lstat',observe):
            self.u.destination(item)
            first=len(calls)
            self.u.destination(item)
        self.assertEqual(first,len(PurePosixPath(item.relative).parts)+2)
        self.assertEqual(len(calls),first*2)

    def test_confirmed_skip_validates_target_without_part_or_alias_queries(self):
        item=self.add()
        self.u.one(item)
        with patch.object(self.u,'destination',side_effect=AssertionError('unneeded partial query')), \
             patch.object(self.u,'restore_legacy',side_effect=AssertionError('unneeded alias query')), \
             patch.object(self.u,'hash_file',side_effect=AssertionError('unneeded hash')):
            self.assertEqual(self.u.one(item),('skipped',0))

    def test_readback_progress_starts_at_zero_and_records_actual_read_bytes(self):
        item=self.add()
        events=[]
        self.u.progress=events.append
        self.u.one(item)
        started=[v for v in events if v['phase']=='回读NAS校验']
        self.assertEqual(started[0]['current_bytes'],0)
        self.assertEqual(self.u.nas_read_bytes,item.size)
        self.assertGreater(self.u.timings['nas_hash'],0)
        self.assertGreater(self.u.timings['upload'],0)

    def test_migration_completion_uses_target_index(self):
        query="""EXPLAIN QUERY PLAN UPDATE path_migrations SET state='completed',updated=?
          WHERE scope=? AND new_relative=? AND sha256=?
          AND state IN ('planned','partial_planned','partial_moved')"""
        plan=self.u.db.execute(query,('at',self.s.scope,'file','digest')).fetchall()
        self.assertIn('path_migrations_target',' '.join(str(row[3]) for row in plan))

    def test_upload_heartbeat_reports_blocked_phase_and_closes(self):
        from main import upload_heartbeat
        reported=threading.Event()
        self.u.current_file='waiting.pdf'
        self.u.activity=('publish',__import__('time').monotonic()-90)
        lines=[]
        def capture(base,message):
            lines.append(message)
            reported.set()
        with patch('main.log',side_effect=capture):
            with upload_heartbeat(self.s.base,self.u,lambda:12,100,interval=0.01):
                self.assertTrue(reported.wait(2))
        self.assertTrue(any('NAS发布或设置时间' in line and '12/100' in line and 'waiting.pdf' in line for line in lines))
        self.assertFalse(any(t.name=='nas-upload-progress' for t in threading.enumerate()))

    def test_verify_all_still_detects_same_size_preserved_time_remote_change(self):
        import os
        item=self.add()
        self.u.one(item)
        target,_=self.u.destination(item)
        before=target.stat()
        changed=b'X'*item.size
        target.write_bytes(changed)
        os.utime(target,ns=(before.st_atime_ns,before.st_mtime_ns))
        with self.assertRaises(Conflict):self.u.one(item,verify_all=True)
        self.assertEqual(target.read_bytes(),changed)

    def test_new_managed_backup_hashes_old_source_during_copy(self):
        from dataclasses import replace
        item=self.add()
        self.u.one(item)
        target,_=self.u.destination(item)
        old_bytes=target.read_bytes()
        item.path.write_bytes(b'new-version-payload')
        info=item.path.stat()
        current=replace(item,size=info.st_size,mtime_ns=info.st_mtime_ns,
                        sha256=hashlib.sha256(item.path.read_bytes()).hexdigest())
        with patch.object(self.u,'hash_file',wraps=self.u.hash_file) as hashing:
            self.u.one(current)
        # One initial comparison; the pre-backup recheck is fused into copying.
        self.assertEqual(sum(call.args[0]==target for call in hashing.call_args_list),1)
        self.assertEqual(target.read_bytes(),item.path.read_bytes())
        self.assertIn(old_bytes,{path.read_bytes() for path in (self.s.target/'history').rglob('*') if path.is_file()})

    def test_streamed_backup_detects_changed_old_source_without_replacing_it(self):
        from dataclasses import replace
        item=self.add()
        self.u.one(item)
        target,_=self.u.destination(item)
        item.path.write_bytes(b'new-version-payload')
        info=item.path.stat()
        current=replace(item,size=info.st_size,mtime_ns=info.st_mtime_ns,
                        sha256=hashlib.sha256(item.path.read_bytes()).hexdigest())
        original=self.u.backup_managed
        def changed_before_backup(*args):
            target.write_bytes(b'outside-change')
            return original(*args)
        with patch.object(self.u,'backup_managed',side_effect=changed_before_backup):
            with self.assertRaises(Conflict):self.u.one(current)
        self.assertEqual(target.read_bytes(),b'outside-change')
        self.assertFalse(any(path.is_file() and not path.name.endswith('.part')
                             for path in (self.s.target/'history').rglob('*')))

    def test_migration_scan_lists_shared_directories_once_for_many_missing_files(self):
        from dataclasses import replace
        item=self.add()
        (self.s.target/'608200').mkdir(parents=True)
        (self.s.target/'legacy').mkdir()
        actual=__import__('os').scandir
        with patch('nas_sync.os.scandir',wraps=actual) as listing, \
             patch.object(self.u,'destination',side_effect=AssertionError('per-file SMB lookup')):
            for index in range(1000):
                candidate=replace(item,relative=f'608200/new-{index}.pdf',
                                  legacy_relatives=(f'legacy/old-{index}.pdf',))
                self.assertIsNone(self.u.migrate(candidate))
        self.assertEqual(listing.call_count,2)

    def test_migration_scan_existing_destination_does_not_hash_formal_guards(self):
        from dataclasses import replace
        item=self.add()
        self.u.one(item)
        item=replace(item,legacy_relatives=('old/previous.pdf',))
        with patch.object(self.u,'formal_ready',side_effect=AssertionError('unneeded guard read')), \
             patch.object(self.u,'destination',side_effect=AssertionError('per-file lookup')):
            self.assertIsNone(self.u.migrate(item))

    def test_migration_without_old_paths_does_not_query_nas(self):
        item=self.add()
        with patch.object(self.u,'scan_exists',side_effect=AssertionError('unneeded NAS query')):
            self.assertIsNone(self.u.migrate(item))

    def test_migration_scan_rechecks_directory_after_discovery_before_move(self):
        raw,mapped=self.mapped_candidate()
        self.u.one(raw)
        actual=self.u.scan_exists
        redirected=[False]
        original=Path.lstat
        blocked=self.s.target/'608200'
        def discover(relative):
            found=actual(relative)
            if found:redirected[0]=True
            return found
        with patch.object(self.u,'scan_exists',side_effect=discover), \
             patch.object(Path,'lstat',lambda path: SimpleNamespace(st_mode=0,st_file_attributes=0x400) if (redirected[0] and plain(path)==str(blocked)) else original(path)):
            with self.assertRaises(Conflict):self.u.migrate(mapped)
        self.assertTrue((self.s.target/raw.relative).is_file())
        self.assertFalse((self.s.target/mapped.relative).exists())

    def test_migration_scan_invalidates_absence_after_partial_move(self):
        raw,mapped=self.mapped_candidate()
        _,old_part=self.u.destination(raw)
        old_part.parent.mkdir(parents=True,exist_ok=True)
        old_part.write_bytes(b'prefix')
        self.u.save(raw,'uploading',6)
        new_relative=self.u.partial_relative(mapped.relative)
        self.assertFalse(self.u.scan_exists(new_relative))
        self.assertIsNone(self.u.migrate(mapped))
        self.assertTrue(self.u.scan_exists(new_relative))
        self.assertFalse(old_part.exists())

    def test_migration_scan_does_not_hide_directory_access_errors(self):
        from dataclasses import replace
        item=replace(self.add(),legacy_relatives=('old/previous.pdf',))
        with patch('nas_sync.os.scandir',side_effect=PermissionError('NAS denied')):
            with self.assertRaises(PermissionError):self.u.migrate(item)

    def test_migration_scan_cache_is_bounded(self):
        item=self.add()
        for index in range(140):
            self.assertFalse(self.u.scan_exists(f'missing-{index}/file.pdf'))
        self.assertEqual(len(self.u.scan_directories),128)

    def test_migration_heartbeat_reports_while_main_thread_waits_and_stops(self):
        from main import migration_progress
        item=self.add()
        reported=threading.Event()
        lines=[]
        def capture(base,message):
            lines.append(message)
            reported.set()
        with patch('main.log',side_effect=capture):
            with migration_progress(self.s.base,43563,interval=0.01) as update:
                update(123,item)
                self.assertTrue(reported.wait(2),'heartbeat missing while main thread waits')
        self.assertTrue(any('已检查 123/43563' in line and item.relative in line for line in lines))
        self.assertFalse(any(t.name=='nas-migration-progress' for t in threading.enumerate()))

    def test_managed_log_only_uses_captured_stdout(self):
        import io
        from main import log
        with patch.dict('os.environ',{'PROJECT_TOOLS_LOG_CAPTURED':'1'}), \
             patch('main.RotatingFileHandler',side_effect=AssertionError('duplicate run.log')), \
             patch('main.sys.stdout',io.StringIO()) as output:
            log(self.s.base,'managed evidence')
        self.assertEqual(output.getvalue().count('managed evidence'),1)

    def test_high_volume_successes_and_archives_are_summarized_with_exact_counts(self):
        from main import UploadEvents
        from types import SimpleNamespace
        clock=[0.0]
        with patch('main.log') as output:
            events=UploadEvents(self.s.base,clock=lambda:clock[0])
            for index in range(500):
                item=SimpleNamespace(relative=f'file-{index}.pdf')
                events.record('archived',item)
                events.record('skipped',item)
                events.progress('upload progress')
            events.flush()
        lines=[call.args[1] for call in output.call_args_list]
        self.assertEqual(len(lines),3)
        self.assertIn('正式目录旧副本归档 499 个',lines[-1])
        self.assertIn('跳过 500 个',lines[-1])
        self.assertIn('file-499.pdf',lines[-1])

    def test_directory_alias_changed_during_old_version_backup_prevents_replacement(self):
        from dataclasses import replace
        item=self.add()
        self.u.one(item)
        original_bytes=(self.s.target/item.relative).read_bytes()
        item.path.write_bytes(b'new verified replacement payload')
        current=item.path.stat()
        updated=replace(item,size=current.st_size,mtime_ns=current.st_mtime_ns,
                        sha256=hashlib.sha256(item.path.read_bytes()).hexdigest())
        blocked=self.s.target/'608200'
        redirected=[False]
        original=Path.lstat
        backup=self.u.backup_managed
        def changed_after_backup(*args):
            result=backup(*args)
            redirected[0]=True
            return result
        with patch.object(self.u,'backup_managed',side_effect=changed_after_backup), \
             patch.object(Path,'lstat',lambda path: SimpleNamespace(st_mode=0,st_file_attributes=0x400) if (redirected[0] and plain(path)==str(blocked)) else original(path)):
            with self.assertRaises(Conflict):self.u.one(updated)
        self.assertTrue(redirected[0])
        self.assertEqual((self.s.target/item.relative).read_bytes(),original_bytes)

    def test_resident_upload_survives_summary_or_cleanup_failure(self):
        from main import scheduled_upload
        with patch('main.run_upload',side_effect=OSError('summary disk temporarily unavailable')), \
             patch('main.log') as output:
            self.assertEqual(scheduled_upload(self.s,threading.Event()),1)
            self.assertIn('summary disk temporarily unavailable',output.call_args.args[1])

    def test_unwritable_operational_log_keeps_upload_loop_alive(self):
        import io
        from main import log
        with patch('main.RotatingFileHandler',side_effect=PermissionError('busy log')), \
             patch('main._log_warned',False), patch('main.sys.stdout',io.StringIO()), \
             patch('main.sys.stderr',io.StringIO()) as errors:
            log(self.s.base,'still uploading')
            self.assertIn('busy log',errors.getvalue())

    def test_upload_rejects_directory_alias_before_writing(self):
        item=self.add()
        blocked=self.s.target/'608200'
        original=Path.lstat
        with patch.object(Path,'lstat',lambda path: SimpleNamespace(st_mode=0,st_file_attributes=0x400) if plain(path)==str(blocked) else original(path)):
            with self.assertRaises(Conflict):self.u.one(item)
        self.assertFalse(self.s.target.exists())

    def test_upload_rechecks_directory_alias_before_publishing(self):
        item=self.add()
        blocked=self.s.target/'608200'
        redirected=[False]
        original=Path.lstat
        def progress(value):
            if value['phase']=='回读NAS校验':redirected[0]=True
        self.u.progress=progress
        with patch.object(Path,'lstat',lambda path: SimpleNamespace(st_mode=0,st_file_attributes=0x400) if (redirected[0] and plain(path)==str(blocked)) else original(path)):
            with self.assertRaises(Conflict):self.u.one(item)
        self.assertTrue(redirected[0])
        self.assertFalse((self.s.target/item.relative).exists())

    def test_archival_journal_recovers_after_interruption_after_rename(self):
        from dataclasses import replace
        item=self.add()
        self.u.one(item)
        alias=self.s.target/'608200/duplicate.pdf'
        alias.write_bytes(item.path.read_bytes())
        item=replace(item,legacy_relatives=('608200/duplicate.pdf',))
        actual=self.u.migration_record
        def interrupted(candidate,previous,state):
            if state=='moved':raise RuntimeError('crash after rename')
            actual(candidate,previous,state)
        with patch.object(self.u,'migration_record',side_effect=interrupted):
            with self.assertRaises(RuntimeError):self.u.archive_legacy_copies(item)
        self.assertFalse(alias.exists())
        self.u.archive_legacy_copies(item)
        self.assertEqual(self.u.db.execute('SELECT state FROM path_migrations').fetchone()[0],'moved')

    def test_all_verified_formal_aliases_are_archived_when_destination_exists(self):
        from dataclasses import replace
        item=self.add()
        item=replace(item,relative='history/608200/old.pdf',
                     legacy_relatives=('608200/old-a.pdf','608200/old-b.pdf'))
        self.u.one(item)
        for alias in item.legacy_relatives:
            path=self.s.target/alias
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_bytes(item.path.read_bytes())
        self.assertEqual(self.u.archive_legacy_copies(item),2)
        for alias in item.legacy_relatives:self.assertFalse((self.s.target/alias).exists())
        self.assertTrue((self.s.target/item.relative).exists())
        self.assertEqual(len(list((self.s.target/'history').rglob('*.pdf'))),3)
        self.assertEqual(self.u.archive_legacy_copies(item),0)

    def test_unknown_alias_and_current_protected_path_are_never_archived(self):
        from dataclasses import replace
        item=self.add()
        self.u.one(item)
        alias=self.s.target/'608200/unknown.pdf'
        alias.write_bytes(b'unknown')
        mapped=replace(item,legacy_relatives=('608200/unknown.pdf',))
        with self.assertRaises(Conflict):self.u.archive_legacy_copies(mapped)
        self.assertEqual(alias.read_bytes(),b'unknown')
        self.u.protected={'608200/unknown.pdf'}
        self.assertEqual(self.u.archive_legacy_copies(mapped),0)
        self.assertTrue(alias.exists())

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        base=self.root/'upload';base.mkdir()
        library=self.root/'fusion';(library/'runtime').mkdir(parents=True)
        source=library/'downloads/HABSHANDBN';source.mkdir(parents=True)
        self.s=Settings(source,library/'runtime/ledger.sqlite3',self.root/'nas',base,chunk=4,attempts=2,scope='test-scope')
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.executescript('CREATE TABLE files(path TEXT,bytes INTEGER,sha256 TEXT,status TEXT); CREATE TABLE file_history(path TEXT,bytes INTEGER,sha256 TEXT);')
        self.u=Uploader(self.s)

    def tearDown(self):
        self.u.close();self.temp.cleanup()

    def add(self,relative='608200/folder/图纸.pdf',data=b'123456789012',status='done'):
        source=self.s.source/relative;source.parent.mkdir(parents=True,exist_ok=True);source.write_bytes(data)
        digest=hashlib.sha256(data).hexdigest()
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('INSERT INTO files(path,bytes,sha256,status) VALUES(?,?,?,?)',('downloads/HABSHANDBN/'+relative,len(data),digest,status))
        return Candidate(source,relative,len(data),source.stat().st_mtime_ns,digest)

    def test_root_mapping_and_ledger_read_only(self):
        self.add();before=self.s.ledger.read_bytes()
        items,ignored=candidates(self.s)
        self.assertEqual(ignored,0);self.assertEqual(len(items),1)
        target,_=self.u.destination(items[0])
        self.assertTrue(str(target).endswith(str(Path('608200/folder/图纸.pdf'))))
        self.assertNotIn('HABSHANDBN',str(target))
        self.assertEqual(self.s.ledger.read_bytes(),before)

    def retained_formal_fixture(self, active=0):
        raw = self.add('608200/图纸__rev_0__123__abcdef.pdf')
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.executescript('ALTER TABLE files ADD COLUMN filename TEXT; ALTER TABLE files ADD COLUMN active INTEGER;')
            db.execute("UPDATE files SET filename='图纸.pdf',active=?", (active,))
            db.execute("INSERT INTO files(path,bytes,sha256,status,filename,active) VALUES(NULL,0,NULL,'pending','图纸.pdf',1)")
        return raw

    def modern_versions(self):
        older=self.add('608200/old-v1.pdf',b'old')
        newer=self.add('608200/old-v2.pdf',b'newest')
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            for name,kind in [('project','TEXT'),('id','TEXT'),('document_id','TEXT'),('folder_id','TEXT'),
                              ('filename','TEXT'),('active','INTEGER'),('updated','TEXT'),('relation','TEXT')]:
                db.execute(f'ALTER TABLE files ADD COLUMN {name} {kind}')
            db.execute('''CREATE TABLE documents(project,id,folder_id,first_id,reference)''')
            db.execute('DROP TABLE file_history')
            db.execute('''CREATE TABLE file_history(id INTEGER PRIMARY KEY,project,file_id,path,bytes,sha256,archived)''')
            db.execute('CREATE TABLE refresh_cycles(project,status)')
            db.execute("INSERT INTO refresh_cycles VALUES('p','complete')")
            for identifier,item,stamp in [('v1',older,'2099-01-01'),('v2',newer,'2099-01-02')]:
                db.execute('''UPDATE files SET project='p',id=?,document_id=?,folder_id='f',filename='drawing.pdf',
                  active=0,updated=?,relation='Document' WHERE path=?''',
                  (identifier,identifier,stamp,item.path.relative_to(self.s.ledger.parent.parent).as_posix()))
            db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,active,status,relation)
              VALUES('p','v3','v3','f','drawing.pdf',1,'pending','Document')''')
            db.executemany("INSERT INTO documents VALUES('p',?,'f','first','REF')",[(name,) for name in ('v1','v2','v3')])
        return older,newer

    def test_modern_pending_latest_keeps_one_formal_and_archives_older_without_reupload(self):
        older,newer=self.modern_versions()
        self.u.one(older);self.u.one(newer)
        before=self.s.ledger.read_bytes()
        items,_=candidates(self.s)
        self.assertEqual(self.s.ledger.read_bytes(),before)
        formal=[i for i in items if not i.relative.startswith('history/')]
        history=[i for i in items if i.relative.startswith('history/')]
        self.assertEqual(len(formal),1);self.assertEqual(formal[0].sha256,newer.sha256)
        self.assertEqual(len(history),1);self.assertTrue(history[0].formal_guards)
        self.u.protected={i.relative.casefold() for i in items}
        self.assertIsNone(self.u.migrate(history[0]))
        self.assertTrue((self.s.target/older.relative).exists())
        self.assertEqual(self.u.one(formal[0]),('migrated',0))
        self.assertEqual(self.u.one(history[0]),('migrated',0))
        self.u.archive_legacy_copies(history[0])
        formal_files=[p for p in self.s.target.rglob('*') if p.is_file() and 'history' not in p.relative_to(self.s.target).parts]
        self.assertEqual([p.read_bytes() for p in formal_files],[b'newest'])
        self.assertEqual((self.s.target/history[0].relative).read_bytes(),b'old')

    def test_modern_missing_newer_history_keeps_newest_available_older(self):
        older,newer=self.modern_versions()
        newer.path.unlink()
        items,_=candidates(self.s)
        formal=[i for i in items if not i.relative.startswith('history/')]
        self.assertEqual(len(formal),1)
        self.assertEqual(formal[0].sha256,older.sha256)

    def test_modern_same_id_multiple_history_rows_keep_one_formal(self):
        older,newer=self.modern_versions()
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('DELETE FROM files WHERE active=0')
            for index,item in enumerate((older,newer),1):
                db.execute('''INSERT INTO file_history VALUES(?, 'p','v3',?,?,?,?)''',
                    (index,item.path.relative_to(self.s.ledger.parent.parent).as_posix(),item.size,item.sha256,f'2099-01-0{index}'))
        items,_=candidates(self.s)
        formal=[i for i in items if not i.relative.startswith('history/')]
        self.assertEqual(len(formal),1)
        self.assertEqual(formal[0].sha256,newer.sha256)
        self.assertEqual(len([i for i in items if i.formal_guards]),1)

    def test_modern_verified_latest_takes_precedence_over_all_histories(self):
        older,newer=self.modern_versions()
        latest=self.add('608200/latest.pdf',b'latest')
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute("DELETE FROM files WHERE path=?",(latest.path.relative_to(self.s.ledger.parent.parent).as_posix(),))
            db.execute("UPDATE files SET path=?,bytes=?,sha256=?,status='done' WHERE id='v3'",
                (latest.path.relative_to(self.s.ledger.parent.parent).as_posix(),latest.size,latest.sha256))
        items,_=candidates(self.s)
        formal=[i for i in items if not i.relative.startswith('history/')]
        self.assertEqual(len(formal),1)
        self.assertEqual(formal[0].sha256,latest.sha256)
        self.assertEqual(len([i for i in items if i.formal_guards]),2)

    def test_modern_corrupt_keeper_does_not_retire_last_nas_old_copy(self):
        older,newer=self.modern_versions()
        self.u.one(older)
        items,_=candidates(self.s)
        formal,history=items
        newer.path.write_bytes(b'broken')
        self.u.protected={i.relative.casefold() for i in items}
        with self.assertRaises(Conflict):self.u.one(formal)
        self.assertIsNone(self.u.migrate(history))
        with self.assertRaisesRegex(Conflict,'正式副本尚未校验就绪'):self.u.one(history)
        self.assertEqual((self.s.target/older.relative).read_bytes(),b'old')

    def test_modern_partial_catalogue_does_not_reclassify_nas_formal_files(self):
        self.modern_versions()
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute("UPDATE refresh_cycles SET status='associations'")
        with self.assertRaisesRegex(Stopped,'尚未完整核对'):candidates(self.s)

    def test_modern_upload_round_publishes_keeper_before_retiring_older_copy(self):
        older,newer=self.modern_versions()
        self.u.one(older)
        at=datetime(2026,11,3,13,tzinfo=BEIJING)
        with patch('main.now',return_value=at),patch('main.connect'),patch('main.log'):
            self.assertEqual(run_upload(self.s,'once',threading.Event()),0)
        formal=[p for p in self.s.target.rglob('*') if p.is_file() and 'history' not in p.relative_to(self.s.target).parts]
        self.assertEqual([p.read_bytes() for p in formal],[b'newest'])
        self.assertIn(b'old',{p.read_bytes() for p in (self.s.target/'history').rglob('*') if p.is_file()})

    def test_modern_keeper_hash_proof_is_reused_and_invalidated_on_change(self):
        older,newer=self.modern_versions()
        formal,history=candidates(self.s)[0]
        self.u.one(formal)
        with patch.object(self.u,'hash_file',wraps=self.u.hash_file) as checksum:
            for _ in range(3):self.assertTrue(self.u.formal_ready(history))
            self.assertEqual(checksum.call_count,0)
            target=self.s.target/formal.relative
            target.write_bytes(b'changed-size')
            self.assertFalse(self.u.formal_ready(history,required=False))

    def test_retained_download_predecessor_stays_in_formal_tree_under_original_name(self):
        raw = self.retained_formal_fixture()
        before = self.s.ledger.read_bytes()
        items, _ = candidates(self.s)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].relative, '608200/图纸.pdf')
        self.assertIn(raw.relative, items[0].legacy_relatives)
        self.assertEqual(self.s.ledger.read_bytes(), before)
        self.u.one(raw)
        self.u.protected = {items[0].relative.casefold()}
        self.assertEqual(self.u.one(items[0]), ('migrated', 0))
        self.assertEqual((self.s.target / items[0].relative).read_bytes(), raw.path.read_bytes())
        self.assertFalse((self.s.target / raw.relative).exists())

    def test_active_download_uses_trusted_original_filename_without_identity_suffix(self):
        self.retained_formal_fixture(active=1)
        items, _ = candidates(self.s)
        self.assertEqual(items[0].relative, '608200/图纸.pdf')

    def test_formal_original_name_keeps_mapped_collision_number(self):
        self.retained_formal_fixture(active=1)
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.executescript('ALTER TABLE files ADD COLUMN project TEXT; ALTER TABLE files ADD COLUMN id TEXT; CREATE TABLE local_paths(project,file_id,local_path,platform_path,local_filename);')
            db.execute("UPDATE files SET project='p',id='one' WHERE status='done'")
            db.execute("INSERT INTO local_paths VALUES('p','one',?,'HABSHANDBN/608200/图纸.pdf','图纸-1.pdf')",
                       ('downloads/HABSHANDBN/608200/图纸__rev_0__123__abcdef.pdf',))
        items, _ = candidates(self.s)
        self.assertEqual(items[0].relative, '608200/图纸-1.pdf')

    def test_physically_archived_predecessor_remains_in_history(self):
        raw = self.retained_formal_fixture()
        archived = self.s.ledger.parent.parent / 'history/version-1/HABSHANDBN' / raw.relative
        archived.parent.mkdir(parents=True)
        raw.path.replace(archived)
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute("UPDATE files SET path=? WHERE status='done'", (archived.relative_to(self.s.ledger.parent.parent).as_posix(),))
        items, _ = candidates(self.s)
        self.assertTrue(items[0].relative.startswith('history/'))

    def mapped_candidate(self, relative='_long_paths/42/图纸__rev_0__v1.pdf', deep=False):
        raw = self.add(relative)
        folder = 'HABSHANDBN/608200/平台原始目录/完整目录名称'
        if deep:
            folder += '/' + '/'.join('directory-'+str(i)+'-'+'x'*35 for i in range(7))
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.executescript('ALTER TABLE files ADD COLUMN project TEXT; ALTER TABLE files ADD COLUMN folder_id TEXT; CREATE TABLE folders(project TEXT,id TEXT,path TEXT);')
            db.execute('UPDATE files SET project=?,folder_id=?',('p','999'))
            db.execute('INSERT INTO folders VALUES(?,?,?)',('p','42',folder))
        before = self.s.ledger.read_bytes()
        mapped = candidates(self.s)[0][0]
        self.assertEqual(self.s.ledger.read_bytes(),before)
        return raw, mapped

    def test_compact_source_restores_platform_directory_and_preserves_version_filename(self):
        raw, mapped = self.mapped_candidate()
        self.assertEqual(mapped.relative,'608200/平台原始目录/完整目录名称/'+raw.path.name)
        self.assertEqual(mapped.legacy_relatives,(raw.relative,))
        self.assertEqual(plain(mapped.path),str(raw.path))

    def test_regular_id_suffixed_directory_restores_the_same_platform_directory(self):
        raw, mapped = self.mapped_candidate('608200/truncated__42/图纸__rev_0__v1.pdf')
        self.assertEqual(mapped.relative,'608200/平台原始目录/完整目录名称/'+raw.path.name)

    def test_old_nas_short_path_migrates_without_upload_and_then_skips(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        old_target,_ = self.u.destination(raw)
        before = self.u.wire
        self.assertEqual(self.u.one(mapped),('migrated',0))
        target,_ = self.u.destination(mapped)
        self.assertEqual(target.read_bytes(),raw.path.read_bytes())
        self.assertFalse(old_target.exists())
        self.assertTrue(raw.path.exists())
        self.assertEqual(self.u.wire,before)
        self.assertEqual(self.u.one(mapped),('skipped',0))
        self.assertEqual(self.u.db.execute('SELECT state FROM path_migrations').fetchone()[0],'moved')

    def test_old_nas_short_path_can_be_recovered_without_upload_cache(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        with self.u.db:
            self.u.db.execute('DELETE FROM uploads')
        self.assertEqual(self.u.one(mapped),('migrated',0))

    def test_stop_before_migration_keeps_old_file_and_resumes_safely(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        actual = self.u.migration_record
        def stop_after_plan(item,previous,state):
            actual(item,previous,state)
            if state=='planned':raise Stopped('deadline')
        with patch.object(self.u,'migration_record',side_effect=stop_after_plan):
            with self.assertRaises(Stopped):self.u.one(mapped)
        self.assertTrue(self.u.destination(raw)[0].exists())
        self.assertFalse(self.u.destination(mapped)[0].exists())
        self.assertEqual(self.u.one(mapped),('migrated',0))

    def test_crash_after_rename_recovers_by_adopting_verified_new_target(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        before = self.u.wire
        with patch.object(self.u,'save',side_effect=RuntimeError('process interrupted')):
            with self.assertRaises(RuntimeError):self.u.one(mapped)
        self.assertFalse(self.u.destination(raw)[0].exists())
        self.assertTrue(self.u.destination(mapped)[0].exists())
        self.assertEqual(self.u.one(mapped),('adopted',0))
        self.assertEqual(self.u.wire,before)
        self.assertEqual(self.u.db.execute('SELECT state FROM path_migrations').fetchone()[0],'completed')

    def test_modified_old_nas_file_is_preserved_as_conflict(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        old_target,_ = self.u.destination(raw)
        old_target.write_bytes(b'external-edit')
        with self.assertRaises(Conflict):
            self.u.one(mapped)
        self.assertEqual(old_target.read_bytes(),b'external-edit')
        self.assertFalse(self.u.destination(mapped)[0].exists())

    def test_existing_canonical_conflict_never_overwrites_either_copy(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        target,_ = self.u.destination(mapped)
        target.parent.mkdir(parents=True)
        target.write_bytes(b'unmanaged')
        with self.assertRaises(Conflict):
            self.u.one(mapped)
        self.assertEqual(target.read_bytes(),b'unmanaged')
        self.assertTrue(self.u.destination(raw)[0].exists())

    def test_identical_canonical_target_adopts_and_retains_existing_duplicate(self):
        raw, mapped = self.mapped_candidate()
        self.u.one(raw)
        target,_ = self.u.destination(mapped)
        target.parent.mkdir(parents=True)
        target.write_bytes(raw.path.read_bytes())
        self.assertEqual(self.u.one(mapped),('adopted',0))
        self.assertTrue(self.u.destination(raw)[0].exists())

    def test_old_partial_moves_and_resumes_under_platform_directory(self):
        raw, mapped = self.mapped_candidate()
        part = self.interrupt_after_chunks(raw)
        length = part.stat().st_size
        before = self.u.wire
        self.assertEqual(self.u.one(mapped),('uploaded',mapped.size-length))
        self.assertEqual(self.u.wire-before,mapped.size-length)
        self.assertFalse(part.exists())

    def test_corrupted_old_partial_is_validated_and_restarted(self):
        raw, mapped = self.mapped_candidate()
        part = self.interrupt_after_chunks(raw)
        part.write_bytes(b'x'*part.stat().st_size)
        self.assertEqual(self.u.one(mapped),('uploaded',mapped.size))
        self.assertEqual(self.u.destination(mapped)[0].read_bytes(),raw.path.read_bytes())

    def test_long_nas_path_is_used_without_compacting_again(self):
        raw, mapped = self.mapped_candidate(deep=True)
        target = self.u.destination(mapped)[0]
        try:
            self.assertGreater(len(plain(target)),260)
            self.assertNotIn('_long_paths',mapped.relative)
            self.assertEqual(self.u.one(mapped),('uploaded',mapped.size))
        finally:
            # tempfile's non-extended Windows rmtree cannot visit this test path.
            if target.exists():target.unlink()
            parent = target.parent
            while plain(parent) != str(self.s.target):
                self.assertTrue(Path(plain(parent)).is_relative_to(self.s.target))
                if parent.exists():parent.rmdir()
                parent = parent.parent

    def test_historical_short_path_uses_its_encoded_folder_id(self):
        raw, mapped = self.mapped_candidate()
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('DELETE FROM files')
            db.execute('ALTER TABLE file_history ADD COLUMN project TEXT')
            db.execute('INSERT INTO file_history VALUES(?,?,?,?)',
                ('downloads/HABSHANDBN/'+raw.relative,raw.size,raw.sha256,'p'))
        restored,_ = candidates(self.s)
        self.assertEqual(Path(restored[0].relative).parent,Path('history')/Path(mapped.relative).parent)
        self.assertIn('__hist-',Path(restored[0].relative).name)
        self.assertEqual(self.u.one(raw),('uploaded',raw.size))
        self.assertEqual(self.u.one(restored[0]),('migrated',0))

    def new_layout(self, relative='long_path/short/图纸.pdf'):
        raw = self.add('608200/original.pdf')
        source = self.s.source.parent/relative
        source.parent.mkdir(parents=True, exist_ok=True)
        raw.path.rename(source)
        folder = 'HABSHANDBN/608200/' + '/'.join('完整目录'+str(i) for i in range(12))
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.executescript('ALTER TABLE files ADD COLUMN project TEXT; ALTER TABLE files ADD COLUMN id TEXT; ALTER TABLE files ADD COLUMN active INTEGER DEFAULT 1; CREATE TABLE local_paths(project TEXT,file_id TEXT,local_path TEXT,platform_path TEXT);')
            db.execute('UPDATE files SET path=?,project=?,id=?', ('downloads/'+relative,'p','f'))
            db.execute('INSERT INTO local_paths VALUES(?,?,?,?)',('p','f','downloads/'+relative,folder+'/图纸.pdf'))
        return source, folder.removeprefix('HABSHANDBN/')+'/图纸.pdf'

    def test_new_overflow_outside_source_root_restores_full_platform_path(self):
        source, expected = self.new_layout()
        before = self.s.ledger.read_bytes()
        items, ignored = candidates(self.s)
        self.assertEqual(ignored,0)
        self.assertEqual(items[0].relative,expected)
        self.assertFalse(items[0].mapping_error)
        self.assertEqual(self.s.ledger.read_bytes(),before)
        self.u.one(items[0])
        self.assertEqual(self.u.destination(items[0])[0].read_bytes(),source.read_bytes())

    def test_new_regular_path_uses_mapping_instead_of_directory_id_guess(self):
        _, expected = self.new_layout('HABSHANDBN/608200/本地目录/图纸.pdf')
        self.assertEqual(candidates(self.s)[0][0].relative,expected)

    def test_relocated_history_and_inactive_files_stay_outside_current_tree(self):
        source, expected = self.new_layout()
        history = self.s.ledger.parent.parent/'history/version-7/long_path/short/旧图纸.pdf'
        history.parent.mkdir(parents=True)
        history.write_bytes(b'old revision')
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('ALTER TABLE file_history ADD COLUMN project TEXT')
            db.execute('INSERT INTO file_history VALUES(?,?,?,?)',
                ('history/version-7/long_path/short/旧图纸.pdf',12,hashlib.sha256(b'old revision').hexdigest(),'p'))
        items, ignored = candidates(self.s)
        self.assertEqual(ignored,0)
        self.assertIn(expected,{i.relative for i in items})
        retained=next(i for i in items if i.relative!=expected)
        self.assertEqual(Path(retained.relative).parent,Path('history')/Path(expected).parent)
        self.assertIn('旧图纸__hist-',Path(retained.relative).name)
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('UPDATE files SET active=0')
        items,_ = candidates(self.s)
        self.assertTrue(all(i.relative.startswith('history/') for i in items))

    def test_new_overflow_missing_mapping_fails_closed(self):
        self.new_layout()
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('DELETE FROM local_paths')
        items,_ = candidates(self.s)
        self.assertTrue(items[0].mapping_error)
        with self.assertRaises(Conflict):self.u.one(items[0])

    def test_all_files_can_live_in_overflow_without_physical_source_root(self):
        _, expected = self.new_layout()
        (self.s.source/'608200').rmdir()
        self.s.source.rmdir()
        config = self.s.base/'config.json'
        config.write_text(json.dumps(dict(source_root=str(self.s.source),verification_method='smb',
            download_ledger=str(self.s.ledger),target_root=r'\\nas\share\project')),encoding='utf-8')
        loaded = load_settings(config)
        self.assertEqual(candidates(loaded)[0][0].relative,expected)

    def test_history_absolute_paths_are_relocated_and_foreign_scopes_ignored(self):
        raw = self.add('608200/file.pdf')
        history = self.s.ledger.parent.parent/'history/version-5/HABSHANDBN/608200/file.pdf'
        history.parent.mkdir(parents=True)
        raw.path.rename(history)
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('DELETE FROM files')
            db.execute('INSERT INTO file_history VALUES(?,?,?)',
                (r'C:\old\fusion\history\version-5\HABSHANDBN\608200\file.pdf',raw.size,raw.sha256))
            db.execute('INSERT INTO file_history VALUES(?,?,?)',
                ('history/version-5/OTHER/608200/file.pdf',raw.size,raw.sha256))
        items, ignored = candidates(self.s)
        self.assertEqual(ignored,1)
        self.assertEqual(Path(items[0].relative).parent,Path('history/608200'))
        self.assertIn('file__hist-',Path(items[0].relative).name)
        self.assertIn('history/version-5/608200/file.pdf',items[0].legacy_relatives)

    def test_ledger_parent_traversal_does_not_upload_program_files(self):
        self.new_layout()
        hidden = self.s.ledger.parent/'private.pdf'
        hidden.write_bytes(b'123456789012')
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            malicious = 'downloads/HABSHANDBN/../../runtime/private.pdf'
            db.execute('UPDATE files SET path=?',(malicious,))
            db.execute('UPDATE local_paths SET local_path=?',(malicious,))
        self.assertEqual(candidates(self.s),([],1))

    def test_repaired_history_has_shared_platform_tree_and_migrates_old_batch_target(self):
        raw, mapped = self.mapped_candidate()
        library = self.s.ledger.parent.parent
        new_path = 'history/HABSHANDBN/608200/group/drawing__rev_0.pdf'
        repaired = library/new_path
        repaired.parent.mkdir(parents=True)
        raw.path.rename(repaired)
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('DELETE FROM files')
            db.execute('ALTER TABLE file_history ADD COLUMN project TEXT')
            db.execute('INSERT INTO file_history VALUES(?,?,?,?)',(new_path,raw.size,raw.sha256,'p'))
            db.execute('CREATE TABLE history_paths(path TEXT,platform_path TEXT,original_path TEXT)')
            db.execute('INSERT INTO history_paths VALUES(?,?,?)',(new_path,
                'HABSHANDBN/608200/group/drawing__rev_0.pdf',
                'history/version-7/HABSHANDBN/608200/group/drawing__rev_0.pdf'))
        item = candidates(self.s)[0][0]
        self.assertEqual(Path(item.relative).parent,Path('history/608200/group'))
        self.assertIn('drawing__rev_0__hist-',Path(item.relative).name)
        self.assertFalse(item.mapping_error)
        old = self.s.target/'history/version-7/608200/group/drawing__rev_0.pdf'
        old.parent.mkdir(parents=True)
        old.write_bytes(repaired.read_bytes())
        self.assertEqual(self.u.one(item),('migrated',0))
        self.assertFalse(old.exists())

    def test_missing_short_path_mapping_is_a_conflict_not_another_short_upload(self):
        raw = self.add('_long_paths/42/file.pdf')
        mapped = candidates(self.s)[0][0]
        self.assertTrue(mapped.mapping_error)
        with self.assertRaises(Conflict):self.u.one(mapped)
        self.assertFalse(self.u.destination(raw)[0].exists())

    def test_platform_path_traversal_is_not_used(self):
        raw, _ = self.mapped_candidate()
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute("UPDATE folders SET path='HABSHANDBN/../escape'")
        mapped = candidates(self.s)[0][0]
        self.assertTrue(mapped.mapping_error)
        with self.assertRaises(Conflict):self.u.one(mapped)

    def test_pending_partial_untracked_and_missing_files_are_not_uploaded(self):
        self.add('608200/pending.pdf',status='downloading')
        self.add('608200/incomplete.part')
        missing=self.add('608200/missing.pdf');missing.path.unlink()
        (self.s.source/'untracked.txt').write_bytes(b'external')
        items,_=candidates(self.s)
        self.assertEqual(items,[])

    def test_upload_hash_verified_and_repeat_skipped(self):
        item=self.add()
        self.assertEqual(self.u.one(item),('uploaded',item.size))
        target,part=self.u.destination(item)
        self.assertEqual(target.read_bytes(),item.path.read_bytes());self.assertFalse(part.exists())
        self.assertEqual(self.u.row(item)['status'],'done')
        self.assertEqual(self.u.one(item),('skipped',0))
        self.assertEqual(self.u.one(item,True),('adopted',0))

    def test_identical_existing_file_is_adopted(self):
        item=self.add();target,_=self.u.destination(item);target.parent.mkdir(parents=True);target.write_bytes(item.path.read_bytes())
        self.assertEqual(self.u.one(item),('adopted',0))

    def test_unmanaged_or_edited_remote_file_never_overwritten(self):
        item=self.add();target,_=self.u.destination(item);target.parent.mkdir(parents=True);target.write_bytes(b'other')
        with self.assertRaises(Conflict):self.u.one(item)
        self.assertEqual(target.read_bytes(),b'other')
        target.unlink();self.u.one(item);target.write_bytes(b'edited')
        with self.assertRaises(Conflict):self.u.one(item)
        self.assertEqual(target.read_bytes(),b'edited')

    def test_managed_local_update_backs_up_old_remote_and_replaces_it(self):
        item=self.add();self.u.one(item)
        item.path.write_bytes(b'new-version')
        data=item.path.read_bytes();updated=Candidate(item.path,item.relative,len(data),item.path.stat().st_mtime_ns,hashlib.sha256(data).hexdigest())
        self.u.one(updated)
        target,_=self.u.destination(item)
        self.assertEqual(target.read_bytes(),data)
        backups=[p for p in (self.s.target/'history').rglob('*') if p.is_file()]
        self.assertEqual(len(backups),1);self.assertEqual(backups[0].read_bytes(),b'123456789012')
        self.assertIn('__hist-',backups[0].name)
        self.assertFalse((self.s.target/'.fusionlive_upload_history').exists())

    def interrupt_after_chunks(self,item):
        calls=[0]
        def check():
            calls[0]+=1
            if calls[0]==4:raise Stopped('deadline')
        self.u.check=check
        with self.assertRaises(Stopped):self.u.one(item)
        target,part=self.u.destination(item)
        self.assertFalse(target.exists());self.assertGreater(part.stat().st_size,0)
        self.u.check=lambda:None
        return part

    def test_interrupted_file_resumes_after_reopen(self):
        item=self.add();part=self.interrupt_after_chunks(item);partial=part.stat().st_size
        self.u.close();self.u=Uploader(self.s)
        self.assertEqual(self.u.one(item),('uploaded',item.size-partial))
        self.assertEqual(self.u.wire,item.size-partial)

    def test_corrupted_partial_is_restarted(self):
        item=self.add();part=self.interrupt_after_chunks(item);part.write_bytes(b'x'*part.stat().st_size)
        before=self.u.wire
        self.assertEqual(self.u.one(item),('uploaded',item.size))
        self.assertEqual(self.u.wire-before,item.size)

    def test_incorrect_download_hash_does_not_publish(self):
        item=self.add();bad=Candidate(item.path,item.relative,item.size,item.mtime_ns,'0'*64)
        with self.assertRaises(Conflict):self.u.one(bad)
        target,_=self.u.destination(item);self.assertFalse(target.exists())

    def test_managed_update_keeps_previous_remote_digest_across_interruption(self):
        old=self.add();self.u.one(old)
        old_data=old.path.read_bytes()
        old.path.write_bytes(b'new-content-with-more-bytes')
        data=old.path.read_bytes()
        item=Candidate(old.path,old.relative,len(data),old.path.stat().st_mtime_ns,hashlib.sha256(data).hexdigest())
        state={'armed':False,'calls':0}
        def progress(value):
            if value['phase']=='开始上传':state['armed']=True
        def check():
            if state['armed']:
                state['calls']+=1
                if state['calls']==3:raise Stopped('stop update')
        self.u.progress=progress;self.u.check=check
        with self.assertRaises(Stopped):self.u.one(item)
        target,_=self.u.destination(item)
        self.assertEqual(target.read_bytes(),old_data)
        self.assertEqual(self.u.row(item)['remote_sha256'],old.sha256)
        self.u.progress=None;self.u.check=lambda:None
        self.u.one(item)
        self.assertEqual(target.read_bytes(),data)

    def test_failed_nas_readback_never_publishes_and_can_recover(self):
        item=self.add();actual=self.u.hash_file
        def bad_readback(path,*args):
            return '0'*64 if path.name.endswith('.part') else actual(path,*args)
        with patch.object(self.u,'hash_file',side_effect=bad_readback):
            with self.assertRaises(OSError):self.u.one(item)
        target,part=self.u.destination(item)
        self.assertFalse(target.exists());self.assertTrue(part.exists())
        self.assertEqual(self.u.one(item),('uploaded',0))
        self.assertEqual(target.read_bytes(),item.path.read_bytes())

    def test_source_change_after_selection_stops_upload(self):
        item=self.add();item.path.write_bytes(b'changed')
        with self.assertRaises(Conflict):self.u.one(item)

    def test_historical_path_deduplicated(self):
        self.add()
        with closing(sqlite3.connect(self.s.ledger)) as db, db:
            db.execute('INSERT INTO file_history SELECT path,bytes,sha256 FROM files')
        self.assertEqual(len(candidates(self.s)[0]),1)

    def test_long_path_fallback_is_preserved_under_destination(self):
        self.add('_long_paths/123/图纸.pdf');items,_=candidates(self.s)
        self.assertEqual(items[0].relative,'_long_paths/123/图纸.pdf')

    def test_midnight_stop_happens_before_connecting(self):
        at=datetime(2026,10,22,0,tzinfo=BEIJING)
        with patch('main.now',return_value=at),patch('main.connect') as connect,patch('main.log'):
            self.assertEqual(run_upload(self.s,'once',threading.Event()),2)
            connect.assert_not_called()

    def test_local_rename_chain_uses_hash_qualified_journal(self):
        old=self.add('608200/old-id/old.pdf')
        self.u.one(old)
        middle='downloads/HABSHANDBN/608200/middle.pdf'
        final='downloads/HABSHANDBN/608200/final.pdf'
        new=self.s.ledger.parent.parent/final
        new.parent.mkdir(parents=True,exist_ok=True)
        old.path.rename(new)
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('CREATE TABLE layout_moves(payload TEXT,state TEXT)')
            for source,target in [('downloads/HABSHANDBN/'+old.relative,middle),(middle,final)]:
                db.execute('INSERT INTO layout_moves VALUES(?,?)',(json.dumps(dict(source=source,target=target,sha256=old.sha256)),'done'))
            db.execute('UPDATE files SET path=?',(final,))
        item=candidates(self.s)[0][0]
        self.assertIn(old.relative,item.legacy_relatives)
        before=self.u.wire
        self.assertEqual(self.u.one(item),('migrated',0))
        self.assertEqual(self.u.wire,before)
        self.assertFalse((self.s.target/'608200/old-id').exists())
        self.assertTrue(self.s.target.is_dir())

    def test_previous_nas_migration_is_followed_after_local_rename(self):
        raw,mapped=self.mapped_candidate()
        self.u.one(raw);self.u.one(mapped)
        renamed=Candidate(mapped.path,'608200/new-name.pdf',mapped.size,mapped.mtime_ns,mapped.sha256,(raw.relative,))
        self.u.close();self.u=Uploader(self.s)
        self.assertEqual(self.u.migrate(renamed),('migrated',0))
        self.assertEqual(self.u.wire,0)
        self.assertFalse(self.u.destination(mapped)[0].exists())

    def test_all_migrations_precede_any_new_upload(self):
        fresh=self.add('608200/a-new.pdf')
        old=self.add('608200/z-old.pdf')
        self.u.one(old)
        renamed=Candidate(old.path,'608200/z-renamed.pdf',old.size,old.mtime_ns,old.sha256,(old.relative,))
        at=datetime(2026,10,22,12,tzinfo=BEIJING)
        actual=Uploader.one
        def verify_order(uploader,item,verify_all=False):
            self.assertTrue(self.u.destination(renamed)[0].exists())
            self.assertFalse(self.u.destination(old)[0].exists())
            return actual(uploader,item,verify_all)
        with patch('main.now',return_value=at),patch('main.connect'),patch('main.log'),patch('main.candidates',return_value=([fresh,renamed],0)),patch.object(Uploader,'one',verify_order):
            self.assertEqual(run_upload(self.s,'once',threading.Event()),0)
        self.assertTrue(self.u.destination(fresh)[0].exists())

    def test_unfinished_local_layout_blocks_before_nas_connection(self):
        (self.s.ledger.parent/'layout.pending').touch()
        with self.assertRaises(Stopped):candidates(self.s)
        at=datetime(2026,10,22,12,tzinfo=BEIJING)
        with patch('main.now',return_value=at),patch('main.connect') as connect,patch('main.log'):
            self.assertEqual(run_upload(self.s,'once',threading.Event()),2)
            connect.assert_not_called()

    def test_migration_does_not_take_another_live_destination(self):
        old=self.add('608200/live.pdf');self.u.one(old)
        history=Candidate(old.path,'history/608200/old.pdf',old.size,old.mtime_ns,old.sha256,(old.relative,))
        self.u.protected={old.relative.casefold(),history.relative.casefold()}
        self.assertIsNone(self.u.migrate(history))
        self.assertTrue(self.u.destination(old)[0].exists())

    def test_max_files_does_not_stall_on_already_uploaded_prefix(self):
        first=self.add('608200/a.pdf');self.u.one(first);self.add('608200/b.pdf')
        at=datetime(2026,10,22,12,tzinfo=BEIJING)
        with patch('main.now',return_value=at),patch('main.connect'),patch('main.log'):
            self.assertEqual(run_upload(self.s,'once',threading.Event(),max_files=1),0)
        self.assertTrue((self.s.target/'608200/b.pdf').is_file())

    def test_resident_mode_retries_pending_work_inside_window(self):
        attempts=[]
        def worker(settings,mode,exiting,*args):
            attempts.append(mode)
            if len(attempts)==2:exiting.set()
            return 2 if len(attempts)==1 else 0
        at=datetime(2026,10,22,12,tzinfo=BEIJING)
        with patch('main.load_settings',return_value=self.s),patch('main.integrity'),patch('main.now',return_value=at),patch('main.log'),patch('main.threading.Thread'),patch('main.signal.signal'),patch('main.run_upload',side_effect=worker),patch('main.time.monotonic',side_effect=itertools.count(0,301)):
            self.assertEqual(main(['schedule']),0)
        self.assertEqual(attempts,['once','once'])


class ScheduleTests(unittest.TestCase):
    def test_window_and_next_day(self):
        for hour,minute,allowed in [(0,0,False),(11,59,False),(12,0,True),(23,59,True)]:
            self.assertEqual(in_window(datetime(2026,10,22,hour,minute,tzinfo=BEIJING)),allowed)
        self.assertEqual(next_start(datetime(2026,10,31,23,59,tzinfo=BEIJING)),datetime(2026,11,1,12,tzinfo=BEIJING))


if __name__=='__main__':unittest.main()
