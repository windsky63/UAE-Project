from contextlib import closing
import hashlib
import itertools
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import datetime

from nas_sync import Settings, Candidate, Uploader, candidates, Stopped, Conflict, BEIJING, plain, load_settings
from main import in_window, next_start, run_upload, main


class UploadTests(unittest.TestCase):
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
            db.execute('INSERT INTO files VALUES(?,?,?,?)',('downloads/HABSHANDBN/'+relative,len(data),digest,status))
        return Candidate(source,relative,len(data),source.stat().st_mtime_ns,digest)

    def test_root_mapping_and_ledger_read_only(self):
        self.add();before=self.s.ledger.read_bytes()
        items,ignored=candidates(self.s)
        self.assertEqual(ignored,0);self.assertEqual(len(items),1)
        target,_=self.u.destination(items[0])
        self.assertTrue(str(target).endswith(str(Path('608200/folder/图纸.pdf'))))
        self.assertNotIn('HABSHANDBN',str(target))
        self.assertEqual(self.s.ledger.read_bytes(),before)

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
        config.write_text(json.dumps(dict(source_root=str(self.s.source),
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
