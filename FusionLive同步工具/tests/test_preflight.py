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
    def test_download_startup_probes_database_without_loading_analysis_dependencies(self):
        folder=self.base/'runtime';folder.mkdir()
        with closing(sqlite3.connect(folder/'ledger.sqlite3')) as db, db:
            db.execute('CREATE TABLE files(id TEXT)')
        import importlib
        original=importlib.import_module
        modules=[]
        def load(name):
            modules.append(name)
            if name!='requests':raise AssertionError('startup imported optional dependency '+name)
            return original(name)
        with patch('preflight.importlib.import_module',side_effect=load):
            report=run_checks(self.base,startup=True)
        self.assertTrue(report['ok'],report['errors'])
        self.assertEqual(modules,['requests'])
        self.assertIn('未执行全库完整性扫描',report['database'])
        self.assertEqual(report['files_checked'],len(REQUIRED_FILES))

    def test_download_startup_rejects_unreadable_database(self):
        folder=self.base/'runtime';folder.mkdir()
        (folder/'ledger.sqlite3').write_bytes(b'not a database')
        report=run_checks(self.base,startup=True)
        self.assertFalse(report['ok'])
        self.assertTrue(any('数据库只读检查失败' in error for error in report['errors']))

    def test_default_diagnostic_still_runs_full_database_check(self):
        folder=self.base/'runtime';folder.mkdir()
        with closing(sqlite3.connect(folder/'ledger.sqlite3')) as db, db:
            db.execute('CREATE TABLE files(id TEXT)')
        report=run_checks(self.base)
        self.assertEqual(report['database'],'quick_check: ok（只读检查）')
        self.assertEqual(report['check_mode'],'full')

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

    def preserve_server_credentials(self):
        del self.files['app/local_credentials.py']
        self.manifest['preserved_local_files'] = ['app/local_credentials.py']
        self.save_manifest()

    def test_overlay_accepts_independent_server_credentials_without_executing_them(self):
        self.preserve_server_credentials()
        path = self.base / 'app/local_credentials.py'
        data = b'raise RuntimeError("credentials must never be imported by preflight")\n'
        path.write_bytes(data)
        report = self.check(startup=True)
        self.assertTrue(report['ok'], report['errors'])
        self.assertEqual(report['files_checked'], len(REQUIRED_FILES))
        self.assertEqual(path.read_bytes(), data)

    def test_undeclared_missing_credentials_manifest_entry_still_fails(self):
        del self.files['app/local_credentials.py']
        self.save_manifest()
        self.assertIn('校验清单缺少必要项目：app/local_credentials.py', self.check()['errors'])

    def test_overlay_missing_or_invalid_server_credentials_fails(self):
        self.preserve_server_credentials()
        path = self.base / 'app/local_credentials.py'
        path.unlink()
        self.assertTrue(any('服务器凭据缺失' in e for e in self.check()['errors']))
        path.write_bytes(b'def broken(\n')
        self.assertTrue(any('服务器凭据 Python 语法' in e for e in self.check()['errors']))

    def test_overlay_cannot_exempt_program_modules_or_conflict_with_hashes(self):
        self.manifest['preserved_local_files'] = ['app/local_credentials.py']
        self.save_manifest()
        self.assertFalse(self.check()['ok'])
        self.preserve_server_credentials()
        del self.files['app/sync.py']
        self.manifest['preserved_local_files'].append('app/sync.py')
        self.save_manifest()
        self.assertFalse(self.check()['ok'])

    def test_overlay_still_checks_program_hashes(self):
        self.preserve_server_credentials()
        (self.base / 'app/sync.py').write_bytes(b'changed = True\n')
        self.assertTrue(any('SHA-256' in e and 'app/sync.py' in e for e in self.check()['errors']))

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
