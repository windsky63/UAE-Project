import contextlib
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

from ledger import Ledger
from paths import relative_path, resolve_path, native_path
from sync import local_valid, main, scan


def doc(identifier='v1', content=True, **updates):
    return {'id': identifier, 'firstVersionId': 'first', 'fileName': 'drawing.pdf', 'revision': '0',
            'reference': 'DRAWING-001', 'contentSize': '3', 'hasContent': 'true' if content else 'false',
            'hasAttachments': 'false', **updates}


def content(identifier='v1', **updates):
    return {'id': identifier, 'filename': 'drawing.pdf', 'revision': '0', 'contentSize': '3',
            'readAccessStatus': 'Available', 'creationDate': '2026-10-01', **updates}


class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base / 'ledger.sqlite3', base_dir=self.base)
        self.client = Mock(project_id='p', cabinet_id='r', project_name='project')

    def tearDown(self):
        self.ledger.close()
        if os.name == 'nt' and self.base.exists():
            resolved = self.base.resolve()
            if not resolved.is_relative_to(Path(tempfile.gettempdir()).resolve()) or not resolved.name.startswith('tmp'):
                raise RuntimeError('拒绝清理非测试临时目录。')
            shutil.rmtree('\\\\?\\' + str(resolved))
        self.temp.cleanup()

    def catalogue(self, records):
        self.ledger.root(self.client, rescan=True)
        root = self.ledger.one("SELECT * FROM folders WHERE id='r'")
        self.ledger.save_children('p', root, [{'id': 'c', 'text': 'Folder'}])
        folder = self.ledger.one("SELECT * FROM folders WHERE id='c'")
        self.ledger.save_children('p', folder, [])
        self.ledger.save_page('p', 'c', len(records), records, len(records), True)
        self.assertTrue(self.ledger.finish_catalogue('p'))

    def related(self, items, identifier='v1'):
        document = self.ledger.one('SELECT * FROM documents WHERE id=?', (identifier,))
        self.ledger.save_related(document, items)

    def mark_done(self, identifier='v1'):
        file = self.base / 'downloads' / (identifier + '.pdf')
        file.parent.mkdir(exist_ok=True)
        file.write_bytes(b'old')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='done',bytes=3,path=?,sha256=?,attempts=1 WHERE id=?",
                                   (relative_path(file, self.base), hashlib.sha256(b'old').hexdigest(), identifier))
        return file

    def test_same_size_changed_timestamp_requeues_and_preserves_old_file(self):
        self.catalogue([doc()])
        self.related([content()])
        original = self.mark_done()
        self.assertTrue(self.ledger.finish_refresh('p'))
        self.catalogue([doc()])
        self.related([content(creationDate='2026-10-02')])
        file = self.ledger.one('SELECT * FROM files')
        self.assertEqual(file['status'], 'pending')
        self.assertIsNone(file['path'])
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM file_history')['n'], 1)
        self.assertEqual(original.read_bytes(), b'old')

    def test_unchanged_file_is_not_requeued_or_retried(self):
        self.catalogue([doc()])
        self.related([content()])
        self.mark_done()
        before = dict(self.ledger.one('SELECT * FROM files'))
        self.assertTrue(self.ledger.finish_refresh('p'))
        self.catalogue([doc()])
        self.assertEqual(self.ledger.one('SELECT related_state FROM documents')['related_state'], 'pending')
        self.related([content()])
        after = self.ledger.one('SELECT * FROM files')
        for key in ('status', 'path', 'sha256', 'bytes', 'attempts'):
            self.assertEqual(after[key], before[key])

    def linked_observations(self):
        common = dict(firstVersionId='first',isVirtualLink='true',versionLabel='1.9')
        source = content(**common,virtualLinkId='source-link',isSourceDocument='true')
        attached = content(**common,virtualLinkId='package-link',isSourceDocument='false',
                           ownerId='package',relationshipType='Attachment',creationDate='2026-10-02')
        return source,attached

    def test_virtual_link_context_timestamp_does_not_create_false_versions(self):
        self.catalogue([doc(),doc('package')])
        source,attached = self.linked_observations()
        self.related([source])
        original = self.mark_done()
        before = dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        for _ in range(3):
            self.related([attached],'package')
            self.related([source])
        after = self.ledger.one("SELECT * FROM files WHERE id='v1'")
        for key in ('status','path','sha256','bytes','attempts','fingerprint'):
            self.assertEqual(after[key],before[key])
        self.assertEqual(self.ledger.one('SELECT COUNT(*) FROM file_history')[0],0)
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM document_files WHERE file_id='v1' AND active=1")[0],2)
        self.assertEqual(original.read_bytes(),b'old')

    def test_virtual_link_real_content_changes_still_requeue(self):
        source,attached = self.linked_observations()
        for change in ({'contentSize':'4'},{'revision':'1'},{'versionLabel':'1.10'},
                       {'contentChecksum':'new'}, {'filename':'renamed.pdf'}):
            with self.subTest(change=change):
                self.catalogue([doc(),doc('package')])
                self.related([source])
                self.mark_done()
                count = self.ledger.one('SELECT COUNT(*) FROM file_history')[0]
                self.related([{**attached,**change}],'package')
                self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v1'")[0],'pending')
                self.assertEqual(self.ledger.one('SELECT COUNT(*) FROM file_history')[0],count+1)

    def test_timestamp_change_within_same_virtual_entry_still_requeues(self):
        self.catalogue([doc()])
        source,_ = self.linked_observations()
        self.related([source])
        self.mark_done()
        self.related([{**source,'creationDate':'2026-10-03'}])
        self.assertEqual(self.ledger.one('SELECT status FROM files')[0],'pending')
        self.assertEqual(self.ledger.one('SELECT COUNT(*) FROM file_history')[0],1)

    def test_new_attachment_is_found_without_main_metadata_change(self):
        record = doc(hasAttachments='true')
        self.catalogue([record])
        self.related([content()])
        self.mark_done()
        self.assertTrue(self.ledger.finish_refresh('p'))
        self.catalogue([record])
        self.related([content(), content('attachment', filename='new.xlsx', relationshipType='Attachment')])
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v1'")['status'], 'done')
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='attachment'")['status'], 'pending')

    def test_placeholder_becomes_uploaded_and_new_version_is_queued(self):
        self.catalogue([doc(content=False, contentSize='0')])
        placeholder = self.ledger.one('SELECT * FROM documents')
        self.ledger.save_related(placeholder, [], placeholder=True)
        self.assertTrue(self.ledger.finish_refresh('p'))
        self.catalogue([doc()])
        self.related([content()])
        self.assertEqual(self.ledger.one('SELECT change_type FROM documents')['change_type'], 'updated')
        self.mark_done()
        self.assertTrue(self.ledger.finish_refresh('p'))
        self.catalogue([doc('v2', revision='1')])
        self.related([content('v2', revision='1')], 'v2')
        self.assertEqual(self.ledger.one("SELECT active FROM documents WHERE id='v1'")['active'], 0)
        self.assertEqual(self.ledger.one("SELECT active FROM files WHERE id='v1'")['active'], 0)
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v2'")['status'], 'pending')
        self.assertTrue((self.base / 'downloads/v1.pdf').exists())

    def test_incomplete_scan_does_not_hide_old_records(self):
        self.catalogue([doc()])
        self.related([content()])
        self.ledger.finish_refresh('p')
        self.ledger.root(self.client, rescan=True)
        self.assertFalse(self.ledger.finish_catalogue('p'))
        self.assertEqual(self.ledger.one('SELECT active FROM documents')['active'], 1)

    def test_completed_empty_folder_hides_old_records_without_deleting_files(self):
        self.catalogue([doc()])
        self.related([content()])
        original = self.mark_done()
        self.ledger.finish_refresh('p')
        self.catalogue([])
        self.assertEqual(self.ledger.one('SELECT active FROM documents')['active'], 0)
        self.assertEqual(self.ledger.one('SELECT active FROM files')['active'], 0)
        self.assertTrue(original.exists())

    def test_interrupted_association_round_resumes_checked_documents(self):
        self.catalogue([doc(), doc('v2')])
        self.related([content()])
        cycle = self.ledger.cycle
        self.assertFalse(self.ledger.finish_refresh('p'))
        self.catalogue([doc(), doc('v2')])
        self.assertEqual(self.ledger.cycle, cycle)
        states = {r['id']: r['related_state'] for r in self.ledger.all('SELECT id,related_state FROM documents')}
        self.assertEqual(states, {'v1': 'done', 'v2': 'pending'})

    def test_moved_and_renamed_folder_is_reconciled(self):
        self.catalogue([doc()])
        self.ledger.root(self.client, rescan=True)
        root = self.ledger.one("SELECT * FROM folders WHERE id='r'")
        self.ledger.save_children('p', root, [{'id': 'parent', 'text': 'New parent'}])
        parent = self.ledger.one("SELECT * FROM folders WHERE id='parent'")
        self.ledger.save_children('p', parent, [{'id': 'c', 'text': 'Renamed'}])
        folder = self.ledger.one("SELECT * FROM folders WHERE id='c'")
        self.assertEqual(folder['path'], 'project/New parent/Renamed')

    def test_shared_file_remains_active_when_one_document_disappears(self):
        self.catalogue([doc(), doc('v2')])
        self.related([content('shared')])
        self.related([content('shared')], 'v2')
        self.assertTrue(self.ledger.finish_refresh('p'))
        self.catalogue([doc('v2')])
        self.related([content('shared')], 'v2')
        self.assertEqual(self.ledger.one('SELECT active FROM files')['active'], 1)

    def test_copied_folder_relocates_legacy_absolute_paths(self):
        self.catalogue([doc()])
        self.related([content()])
        original = self.mark_done()
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET path=?', (str(original.resolve()),))
        self.ledger.close()
        with tempfile.TemporaryDirectory() as destination:
            moved = Path(destination) / 'different-name'
            shutil.copytree(self.base, moved)
            copy = Ledger(moved / 'ledger.sqlite3', base_dir=moved)
            try:
                item = copy.one('SELECT * FROM files')
                self.assertFalse(Path(item['path']).is_absolute())
                self.assertTrue(resolve_path(item['path'], moved).is_relative_to(moved))
                self.assertTrue(local_valid(item, moved))
            finally:
                copy.close()
        self.ledger = Ledger(self.base / 'ledger.sqlite3', base_dir=self.base)

    def test_relative_paths_reject_escape(self):
        with self.assertRaises(ValueError):
            relative_path('../outside.pdf', self.base)

    @unittest.skipUnless(os.name == 'nt', 'Windows long path behavior')
    def test_windows_long_path_round_trip(self):
        path = self.base / 'downloads' / ('deep' * 20) / ('folder' * 15) / ('file' * 15 + '.pdf')
        self.assertGreater(len(str(path)), 260)
        target = native_path(path)
        target.parent.mkdir(parents=True)
        target.write_bytes(b'pdf')
        stored = relative_path(target, self.base)
        self.assertFalse(stored.startswith('\\\\?\\'))
        self.assertEqual(resolve_path(stored, self.base).read_bytes(), b'pdf')


class FormalFlowTests(unittest.TestCase):
    def setUp(self):
        background=patch('analysis_worker.ensure_background_worker',return_value=None)
        background.start()
        self.addCleanup(background.stop)

    def test_forced_refresh_rechecks_then_skips_unchanged_file(self):
        events = []
        responses = {'documents': [doc()], 'files': [content()], 'data': b'old'}

        class Client:
            project_id, cabinet_id, project_name = 'p', 'r', 'project'
            auth_attempted = True
            logout_result = None
            def __init__(self, *_):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *_):
                events.append('logout')
                self.logout_result = {'confirmed': True}
            def folders(self, identifier):
                events.append('tree:' + identifier)
                return [{'id': 'c', 'text': 'Folder'}] if identifier == 'r' else []
            def document_page(self, identifier, start, limit):
                events.append('catalogue')
                return len(responses['documents']), responses['documents']
            def related(self, identifier, virtual=None):
                events.append('related')
                return responses['files']
            def download(self, item, target, progress=None, verified=None):
                events.append('download:' + item['id'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(responses['data'])
                return {'path': str(target), 'bytes': len(responses['data']), 'sha256': hashlib.sha256(responses['data']).hexdigest()}

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            def factory():
                ledger = Ledger(base / 'ledger.sqlite3', base_dir=base)
                with ledger.db:
                    ledger.db.execute('INSERT OR IGNORE INTO download_scopes VALUES(?,?,?)', ('p', '{"mode":"all"}', '2026-10-20T02:00:00+08:00'))
                return ledger
            with patch('sync.RunLock', return_value=contextlib.nullcontext()), patch('sync.RUNTIME', base / 'runtime'), patch('sync.Ledger', side_effect=factory), patch('sync.FusionClient', Client), patch('sync.check_window'), patch('sync.signal.signal'), patch('sync.export_csv'), patch.dict(os.environ, {'FUSIONLIVE_USERNAME': 'test', 'FUSIONLIVE_PASSWORD': 'fake'}):
                self.assertEqual(main(['sync', '--rescan']), 0)
                with contextlib.closing(sqlite3.connect(base / 'ledger.sqlite3')) as db:
                    first_path = db.execute('SELECT path FROM files').fetchone()[0]
                self.assertFalse(Path(first_path).is_absolute())
                events.clear()
                self.assertEqual(main(['sync', '--rescan']), 0)
                self.assertIn('catalogue', events)
                self.assertIn('related', events)
                self.assertNotIn('download:v1', events)
                responses['files'] = [content(), content('extra', filename='new.pdf')]
                events.clear()
                self.assertEqual(main(['sync', '--rescan']), 0)
                self.assertEqual([e for e in events if e.startswith('download:')], ['download:extra'])
                responses['files'] = [content(creationDate='2026-10-03'), content('extra', filename='new.pdf')]
                responses['data'] = b'new'
                events.clear()
                self.assertEqual(main(['sync', '--rescan']), 0)
                self.assertEqual([e for e in events if e.startswith('download:')], ['download:v1'])
                self.assertEqual((base / first_path).read_bytes(), b'new')
                with contextlib.closing(sqlite3.connect(base / 'ledger.sqlite3')) as db:
                    archived = db.execute('SELECT path FROM file_history ORDER BY id LIMIT 1').fetchone()[0]
                self.assertTrue(archived.startswith('history/'))
                self.assertEqual((base / archived).read_bytes(), b'old')
                self.assertLess(events.index('catalogue'), events.index('related'))
                self.assertLess(events.index('related'), events.index('download:v1'))
                self.assertEqual(events[-1], 'logout')
                with contextlib.closing(sqlite3.connect(base / 'ledger.sqlite3')) as db:
                    prior_path = db.execute("SELECT path FROM files WHERE id='v1'").fetchone()[0]
                responses['documents'] = [doc(uploadDate='2026-10-04')]
                responses['data'] = b'upd'
                events.clear()
                self.assertEqual(main(['sync', '--rescan']), 0)
                self.assertIn('download:v1', events)
                self.assertEqual((base / prior_path).read_bytes(), b'upd')
                with contextlib.closing(sqlite3.connect(base / 'ledger.sqlite3')) as db:
                    archived = db.execute('SELECT path FROM file_history ORDER BY id DESC LIMIT 1').fetchone()[0]
                self.assertEqual((base / archived).read_bytes(), b'new')
                events.clear()
                self.assertEqual(main(['sync', '--rescan']), 0)
                self.assertNotIn('download:v1', events)


if __name__ == '__main__':
    unittest.main()
