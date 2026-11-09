"""Exercise quarantine on real temporary trees, including data and link guards."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'cleanup_deployment.py'
spec = importlib.util.spec_from_file_location('cleanup_deployment', SOURCE)
cleanup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cleanup)


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / '中文 项目'
        self.root.mkdir()
        self.files = ['admin_launcher.py', 'FusionLive同步工具/main.py', '管理中心/manager.py',
                      '绿联云上传工具/main.py', 'Ima知识库管理/ima_manager/sync.py',
                      'codex-wechat/wechat_local/state.py']
        for name in self.files:
            self.put(name)
        self.manifest = {'files':{name:{'sha256':'baseline'} for name in self.files}}
        self.save_manifest()

    def put(self, name, data=b'original'):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def save_manifest(self):
        (self.root / cleanup.MANIFEST).write_text(json.dumps(self.manifest), encoding='utf-8')

    def test_moves_only_obsolete_code_and_docs_and_preserves_data(self):
        extras = ['旧说明.md', '管理中心/old.py', '旧使用说明.txt']
        protected = ['logs/old.py', 'runtime/something.md', 'downloads/x.py',
                     'history/a.md', 'exports/a.md', 'Ima知识库管理/待同步/a.md',
                     '.venv-analysis/a.py', 'templates/a.md', 'backup/a.py',
                     'unknown.sqlite3-wal', 'a.log', 'notes.txt', 'drawing.dwg',
                     'unknown.blob', 'config.json', 'local_credentials.py',
                     'credentials.bootstrap.json', 'custom_rules.json',
                     'codex-wechat/.agents/skills/test/SKILL.md']
        for name in extras + protected:
            self.put(name)
        proposal = cleanup.plan(self.root)
        self.assertEqual({r['path'] for r in proposal['candidates']}, set(extras))
        self.assertTrue(all((self.root / name).exists() for name in extras))
        destination, record = cleanup.quarantine(self.root, proposal)
        self.assertEqual(set(record['moved']), set(extras))
        self.assertFalse(record['errors'])
        for name in extras:
            self.assertFalse((self.root / name).exists())
            self.assertEqual((destination / name).read_bytes(), b'original')
        for name in protected + self.files:
            self.assertEqual((self.root / name).read_bytes(), b'original')
        self.assertFalse(cleanup.plan(self.root)['candidates'])
        self.assertEqual(len(json.loads((destination/'清理记录.json').read_text(encoding='utf-8'))['moved']), 3)

    def test_changed_source_aborts_before_any_move(self):
        self.put('one.py'); self.put('two.py')
        proposal = cleanup.plan(self.root)
        self.put('two.py', b'changed')
        with self.assertRaises(ValueError):
            cleanup.quarantine(self.root, proposal)
        self.assertTrue((self.root/'one.py').exists())

    def test_missing_or_unsafe_manifest_refuses_cleanup(self):
        self.manifest['files']['../outside.py'] = {'sha256':'x'}
        self.save_manifest()
        with self.assertRaises(ValueError):
            cleanup.plan(self.root)
        (self.root/cleanup.MANIFEST).unlink()
        with self.assertRaises(FileNotFoundError):
            cleanup.plan(self.root)

    def test_symlink_is_never_followed(self):
        outside = Path(self.temp.name)/'outside'
        outside.mkdir(); (outside/'a.py').write_bytes(b'outside')
        try:
            (self.root/'linked').symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest('Symbolic link creation not permitted')
        proposal = cleanup.plan(self.root)
        self.assertIn('linked', proposal['skipped_links'])
        self.assertFalse(proposal['candidates'])
        self.assertEqual((outside/'a.py').read_bytes(), b'outside')

    def test_real_cli_defaults_to_preview_and_cancel_does_not_move(self):
        self.put('tools/cleanup_deployment.py', SOURCE.read_bytes())
        self.manifest['files']['tools/cleanup_deployment.py']={'sha256':'baseline'}
        self.save_manifest()
        old = self.put('obsolete.py')
        for arguments, answer in [([], ''), (['--apply'], 'cancel\n')]:
            result = subprocess.run([sys.executable, '-X', 'utf8', '-B',
                str(self.root/'tools/cleanup_deployment.py'), *arguments], input=answer,
                capture_output=True, text=True, encoding='utf-8', timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(old.exists())
        self.assertTrue(list((self.root/'runtime/cleanup-reports').glob('*.json')))

    @unittest.skipUnless(os.name == 'nt', 'Windows CMD integration')
    def test_cmd_handles_spaces_and_chinese_path(self):
        launcher = SOURCE.parents[1]/'清理多余文件.cmd'
        target = self.put('清理多余文件.cmd', launcher.read_bytes())
        self.put('tools/cleanup_deployment.py', SOURCE.read_bytes())
        # A tiny process fixture records forwarded arguments; no real cleanup.
        python = self.put('.venv-analysis/Scripts/python.cmd', b'')
        python.unlink()
        import venv
        venv.EnvBuilder(with_pip=False).create(python.parent.parent)
        self.put('tools/cleanup_deployment.py', b'import sys\nprint("ARGS",sys.argv[1:])\n')
        command = '""' + str(target) + '" --apply"'
        result = subprocess.run(os.environ.get('COMSPEC','cmd.exe')+' /d /s /c '+command,
            env={**os.environ,'PROJECT_CLEANUP_NO_PAUSE':'1'}, capture_output=True,
            text=True, encoding='utf-8', errors='replace', timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('--apply',result.stdout)


if __name__ == '__main__':
    unittest.main()
