from http.server import ThreadingHTTPServer
from contextlib import closing
import json
import sqlite3
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import Request,urlopen

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager,TOOLS
from codex_chat import CodexChat,AppServer,public_item,database,permission_arguments,transport_overrides
from server import handler


class CodexChatTests(unittest.TestCase):
    def test_business_tool_round_trip_through_shared_broker(self):
        identity = self.chat.create()['session']['id']
        text = '生成最新管道简报并发送到微信 business fixture'
        result = dict(id='fixture-task', state='succeeded', result=dict(phase='submitted', sent=True))
        with patch.object(self.chat.project_tools, 'execute', return_value=result) as execute:
            self.chat.send(identity, text, 'business-request-1')
            self.wait(lambda: self.chat.snapshot(identity)['session']['status'] == 'completed')
        execute.assert_called_once()
        args = execute.call_args.args
        self.assertEqual(args[:3], (identity, identity+':user:business-request-1', text))
        self.assertEqual(args[3], {'action':'generate_and_send'})
        snapshot = self.chat.snapshot(identity)
        self.assertTrue(any(e['payload'].get('type') == 'dynamicToolCall' for e in snapshot['events']))
        self.assertIn('submitted', snapshot['messages'][-1]['text'])

    def test_business_tool_failure_is_returned_without_an_external_action(self):
        identity = self.chat.create()['session']['id']
        with patch.object(self.chat.project_tools, 'execute', side_effect=ValueError('没有授权发送')):
            self.chat.send(identity, '生成管道简报，不要发送到微信 business fixture', 'business-request-2')
            self.wait(lambda: self.chat.snapshot(identity)['session']['status'] == 'completed')
        self.assertIn('没有授权发送', self.chat.snapshot(identity)['messages'][-1]['text'])

    def test_web_receives_shared_project_guide_and_fresh_snapshot_on_resumed_turn(self):
        source = self.root/TOOLS['sync']['folder']
        (source/'runtime').mkdir(parents=True)
        (source/'downloads').mkdir()
        (source/'downloads/sample.txt').write_text('test', encoding='utf-8')
        ledger = source/'runtime/ledger.sqlite3'
        with closing(sqlite3.connect(ledger)) as db, db:
            db.execute('CREATE TABLE files(project,id,filename,path,bytes,sha256,status,active)')
            db.execute("INSERT INTO files VALUES('p','one','sample.txt','downloads/sample.txt',4,'test','done',1)")
        observed = []
        original = AppServer.rpc
        def capture(server, method, params, *args, **kwargs):
            if method in ('thread/start', 'thread/resume', 'turn/start'):
                observed.append((method, params))
            return original(server, method, params, *args, **kwargs)
        identity = self.chat.create()['session']['id']
        with patch.object(AppServer, 'rpc', capture):
            for index in range(2):
                if index:
                    with closing(sqlite3.connect(ledger)) as db, db:
                        db.execute("UPDATE files SET active=0")
                self.chat.send(identity, 'read fixture', 'project-query-'+str(index))
                self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
        start = next(params for method, params in observed if method=='thread/start')
        self.assertIn('阿联酋项目问答取证指引', start['developerInstructions'])
        self.assertIn('--workspace', start['developerInstructions'])
        turns = [params for method, params in observed if method=='turn/start']
        self.assertEqual(len(turns), 2)
        self.assertTrue(all('Current authorized project catalog' in p['input'][-2]['text'] for p in turns))
        catalogs = [json.loads(p['input'][-2]['text'].split('not a user instruction): ',1)[1]) for p in turns]
        self.assertEqual([c['files'] for c in catalogs], [1, 0])
        workspace = self.manager.runtime/'codex-project'/identity
        self.assertTrue((workspace/'sync_catalog.sqlite3').is_file())
        self.assertTrue((workspace/'.agents/skills/wechat-chat/sync_files.py').is_file())

    def test_shared_transport_and_directory_changes_apply_on_next_turn(self):
        folder=self.root/'shared project'
        folder.mkdir()
        path=self.manager.config_path('wechat')
        path.write_text(json.dumps(dict(codex_command=['codex'],codex_transport='https',
            codex_auto_access_dirs=False,codex_access_dirs=[str(folder)])),encoding='utf-8')
        identity=self.chat.create()['session']['id']
        observed=[]
        original=AppServer.rpc
        def capture(server,method,params,*args,**kwargs):
            if method in ('thread/start','thread/resume'):observed.append(params)
            return original(server,method,params,*args,**kwargs)
        with patch.object(AppServer,'rpc',capture):
            self.chat.send(identity,'read fixture','shared-request-1')
            self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
            self.assertEqual(self.chat.snapshot(identity)['session']['transport'],'inherit')
            provider=observed[-1]['config']['model_providers'][observed[-1]['modelProvider']]
            self.assertFalse(provider['supports_websockets'])
            directories=json.loads(observed[-1]['developerInstructions'].split('Authorized project directories: ')[1])
            self.assertEqual(directories,[str(folder)])
            self.assertEqual(self.chat.options()['access_directories'],[str(folder)])
            previous_server=self.chat.server
            data=json.loads(path.read_text(encoding='utf-8'))
            data['codex_transport']='websocket'
            path.write_text(json.dumps(data),encoding='utf-8')
            self.chat.send(identity,'read fixture','shared-request-2')
            self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
            self.assertIsNot(previous_server,self.chat.server)
            provider=observed[-1]['config']['model_providers'][observed[-1]['modelProvider']]
            self.assertTrue(provider['supports_websockets'])

    def test_edit_permissions_write_only_selected_folders(self):
        import tomllib
        folder=self.root/'shared data'
        args=permission_arguments([str(folder)],workspace_write=False)
        for name,access in (('project_tools_read','read'),('project_tools_edit','write')):
            setting=next(v.split('=',1)[1] for v in args if v.startswith('permissions.'+name+'='))
            fs=tomllib.loads('p='+setting)['p']['filesystem']
            self.assertEqual(fs[str(folder)],access)
            self.assertEqual(fs[':workspace_roots']['.'],'read')

    def test_web_and_wechat_construct_identical_pool_launch_settings(self):
        from types import SimpleNamespace
        from wechat_local import app_server
        from wechat_local.config import load_config
        from wechat_local.codex_settings import access_directories
        captured = []
        def connect(command, cwd, scope, callback):
            captured.append((command, str(Path(cwd).resolve()), scope))
            return SimpleNamespace(process=SimpleNamespace(pid=123, poll=lambda: None), close=lambda: None)
        self.chat.command_override = None
        executable = str(self.root / 'codex.exe')
        base = self.manager.folder('wechat')
        config = load_config(base / 'wechat.codex.json')
        directories = access_directories(base, None if config.codex_auto_access_dirs else config.codex_access_dirs)
        backend = app_server.ResidentBackend(config, SimpleNamespace())
        try:
            with patch('codex_chat.SharedConnection', side_effect=connect), patch.object(app_server, 'SharedConnection', side_effect=connect), \
                 patch('codex_chat.shutil.which', return_value=executable):
                self.chat.get_server()
                backend.ensure([executable], base, directories)
            self.assertEqual(captured[0][:2], captured[1][:2])
            self.assertEqual([row[2] for row in captured], ['web', 'wechat'])
        finally:
            backend.close()

    def test_transport_settings_preserve_auth_endpoint_and_original_config(self):
        base = {'openai_base_url': 'https://gateway.example/v1'}
        result = transport_overrides(base, 'https')
        provider = result['config']['model_providers'][result['modelProvider']]
        self.assertFalse(provider['supports_websockets'])
        self.assertTrue(provider['requires_openai_auth'])
        self.assertEqual(provider['base_url'], base['openai_base_url'])
        self.assertEqual(base, {'openai_base_url': 'https://gateway.example/v1'})
        custom = dict(model_provider='gateway', model_providers={'gateway': dict(
            name='gateway', base_url='https://gateway.example/v1', env_key='GATEWAY_KEY',
            supports_websockets=False, http_headers={'X-Route': 'project'})})
        override = transport_overrides(custom, 'websocket')['config']['model_providers']['gateway']
        self.assertTrue(override['supports_websockets'])
        self.assertEqual(override['env_key'], 'GATEWAY_KEY')
        self.assertEqual(override['http_headers'], {'X-Route': 'project'})
        self.assertFalse(custom['model_providers']['gateway']['supports_websockets'])
        self.assertEqual(transport_overrides({}, 'auto'), dict(modelProvider='openai'))
        with self.assertRaises(ValueError):transport_overrides({}, 'invalid')
        with self.assertRaises(ValueError):transport_overrides(dict(model_provider='amazon-bedrock'), 'https')

    def test_transport_persists_and_switch_rebinds_existing_thread(self):
        identity = self.chat.create(transport='https')['session']['id']
        self.chat.send(identity, 'read fixture', 'transport-request')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
        session = self.chat.snapshot(identity)['session']
        self.assertEqual(session['transport'], 'https')
        self.assertIn(session['provider_thread'], self.chat.bindings)
        self.chat.configure(identity, transport='auto')
        self.assertNotIn(session['provider_thread'], self.chat.bindings)
        self.chat.send(identity, 'read fixture', 'transport-request-2')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
        self.assertEqual(self.chat.snapshot(identity)['session']['provider_thread'], session['provider_thread'])
        self.assertEqual(self.chat.snapshot(identity)['session']['transport'], 'auto')

    def test_retry_notifications_are_connection_status_until_terminal_failure(self):
        identity = self.chat.create()['session']['id']
        self.chat.bindings['retry-thread'] = identity
        self.chat.status(identity, 'running')
        params = dict(threadId='retry-thread', error=dict(message='Reconnecting... 1/5'), willRetry=True)
        self.chat.notification(dict(method='error', params=params))
        snapshot = self.chat.snapshot(identity)
        self.assertEqual(snapshot['session']['status'], 'running')
        self.assertFalse(any(e['kind']=='error' for e in snapshot['events']))
        self.assertEqual(snapshot['events'][-1]['kind'], 'connection_status')
        self.chat.notification(dict(method='error', params={**params, 'willRetry': False}))
        self.assertEqual(self.chat.events(identity)[-1]['kind'], 'error')

    def test_named_profiles_support_windows_sandbox_and_preserve_write_boundaries(self):
        import tomllib
        for platform, root_access in (('nt', 'read'), ('posix', 'deny')):
            with self.subTest(platform=platform), patch('codex_chat.os.name', platform):
                arguments = permission_arguments()
                for name in ('project_tools_read', 'project_tools_edit'):
                    setting = next(value.split('=',1)[1] for value in arguments if value.startswith('permissions.'+name+'='))
                    profile = tomllib.loads('profile='+setting)['profile']
                    self.assertEqual(profile['filesystem'][':root'],root_access)
                    self.assertEqual(profile['filesystem'][':minimal'],'read')
                    self.assertFalse(profile['network']['enabled'])
                    self.assertEqual(profile['filesystem'][':workspace_roots']['.'], 'read' if name.endswith('read') else 'write')

    def test_missing_rollout_recovers_without_deleting_local_history(self):
        identity = self.chat.create()['session']['id']
        with database(self.chat.path) as db:
            db.execute('UPDATE sessions SET provider_thread=? WHERE id=?',('missing-rollout',identity))
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('previous',identity,'assistant','以前的回复','2026-01-01'))
        self.chat.send(identity,'read fixture','recover-request')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status'] not in ('running','stopping'))
        snapshot=self.chat.snapshot(identity)
        self.assertEqual(snapshot['session']['status'],'completed',snapshot['events'])
        self.assertNotEqual(snapshot['session']['provider_thread'],'missing-rollout')
        self.assertTrue(any(item['text']=='以前的回复' for item in snapshot['messages']))
        self.assertTrue(any(event['kind']=='notice' for event in snapshot['events']))
        self.assertEqual(sum(item['role']=='user' for item in snapshot['messages']),1)

    def test_authentication_errors_do_not_silently_replace_threads(self):
        identity=self.chat.create()['session']['id']
        with database(self.chat.path) as db:
            db.execute('UPDATE sessions SET provider_thread=? WHERE id=?',('auth-failure',identity))
        self.chat.send(identity,'read fixture','auth-request')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='failed')
        self.assertEqual(self.chat.snapshot(identity)['session']['provider_thread'],'auth-failure')

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='codex-chat-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'fixture.txt').write_text('SIMULATION: shop=2, field=1',encoding='utf-8')
        folder=self.root/TOOLS['wechat']['folder']
        folder.mkdir()
        (folder/'wechat.codex.json').write_text('{"codex_command":["codex"]}',encoding='utf-8')
        self.manager=Manager(self.root,self.root/'runtime')
        self.command=[sys.executable,'-u',str(Path(__file__).with_name('fake_codex_server.py'))]
        self.chat=CodexChat(self.manager,self.command)
        self.manager.codex_chat=self.chat
        self.addCleanup(self.chat.close)

    def wait(self,predicate,timeout=6):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            if predicate():return
            with self.chat.condition:self.chat.condition.wait(timeout=.03)
        self.fail('Timed out waiting for streamed state')

    def test_streamed_deltas_arrive_before_completion_and_private_reasoning_is_absent(self):
        identity=self.chat.create()['session']['id']
        self.chat.send(identity,'read fixture','request-123')
        self.wait(lambda:any(e['kind']=='message_delta' for e in self.chat.events(identity)))
        self.assertEqual(self.chat.snapshot(identity)['session']['status'],'running')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
        snapshot=self.chat.snapshot(identity)
        self.assertIn('预制 2 个',snapshot['messages'][-1]['text'])
        self.assertTrue(any(e['kind']=='command_output' for e in snapshot['events']))
        self.assertTrue(any(e['kind']=='summary_delta' for e in snapshot['events']))
        self.assertNotIn('PRIVATE_CHAIN_TEST_TOKEN',json.dumps(snapshot))
        self.assertNotIn('content',public_item(dict(id='x',type='reasoning',summary=['public'],content=['private'])))
        after=snapshot['events'][2]['seq']
        self.assertTrue(all(e['seq']>after for e in self.chat.events(identity,after)))

    def test_duplicate_message_and_concurrent_turn_are_not_replayed(self):
        identity=self.chat.create()['session']['id']
        self.chat.send(identity,'hang','request-123')
        self.assertTrue(self.chat.send(identity,'hang','request-123')['duplicate'])
        with self.assertRaises(ValueError):self.chat.send(identity,'different','request-123')
        with self.assertRaises(ValueError):self.chat.send(identity,'again','request-456')
        second=self.chat.create()['session']['id']
        with self.assertRaises(ValueError):self.chat.send(second,'read','request-999')
        self.wait(lambda:self.chat.snapshot(identity)['session']['turn_id'] is not None)
        self.chat.stop(identity)
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='interrupted')
        self.assertEqual(len(self.chat.snapshot(identity)['messages']),1)

    def test_resume_keeps_conversation_and_stop_allows_next_turn(self):
        identity=self.chat.create()['session']['id']
        self.chat.send(identity,'hang','request-123')
        self.wait(lambda:self.chat.snapshot(identity)['session']['turn_id'] is not None)
        provider=self.chat.snapshot(identity)['session']['provider_thread']
        self.chat.stop(identity)
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='interrupted')
        self.chat.send(identity,'read fixture','request-456')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
        snapshot=self.chat.snapshot(identity)
        self.assertEqual(snapshot['session']['provider_thread'],provider)
        self.assertEqual(len(snapshot['messages']),3)

    def test_permission_and_input_validation(self):
        with self.assertRaises(ValueError):self.chat.create(mode='danger-full-access')
        with self.assertRaises(ValueError):self.chat.create(model='x\ny')
        identity=self.chat.create()['session']['id']
        with self.assertRaises(ValueError):self.chat.send(identity,'','request-123')
        with self.assertRaises(ValueError):self.chat.send(identity,'x'*16001,'request-123')
        self.chat.send(identity,'hang','request-123')
        with self.assertRaises(ValueError):self.chat.configure(identity,mode='workspace-write')
        self.chat.stop(identity)

    def test_empty_session_is_reused_and_session_operations_preserve_history(self):
        identity=self.chat.create()['session']['id']
        self.assertEqual(self.chat.create()['session']['id'],identity)
        self.chat.send(identity,'read fixture','request-123')
        self.wait(lambda:self.chat.snapshot(identity)['session']['status']=='completed')
        messages=self.chat.snapshot(identity)['messages']
        self.chat.session_action(identity,'rename','已核对的资料')
        self.chat.session_action(identity,'archive')
        self.assertNotIn(identity,[r['id'] for r in self.chat.list()])
        self.assertEqual(self.chat.list('archived')[0]['title'],'已核对的资料')
        with self.assertRaisesRegex(ValueError,'恢复'):self.chat.send(identity,'again','request-456')
        self.chat.session_action(identity,'delete')
        self.assertEqual(self.chat.list('archived'),[])
        self.assertEqual(self.chat.list('trash')[0]['id'],identity)
        self.chat.session_action(identity,'restore')
        self.assertEqual(self.chat.list()[0]['id'],identity)
        self.assertEqual(self.chat.snapshot(identity)['messages'],messages)

    def test_running_turn_cannot_be_archived_or_deleted(self):
        identity=self.chat.create()['session']['id']
        self.chat.send(identity,'hang','request-123')
        for action in ('archive','delete'):
            with self.assertRaisesRegex(ValueError,'停止'):self.chat.session_action(identity,action)
        with self.assertRaises(ValueError):self.chat.session_action(identity,'rename','')
        with self.assertRaises(ValueError):self.chat.list('all;DROP TABLE sessions')
        self.chat.stop(identity)

    def test_archive_releases_loaded_thread_and_restore_resumes_persistent_history(self):
        identity = self.chat.create()['session']['id']
        self.chat.send(identity, 'read fixture', 'archive-reuse-1')
        self.wait(lambda: self.chat.snapshot(identity)['session']['status'] == 'completed')
        thread = self.chat.snapshot(identity)['session']['provider_thread']
        pid = self.chat.server.process.pid
        self.chat.session_action(identity, 'archive')
        self.assertNotIn(thread, self.chat.bindings)
        self.assertIsNone(self.chat.server.process.poll())
        self.chat.session_action(identity, 'restore')
        self.chat.send(identity, 'read fixture', 'archive-reuse-2')
        self.wait(lambda: self.chat.snapshot(identity)['session']['status'] == 'completed')
        self.assertEqual(self.chat.snapshot(identity)['session']['provider_thread'], thread)
        self.assertEqual(self.chat.server.process.pid, pid)

    def test_sse_replays_only_requested_session_and_resumes_by_cursor(self):
        identity=self.chat.create()['session']['id']
        self.chat.event(identity,'summary_delta',dict(id='a',delta='public summary'))
        second=self.chat.create()['session']['id']
        self.chat.event(second,'summary_delta',dict(id='b',delta='OTHER_SESSION'))
        self.chat.event(identity,'status',dict(status='completed'))
        after=self.chat.events(identity)[0]['seq']
        server=ThreadingHTTPServer(('127.0.0.1',0),handler(self.manager,'test-token'))
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True);thread.start()
        try:
            request=Request(f'http://127.0.0.1:{server.server_port}/api/codex/events?id={identity}&after={after}',
                            headers={'Cookie':'project_session=test-token'})
            with urlopen(request,timeout=3) as response:
                self.assertIn('text/event-stream',response.headers['Content-Type'])
                lines=[]
                while True:
                    line=response.readline().decode('utf-8');lines.append(line)
                    if line.startswith('data:'):break
                joined=''.join(lines)
                self.assertIn('completed',joined)
                self.assertNotIn('OTHER_SESSION',joined)
                self.assertNotIn('public summary',joined)
        finally:
            server.shutdown();server.server_close();thread.join(timeout=3)


if __name__=='__main__':unittest.main()
