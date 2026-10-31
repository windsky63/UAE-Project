"""Release corruption must be found offline, before the scheduled login."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from preflight import run_checks, RELEASE_ID, REQUIRED_FILES, MANIFEST


class PreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.files = {}
        for name in REQUIRED_FILES:
            data = b'x = 1\n' if name.endswith('.py') else b'@echo off\r\n' if name.endswith('.cmd') else b'requests>=2.31,<3\n'
            (self.base / name).parent.mkdir(parents=True, exist_ok=True)
            (self.base / name).write_bytes(data)
            self.files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        self.manifest = {'format': 1, 'release': RELEASE_ID, 'files': self.files}
        self.save_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def save_manifest(self):
        (self.base / MANIFEST).write_text(json.dumps(self.manifest, ensure_ascii=False), encoding='utf-8')

    def check(self, **kwargs):
        return run_checks(self.base, runtime=False, **kwargs)

    def test_intact_release_passes_without_reading_credentials(self):
        result = self.check()
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(result['files_checked'], len(REQUIRED_FILES))
        self.assertFalse(result['network_accessed'])

    def test_missing_runtime_module_is_named(self):
        (self.base / 'app/scope_review.py').unlink()
        result = self.check()
        self.assertFalse(result['ok'])
        self.assertTrue(any('scope_review.py' in item for item in result['errors']))

    def test_mixed_version_or_modified_file_fails_hash_check(self):
        (self.base / 'app/scheduler.py').write_text('x = 2\n', encoding='utf-8')
        result = self.check()
        self.assertFalse(result['ok'])
        self.assertTrue(any('SHA-256' in item and 'scheduler.py' in item for item in result['errors']))

    def test_mismatched_manifest_release_is_rejected(self):
        self.manifest['release'] = 'old-release'
        self.save_manifest()
        self.assertFalse(self.check()['ok'])

    def test_missing_or_invalid_manifest_is_rejected(self):
        (self.base / MANIFEST).unlink()
        self.assertFalse(self.check()['ok'])
        (self.base / MANIFEST).write_text('[]', encoding='utf-8')
        self.assertFalse(self.check()['ok'])

    def test_required_file_cannot_be_omitted_from_manifest(self):
        del self.files['app/sync.py']
        self.save_manifest()
        self.assertFalse(self.check()['ok'])

    def test_archived_launcher_is_still_verified(self):
        path = next(n for n in REQUIRED_FILES if n.startswith('maintenance/'))
        (self.base / path).write_bytes(b'modified')
        self.assertTrue(any(path in error for error in self.check()['errors']))

    def test_manifest_rejects_traversal_and_unapproved_directories(self):
        for name in ('../outside.py', 'maintenance/../../outside.py', 'other/script.py', 'C:/outside.py', '..\\outside.py'):
            self.files[name] = {'sha256': 'bad', 'bytes': 0}
            self.save_manifest()
            result = self.check()
            self.assertTrue(any('非法程序路径' in error and name in error for error in result['errors']))
            del self.files[name]

    def test_syntax_failure_is_reported_even_if_hash_matches(self):
        data = b'def invalid(\n'
        (self.base / 'app/sync.py').write_bytes(data)
        self.files['app/sync.py'] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        self.save_manifest()
        result = self.check()
        self.assertFalse(result['ok'])
        self.assertTrue(any('语法' in item for item in result['errors']))

    def test_progress_database_and_downloads_are_not_hashed(self):
        (self.base / 'runtime').mkdir()
        (self.base / 'runtime' / 'ledger.sqlite3').write_bytes(b'changing progress data')
        (self.base / 'downloads').mkdir()
        (self.base / 'downloads' / 'drawing.pdf').write_bytes(b'file data')
        self.assertTrue(self.check()['ok'])

    def test_untracked_development_files_are_not_startup_dependencies(self):
        for name in ('tests/test_example.py', 'devtools/inspect_example.py'):
            path = self.base / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('not valid Python; never run this', encoding='utf-8')
        self.assertTrue(self.check()['ok'])

    def test_database_check_is_read_only_and_rejects_corruption(self):
        folder = self.base / 'runtime'
        folder.mkdir()
        database = folder / 'ledger.sqlite3'
        with closing(sqlite3.connect(database)) as db:
            db.execute('CREATE TABLE preserved(value TEXT)')
            db.execute("INSERT INTO preserved VALUES('keep')")
            db.commit()
        before = database.read_bytes()
        result = run_checks(self.base)
        self.assertTrue(result['ok'], result['errors'])
        self.assertEqual(database.read_bytes(), before)
        database.write_bytes(b'not a SQLite database')
        result = run_checks(self.base)
        self.assertFalse(result['ok'])
        self.assertTrue(any('数据库' in item for item in result['errors']))

    def test_missing_requests_fails_before_any_network_use(self):
        import importlib
        original = importlib.import_module
        def dependency(name):
            if name == 'requests':
                raise ImportError('missing')
            return original(name)
        with patch('preflight.importlib.import_module', side_effect=dependency):
            result = run_checks(self.base)
        self.assertFalse(result['ok'])
        self.assertTrue(any('requests' in item for item in result['errors']))


if __name__ == '__main__':
    unittest.main()
