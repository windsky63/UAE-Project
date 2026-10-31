"""Download eligibility, ordering and bounded selection work on a large catalogue."""
from pathlib import Path
import tempfile
import unittest

from ledger import Ledger
from sync import next_download_item


class DownloadSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.ledger = Ledger(base/'ledger.sqlite3', base_dir=base)
        self.addCleanup(self.ledger.close)
        self.ledger.cycle = 9
        for table in ('download_update_priority', 'download_unit_priority'):
            self.ledger.db.execute(f'CREATE TEMP TABLE {table}(project,file_id,PRIMARY KEY(project,file_id))')

    def add(self, identifier, document=None, folder='folder'):
        document = document or identifier
        db = self.ledger.db
        db.execute('''INSERT OR IGNORE INTO documents
            (project,id,folder_id,metadata,related_state,checked_cycle)
            VALUES('p',?,?,'{}','done',9)''', (document, folder))
        db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,metadata)
            VALUES('p',?,?,?,?, '{}')''', (identifier, document, folder, identifier+'.pdf'))
        db.execute('INSERT INTO document_files(project,document_id,folder_id,file_id) VALUES(?,?,?,?)',
                   ('p', document, folder, identifier))

    def test_large_catalogue_does_not_rescan_documents_for_every_file(self):
        db = self.ledger.db
        with db:
            db.executemany('''INSERT INTO documents(project,id,folder_id,metadata,related_state,checked_cycle)
                VALUES('p',?,'folder','{}','done',9)''', [(f'd{i:04}',) for i in range(1800)])
            for i in range(500):
                self.add(f'f{i:04}', document=f'd{i+1300:04}')
        ticks = 0

        def budget():
            nonlocal ticks
            ticks += 1
            return int(ticks > 1000)

        db.set_progress_handler(budget, 1000)
        try:
            self.assertEqual(next_download_item(self.ledger, 'p')['id'], 'f0000')
        finally:
            db.set_progress_handler(None, 0)

    def test_priority_units_then_new_before_updates(self):
        for identifier in ('ordinary', 'updated', 'priority-new', 'priority-updated'):
            self.add(identifier)
        db = self.ledger.db
        db.executemany('INSERT INTO download_unit_priority VALUES(?,?)',
                       [('p', 'priority-new'), ('p', 'priority-updated')])
        db.executemany('INSERT INTO download_update_priority VALUES(?,?)',
                       [('p', 'updated'), ('p', 'priority-updated')])
        for expected in ('priority-new', 'priority-updated', 'ordinary', 'updated'):
            self.assertEqual(next_download_item(self.ledger, 'p')['id'], expected)
            db.execute("UPDATE files SET status='done' WHERE id=?", (expected,))
        self.assertIsNone(next_download_item(self.ledger, 'p'))

    def test_stale_inactive_and_exhausted_candidates_are_excluded(self):
        cases = [('stale', "UPDATE documents SET checked_cycle=8 WHERE id='stale'"),
                 ('inactive-document', "UPDATE documents SET active=0 WHERE id='inactive-document'"),
                 ('unchecked', "UPDATE documents SET related_state='pending' WHERE id='unchecked'"),
                 ('inactive-link', "UPDATE document_files SET active=0 WHERE file_id='inactive-link'"),
                 ('inactive-file', "UPDATE files SET active=0 WHERE id='inactive-file'"),
                 ('permanent', "UPDATE files SET retryable=0 WHERE id='permanent'"),
                 ('exhausted', "UPDATE files SET attempts=3 WHERE id='exhausted'")]
        for identifier, sql in cases:
            self.add(identifier)
            self.ledger.db.execute(sql)
        self.assertIsNone(next_download_item(self.ledger, 'p'))
        self.add('eligible')
        self.assertEqual(next_download_item(self.ledger, 'p')['id'], 'eligible')


if __name__ == '__main__':
    unittest.main()
