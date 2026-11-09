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
    @unittest.skipUnless(sys.platform=='win32','Windows interactive console launcher')
    def test_nas_setup_uses_real_console_batch_entry_without_redirected_input(self):
        nas=self.manager.folder('nas')
        (nas/'setup_nas_ssh.py').touch()
        (nas/'配置NAS本机校验.cmd').touch()
        with patch('manager.locked',return_value=False),patch('manager.subprocess.Popen') as launch:
            self.manager.nas_setup()
        command=launch.call_args.args[0]
        self.assertEqual(command,['cmd.exe','/d','/c','配置NAS本机校验.cmd'])
        self.assertTrue(launch.call_args.kwargs['close_fds'])
        self.assertNotIn('stdin',launch.call_args.kwargs)
        self.assertNotIn('stdout',launch.call_args.kwargs)
    def test_nas_ssh_defaults_and_read_status_do_not_connect(self):
        self.manager.config_path('nas').unlink()
        with patch('manager.subprocess.Popen',side_effect=AssertionError('read launched SSH')):
            cfg=self.manager.config('nas')
            self.assertTrue(cfg['data']['remote_hash']['enabled'])
            self.assertEqual(cfg['data']['verification_method'],'ssh')
            self.assertFalse(cfg['data']['remote_hash']['fallback_to_smb'])
            self.assertEqual(self.manager.nas_hash_status()['state'],'unconfigured')

    def test_nas_ssh_configuration_roundtrip_and_password_rejection(self):
        current=self.manager.config('nas')
        remote={**current['data']['remote_hash'],'enabled':True,'host':'nas',
                'root_path':'/volume1/share','username':'windsky'}
        with patch.object(self.manager,'_validate',return_value='') as validate:
            saved=self.manager.save_config('nas',{**current['data'],'verification_method':'ssh','remote_hash':remote},current['revision'])
        validate.assert_called_once()
        self.assertEqual(saved['data']['remote_hash']['root_path'],'/volume1/share')
        with self.assertRaisesRegex(ValueError,'密码'):
            self.manager.save_config('nas',{**saved['data'],'remote_hash':{**remote,'password':'not-saved'}},saved['revision'])
        self.assertNotIn('not-saved',self.manager.config_path('nas').read_text(encoding='utf-8'))

    def test_nas_hash_check_records_configuration_revision(self):
        cfg=self.manager.config('nas')
        with patch.object(self.manager,'_validate',return_value=''):
            self.manager.save_config('nas',{**cfg['data'],'verification_method':'ssh','remote_hash':{
                **cfg['data']['remote_hash'],'enabled':True,'root_path':'/volume1/share'}},cfg['revision'])
        with patch('manager.locked',return_value=False),patch.object(self.manager,'action',return_value={'id':'check1'}):
            self.manager.nas_hash_check()
        with patch.object(self.manager,'tasks',return_value=[{'id':'check1','state':'succeeded'}]):
            self.assertEqual(self.manager.nas_hash_status()['state'],'ready')
            cfg=self.manager.config('nas')
            with patch.object(self.manager,'_validate',return_value=''):
                self.manager.save_config('nas',{**cfg['data'],'retry_attempts':3},cfg['revision'])
            self.assertEqual(self.manager.nas_hash_status()['state'],'unverified')

    def test_nas_hash_check_rejects_active_upload_and_has_explicit_command(self):
        with patch.object(self.manager,'python',return_value=sys.executable):
            command,cwd=self.manager.command('nas','hash-check')
        self.assertEqual(command[-1],'remote-hash-check')
        cfg=self.manager.config('nas')
        with patch.object(self.manager,'_validate',return_value=''):
            self.manager.save_config('nas',{**cfg['data'],'verification_method':'ssh','remote_hash':{
                **cfg['data']['remote_hash'],'enabled':True,'root_path':'/volume1/share'}},cfg['revision'])
        with patch('manager.locked',return_value=True),patch.object(self.manager,'action') as action:
            with self.assertRaisesRegex(ValueError,'停止'):self.manager.nas_hash_check()
        action.assert_not_called()

    def test_analysis_cache_cleanup_command_uses_shared_maintenance_cli(self):
        with patch.object(self.manager,'python',return_value=sys.executable):
            command,cwd=self.manager.command('sync','clean-analysis-cache')
        self.assertEqual(command,[sys.executable,'-u',str(self.sync/'app/archive_cache.py'),
                                 '--cleanup','--base-dir',str(self.sync)])
        self.assertEqual(cwd,self.sync)
        sync=next(t for t in self.manager.overview()['tools'] if t['id']=='sync')
        self.assertIn('clean-analysis-cache',sync['actions'])

    def test_analysis_cache_cleanup_rejects_busy_worker_before_launch(self):
        queue=self.sync/'runtime/file_analysis/queue.sqlite3'
        queue.parent.mkdir(parents=True,exist_ok=True)
        queue.touch()
        with patch.object(self.manager,'python',return_value=sys.executable), \
             patch('manager.locked',side_effect=lambda p:Path(p)==queue.parent/'worker.lock'), \
             patch('manager.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError,'后台解析'):
                self.manager.action('sync','clean-analysis-cache')
        launch.assert_not_called()

    def test_history_cleanup_status_read_never_launches_cleanup(self):
        with patch('manager.subprocess.Popen',side_effect=AssertionError('read launched cleanup')):
            self.assertEqual(self.manager.history_cleanup_status()['state'],'idle')
        self.assertFalse((self.sync/'runtime/history_retention_status.json').exists())

    def test_manual_history_cleanup_command_is_explicit_and_independent(self):
        with patch.object(self.manager,'python',return_value=sys.executable):
            command,cwd = self.manager.command('sync','clean-history')
        self.assertIn('--execute',command)
        self.assertIn('--progress-pipe',command)
        self.assertEqual(Path(command[2]),self.sync/'app/history_retention.py')
        self.assertEqual(cwd,self.sync)
        self.assertNotIn('scheduler',command)

    def test_manual_history_cleanup_rejects_busy_download_before_launch(self):
        with patch.object(self.manager,'python',return_value=sys.executable), \
             patch('manager.locked',side_effect=lambda p:Path(p).name=='sync.lock'), \
             patch('manager.subprocess.Popen') as launch:
            with self.assertRaisesRegex(ValueError,'同步下载'):
                self.manager.action('sync','clean-history')
        launch.assert_not_called()

    def test_history_cleanup_reports_failed_start_instead_of_polling_forever(self):
        from manager import writing
        with writing(self.manager.tasks_path) as db:
            db.execute("INSERT INTO tasks VALUES('new-job','sync','clean-history','manual','failed',123,2,'2026-11-08','2026-11-08','manual.log')")
        status = self.manager.history_cleanup_status()
        self.assertEqual(status['job_id'],'new-job')
        self.assertEqual(status['state'],'failed')

    def test_history_cleanup_stale_status_does_not_hide_process_exit(self):
        from manager import writing
        self.manager.history_progress=dict(job_id='finished-job',state='running',deleted_files=2,manual_only=True)
        with writing(self.manager.tasks_path) as db:
            db.execute("INSERT INTO tasks VALUES('finished-job','sync','clean-history','manual','succeeded',123,0,'2026-11-08','2026-11-08','manual.log')")
        status=self.manager.history_cleanup_status()
        self.assertEqual(status['state'],'interrupted')
        self.assertEqual(status['deleted_files'],2)

    def test_history_cleanup_status_never_reads_obsolete_state_file(self):
        from manager import writing
        with writing(self.manager.tasks_path) as db:
            db.execute("INSERT INTO tasks VALUES('live-job','sync','clean-history','manual','running',123,NULL,'2026-11-08',NULL,'manual.log')")
        with patch('manager.read_json',side_effect=AssertionError('must use memory')):
            status=self.manager.history_cleanup_status()
        self.assertEqual(status['state'],'running')

    def test_history_cleanup_pipe_updates_memory_and_stops_without_files(self):
        command=[sys.executable,'-u','-c',
                 "import sys,json; job=sys.argv[-1]; "
                 "print('@history-cleanup '+json.dumps(dict(job_id=job,state='running',deleted_files=3)),flush=True); "
                 "request=json.loads(sys.stdin.readline()); "
                 "assert request==dict(command='stop',job_id=job); "
                 "print('@history-cleanup '+json.dumps(dict(job_id=job,state='stopped',deleted_files=3)),flush=True); "
                 "print('cleanup safely stopped',flush=True)"]
        with patch.object(self.manager,'command',return_value=(command,self.sync)):
            task=self.manager.action('sync','clean-history')
        process=self.manager.children[task['id']]
        try:
            import time
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if self.manager.history_cleanup_status().get('deleted_files')==3:
                    break
                time.sleep(.01)
            self.assertEqual(self.manager.history_cleanup_status()['deleted_files'],3)
            with patch('manager.subprocess.Popen',side_effect=AssertionError('stop must use existing pipe')):
                result=self.manager.action('sync','stop-history-cleanup')
            self.assertEqual(result['id'],task['id'])
            process.wait(timeout=5)
            self.assertEqual(self.manager.history_cleanup_status()['state'],'stopped')
            logs=list((self.root/'logs').rglob('*.log'))
            text=logs[0].read_text(encoding='utf-8')
            self.assertNotIn('"state": "running"',text)
            self.assertIn('"state": "stopped"',text)
            self.assertFalse(list((self.sync/'runtime').glob('history_retention*.json*')))
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=5)
            self.manager.tasks()

    def test_history_cleanup_memory_is_not_restored_after_restart(self):
        from manager import writing
        self.manager.history_progress=dict(job_id='old-job',state='running',deleted_files=99)
        with writing(self.manager.tasks_path) as db:
            db.execute("INSERT INTO tasks VALUES('old-job','sync','clean-history','manual','running',123,NULL,'2026-11-08',NULL,'manual.log')")
        restarted=Manager(self.root,self.manager.runtime)
        status=restarted.history_cleanup_status()
        self.assertEqual(status['state'],'interrupted')
        self.assertNotIn('deleted_files',status)

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
        nas=dict(source_root='../downloads',download_ledger='../ledger.sqlite3',target_root='\\\\nas\\share',verification_method='smb',
            start_hour_beijing=12,stop_hour_beijing=0,chunk_bytes=1048576,retry_attempts=2,preserve_previous_versions=True)
        self.manager.config_path('nas').write_text(json.dumps(nas),encoding='utf-8')
        self.manager.config_path('wechat').write_text('{"chats":[],"codex_enabled":false,"allow_send":false}',encoding='utf-8')

    def test_overview_is_read_only_and_reports_missing_cache_as_unknown(self):
        before=self.ledger.read_bytes()
        with patch('manager.subprocess.Popen',side_effect=AssertionError('Read launched a process')):
            info=self.manager.overview()
        self.assertEqual(info['downloads'],{'done':1,'pending':1})
        self.assertIsNone(info['file_inventory'])
        self.assertIsNone(info['download_directory'])
        self.assertIsNone(info['analysis'])
        self.assertEqual(len(info['tools']),4)
        self.assertEqual(self.ledger.read_bytes(),before)

    def test_overview_reads_latest_generated_snapshot_without_rescanning(self):
        folder=self.sync/'logs/2026-11-02'
        folder.mkdir(parents=True)
        directory=dict(total={'files':3,'bytes':9},latest={'files':1,'bytes':3},
                       retained={'files':1,'bytes':3},other={'files':1,'bytes':3},new={'files':0,'bytes':0})
        (folder/'推送状态.json').write_text(json.dumps({'download_directory':directory}),encoding='utf-8')
        body='snapshot text'
        snapshot=dict(snapshot_format='daily-file-snapshot-v1',date='2026-11-02',ready=True,
                      generated_at='2026-11-02T10:40:00+08:00',text=body,
                      body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                      file_inventory=dict(total=directory['total'],downloads=directory['total'],
                                          history=dict(files=0,bytes=0),pending_publication=dict(files=0,bytes=0)))
        (folder/'日报快照.json').write_text(json.dumps(snapshot),encoding='utf-8')
        with patch('manager.now',return_value='2026-11-02T11:00:00+08:00'):
            self.assertEqual(self.manager.overview()['download_directory']['files'],3)
        (self.sync/'downloads/new.pdf').write_bytes(b'new')
        with patch('manager.now',return_value='2026-11-03T11:00:00+08:00'):
            info=self.manager.overview()
            self.assertEqual(info['download_directory']['files'],3)
            self.assertEqual(info['file_inventory']['total']['files'],3)
            self.assertEqual(info['file_inventory']['snapshot_date'],'2026-11-02')

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
        self.assertTrue(any('offline test' in row['message'] for row in self.manager.logs()['items']))

    def test_nas_receives_single_log_capture_flag(self):
        command=[sys.executable,'-c',"import os;print('capture='+os.environ.get('PROJECT_TOOLS_LOG_CAPTURED',''),flush=True)"]
        with patch.object(self.manager,'command',return_value=(command,self.root)):
            task=self.manager.action('nas','check')
        self.manager.children[task['id']].wait(timeout=5)
        self.assertTrue(any('capture=1' in row['message'] for row in self.manager.logs('nas',compact=False)['items']))

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
        for name in ('paths.py','push_settings.py','analysis_settings.py'):
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
        self.assertEqual(config['data']['codex_timeout_seconds'],0)
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
                self.manager.save_config('wechat',{**result['data'],'codex_timeout_seconds':-1},result['revision'])
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
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
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
