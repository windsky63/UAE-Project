from contextlib import closing
from contextlib import redirect_stdout
import hashlib
import io
from pathlib import Path
from types import SimpleNamespace
import sqlite3
from unittest.mock import patch

import nas_history
from nas_sync import Candidate, Conflict, Stopped, candidates, history_relative, plain
from main import main
import test_upload


# Only fixture helpers are shared; each existing upload test runs once.
import unittest
class UnifiedHistoryTests(unittest.TestCase):
    def setUp(self):
        self.case=test_upload.UploadTests()
        self.case.setUp()
        self.addCleanup(self.case.tearDown)
        self.u,self.s=self.case.u,self.case.s

    def legacy(self, origin, data, stamp='20261029-120000-000001'):
        relative='.fusionlive_upload_history/'+stamp+'/'+origin
        path=self.s.target/relative
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        return path

    def test_batch_versions_share_parent_keep_distinct_identities_and_stable_names(self):
        raw=self.case.add('608200/group/drawing.pdf')
        with closing(sqlite3.connect(self.s.ledger)) as db,db:
            db.execute('DELETE FROM files')
            for batch,data in [('version-1',b'old version'),('version-2',b'new version')]:
                relative='history/'+batch+'/HABSHANDBN/'+raw.relative
                path=self.s.ledger.parent.parent/relative
                path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
                db.execute('INSERT INTO file_history VALUES(?,?,?)',(relative,len(data),hashlib.sha256(data).hexdigest()))
        items,_=candidates(self.s)
        self.assertEqual(len(items),2)
        self.assertEqual({str(Path(i.relative).parent) for i in items},{str(Path('history/608200/group'))})
        self.assertEqual(len({i.relative for i in items}),2)
        self.assertTrue(all('__hist-' in i.relative for i in items))
        self.assertEqual([i.relative for i in candidates(self.s)[0]],[i.relative for i in items])

    def test_known_timestamp_backups_preview_then_move_without_touching_current(self):
        item=self.case.add();self.u.one(item)
        paths=[self.legacy(item.relative,item.path.read_bytes(),stamp) for stamp in
               ('20261029-120000-000001','20261029-120000-000002')]
        proposal=nas_history.plan(self.u,[item])
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual({e['status'] for e in proposal['entries']},{'ready'})
        self.assertEqual(len({e['target'] for e in proposal['entries']}),2)
        result=nas_history.apply(self.u,proposal)
        self.assertEqual([e['status'] for e in result['entries']],['moved','moved'])
        self.assertFalse(any(p.exists() for p in paths))
        self.assertEqual(self.u.destination(item)[0].read_bytes(),item.path.read_bytes())
        self.assertFalse((self.s.target/'.fusionlive_upload_history').exists())
        self.assertEqual(self.u.wire,item.size)

    def test_unknown_content_is_reported_and_retained(self):
        item=self.case.add();self.u.one(item)
        path=self.legacy(item.relative,b'unknown version')
        proposal=nas_history.plan(self.u,[item])
        self.assertEqual(proposal['entries'][0]['status'],'conflict')
        nas_history.apply(self.u,proposal)
        self.assertEqual(path.read_bytes(),b'unknown version')

    def test_source_changed_after_preview_never_moves(self):
        item=self.case.add();self.u.one(item)
        path=self.legacy(item.relative,item.path.read_bytes())
        proposal=nas_history.plan(self.u,[item]);path.write_bytes(b'changed')
        result=nas_history.apply(self.u,proposal)
        self.assertEqual(result['entries'][0]['status'],'conflict')
        self.assertTrue(path.exists())

    def test_destination_appearing_after_preview_preserves_both_files(self):
        item=self.case.add();self.u.one(item)
        path=self.legacy(item.relative,item.path.read_bytes())
        proposal=nas_history.plan(self.u,[item])
        target=self.s.target/proposal['entries'][0]['target']
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(b'external')
        result=nas_history.apply(self.u,proposal)
        self.assertEqual(result['entries'][0]['status'],'conflict')
        self.assertEqual(target.read_bytes(),b'external');self.assertTrue(path.exists())

    def test_interrupted_move_is_recovered_from_original_or_regenerated_plan(self):
        item=self.case.add();self.u.one(item)
        self.legacy(item.relative,item.path.read_bytes())
        proposal=nas_history.plan(self.u,[item]);actual=self.u.migration_record
        def interrupted(candidate,old,state):
            if state=='moved':raise RuntimeError('fixture crash after rename')
            actual(candidate,old,state)
        with patch.object(self.u,'migration_record',side_effect=interrupted):
            with self.assertRaises(RuntimeError):nas_history.apply(self.u,proposal)
        proposal=nas_history.plan(self.u,[item])
        self.assertEqual(len(proposal['entries']),1)
        result=nas_history.apply(self.u,proposal)
        self.assertEqual(result['entries'][0]['status'],'recovered')

    def test_unknown_regular_batch_file_is_not_moved_on_hash_alone(self):
        item=self.case.add();self.u.one(item)
        path=self.s.target/'history/version-3/unknown/file.pdf'
        path.parent.mkdir(parents=True);path.write_bytes(item.path.read_bytes())
        result=nas_history.plan(self.u,[item])
        self.assertEqual(result['entries'][0]['status'],'conflict')
        self.assertTrue(path.exists())

    def test_old_batch_directory_can_be_mapped_using_known_upload_record(self):
        item=self.case.add();self.u.one(item)
        path=self.s.target/'history/version-3'/item.relative
        path.parent.mkdir(parents=True);path.write_bytes(item.path.read_bytes())
        proposal=nas_history.plan(self.u,[item])
        self.assertEqual(proposal['entries'][0]['status'],'ready')
        self.assertEqual(Path(proposal['entries'][0]['target']).parent,Path('history')/Path(item.relative).parent)
        self.assertEqual(nas_history.apply(self.u,proposal)['entries'][0]['status'],'moved')

    def test_private_backup_interruption_keeps_current_and_retries_same_archive(self):
        old=self.case.add();self.u.one(old)
        old.path.write_bytes(b'new version')
        item=Candidate(old.path,old.relative,old.path.stat().st_size,old.path.stat().st_mtime_ns,hashlib.sha256(old.path.read_bytes()).hexdigest())
        actual=self.u.hash_file
        def interrupt(path,*args):
            if path.name.startswith('.fusionlive-archive-'):raise Stopped('fixture stop before backup publish')
            return actual(path,*args)
        with patch.object(self.u,'hash_file',side_effect=interrupt):
            with self.assertRaises(Stopped):self.u.one(item)
        self.assertEqual(self.u.destination(item)[0].read_bytes(),b'123456789012')
        self.u.one(item)
        backups=[p for p in (self.s.target/'history').rglob('*') if p.is_file()]
        self.assertEqual(len(backups),1)
        self.assertEqual(backups[0].read_bytes(),b'123456789012')
        self.assertEqual(self.u.db.execute('SELECT state FROM archive_copies').fetchone()[0],'verified')

    def test_long_history_component_keeps_identity_and_extension_within_limit(self):
        digest='a'*64
        name=Path(history_relative('608200/'+('图'*240)+'.pdf','version-one',digest)).name
        self.assertLessEqual(len(name.encode('utf-16-le'))//2,240)
        self.assertLessEqual(len(name.encode('utf-8')),240)
        self.assertTrue(name.endswith('.pdf'));self.assertIn('__hist-',name)

    def test_conflicting_history_backup_prevents_current_replacement(self):
        old=self.case.add();self.u.one(old)
        relative=history_relative(old.relative,'nas|'+self.s.scope+'|'+old.relative+'|'+old.sha256,old.sha256)
        archive=self.s.target/relative
        archive.parent.mkdir(parents=True);archive.write_bytes(b'outside change')
        old.path.write_bytes(b'new version')
        item=Candidate(old.path,old.relative,old.path.stat().st_size,old.path.stat().st_mtime_ns,hashlib.sha256(old.path.read_bytes()).hexdigest())
        with self.assertRaises(Conflict):self.u.one(item)
        self.assertEqual(self.u.destination(item)[0].read_bytes(),b'123456789012')
        self.assertEqual(archive.read_bytes(),b'outside change')

    def test_preview_refuses_linked_directories(self):
        item=self.case.add();self.u.one(item)
        path=self.legacy(item.relative,item.path.read_bytes())
        linked=path.parent
        actual=Path.lstat
        with patch.object(Path,'lstat',lambda p: SimpleNamespace(st_mode=0,st_file_attributes=0x400) if plain(p)==str(linked) else actual(p)):
            proposal=nas_history.plan(self.u,[item])
        self.assertEqual([e['status'] for e in proposal['entries']],['conflict'])
        self.assertTrue(path.exists())

    def test_cli_preview_and_apply_use_persisted_plan_without_uploading(self):
        item=self.case.add();self.u.one(item)
        path=self.legacy(item.relative,item.path.read_bytes())
        with patch('main.load_settings',return_value=self.s),patch('main.connect'),patch('main.signal.signal'),patch('main.threading.Thread'),redirect_stdout(io.StringIO()):
            self.assertEqual(main(['history-plan']),0)
            self.assertTrue(path.exists())
            self.assertTrue((self.s.base/'runtime/nas_history_plan.json').is_file())
            self.assertEqual(main(['history-migrate']),0)
        self.assertFalse(path.exists())
        self.assertTrue((self.s.base/'runtime/nas_history_result.json').is_file())

    def test_apply_cannot_move_current_files_using_edited_source(self):
        item=self.case.add();self.u.one(item)
        self.legacy(item.relative,item.path.read_bytes())
        proposal=nas_history.plan(self.u,[item])
        proposal['entries'][0]['source']=item.relative
        self.assertEqual(nas_history.apply(self.u,proposal)['entries'][0]['status'],'conflict')
        self.assertEqual(self.u.destination(item)[0].read_bytes(),item.path.read_bytes())
