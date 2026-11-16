from pathlib import Path
import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from ledger import Ledger
import history_repair as repair
from file_layout import LayoutError, ensure_schema


class HistoryRepairTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.ledger = Ledger(self.base/'runtime/ledger.sqlite3', base_dir=self.base)
        self.addCleanup(self.ledger.close)
        self.current = self.base/'downloads/Project/Drawings/current.pdf'
        self.current.parent.mkdir(parents=True)
        self.current.write_bytes(b'current')
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO folders(project,id,name,path) VALUES('p','42','Drawings','Project/Drawings')")
            self.ledger.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,metadata,
              status,bytes,path,sha256,active) VALUES('p','f','d','42','current.pdf','{}','done',7,?,?,1)''',
              ('downloads/Project/Drawings/current.pdf',hashlib.sha256(b'current').hexdigest()))

    def add(self, batch='version-1', filename='drawing__rev_A__id.pdf', data=b'old', folder='Project/Drawings'):
        source = 'history/'+batch+'/'+folder+'/'+filename
        path = self.base/source
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(data)
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
              VALUES('p','f',?,?,?,'2026-10-29')''',(source,len(data),hashlib.sha256(data).hexdigest()))
        return source

    def test_versions_share_one_directory_and_current_files_and_rows_are_untouched(self):
        self.add()
        self.add('version-2',data=b'other version')
        before = [dict(r) for r in self.ledger.all('SELECT * FROM files')]
        stamp = self.current.stat()
        proposal = repair.plan(self.ledger)
        self.assertEqual(len(proposal['moves']),2)
        self.assertEqual({str(Path(m['target']).parent) for m in proposal['moves']},{str(Path('history/Project/Drawings'))})
        self.assertNotEqual(proposal['moves'][0]['target'],proposal['moves'][1]['target'])
        self.assertEqual(repair.apply(self.ledger,proposal),2)
        self.assertEqual({(self.base/r['path']).read_bytes() for r in self.ledger.all('SELECT path FROM file_history')},
                         {b'old',b'other version'})
        self.assertEqual([dict(r) for r in self.ledger.all('SELECT * FROM files')],before)
        self.assertEqual(self.current.read_bytes(),b'current')
        self.assertEqual(self.current.stat().st_mtime_ns,stamp.st_mtime_ns)
        self.assertEqual(repair.apply(self.ledger),0)
        self.assertFalse((self.base/'history/version-1').exists())
        self.assertTrue(list((self.base/'runtime/backups').glob('before-history-repair-*.sqlite3')))

    def test_folder_id_restores_original_hierarchy(self):
        self.add(folder='_long_paths/42')
        self.assertEqual(repair.plan(self.ledger)['moves'][0]['target'],
                         'history/Project/Drawings/drawing__rev_A__id.pdf')

    def test_overflow_versions_use_the_same_parent_even_with_different_filename_lengths(self):
        folder = 'Project/'+'/'.join('Ancestor'+str(i)+'x'*28 for i in range(5))+'/Drawings'
        with self.ledger.db:
            self.ledger.db.execute('UPDATE folders SET path=?',(folder,))
        self.add(folder='_long_paths/42',filename='short.pdf')
        self.add('version-2',folder='_long_paths/42',filename='long-'+'x'*55+'.pdf')
        proposal = repair.plan(self.ledger)
        self.assertEqual(len({str(Path(m['target']).parent) for m in proposal['moves']}),1)
        self.assertTrue(all(m['target'].startswith('history/long_path/') for m in proposal['moves']))
        repair.apply(self.ledger,proposal)
        self.assertTrue(all((self.base/r['path']).exists() for r in self.ledger.all('SELECT path FROM file_history')))

    def test_missing_short_mapping_preserves_files(self):
        source = self.add(folder='long_path/unknown')
        with self.assertRaises(LayoutError):repair.plan(self.ledger)
        self.assertTrue((self.base/source).exists())

    def test_existing_destination_is_kept_and_version_gets_unique_filename(self):
        self.add()
        existing = self.base/'history/Project/Drawings/drawing__rev_A__id.pdf'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'external')
        proposal = repair.plan(self.ledger)
        self.assertIn('__version-1.pdf',proposal['moves'][0]['target'])
        repair.apply(self.ledger,proposal)
        self.assertEqual(existing.read_bytes(),b'external')

    def test_hash_mismatch_preserves_source_and_can_resume_after_correction(self):
        source = self.add()
        proposal = repair.plan(self.ledger)
        (self.base/source).write_bytes(b'bad')
        with self.assertRaises(LayoutError):repair.apply(self.ledger,proposal)
        self.assertTrue((self.base/source).exists())
        (self.base/source).write_bytes(b'old')
        self.assertEqual(repair.apply(self.ledger),1)

    def test_interruption_after_publication_resumes_database_update(self):
        source = self.add()
        actual = repair.publish
        def interrupt(*args):
            actual(*args)
            raise OSError('interrupted after move')
        with patch('history_repair.publish',side_effect=interrupt):
            with self.assertRaises(OSError):repair.apply(self.ledger)
        self.assertFalse((self.base/source).exists())
        self.assertEqual(repair.apply(self.ledger),1)
        row = self.ledger.one('SELECT path FROM file_history')
        self.assertEqual((self.base/row['path']).read_bytes(),b'old')

    def test_untracked_historical_file_is_preserved(self):
        source = self.base/'history/untracked-abc/Project/other.txt'
        source.parent.mkdir(parents=True)
        source.write_bytes(b'untracked')
        repair.apply(self.ledger)
        self.assertEqual((self.base/'history/Project/other.txt').read_bytes(),b'untracked')

    def test_unfinished_original_migration_blocks_history_repair(self):
        source = self.add()
        ensure_schema(self.ledger.db)
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO layout_moves VALUES('pending','{}','staged')")
        with self.assertRaises(LayoutError):repair.apply(self.ledger)
        self.assertTrue((self.base/source).exists())

    def test_preview_does_not_change_database_or_files(self):
        source = self.add()
        before = self.ledger.db.iterdump()
        text = '\n'.join(before)
        repair.plan(self.ledger)
        self.assertEqual('\n'.join(self.ledger.db.iterdump()),text)
        self.assertTrue((self.base/source).exists())

    def test_target_appearing_after_plan_never_overwrites_content(self):
        source = self.add()
        proposal = repair.plan(self.ledger)
        target = self.base/proposal['moves'][0]['target']
        target.parent.mkdir(parents=True)
        target.write_bytes(b'outside change')
        with self.assertRaises(LayoutError):repair.apply(self.ledger,proposal)
        self.assertEqual(target.read_bytes(),b'outside change')
        self.assertTrue((self.base/source).exists())
