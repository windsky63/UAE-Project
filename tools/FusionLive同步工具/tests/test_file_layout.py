"""Real temporary files and SQLite journals; no network or production migration."""
from contextlib import closing
from pathlib import Path
import hashlib
import json
import sqlite3
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from ledger import Ledger
import file_layout as layout


class LayoutTests(unittest.TestCase):
    def test_automatic_archive_matches_manual_history_destination_without_tree_scan(self):
        old, staged = self.prepare_incremental_fixture()
        import path_maintenance as maintenance
        expected = [m for m in maintenance.plan(self.ledger)['moves'] if m['target'].startswith('history/')]
        keys = self.ledger._publication_groups[('p','one')]
        with patch('history_repair.os.walk',side_effect=AssertionError('automatic archive scanned history')):
            proposal = layout.plan(self.ledger,untracked=False,keys=keys)
        archives = [m for m in proposal['moves'] if m['target'].startswith('history/')]
        self.assertEqual([m['target'] for m in archives],[m['target'] for m in expected])
        self.assertEqual(archives[0]['target'],'history/Project/Pipe/Drawings/ISO-001.pdf')
        layout.prepare_downloads(self.ledger,keys=keys)
        row=self.ledger.one('SELECT path FROM file_history')
        self.assertEqual((self.base/row['path']).read_bytes(),b'old')
        self.assertEqual(old.read_bytes(),b'new')
        self.assertEqual(self.ledger.one('SELECT platform_path FROM history_paths')[0],
                         'Project/Pipe/Drawings/ISO-001.pdf')

    def test_automatic_archive_preserves_same_name_and_unrelated_legacy_history(self):
        old, staged = self.prepare_incremental_fixture()
        existing=self.base/'history/Project/Pipe/Drawings/ISO-001.pdf'
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b'previous history')
        legacy=self.base/'history/version-unrelated/Elsewhere/keep.pdf'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'keep')
        stamp=legacy.stat().st_mtime_ns
        keys=self.ledger._publication_groups[('p','one')]
        layout.prepare_downloads(self.ledger,keys=keys)
        archived=self.ledger.one('SELECT path FROM file_history')[0]
        self.assertTrue(archived.startswith('history/Project/Pipe/Drawings/ISO-001__version-'))
        self.assertEqual((self.base/archived).read_bytes(),b'old')
        self.assertEqual(existing.read_bytes(),b'previous history')
        self.assertEqual(legacy.read_bytes(),b'keep')
        self.assertEqual(legacy.stat().st_mtime_ns,stamp)
        self.assertEqual(old.read_bytes(),b'new')

    def test_publication_reuses_protected_hash_reads_within_one_apply(self):
        old, staged = self.prepare_incremental_fixture()
        expected = hashlib.sha256(b'new').hexdigest()
        reads = []
        checksum = layout.checksum
        def counted(path, *args, **kwargs):
            value = checksum(path, *args, **kwargs)
            reads.append(value)
            return value
        with patch('file_layout.checksum', side_effect=counted):
            layout.prepare_downloads(self.ledger, keys=self.ledger._publication_groups[('p', 'one')])
        self.assertLessEqual(reads.count(expected), 1 if os.name == 'nt' else 2)
        self.assertEqual((self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")[0]).read_bytes(), b'new')
        self.assertEqual((self.base/self.ledger.one('SELECT path FROM file_history')[0]).read_bytes(), b'old')

    @unittest.skipUnless(os.name == 'nt', 'Windows deny-write sharing guard')
    def test_read_guard_blocks_another_process_and_releases_after_publication(self):
        path = self.base/'protected.bin'
        path.write_bytes(b'old')
        script = "import sys\ntry:\n open(sys.argv[1],'r+b').close()\n print('writable')\nexcept OSError:\n print('blocked')"
        with layout.PublicationChecks(lambda: False, None) as checks:
            self.assertEqual(checks.checksum(path), hashlib.sha256(b'old').hexdigest())
            child = subprocess.run([sys.executable, '-B', '-c', script, str(path)],
                                   capture_output=True, text=True, timeout=15)
            self.assertEqual(child.returncode, 0, child.stderr)
            self.assertEqual(child.stdout.strip(), 'blocked')
        child = subprocess.run([sys.executable, '-B', '-c', script, str(path)],
                               capture_output=True, text=True, timeout=15)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'writable')

    def test_replaced_file_identity_never_inherits_renamed_hash_proof(self):
        source, target = self.base/'source.bin', self.base/'target.bin'
        source.write_bytes(b'old')
        rename = os.rename
        def replace_identity(first, second):
            rename(first, second)
            Path(second).unlink()
            Path(second).write_bytes(b'new')
        with layout.PublicationChecks(lambda: False, None) as checks:
            self.assertEqual(checks.checksum(source), hashlib.sha256(b'old').hexdigest())
            with patch('file_layout.os.rename', side_effect=replace_identity):
                checks.rename(source, target)
            self.assertEqual(checks.checksum(target), hashlib.sha256(b'new').hexdigest())
        target.write_bytes(b'bad')
        with layout.PublicationChecks(lambda: False, None) as checks:
            self.assertEqual(checks.checksum(target), hashlib.sha256(b'bad').hexdigest())

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.db = self.base / 'runtime/ledger.sqlite3'
        self.ledger = Ledger(self.db, base_dir=self.base)
        self.addCleanup(self.ledger.close)

    def add(self, identifier, folder='Project/Pipe/Drawings', filename='ISO-001.pdf',
            source=None, data=b'old', active=1, status='done'):
        source = source or f'downloads/legacy/{identifier}__rev_0.pdf'
        path = self.base / source
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with self.ledger.db:
            self.ledger.db.execute('INSERT INTO folders(project,id,name,path) VALUES(?,?,?,?)',
                                   ('p', identifier, folder.split('/')[-1], folder))
            self.ledger.db.execute('INSERT INTO documents(project,id,folder_id,filename,metadata,related_state) VALUES(?,?,?,?,?,?)',
                                   ('p', identifier, identifier, filename, '{}', 'done'))
            self.ledger.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,
              size,bytes,path,sha256,active,downloaded_at,download_date_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                ('p', identifier, identifier, identifier, filename, '{}', status, len(data), len(data), source,
                 hashlib.sha256(data).hexdigest(), active, '2026-10-20T08:00:00+08:00', '下载完成实录'))
            self.ledger.db.execute('INSERT INTO document_files VALUES(?,?,?,?,?)',
                                   ('p', identifier, identifier, identifier, active))
        return path

    def test_unchanged_batches_skip_global_planning_and_database_writes(self):
        self.add('one')
        layout.prepare_downloads(self.ledger, reuse=True)
        expected = layout.local_update_keys(self.ledger, reuse=True)
        # Progress is not a catalogue change.
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET attempts=attempts+1,status='failed',error='temporary'")
            self.ledger.db.execute("UPDATE documents SET checked_cycle=checked_cycle+1")
        before = self.ledger.db.total_changes
        with patch('file_layout.plan', side_effect=AssertionError('replanned unchanged catalogue')), \
             patch('file_layout.targets', side_effect=AssertionError('rebuilt unchanged mapping')), \
             patch('file_layout.replacement_links', side_effect=AssertionError('rebuilt unchanged dependencies')):
            for _ in range(3):
                self.assertTrue(layout.prepare_downloads(self.ledger, reuse=True))
                self.assertEqual(layout.local_update_keys(self.ledger, reuse=True), expected)
        self.assertEqual(self.ledger.db.total_changes, before)

    def test_catalogue_rename_invalidates_cache_and_moves_existing_bytes(self):
        self.add('one')
        layout.prepare_downloads(self.ledger, reuse=True)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET filename='renamed.pdf' WHERE id='one'")
        layout.prepare_downloads(self.ledger, reuse=True)
        row = self.ledger.one('SELECT path FROM files')
        self.assertTrue(row['path'].endswith('/renamed.pdf'))
        self.assertEqual((self.base/row['path']).read_bytes(), b'old')

    def test_external_connection_write_invalidates_cache(self):
        self.add('one')
        layout.prepare_downloads(self.ledger, reuse=True)
        with closing(sqlite3.connect(self.db)) as other:
            other.execute("UPDATE folders SET path='Project/NewFolder'")
            other.commit()
        layout.prepare_downloads(self.ledger, reuse=True)
        self.assertEqual((self.base/self.ledger.one('SELECT path FROM files')['path']).read_bytes(), b'old')
        self.assertIn('/NewFolder/', self.ledger.one('SELECT path FROM files')['path'])

    def test_migration_marker_overrides_warm_cache(self):
        self.add('one')
        layout.prepare_downloads(self.ledger, reuse=True)
        (self.base/'runtime/layout.pending').write_text('interrupted recovery', encoding='utf-8')
        with patch('file_layout.apply', wraps=layout.apply) as apply:
            layout.prepare_downloads(self.ledger, reuse=True)
        self.assertTrue(apply.called)
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_new_collision_rebuilds_mapping_without_overwriting_downloaded_file(self):
        self.add('one', filename='same.pdf')
        layout.prepare_downloads(self.ledger, reuse=True)
        self.add('two', filename='same.pdf', data=b'second')
        layout.prepare_downloads(self.ledger, reuse=True)
        rows = self.ledger.all('SELECT id,path FROM files')
        self.assertEqual(len({row['path'].casefold() for row in rows}), 2)
        self.assertEqual({row['id']: (self.base/row['path']).read_bytes() for row in rows},
                         {'one': b'old', 'two': b'second'})

    def test_warm_cache_still_publishes_staged_replacement_by_dependency_group(self):
        old, staged = self.prepare_incremental_fixture()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path=NULL,sha256=NULL,bytes=0,status='pending' WHERE id='one'")
        layout.prepare_downloads(self.ledger, reuse=True)
        layout.local_update_keys(self.ledger, reuse=True)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path='runtime/download_ready/new.pdf',sha256=?,bytes=3,status='done' WHERE id='one'",
                                   (hashlib.sha256(b'new').hexdigest(),))
        with patch('file_layout.targets', side_effect=AssertionError('rebuilt full mapping')):
            layout.prepare_downloads(self.ledger, reuse=True)
        self.assertFalse(staged.exists())
        self.assertEqual((self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")['path']).read_bytes(), b'new')
        history = self.ledger.one('SELECT path FROM file_history')['path']
        self.assertEqual((self.base/history).read_bytes(), b'old')

    def test_plain_platform_names_and_read_only_preview(self):
        old = self.add('one')
        before = dict(self.ledger.one('SELECT * FROM files'))
        proposal = layout.plan(self.ledger)
        self.assertTrue(old.exists())
        self.assertEqual(dict(self.ledger.one('SELECT * FROM files')), before)
        self.assertEqual(proposal['moves'][0]['target'], 'downloads/Project/Pipe/Drawings/ISO-001.pdf')
        layout.apply(self.ledger, proposal, log=lambda _: None)
        row = self.ledger.one('SELECT * FROM files')
        self.assertEqual((self.base/row['path']).read_bytes(), b'old')
        self.assertEqual(row['downloaded_at'], before['downloaded_at'])
        self.assertEqual(row['sha256'], before['sha256'])
        location = self.ledger.one('SELECT * FROM local_paths')
        self.assertEqual(location['original_filename'], 'ISO-001.pdf')
        self.assertEqual(location['local_filename'], 'ISO-001.pdf')
        self.assertEqual(location['local_directory'], 'downloads/Project/Pipe/Drawings')
        self.assertFalse(old.exists())

    def repeat_completed_publication(self):
        staged = self.add('one', source='runtime/download_ready/one/content', data=b'new')
        proposal = layout.plan(self.ledger, untracked=False)
        self.assertEqual(layout.apply(self.ledger, proposal, log=lambda _: None), 1)
        target = self.base / self.ledger.one('SELECT path FROM files')['path']
        # The portable staging address can be reused by a later download or
        # restored ledger. A prior success does not prove its current publish.
        target.replace(staged)
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET path=?', (staged.relative_to(self.base).as_posix(),))
        repeated = layout.plan(self.ledger, untracked=False)
        self.assertEqual(repeated['moves'], proposal['moves'])
        return staged, target, repeated

    def test_completed_journal_does_not_skip_repeated_staged_publication(self):
        staged, target, proposal = self.repeat_completed_publication()
        self.assertEqual(layout.apply(self.ledger, proposal, log=lambda _: None), 1)
        self.assertFalse(staged.exists())
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.ledger.one('SELECT path FROM files')['path'], target.relative_to(self.base).as_posix())
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state='done'")[0], 2)

    def test_repeated_publication_rechecks_bytes_despite_completed_journal(self):
        staged, target, proposal = self.repeat_completed_publication()
        staged.write_bytes(b'bad')
        with self.assertRaisesRegex(layout.LayoutError, 'SHA256'):
            layout.apply(self.ledger, proposal, log=lambda _: None)
        self.assertEqual(staged.read_bytes(), b'bad')
        self.assertFalse(target.exists())

    def test_repeated_publication_recovers_its_new_attempt_after_interruption(self):
        staged, target, proposal = self.repeat_completed_publication()
        with patch('file_layout.publish', side_effect=OSError('interrupted repeated publication')):
            with self.assertRaisesRegex(OSError, 'interrupted'):
                layout.apply(self.ledger, proposal, log=lambda _: None)
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state='done'")[0], 1)
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state='staged'")[0], 1)
        layout.apply(self.ledger, log=lambda _: None)
        self.assertFalse(staged.exists())
        self.assertEqual(target.read_bytes(), b'new')
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state='done'")[0], 2)
        self.assertEqual(self.ledger.one('SELECT path FROM files')['path'], target.relative_to(self.base).as_posix())
        self.assertTrue(list((self.base/'runtime/backups').glob('*.sqlite3')))
        self.assertEqual(layout.plan(self.ledger)['moves'], [])

    def test_long_path_retains_ancestor_names_without_numbered_groups(self):
        prefix = 'Project/' + '/'.join('LongParent'+str(i)+'x'*30 for i in range(6))
        self.add('one', folder=prefix+'/AreaOne/Drawings')
        self.add('two', folder=prefix+'/AreaTwo/Drawings')
        proposal = layout.plan(self.ledger)
        self.assertEqual(len(proposal['mapping']), 2)
        for row in proposal['mapping']:
            self.assertTrue(row['path'].startswith('downloads/long_path/'))
            self.assertLessEqual(layout.length(self.base/row['path']), layout.LIMIT)
            self.assertTrue(row['omitted_parent'])
            self.assertTrue(row['platform_path'].endswith('/Drawings/ISO-001.pdf'))
            self.assertNotIn('__', row['path'])
        self.assertIn('/AreaOne/Drawings/', proposal['mapping'][0]['path'])
        self.assertIn('/AreaTwo/Drawings/', proposal['mapping'][1]['path'])
        layout.apply(self.ledger, proposal, log=lambda _: None)
        self.assertFalse((self.base/'downloads/Project').exists())
        self.assertTrue((self.base/'runtime/path_mapping.json').exists())

    def test_duplicate_names_get_simple_numbers_without_overwriting(self):
        old = self.add('one', filename='drawing.pdf')
        self.add('two', filename='DRAWING.pdf')
        proposal = layout.plan(self.ledger)
        names = [Path(entry['path']).name for entry in proposal['mapping']]
        self.assertEqual(names, ['drawing-1.pdf', 'DRAWING-2.pdf'])
        layout.apply(self.ledger, proposal, log=lambda _: None)
        for row in self.ledger.all('SELECT path FROM files'):
            self.assertEqual((self.base/row['path']).read_bytes(), b'old')
        self.assertFalse(old.exists())

    def test_history_stays_in_downloads_until_current_version_downloaded(self):
        old = self.add('one')
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
              SELECT project,id,path,bytes,sha256,'2026-10-29' FROM files''')
            self.ledger.db.execute("UPDATE files SET path=NULL,sha256=NULL,bytes=0,status='pending'")
        layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        history = self.ledger.one('SELECT path FROM file_history')['path']
        self.assertTrue(old.exists())
        self.assertEqual(history, 'downloads/legacy/one__rev_0.pdf')
        staged = self.base/'runtime/download_ready/new.pdf'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'new')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path='runtime/download_ready/new.pdf',sha256=?,bytes=3,status='done'",
                                   (hashlib.sha256(b'new').hexdigest(),))
        layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        history = self.ledger.one('SELECT path FROM file_history')['path']
        self.assertTrue(history.startswith('history/'))
        self.assertEqual((self.base/history).read_bytes(), b'old')
        self.assertFalse(old.exists())
        row = self.ledger.one('SELECT * FROM files')
        self.assertEqual(row['target_path'], 'downloads/Project/Pipe/Drawings/ISO-001.pdf')
        self.assertEqual((self.base/row['path']).read_bytes(), b'new')

    def prepare_incremental_fixture(self):
        self.add('one')
        self.add('unrelated', filename='other.pdf', data=b'keep')
        layout.prepare_downloads(self.ledger)
        old = self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")['path']
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
                SELECT project,id,path,bytes,sha256,'fixture' FROM files WHERE id='one' ''')
            self.ledger.db.execute("UPDATE files SET path=NULL,sha256=NULL,bytes=0,status='pending' WHERE id='one'")
        layout.local_update_keys(self.ledger)
        staged = self.base/'runtime/download_ready/new.pdf'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'new')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path='runtime/download_ready/new.pdf',sha256=?,bytes=3,status='done' WHERE id='one'",
                                   (hashlib.sha256(b'new').hexdigest(),))
        return old, staged

    def history_alias(self, *, canonical=True, copies=1):
        self.add('one')
        if canonical:
            layout.prepare_downloads(self.ledger)
        with self.ledger.db:
            for _ in range(copies):
                self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
                    SELECT project,id,path,bytes,sha256,'fixture' FROM files WHERE id='one' ''')
        return self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")['path']

    def test_identical_history_alias_is_copied_without_moving_current_file(self):
        current = self.history_alias()
        queue_path = self.base/'runtime/file_analysis/queue.sqlite3'
        queue_path.parent.mkdir(exist_ok=True)
        with closing(sqlite3.connect(queue_path)) as queue:
            queue.execute('CREATE TABLE jobs(path TEXT,origin TEXT)')
            queue.execute('INSERT INTO jobs VALUES(?,?)', (str(current), str(current)))
            queue.commit()
        proposal = layout.plan(self.ledger, untracked=False)
        self.assertEqual(len(proposal['moves']), 1)
        self.assertTrue(proposal['moves'][0]['copy'])
        self.assertEqual(current.read_bytes(), b'old')  # Preview is read-only.
        layout.prepare_downloads(self.ledger)
        history = self.base/self.ledger.one('SELECT path FROM file_history')['path']
        self.assertEqual(current.read_bytes(), b'old')
        self.assertEqual(history.read_bytes(), b'old')
        self.assertNotEqual(current, history)
        with closing(sqlite3.connect(queue_path)) as queue:
            self.assertEqual(queue.execute('SELECT path,origin FROM jobs').fetchone(),
                             (str(current), str(current)))
        history.write_bytes(b'changed history')
        self.assertEqual(current.read_bytes(), b'old')  # Separate bytes, never a hard link.

    def test_alias_can_be_archived_while_current_path_is_renamed(self):
        previous = self.history_alias(canonical=False)
        layout.prepare_downloads(self.ledger)
        current = self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")['path']
        history = self.base/self.ledger.one('SELECT path FROM file_history')['path']
        self.assertEqual(current.read_bytes(), b'old')
        self.assertEqual(history.read_bytes(), b'old')
        self.assertFalse(previous.exists())

    def test_multiple_identical_history_aliases_get_independent_archives(self):
        current = self.history_alias(copies=2)
        layout.prepare_downloads(self.ledger)
        histories = [self.base/r['path'] for r in self.ledger.all('SELECT path FROM file_history')]
        self.assertEqual(len(set(histories)), 2)
        self.assertTrue(all(p.read_bytes() == b'old' for p in histories))
        self.assertEqual(current.read_bytes(), b'old')

    def test_history_alias_with_conflicting_identity_is_not_repaired(self):
        current = self.history_alias()
        with self.ledger.db:
            self.ledger.db.execute('UPDATE file_history SET sha256=?', (hashlib.sha256(b'new').hexdigest(),))
        with self.assertRaisesRegex(layout.LayoutError, '内容身份不一致'):
            layout.prepare_downloads(self.ledger)
        self.assertEqual(current.read_bytes(), b'old')
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],
                         self.ledger.one('SELECT path FROM files')[0])

    def test_shared_old_versions_archive_before_new_publication(self):
        old, staged = self.prepare_incremental_fixture()
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived)
                SELECT project,file_id,'another-version',path,bytes,sha256,'fixture-2' FROM file_history''')
        proposal = layout.plan(self.ledger,untracked=False)
        archives = [m for m in proposal['moves'] if m['source']==old.relative_to(self.base).as_posix()]
        self.assertEqual(len(archives),2)
        self.assertTrue(archives[0]['copy'])
        self.assertFalse(archives[1].get('copy',False))
        layout.prepare_downloads(self.ledger)
        self.assertEqual(old.read_bytes(),b'new')
        self.assertFalse(staged.exists())
        histories = self.ledger.all('SELECT * FROM file_history')
        self.assertEqual(len({r['path'] for r in histories}),2)
        self.assertTrue(all((self.base/r['path']).read_bytes()==b'old' for r in histories))
        self.assertEqual(layout.plan(self.ledger,untracked=False)['moves'],[])

    def test_shared_archive_recovers_after_copy_was_staged(self):
        old, staged = self.prepare_incremental_fixture()
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
                SELECT project,file_id,path,bytes,sha256,'fixture-2' FROM file_history''')
        original = layout.PublicationChecks.rename
        def interrupt(checks,source,target):
            original(checks,source,target)
            if source.name=='history-copy.part':
                raise KeyboardInterrupt()
        with patch.object(layout.PublicationChecks,'rename',interrupt):
            with self.assertRaises(KeyboardInterrupt):
                layout.prepare_downloads(self.ledger)
        self.assertEqual(old.read_bytes(),b'old')
        self.ledger.close()
        self.ledger = Ledger(self.db,base_dir=self.base)
        self.addCleanup(self.ledger.close)
        layout.prepare_downloads(self.ledger)
        self.assertEqual(old.read_bytes(),b'new')
        self.assertTrue(all((self.base/r['path']).read_bytes()==b'old'
                            for r in self.ledger.all('SELECT path FROM file_history')))
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_conflicting_shared_versions_pass_path_to_automatic_repair(self):
        old, staged = self.prepare_incremental_fixture()
        with self.ledger.db:
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
                SELECT project,file_id,path,bytes,?,'fixture-2' FROM file_history''',
                (hashlib.sha256(b'bad').hexdigest(),))
        with self.assertRaises(layout.LayoutPlanningError) as caught:
            layout.prepare_downloads(self.ledger)
        self.assertEqual(caught.exception.paths,(old.relative_to(self.base).as_posix(),))
        self.assertEqual(old.read_bytes(),b'old')
        self.assertEqual(staged.read_bytes(),b'new')
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_current_aliases_can_publish_to_distinct_destinations(self):
        source = self.add('one')
        self.add('two',filename='second.pdf',source=source.relative_to(self.base).as_posix())
        layout.prepare_downloads(self.ledger)
        paths = [self.base/r['path'] for r in self.ledger.all('SELECT path FROM files')]
        self.assertEqual(len(set(paths)),2)
        self.assertTrue(all(p.read_bytes()==b'old' for p in paths))
        paths[0].write_bytes(b'changed')
        self.assertEqual(paths[1].read_bytes(),b'old')

    def test_stationary_alias_keeps_bytes_when_other_owner_moves(self):
        self.add('one')
        layout.prepare_downloads(self.ledger)
        source = self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")[0]
        self.add('two',filename='second.pdf',source=source.relative_to(self.base).as_posix())
        layout.prepare_downloads(self.ledger)
        self.assertEqual(source.read_bytes(),b'old')
        other = self.base/self.ledger.one("SELECT path FROM files WHERE id='two'")[0]
        self.assertEqual(other.read_bytes(),b'old')
        other.write_bytes(b'changed')
        self.assertEqual(source.read_bytes(),b'old')

    def test_missing_history_path_clears_before_staged_new_version_reuses_it(self):
        old, staged = self.prepare_incremental_fixture()
        old.unlink()
        before = dict(self.ledger.one('SELECT * FROM file_history'))
        keys = self.ledger._publication_groups[('p','one')]
        layout.prepare_downloads(self.ledger, keys=keys)
        current = self.ledger.one("SELECT * FROM files WHERE id='one'")
        history = self.ledger.one('SELECT * FROM file_history')
        self.assertEqual((self.base/current['path']).read_bytes(), b'new')
        self.assertIsNone(history['path'])
        self.assertEqual(history['sha256'], before['sha256'])
        self.assertEqual(history['fingerprint'], before['fingerprint'])
        self.assertEqual(layout.plan(self.ledger)['moves'], [])

    def test_actual_content_must_match_before_alias_copy_is_published(self):
        current = self.history_alias()
        current.write_bytes(b'bad')
        with self.assertRaisesRegex(layout.LayoutError, 'SHA256'):
            layout.prepare_downloads(self.ledger)
        self.assertEqual(current.read_bytes(), b'bad')
        self.assertEqual(self.ledger.one('SELECT path FROM file_history')[0],
                         self.ledger.one('SELECT path FROM files')[0])

    def test_history_copy_recovers_after_publication_before_reference_commit(self):
        current = self.history_alias()
        original = layout.publish

        def interrupt(*args):
            original(*args)
            raise KeyboardInterrupt()

        with patch('file_layout.publish', side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                layout.prepare_downloads(self.ledger)
        self.assertEqual(current.read_bytes(), b'old')
        self.ledger.close()
        self.ledger = Ledger(self.db, base_dir=self.base)
        self.addCleanup(self.ledger.close)
        layout.prepare_downloads(self.ledger)
        self.assertEqual(current.read_bytes(), b'old')
        self.assertEqual((self.base/self.ledger.one('SELECT path FROM file_history')[0]).read_bytes(), b'old')
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_incremental_publication_skips_global_maintenance_and_preserves_versions(self):
        old, staged = self.prepare_incremental_fixture()
        before = dict(self.ledger.one("SELECT * FROM files WHERE id='unrelated'"))
        backups = list((self.base/'runtime/backups').glob('*.sqlite3'))
        mapping = (self.base/'runtime/path_mapping.json').read_bytes()
        keys = self.ledger._publication_groups[('p', 'one')]
        self.assertEqual(keys, {('p', 'one')})
        expected = layout.plan(self.ledger, untracked=False)['moves']
        self.assertEqual(layout.plan(self.ledger, untracked=False, keys=keys)['moves'], expected)
        original_all = self.ledger.all

        def no_global_scan(sql, args=()):
            if sql in ('SELECT * FROM files', 'SELECT * FROM file_history', 'SELECT * FROM documents'):
                raise AssertionError('incremental publication scanned entire catalogue')
            return original_all(sql, args)

        with patch.object(self.ledger, 'all', side_effect=no_global_scan), \
             patch('file_layout.targets', side_effect=AssertionError('rebuilt full mapping')), \
             patch('file_layout.os.walk', side_effect=AssertionError('walked full download tree')):
            layout.prepare_downloads(self.ledger, keys=keys)
        current = self.ledger.one("SELECT * FROM files WHERE id='one'")
        self.assertEqual((self.base/current['path']).read_bytes(), b'new')
        history = self.ledger.one('SELECT * FROM file_history')
        self.assertEqual((self.base/history['path']).read_bytes(), b'old')
        self.assertEqual(before, dict(self.ledger.one("SELECT * FROM files WHERE id='unrelated'")))
        self.assertEqual(backups, list((self.base/'runtime/backups').glob('*.sqlite3')))
        self.assertEqual(mapping, (self.base/'runtime/path_mapping.json').read_bytes())
        self.assertFalse(staged.exists())
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_incremental_publication_recovers_after_move_and_before_commit(self):
        old, staged = self.prepare_incremental_fixture()
        keys = self.ledger._publication_groups[('p', 'one')]
        original = layout.publish

        def interrupted(*args):
            original(*args)
            raise OSError('fixture crash after publication')

        with patch('file_layout.publish', side_effect=interrupted):
            with self.assertRaises(OSError):
                layout.prepare_downloads(self.ledger, keys=keys)
        self.assertTrue((self.base/'runtime/layout.pending').exists())
        layout.prepare_downloads(self.ledger, keys=keys)
        self.assertEqual((self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")['path']).read_bytes(), b'new')
        self.assertEqual((self.base/self.ledger.one('SELECT path FROM file_history')['path']).read_bytes(), b'old')
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_nonblocking_publication_defers_sharing_violation_and_recovers(self):
        old, staged = self.prepare_incremental_fixture()
        keys = self.ledger._publication_groups[('p', 'one')]
        busy = PermissionError('fixture sharing violation')
        busy.winerror = 32
        with patch('file_layout.os.rename', side_effect=busy):
            self.assertFalse(layout.prepare_downloads(self.ledger, keys=keys, wait=False))
        self.assertEqual(old.read_bytes(), b'old')
        self.assertEqual(staged.read_bytes(), b'new')
        self.assertTrue((self.base/'runtime/layout.pending').exists())
        self.assertGreater(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state!='done'")[0], 0)
        self.assertTrue(layout.prepare_downloads(self.ledger, keys=keys, wait=False))
        self.assertEqual((self.base/self.ledger.one("SELECT path FROM files WHERE id='one'")['path']).read_bytes(), b'new')
        self.assertEqual((self.base/self.ledger.one('SELECT path FROM file_history')['path']).read_bytes(), b'old')
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_nonblocking_publication_does_not_hide_non_sharing_errors(self):
        old, staged = self.prepare_incremental_fixture()
        keys = self.ledger._publication_groups[('p', 'one')]
        with patch('file_layout.os.rename', side_effect=PermissionError('fixture denied')):
            with self.assertRaises(PermissionError):
                layout.prepare_downloads(self.ledger, keys=keys, wait=False)
        self.assertEqual(old.read_bytes(), b'old')
        self.assertEqual(staged.read_bytes(), b'new')

    def test_incremental_checksum_failure_keeps_bytes_and_journal(self):
        old, staged = self.prepare_incremental_fixture()
        staged.write_bytes(b'bad')
        with self.assertRaises(layout.LayoutError):
            layout.prepare_downloads(self.ledger, keys=self.ledger._publication_groups[('p', 'one')])
        self.assertTrue((self.base/'runtime/layout.pending').exists())
        sources = [old, staged, *list((self.base/'runtime/path_migration').rglob('*'))]
        self.assertTrue(any(p.is_file() and p.read_bytes() == b'old' for p in sources))
        self.assertTrue(any(p.is_file() and p.read_bytes() == b'bad' for p in sources))

    def test_incremental_shared_predecessor_waits_for_every_successor(self):
        old = self.add('old', filename='same.pdf', active=0)
        for identifier in ('new-a', 'new-b'):
            self.add(identifier, filename='same.pdf', status='pending').unlink()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE documents SET folder_id='old',first_id='lineage'")
            self.ledger.db.execute("UPDATE files SET folder_id='old'")
            self.ledger.db.execute("UPDATE files SET path=NULL,bytes=0,sha256=NULL WHERE active=1")
            self.ledger.db.execute("UPDATE document_files SET folder_id='old'")
            self.ledger.db.execute("INSERT INTO refresh_cycles VALUES('p',1,'complete','fixture')")
        layout.prepare_downloads(self.ledger)
        updates = layout.local_update_keys(self.ledger)
        self.assertTrue({('p', 'new-a'), ('p', 'new-b')} <= updates)
        keys = self.ledger._publication_groups[('p', 'new-a')]
        self.assertEqual(keys, {('p', 'old'), ('p', 'new-a'), ('p', 'new-b')})
        for identifier in ('new-a', 'new-b'):
            staged = self.base/f'runtime/download_ready/{identifier}.pdf'
            staged.parent.mkdir(parents=True, exist_ok=True)
            data = identifier.encode()
            staged.write_bytes(data)
            with self.ledger.db:
                self.ledger.db.execute('''UPDATE files SET path=?,bytes=?,sha256=?,status='done'
                    WHERE id=?''', (str(staged.relative_to(self.base)), len(data), hashlib.sha256(data).hexdigest(), identifier))
            full = layout.plan(self.ledger, untracked=False)
            partial = layout.plan(self.ledger, untracked=False, keys=keys)
            self.assertEqual(sorted(partial['moves'], key=lambda m: m['source']),
                             sorted(full['moves'], key=lambda m: m['source']))
            layout.prepare_downloads(self.ledger, keys=keys)
            if identifier == 'new-a':
                self.assertEqual(old.read_bytes(), b'old')
        self.assertFalse(old.exists())
        self.assertEqual((self.base/self.ledger.one("SELECT path FROM files WHERE id='old'")['path']).read_bytes(), b'old')
        for identifier in ('new-a', 'new-b'):
            self.assertEqual((self.base/self.ledger.one('SELECT path FROM files WHERE id=?', (identifier,))['path']).read_bytes(), identifier.encode())

    def test_inactive_files_only_archived_after_complete_inventory(self):
        old = self.add('one', active=0)
        self.assertEqual(layout.plan(self.ledger)['moves'], [])
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO refresh_cycles VALUES('p',1,'complete','fixture')")
        layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        self.assertFalse(old.exists())
        row = self.ledger.one('SELECT * FROM files')
        self.assertTrue(row['path'].startswith('history/'))
        self.assertIsNone(row['target_path'])
        self.ledger.clean_partial_downloads()

    def test_untracked_files_are_preserved_in_history(self):
        path = self.base/'downloads/old-folder/untracked.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'keep')
        proposal = layout.plan(self.ledger)
        self.assertEqual(len(proposal['moves']), 1)
        layout.apply(self.ledger, proposal, log=lambda _: None)
        self.assertFalse(path.exists())
        self.assertEqual((self.base/proposal['moves'][0]['target']).read_bytes(), b'keep')

    def test_interrupted_staged_move_resumes_without_redownload(self):
        old = self.add('one')
        proposal = layout.plan(self.ledger)
        with patch('file_layout.publish', side_effect=OSError('fixture interrupt')):
            with self.assertRaises(OSError):
                layout.apply(self.ledger, proposal, log=lambda _: None)
        self.assertFalse(old.exists())
        self.assertTrue((self.base/'runtime/layout.pending').exists())
        self.assertEqual(self.ledger.one('SELECT path FROM files')['path'], proposal['moves'][0]['source'])
        layout.apply(self.ledger, log=lambda _: None)
        self.assertFalse((self.base/'runtime/layout.pending').exists())
        self.assertEqual((self.base/self.ledger.one('SELECT path FROM files')['path']).read_bytes(), b'old')

    def test_interruption_after_publish_before_db_commit_resumes(self):
        self.add('one')
        original = layout.publish
        def interrupted(*args):
            original(*args)
            raise OSError('after publication')
        with patch('file_layout.publish', side_effect=interrupted):
            with self.assertRaises(OSError):
                layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        layout.apply(self.ledger, log=lambda _: None)
        self.assertEqual(self.ledger.one('SELECT state FROM layout_moves')['state'], 'done')

    def test_hash_mismatch_preserves_source_and_recovery_journal(self):
        old = self.add('one')
        old.write_bytes(b'bad')
        with self.assertRaises(layout.LayoutError):
            layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        self.assertEqual(old.read_bytes(), b'bad')
        self.assertEqual(self.ledger.one('SELECT state FROM layout_moves')['state'], 'pending')

    def test_name_swaps_stage_all_sources_before_publication(self):
        first = self.add('one', folder='Project', filename='a.pdf', source='downloads/Project/b.pdf', data=b'aaa')
        second = self.add('two', folder='Project', filename='b.pdf', source='downloads/Project/a.pdf', data=b'bbb')
        layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        self.assertEqual(first.read_bytes(), b'bbb')
        self.assertEqual(second.read_bytes(), b'aaa')

    def test_non_ntfs_copy_preserves_timestamp_and_contents(self):
        self.add('one')
        proposal = layout.plan(self.ledger)
        timestamp = (self.base/proposal['moves'][0]['source']).stat().st_mtime_ns
        with patch('file_layout.os.link', side_effect=OSError('unsupported')):
            layout.apply(self.ledger, proposal, log=lambda _: None)
        destination = self.base/proposal['moves'][0]['target']
        self.assertEqual(destination.read_bytes(), b'old')
        self.assertEqual(destination.stat().st_mtime_ns, timestamp)

    def test_windows_reserved_names_use_compatibility_suffix(self):
        self.add('one', folder='Project/CON', filename='a:b.pdf')
        mapping = layout.targets(self.ledger)[('p', 'one')]
        self.assertEqual(mapping['path'], 'downloads/Project/CON-兼容/a-b-兼容.pdf')
        self.assertEqual(mapping['platform_path'], 'Project/CON/a:b.pdf')

    def test_missing_source_is_reported_and_not_marked_done(self):
        old = self.add('one', status='pending')
        old.unlink()
        proposal = layout.plan(self.ledger)
        self.assertEqual(len(proposal['missing']), 1)
        layout.apply(self.ledger, proposal, log=lambda _: None)
        self.assertEqual(self.ledger.one('SELECT status FROM files')['status'], 'pending')

    def test_collision_numbers_remain_stable_after_new_file_arrives(self):
        self.add('one')
        self.add('two')
        layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        before = {(r['project'], r['file_id']): r['local_path'] for r in self.ledger.all('SELECT * FROM local_paths')}
        self.add('three')
        after = layout.targets(self.ledger)
        self.assertEqual(after[('p', 'one')]['path'], before[('p', 'one')])
        self.assertEqual(after[('p', 'two')]['path'], before[('p', 'two')])
        self.assertTrue(after[('p', 'three')]['path'].endswith('ISO-001-3.pdf'))

    def test_natural_numbered_filename_is_reserved(self):
        self.add('one', filename='drawing.pdf')
        self.add('two', filename='drawing.pdf')
        self.add('three', filename='drawing-1.pdf')
        mapping = layout.targets(self.ledger)
        self.assertTrue(mapping[('p', 'three')]['path'].endswith('/drawing-1.pdf'))
        self.assertTrue(mapping[('p', 'one')]['path'].endswith('/drawing-2.pdf'))
        self.assertTrue(mapping[('p', 'two')]['path'].endswith('/drawing-3.pdf'))

    def test_analysis_queue_and_reporting_references_follow_migration(self):
        old = self.add('one')
        cache = self.base/'runtime/file_analysis'
        cache.mkdir()
        with closing(sqlite3.connect(cache/'queue.sqlite3')) as queue:
            queue.execute('CREATE TABLE jobs(path TEXT,origin TEXT)')
            queue.execute('INSERT INTO jobs VALUES(?,?)', (str(old), 'ISO-001.pdf / '+str(old)))
            queue.commit()
        layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        current = str(self.base/self.ledger.one('SELECT path FROM files')['path'])
        with closing(sqlite3.connect(cache/'queue.sqlite3')) as queue:
            path, origin = queue.execute('SELECT * FROM jobs').fetchone()
        self.assertEqual(path, current)
        self.assertEqual(origin, 'ISO-001.pdf / '+current)

    def test_partial_cleanup_never_deletes_real_platform_part_file(self):
        first = self.add('one', folder='Project', filename='a.pdf', source='downloads/Project/a.pdf')
        peer = self.add('two', folder='Project', filename='a.pdf.part', source='downloads/Project/a.pdf.part', data=b'peer')
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET target_path=path')
        self.ledger.clean_partial_downloads()
        self.ledger.clean_partial_for(first)
        self.assertEqual(peer.read_bytes(), b'peer')

    def test_download_preparation_resumes_partially_published_name_swap(self):
        first = self.add('one', folder='Project', filename='a.pdf', source='downloads/Project/b.pdf', data=b'aaa')
        second = self.add('two', folder='Project', filename='b.pdf', source='downloads/Project/a.pdf', data=b'bbb')
        original = layout.publish
        def interrupted(*args):
            original(*args)
            raise OSError('after first publication')
        with patch('file_layout.publish', side_effect=interrupted):
            with self.assertRaises(OSError):
                layout.apply(self.ledger, layout.plan(self.ledger), log=lambda _: None)
        layout.prepare_downloads(self.ledger)
        self.assertEqual(first.read_bytes(), b'bbb')
        self.assertEqual(second.read_bytes(), b'aaa')
        self.assertFalse((self.base/'runtime/layout.pending').exists())

    def test_console_progress_covers_scanning_backup_staging_and_publication(self):
        self.add('one')
        lines = []
        with layout.MigrationProgress(self.base, output=lines.append, interval=0) as progress:
            layout.apply(self.ledger, progress=progress, log=progress.message)
        text = '\n'.join(lines)
        for phase in ('构建平台目录映射', '扫描已登记文件', '备份数据库', '保存迁移恢复记录',
                      '校验源文件并暂存', '发布并更新台账', '更新原名和本地路径映射', '迁移完成'):
            self.assertIn(phase, text)
        self.assertIn('1/1', text)
        self.assertIn('3/3 字节', text)
        self.assertIn('本阶段', text)
        self.assertTrue((self.base/'runtime/path_migration.log').is_file())

    def test_heartbeat_is_visible_when_main_thread_has_no_new_progress(self):
        lines, heard = [], threading.Event()
        def output(line):
            lines.append(line)
            if '仍在处理' in line:
                heard.set()
        with layout.MigrationProgress(self.base, output=output, interval=0, heartbeat=.02) as progress:
            progress.phase('等待磁盘响应')
            self.assertTrue(heard.wait(2))
        self.assertTrue(any('等待磁盘响应' in line and '仍在处理' in line for line in lines))
        self.assertFalse(progress.thread.is_alive())

    def test_checksum_reports_actual_bytes_without_changing_content(self):
        data = b'x' * (2 * 1024 * 1024 + 100)
        source = self.base/'large.bin'
        source.write_bytes(data)
        lines = []
        with layout.MigrationProgress(self.base, output=lines.append, interval=0) as progress:
            progress.phase('校验源文件', 1)
            progress.update(current=source.name)
            self.assertEqual(layout.checksum(source, progress=progress), hashlib.sha256(data).hexdigest())
            self.assertEqual(progress.byte_count, len(data))
            self.assertEqual(progress.byte_total, len(data))
        self.assertTrue(any('2,097,252/2,097,252 字节' in line for line in lines))
