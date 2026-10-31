"""Extraction reuse across parser versions and source versions."""
from contextlib import closing
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from analysis_worker import AnalysisQueue
from archive_cache import extract_cached, cleanup_history
from local_analysis import analyze, digest, extract_zip
from tests.test_local_analysis import PCF


class ArchiveCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / 'downloads/source.zip'
        self.path.parent.mkdir()
        self.database = self.base / 'runtime/ledger.sqlite3'
        self.database.parent.mkdir()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE files(project,id,filename,path,status,bytes,sha256,active)')
        self.queue = AnalysisQueue(self.database, base_dir=self.base, log=lambda _: None)
        self.addCleanup(self.queue.close)

    def publish(self, content=PCF):
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('H4-262/model.pcf', content)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('DELETE FROM files')
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,1)',
                       ('p', 'source', self.path.name, str(self.path), 'done', self.path.stat().st_size, digest(self.path)))

    def test_history_lookup_stays_bounded_with_many_obsolete_jobs(self):
        # Unrelated historical jobs must not scan every completed current job.
        rows = [(f'old-{i}', 'p', f'old-file-{i}', '', '', 0, 'done', 0)
                for i in range(1500)]
        rows += [(f'new-{i}', 'p', f'new-file-{i}', '', '', 1, 'done', 0)
                 for i in range(500)]
        rows.append(('pending', 'p', 'pending', '', '', 1, 'pending', 0))
        with self.queue.db:
            self.queue.db.executemany('''INSERT INTO jobs
                (key,project,file_id,path,origin,active,state,updated)
                VALUES(?,?,?,?,?,?,?,?)''', rows)
        ticks = 0

        def budget():
            nonlocal ticks
            ticks += 1
            return int(ticks > 1000)

        self.queue.db.set_progress_handler(budget, 1000)
        try:
            self.assertEqual(cleanup_history(self.queue), 0)
        finally:
            self.queue.db.set_progress_handler(None, 0)

    def run_queue(self):
        self.assertEqual(self.queue.discover(), 1)
        self.assertTrue(self.queue.process_next())
        job = self.queue.db.execute('SELECT * FROM jobs WHERE active=1').fetchone()
        self.assertEqual(job['state'], 'done', job['error'])
        return json.loads((self.queue.cache / job['result_path']).read_text(encoding='utf-8'))

    def member(self, report):
        return Path(next(r['extracted_path'] for r in report['archive_members'] if r.get('sha256')))

    def test_parser_upgrade_reuses_extraction(self):
        self.publish()
        first = self.run_queue()
        self.queue.version = 'new-business-parser'
        with patch('local_analysis.extract_zip', side_effect=AssertionError('must not unpack')):
            second = self.run_queue()
        self.assertEqual(self.member(second).read_text(), PCF)
        self.assertNotEqual(self.member(first), self.member(second))
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM jobs WHERE active=0').fetchone()[0], 1)

    def test_timestamp_change_and_different_file_id_reuse_same_bytes(self):
        self.publish()
        self.run_queue()
        stamp = self.path.stat().st_mtime_ns
        os.utime(self.path, ns=(stamp + 1000000000, stamp + 1000000000))
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE files SET id='another-id'")
        with patch('local_analysis.extract_zip', side_effect=AssertionError('must not unpack')):
            self.run_queue()

    def test_old_report_seeds_missing_content_cache_without_unpacking(self):
        self.publish()
        self.run_queue()
        for manifest in (self.queue.cache / 'extraction_cache').glob('*/manifest.json'):
            manifest.unlink()
        self.queue.version = 'legacy-migration'
        with patch('local_analysis.extract_zip', side_effect=AssertionError('must adopt old files')):
            current = self.member(self.run_queue())
        self.assertEqual(current.read_text(), PCF)

    def test_corrupt_same_size_cache_is_rebuilt(self):
        self.publish()
        old = self.member(self.run_queue())
        old.write_bytes(old.read_bytes().replace(b'L001', b'L999'))
        self.queue.version = 'recheck-cache'
        with patch('local_analysis.extract_zip', wraps=extract_zip) as extractor:
            current = self.member(self.run_queue())
        self.assertEqual(extractor.call_count, 1)
        self.assertEqual(current.read_text(), PCF)

    def test_new_archive_content_is_unpacked(self):
        self.publish()
        self.run_queue()
        self.publish(PCF.replace('L001', 'L002'))
        with patch('local_analysis.extract_zip', wraps=extract_zip) as extractor:
            current = self.member(self.run_queue())
        self.assertEqual(extractor.call_count, 1)
        self.assertIn('L002', current.read_text())

    def test_nested_archive_reuse_and_budget_on_cache_hit(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as archive:
            archive.writestr('H4-262/model.pcf', PCF)
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('nested.zip', nested.getvalue())
        cache = self.base / 'shared'
        kwargs = dict(root=self.path, output=self.base / 'runs', extraction_cache=cache)
        analyze(**kwargs)
        with patch('local_analysis.extract_zip', side_effect=AssertionError('nested cache miss')):
            _, report, _ = analyze(**kwargs)
        self.assertEqual(len(report['archive_members']), 2)
        with self.assertRaisesRegex(ValueError, '展开总大小'):
            extract_cached(self.path, self.base / 'too-small', cache=cache, sha=digest(self.path),
                           extractor=extract_zip, budget=dict(bytes=1, files=20), audit=[])
        self.assertFalse((self.base / 'too-small').exists())

    def test_cache_survives_copy_fallback(self):
        self.publish()
        self.run_queue()
        self.queue.version = 'copy-fallback'
        with patch('archive_cache.os.link', side_effect=OSError('unsupported')), \
                patch('local_analysis.extract_zip', side_effect=AssertionError('must not unpack')):
            current = self.member(self.run_queue())
        self.assertEqual(current.read_text(), PCF)

    def test_old_extraction_is_cleaned_only_after_replacement_succeeds(self):
        self.publish()
        old = self.member(self.run_queue())
        old_report = next(self.queue.cache.glob('jobs/*/analysis-*/analysis.json'))
        self.assertTrue((old_report.parent / 'split_idf').is_dir())
        self.publish(PCF.replace('L001', 'L002'))
        self.assertEqual(self.queue.discover(), 1)
        cleanup_history(self.queue)
        self.assertTrue(old.is_file())
        self.assertTrue(self.queue.process_next())
        self.assertFalse(old.exists())
        self.assertFalse((old_report.parent / 'split_idf').exists())
        self.assertTrue(old_report.is_file())
        self.assertTrue(self.path.is_file())
        self.assertEqual(len(list((self.queue.cache / 'extraction_cache').glob('*/manifest.json'))), 1)

    def test_failed_replacement_keeps_old_extraction(self):
        self.publish()
        old = self.member(self.run_queue())
        self.publish('invalid pcf')
        self.assertEqual(self.queue.discover(), 1)
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.queue.counts(), {'failed': 1})
        cleanup_history(self.queue)
        self.assertTrue(old.is_file())

    def test_current_other_file_keeps_shared_historical_content(self):
        self.publish()
        self.run_queue()
        other = self.path.with_name('other.zip')
        other.write_bytes(self.path.read_bytes())
        old_sha = digest(other)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,1)',
                       ('p', 'other', other.name, str(other), 'done', other.stat().st_size, old_sha))
        self.assertEqual(self.queue.discover(), 1)
        self.assertTrue(self.queue.process_next())
        with zipfile.ZipFile(self.path, 'w') as archive:
            archive.writestr('H4-262/model.pcf', PCF.replace('L001', 'L002'))
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE files SET bytes=?,sha256=? WHERE id='source'", (self.path.stat().st_size, digest(self.path)))
        self.run_queue()
        self.assertTrue((self.queue.cache / 'extraction_cache' / ('v1-zip-' + old_sha) / 'manifest.json').is_file())
        job = self.queue.db.execute("SELECT result_path FROM jobs WHERE active=1 AND file_id='other'").fetchone()
        report = json.loads((self.queue.cache / job['result_path']).read_text(encoding='utf-8'))
        self.assertEqual(self.member(report).read_text(), PCF)

    def test_sevenzip_reuses_verified_content_cache(self):
        import py7zr
        path = self.path.with_suffix('.7z')
        with py7zr.SevenZipFile(path, 'w') as archive:
            archive.writestr(PCF, 'H4-262/model.pcf')
        kwargs = dict(root=path, output=self.base / 'runs')
        analyze(**kwargs)
        with patch('local_analysis.extract_7z', side_effect=AssertionError('must not unpack 7z')):
            _, report, _ = analyze(**kwargs)
        self.assertEqual(self.member(report).read_text(), PCF)

    def test_new_outer_archive_adopts_legacy_nested_extraction(self):
        nested = io.BytesIO()
        with zipfile.ZipFile(nested, 'w') as archive:
            archive.writestr('H4-262/model.pcf', PCF)
        def publish_outer(note):
            with zipfile.ZipFile(self.path, 'w') as archive:
                archive.writestr('nested.zip', nested.getvalue())
                archive.writestr('note.txt', note)
            with closing(sqlite3.connect(self.database)) as db, db:
                db.execute('DELETE FROM files')
                db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,1)',
                    ('p', 'source', self.path.name, str(self.path), 'done', self.path.stat().st_size, digest(self.path)))
        publish_outer('version one')
        self.run_queue()
        for manifest in (self.queue.cache / 'extraction_cache').glob('*/manifest.json'):
            manifest.unlink()
        publish_outer('version two')
        with patch('local_analysis.extract_zip', wraps=extract_zip) as extractor:
            report = self.run_queue()
        self.assertEqual(extractor.call_count, 1)  # Only the changed outer archive.
        self.assertEqual(len(report['pipeline_results']), 1)

    def test_cleanup_rejects_directory_outside_cache(self):
        from archive_cache import _remove_tree
        outside = self.base / 'keep'
        outside.mkdir()
        (outside / 'evidence.txt').write_text('keep')
        with self.assertRaises(ValueError):
            _remove_tree(outside, self.queue.cache)
        self.assertTrue((outside / 'evidence.txt').is_file())

    def test_cleanup_includes_interrupted_superseded_attempts(self):
        self.publish()
        old = self.member(self.run_queue())
        job = self.queue.db.execute('SELECT key FROM jobs WHERE active=1').fetchone()['key']
        attempt = self.queue.cache / 'jobs' / job / 'analysis-interrupted'
        (attempt / 'extracted').mkdir(parents=True)
        (attempt / 'extracted/stale.txt').write_text('old extraction')
        (attempt / 'analysis.json').write_text('{}')
        with self.queue.db:
            self.queue.db.execute('UPDATE jobs SET result_path=NULL WHERE key=?', (job,))
        self.publish(PCF.replace('L001', 'L002'))
        self.run_queue()
        self.assertFalse(old.exists())
        self.assertFalse((attempt / 'extracted').exists())
        self.assertTrue((attempt / 'analysis.json').is_file())

    def test_reactivated_cleaned_job_rebuilds_paths_using_shared_cache(self):
        self.publish()
        original_version = self.queue.version
        old = self.member(self.run_queue())
        self.queue.version = 'another-parser'
        self.run_queue()
        self.assertFalse(old.exists())
        self.queue.version = original_version
        self.assertEqual(self.queue.discover(), 0)  # Existing job, requeued.
        self.assertEqual(self.queue.counts(), {'pending': 1})
        with patch('local_analysis.extract_zip', side_effect=AssertionError('must reuse cache')):
            self.assertTrue(self.queue.process_next())
        row = self.queue.db.execute('SELECT state,result_path FROM jobs WHERE active=1').fetchone()
        self.assertEqual(row['state'], 'done')
        report = json.loads((self.queue.cache / row['result_path']).read_text(encoding='utf-8'))
        self.assertEqual(self.member(report).read_text(), PCF)
