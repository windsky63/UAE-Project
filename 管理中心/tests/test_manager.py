from contextlib import closing
import hashlib
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager, TOOLS, clean_log
from server import handler


class ManagerTests(unittest.TestCase):
    def test_missing_nas_config_can_be_completed_and_saved(self):
        path = self.manager.config_path('nas')
        path.unlink()
        result = self.manager.config('nas')
        self.assertFalse(result['exists'])
        self.assertEqual(result['data']['start_hour_beijing'], 12)
        self.assertEqual(result['data']['stop_hour_beijing'], 0)
        self.assertEqual(result['data']['target_root'], r'\\nas.example.test\工程项目文件\阿联酋项目FusionLive平台资料')
        self.assertFalse(path.exists())
        saved = self.manager.save_config('nas', {'target_root': r'\\nas\share'}, result['revision'])
        self.assertTrue(saved['exists'])
        self.assertEqual(saved['data']['chunk_bytes'], 1048576)

    def test_nas_template_defaults_keep_existing_connection(self):
        path = self.manager.config_path('nas')
        path.with_name('config.example.json').write_text(json.dumps({'target_root': r'\\template\share'}), encoding='utf-8')
        self.assertEqual(self.manager.config('nas')['data']['target_root'], r'\\nas\share')
        path.unlink()
        result = self.manager.config('nas')
        self.assertEqual(result['data']['target_root'], r'\\template\share')
        saved = self.manager.save_config('nas', result['data'], result['revision'])
        self.assertEqual(saved['data']['target_root'], r'\\template\share')

    def test_nas_unknown_field_names_are_reported_without_writing(self):
        result = self.manager.config('nas')
        with self.assertRaisesRegex(ValueError, 'extra_field'):
            self.manager.save_config('nas', {**result['data'], 'extra_field': True}, result['revision'])
        self.assertEqual(self.manager.config('nas'), result)

    def test_missing_wechat_config_has_requested_editable_defaults(self):
        self.manager.config_path('wechat').unlink(missing_ok=True)
        result=self.manager.config('wechat')
        self.assertFalse(result['exists'])
        self.assertEqual(result['data']['chats'],[])
        self.assertEqual(result['data']['sync_sources'],[])
        self.assertEqual(result['data']['codex_command'],['codex'])
        self.assertTrue(result['data']['codex_enabled'])
        self.assertTrue(result['data']['allow_send'])
        self.assertEqual(result['data']['database_root'],'D:/xwechat_files')
        self.assertEqual(result['data']['database_account'],'')
        self.assertFalse(self.manager.config_path('wechat').exists())

    def test_partial_wechat_config_keeps_values_and_fills_missing_arrays(self):
        self.manager.config_path('wechat').write_text('{"account_name":"existing"}',encoding='utf-8')
        result=self.manager.config('wechat')
        self.assertTrue(result['exists'])
        self.assertEqual(result['data']['account_name'],'existing')
        self.assertEqual(result['data']['chats'],[])

    def test_existing_wechat_switches_and_database_paths_are_not_overridden_by_new_defaults(self):
        existing = dict(codex_enabled=False, allow_send=False, database_root='C:/custom', database_account='custom')
        self.manager.config_path('wechat').write_text(json.dumps(existing), encoding='utf-8')
        result = self.manager.config('wechat')['data']
        for key, value in existing.items():
            self.assertEqual(result[key], value)
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        for meta in TOOLS.values():
            (self.root/meta['folder']/'runtime').mkdir(parents=True)
        self.manager=Manager(self.root,self.root/'admin-runtime')
        self.sync=self.manager.folder('sync')
        (self.sync/'downloads').mkdir()
        self.ledger=self.sync/'runtime/ledger.sqlite3'
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute('CREATE TABLE files(project,id,filename,path,bytes,sha256,status,active)')
            for identity,state,active in [('one','done',1),('old','done',0),('pending','pending',1)]:
                content=b'PIPELINE-REFERENCE H4-262-L001'
                (self.sync/'downloads'/f'{identity}.pcf').write_bytes(content)
                db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                    ('p',identity,identity+'.pcf','downloads/'+identity+'.pcf',len(content),hashlib.sha256(content).hexdigest(),state,active))
        nas=dict(source_root='../downloads',download_ledger='../ledger.sqlite3',target_root='\\\\nas\\share',
            start_hour_beijing=12,stop_hour_beijing=0,chunk_bytes=1048576,retry_attempts=2,preserve_previous_versions=True)
        self.manager.config_path('nas').write_text(json.dumps(nas),encoding='utf-8')
        self.manager.config_path('wechat').write_text('{"chats":[],"codex_enabled":false,"allow_send":false}',encoding='utf-8')

    def test_overview_is_read_only_and_reports_missing_cache_as_unknown(self):
        before=self.ledger.read_bytes()
        with patch('manager.subprocess.Popen',side_effect=AssertionError('Read launched a process')):
            info=self.manager.overview()
        self.assertEqual(info['downloads'],{'done':1,'pending':1})
        self.assertIsNone(info['analysis'])
        self.assertEqual(len(info['tools']),3)
        self.assertEqual(self.ledger.read_bytes(),before)

    def test_file_filter_preview_and_stale_version(self):
        result=self.manager.files(query='one')
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['state'],'ready')
        identity=result['items'][0]['id']
        self.assertIn('H4-262',self.manager.preview(identity)['text'])
        self.assertEqual(self.manager.files(query="' OR 1=1 --")['total'],0)
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE files SET active=0 WHERE id='one'")
        with self.assertRaisesRegex(ValueError,'版本'):
            self.manager.artifact(identity)

    def test_download_path_cannot_escape(self):
        (self.sync/'secret.pcf').write_bytes(b'secret')
        with closing(sqlite3.connect(self.ledger)) as db, db:
            db.execute("UPDATE files SET path='downloads/../secret.pcf',bytes=6 WHERE id='one'")
        row=self.manager.files()['items'][0]
        self.assertEqual(row['state'],'missing')
        with self.assertRaisesRegex(ValueError,'路径'):
            self.manager.preview(row['id'])

    def test_config_validation_and_optimistic_revision(self):
        config=self.manager.config('nas')
        wrong={**config['data'],'retry_attempts':0}
        with self.assertRaisesRegex(ValueError,'重试'):
            self.manager.save_config('nas',wrong,config['revision'])
        self.assertEqual(self.manager.config('nas'),config)
        value={**config['data'],'retry_attempts':3}
        result=self.manager.save_config('nas',value,config['revision'])
        self.assertEqual(result['data']['retry_attempts'],3)
        self.assertTrue(list(self.manager.runtime.glob('config-nas-*.bak')))
        with self.assertRaisesRegex(ValueError,'其他窗口'):
            self.manager.save_config('nas',value,config['revision'])

    def test_managed_subprocess_and_duplicate_submission(self):
        command=[sys.executable,'-c','import time;print("offline test",flush=True);time.sleep(.3)']
        with patch.object(self.manager,'command',return_value=(command,self.root)):
            task=self.manager.action('sync','check')
            with self.assertRaisesRegex(ValueError,'正在运行'):
                self.manager.action('sync','check')
        self.manager.children[task['id']].wait(timeout=5)
        result=self.manager.tasks()[0]
        self.assertEqual(result['state'],'succeeded')
        self.assertIn('offline test',self.manager.logs()['items'][0]['message'])

    @patch('manager.Manager.python',return_value=sys.executable)
    def test_command_allowlist_never_uses_shell(self, mock_python):
        for tool in TOOLS:
            with self.assertRaises(ValueError):
                self.manager.command(tool,'delete-all')
        command,_=self.manager.command('nas','check')
        self.assertEqual(command[-1],'integrity')
        command,_=self.manager.command('wechat','stop')
        self.assertEqual(command[-1],'--stop')
        self.assertNotIn('check',self.manager.command('nas','check')[0][-1])

    def test_log_redaction(self):
        self.assertEqual(clean_log('password=abc token unaffected'),'password=[已隐藏] token unaffected')

    @patch('manager.Manager.python',return_value=sys.executable)
    def test_sync_config_uses_original_validator_without_platform_access(self, mock_python):
        actual=Path(__file__).resolve().parents[2]/TOOLS['sync']['folder']/'app'
        target=self.sync/'app'
        target.mkdir()
        for name in ('app_paths.py','push_settings.py','analysis_settings.py'):
            (target/name).write_bytes((actual/name).read_bytes())
        config=self.manager.config('sync')
        data={**config['data'],'briefing_title':'本地测试简报','recipients':['模拟群'],
              'analysis_poll_seconds':60,'analysis_idle_log_seconds':120,'analysis_idle_exit_seconds':300}
        result=self.manager.save_config('sync',data,config['revision'])
        self.assertEqual(result['data']['briefing_title'],'本地测试简报')
        self.assertEqual(result['data']['analysis_poll_seconds'],60)
        self.assertEqual(result['data']['analysis_idle_log_seconds'],120)
        self.assertEqual(result['data']['analysis_idle_exit_seconds'],300)
        with self.assertRaises(ValueError):
            self.manager.save_config('sync',{**result['data'],'analysis_poll_seconds':0},result['revision'])
        invalid={**result['data'],'prepare_time_beijing':'12:00','send_time_beijing':'11:00'}
        with self.assertRaises(ValueError):
            self.manager.save_config('sync',invalid,result['revision'])
        self.assertEqual(self.manager.config('sync')['revision'],result['revision'])

    def test_wechat_config_uses_pydantic_before_saving(self):
        actual=Path(__file__).resolve().parents[2]/TOOLS['wechat']['folder']
        python=actual.parent/'.venv-analysis/Scripts/python.exe'
        if not python.is_file():
            self.skipTest('Local WeChat Python environment unavailable')
        package=self.manager.folder('wechat')/'wechat_local'
        package.mkdir()
        (package/'__init__.py').write_text('',encoding='utf-8')
        (package/'config.py').write_bytes((actual/'wechat_local/config.py').read_bytes())
        config=self.manager.config('wechat')
        self.assertEqual(config['data']['codex_timeout_seconds'],600)
        self.assertEqual(config['data']['reply_max_age_seconds'],900)
        self.assertTrue(config['data']['mention_reply_sender'])
        self.assertTrue(config['data']['query_progress_enabled'])
        self.assertTrue(config['data']['chat_memory_enabled'])
        self.assertEqual(config['data']['reply_target_chars'],600)
        self.assertEqual(config['data']['recent_messages'],6)
        self.assertEqual(config['data']['file_send_cleanup_days'],2)
        self.assertEqual(config['data']['codex_backend'],'app-server')
        self.assertEqual(config['data']['codex_session_rotate_percent'],70)
        self.assertEqual(config['data']['codex_session_prewarm_percent'],55)
        with patch.object(self.manager,'python',return_value=str(python)):
            result=self.manager.save_config('wechat',{**config['data'],'codex_timeout_seconds':60,
                'mention_reply_sender':False,'query_progress_enabled':False,
                'chat_memory_enabled':False,'reply_target_chars':300,'codex_backend':'exec',
                'codex_session_rotate_percent':80,'codex_session_prewarm_percent':60,
                'file_send_cleanup_days':7},config['revision'])
            self.assertEqual(result['data']['codex_timeout_seconds'],60)
            self.assertFalse(result['data']['mention_reply_sender'])
            self.assertFalse(result['data']['query_progress_enabled'])
            self.assertFalse(result['data']['chat_memory_enabled'])
            self.assertEqual(result['data']['reply_target_chars'],300)
            self.assertEqual(result['data']['codex_backend'],'exec')
            self.assertEqual(result['data']['codex_session_rotate_percent'],80)
            self.assertEqual(result['data']['codex_session_prewarm_percent'],60)
            self.assertEqual(result['data']['file_send_cleanup_days'],7)
            self.assertFalse(result['data']['allow_send'])
            with self.assertRaises(ValueError):
                self.manager.save_config('wechat',{**result['data'],'codex_timeout_seconds':0},result['revision'])
            with self.assertRaises(ValueError):
                self.manager.save_config('wechat',{**result['data'],'codex_session_prewarm_percent':90},result['revision'])
            with self.assertRaises(ValueError):
                self.manager.save_config('wechat',{**result['data'],'file_send_cleanup_days':-1},result['revision'])
        self.assertEqual(self.manager.config('wechat')['revision'],result['revision'])

    def test_wechat_directory_defaults_discover_siblings_and_manual_empty_is_preserved(self):
        extra = self.root/'项目资料 空格'
        extra.mkdir()
        config = self.manager.config('wechat')
        self.assertTrue(config['data']['codex_auto_access_dirs'])
        self.assertIn(str(extra.resolve()),config['data']['codex_access_dirs'])
        self.assertNotIn(str(self.manager.folder('wechat').resolve()),config['data']['codex_access_dirs'])
        self.manager.config_path('wechat').write_text(json.dumps(dict(
            codex_auto_access_dirs=False,codex_access_dirs=[])),encoding='utf-8')
        config = self.manager.config('wechat')
        self.assertEqual(config['data']['codex_access_dirs'],[])
        self.assertIn(str(extra.resolve()),config['discovered_access_dirs'])

    def test_every_tool_uses_shared_python_even_if_old_wechat_environment_exists(self):
        shared=self.root/'.venv-analysis/Scripts/python.exe'
        shared.parent.mkdir(parents=True)
        shared.touch()
        old=self.manager.folder('wechat')/'.venv/Scripts/python.exe'
        old.parent.mkdir(parents=True)
        old.touch()
        for tool in TOOLS:
            self.assertEqual(self.manager.python(tool),str(shared))
        shared.unlink()
        with self.assertRaisesRegex(ValueError,'共享环境'):
            self.manager.python('wechat')

    def test_loopback_api_session_csrf_and_files(self):
        static=self.root/'static'
        static.mkdir()
        (static/'index.html').write_text('<h1>local tools</h1>',encoding='utf-8')
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.manager,'test-token',static))
        thread=threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        base=f'http://127.0.0.1:{server.server_port}'
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(base+'/api/overview')
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
            with opener.open(base+'/api/session?token=test-token') as response:
                self.assertIn(b'local tools',response.read())
            with opener.open(base+'/api/files') as response:
                identity=json.load(response)['items'][0]['id']
            with opener.open(base+'/api/preview?id='+identity) as response:
                self.assertIn('H4-262',json.load(response)['text'])
            request=urllib.request.Request(base+'/api/action',data=b'{"tool":"nas","action":"start"}',
                headers={'Content-Type':'application/json','X-Admin-Request':'1','Origin':'https://untrusted.example'})
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(request)
            self.assertEqual(error.exception.code,403)
            error.exception.close()
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(base+'/api/files?limit=100000')
            error.exception.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)


if __name__=='__main__':
    unittest.main()
