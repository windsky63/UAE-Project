"""Relocate project configuration without rewriting NAS shares or credentials."""
import importlib.util
import ast
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

SOURCE = Path(__file__).resolve().parents[1]/'prepare_deployment.py'
spec = importlib.util.spec_from_file_location('prepare_deployment',SOURCE)
deployment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(deployment)


class PrepareDeploymentTests(unittest.TestCase):
    def make_overlay(self, root):
        payload = root / 'program.py'
        payload.write_bytes(b'print("fixture")\n')
        manifest = dict(release='fixture-release', existing_server_overlay=True, files={
            'program.py': dict(bytes=payload.stat().st_size, sha256=hashlib.sha256(payload.read_bytes()).hexdigest())})
        (root / 'deployment-manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
        config = root / 'codex-wechat/wechat.codex.json'
        config.parent.mkdir()
        old = dict(chats=[{'id': 'server-chat'}], codex_timeout_seconds=600,
                   codex_idle_timeout_seconds=300, database_account='server-account', allow_send=False)
        config.write_text(json.dumps(old), encoding='utf-8')
        return config, old

    def test_server_overlay_backs_up_and_only_updates_requested_wechat_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            config, old = self.make_overlay(root)
            receipts = root / 'Ima知识库管理/runtime/state.sqlite3'
            receipts.parent.mkdir(parents=True)
            receipts.write_bytes(b'preserve-server-receipts')
            result = deployment.apply_server_update(root, import_credentials=False)
            updated = json.loads(config.read_text(encoding='utf-8'))
            self.assertEqual(updated, dict(old, codex_timeout_seconds=0, codex_idle_timeout_seconds=0,
                                          codex_connect_timeout_seconds=90))
            self.assertEqual(json.loads((Path(result['backup']) / 'wechat.codex.json').read_text()), old)
            self.assertEqual(receipts.read_bytes(), b'preserve-server-receipts')
            self.assertFalse(result['network_accessed'])
            again = deployment.apply_server_update(root, import_credentials=False)
            self.assertEqual(again['updated_wechat_fields'], [])
            self.assertIsNone(again['backup'])

    def test_server_overlay_preview_and_failed_checksum_never_change_config(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            config, old = self.make_overlay(root)
            result = deployment.apply_server_update(root, check_only=True, import_credentials=False)
            self.assertTrue(result['updated_wechat_fields'])
            self.assertEqual(json.loads(config.read_text()), old)
            self.assertFalse((root / '管理中心/runtime').exists())
            (root / 'program.py').write_bytes(b'changed after packaging')
            with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                deployment.apply_server_update(root, import_credentials=False)
            self.assertEqual(json.loads(config.read_text()), old)

    def test_server_overlay_keeps_custom_network_retry_limit(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder).resolve()
            config, old = self.make_overlay(root)
            old['codex_connect_timeout_seconds'] = 180
            config.write_text(json.dumps(old), encoding='utf-8')
            deployment.apply_server_update(root, import_credentials=False)
            self.assertEqual(json.loads(config.read_text())['codex_connect_timeout_seconds'], 180)

    def test_relocated_settings_preserve_knowledge_base_and_external_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder).resolve()
            (root/'deployment-manifest.json').write_text('{}',encoding='utf-8')
            path=root/'Ima知识库管理/runtime/settings.json'
            path.parent.mkdir(parents=True)
            original={'source':r'D:\old\Ima知识库管理\待同步', 'kb':'configured-kb',
                      'database_path':r'D:\old\FusionLive同步工具\runtime\ledger.sqlite3',
                      'ledger_path':r'\\nas\shared\ledger.xlsx','auto_sync_enabled':False}
            path.write_text(json.dumps(original),encoding='utf-8')
            preview=deployment.prepare(root,check_only=True)
            self.assertTrue(preview['updated_config_paths'])
            self.assertEqual(json.loads(path.read_text()),original)
            deployment.prepare(root,import_credentials=False)
            result=json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(result['source'],str(root/'Ima知识库管理/待同步'))
            self.assertEqual(result['database_path'],str(root/'FusionLive同步工具/runtime/ledger.sqlite3'))
            self.assertEqual(result['kb'],'configured-kb')
            self.assertEqual(result['ledger_path'],original['ledger_path'])
            self.assertFalse(result['auto_sync_enabled'])
            self.assertEqual(deployment.prepare(root,import_credentials=False)['updated_config_paths'],[])

    def test_relative_paths_and_external_folders_are_preserved(self):
        root=Path.cwd()
        for value in ('../FusionLive同步工具/downloads',r'D:\xwechat_files',
                      r'\\nas\FusionLive同步工具\downloads',None):
            self.assertEqual(deployment.relocated_path(value,root),value)

    def test_parent_escape_and_unidentified_installation_are_rejected(self):
        with self.assertRaises(ValueError):
            deployment.relocated_path(r'D:\old\FusionLive同步工具\runtime\..\..\outside',Path.cwd())
        with tempfile.TemporaryDirectory() as folder,self.assertRaises(ValueError):
            deployment.prepare(folder,import_credentials=False)


class PackagedStartupTests(unittest.TestCase):
    def setUp(self):
        root = SOURCE.parent.parent
        spec = importlib.util.spec_from_file_location('deployment_builder', root / '管理中心/build_deployment.py')
        self.builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.builder)
        sync = root / 'FusionLive同步工具'
        source = (sync / 'app/preflight.py').read_bytes()
        required = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                        if isinstance(node, ast.Assign) and any(
                            isinstance(t, ast.Name) and t.id == 'REQUIRED_FILES' for t in node.targets))
        release = next(ast.literal_eval(node.value) for node in ast.parse(source).body
                       if isinstance(node, ast.Assign) and any(
                           isinstance(t, ast.Name) and t.id == 'RELEASE_ID' for t in node.targets))
        prefix = 'FusionLive同步工具/'
        self.entries = {prefix + name: b'# fixture\n' for name in required if name != 'app/local_credentials.py'}
        self.entries[prefix + 'app/preflight.py'] = source
        self.entries[prefix + 'app/paths.py'] = (sync / 'app/paths.py').read_bytes()
        self.manifest = dict(format=1, release=release, files={
            name[len(prefix):]: dict(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
            for name, data in self.entries.items()})

    def verify(self):
        self.entries['FusionLive同步工具/release_manifest.json'] = json.dumps(self.manifest).encode('utf-8')
        self.builder.verify_sync_startup(self.entries)

    def test_old_overlay_with_valid_zip_hashes_is_rejected_by_actual_checker(self):
        with self.assertRaisesRegex(ValueError, 'Packaged sync startup check failed'):
            self.verify()

    def test_declared_server_credentials_pass_actual_checker(self):
        self.manifest['preserved_local_files'] = ['app/local_credentials.py']
        self.verify()

    def test_preserved_credentials_do_not_hide_missing_program_module(self):
        self.manifest['preserved_local_files'] = ['app/local_credentials.py']
        del self.entries['FusionLive同步工具/app/sync.py']
        del self.manifest['files']['app/sync.py']
        with self.assertRaisesRegex(ValueError, 'Packaged sync startup check failed'):
            self.verify()


if __name__=='__main__': unittest.main()
