from http.cookiejar import CookieJar
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
import sqlite3
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import Manager
from server import handler
import ima_control


class ImaControlTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = Manager(self.root, self.root / 'admin-runtime')
        self.base = self.manager.folder('ima')
        (self.base / 'runtime').mkdir(parents=True)

    def test_snapshot_is_local_read_only_and_does_not_return_keys(self):
        path = self.manager.config_path('ima')
        path.write_text(json.dumps({'api_key': 'never-echo-key', 'client_id': 'never-echo-client'}), encoding='utf-8')
        (self.base / 'runtime/credentials.dat').write_bytes(b'encrypted-credential-fixture')
        with patch('subprocess.run', side_effect=AssertionError('status must not start a process')), \
             patch('subprocess.Popen', side_effect=AssertionError('status must not start a process')):
            value = ima_control.snapshot(self.manager)
        self.assertTrue(value['credentials_configured'])
        self.assertNotIn('never-echo', json.dumps(value))
        self.assertEqual(value['state'], 'stopped')
        self.assertFalse((self.base / '待同步').exists())

    def test_config_is_saved_to_original_tool_and_revision_conflicts_are_rejected(self):
        first = self.manager.config('ima')
        data = {**first['data'], 'interval_seconds': 120}
        saved = self.manager.save_config('ima', data, first['revision'])
        self.assertEqual(saved['data']['interval_seconds'], 120)
        self.assertTrue(self.manager.config_path('ima').is_file())
        with self.assertRaisesRegex(ValueError, '修改'):
            self.manager.save_config('ima', data, first['revision'])
        with self.assertRaises(ValueError):
            self.manager.save_config('ima', {**data, 'api_key': 'not-allowed'}, saved['revision'])

    def test_daily_time_defaults_migrate_and_validate(self):
        self.manager.config_path('ima').write_text('{"interval_seconds": 60}', encoding='utf-8')
        current = self.manager.config('ima')
        self.assertEqual(current['data']['sync_time'], '13:00')
        saved = self.manager.save_config('ima', {'sync_time': '14:30'}, current['revision'])
        self.assertEqual(saved['data']['sync_time'], '14:30')
        for value in ('24:00', '1:00', '13:60', '', None, 1300):
            with self.assertRaisesRegex(ValueError, '每日同步时间'):
                self.manager.save_config('ima', {'sync_time': value}, saved['revision'])

    def test_existing_excel_settings_upgrade_to_database_without_rewriting_runtime_config(self):
        path = self.manager.config_path('ima')
        downloads = self.root / 'custom/downloads'
        original = json.dumps({'downloads_path': str(downloads), 'ledger_path': ''})
        path.write_text(original, encoding='utf-8')
        current = self.manager.config('ima')
        self.assertEqual(current['data']['database_path'], str(downloads.parent / 'runtime/ledger.sqlite3'))
        self.assertEqual(path.read_text(encoding='utf-8'), original)
        database = self.root / 'chosen/runtime/ledger.sqlite3'
        saved = self.manager.save_config('ima', {'database_path': str(database)}, current['revision'])
        self.assertEqual(saved['data']['database_path'], str(database))

    def test_legacy_collection_range_is_hidden_and_removed_when_saving(self):
        path = self.manager.config_path('ima')
        legacy = {'collect_scope': 'project', 'collect_attachments': True, 'collect_guides': True}
        path.write_text(json.dumps(legacy), encoding='utf-8')
        current = self.manager.config('ima')
        self.assertFalse(set(legacy) & current['data'].keys())
        self.manager.save_config('ima', current['data'], current['revision'])
        self.assertFalse(set(legacy) & json.loads(path.read_text(encoding='utf-8')).keys())
        with self.assertRaisesRegex(ValueError, '不支持'):
            value = self.manager.config('ima')
            self.manager.save_config('ima', {**value['data'], 'collect_scope': 'project'}, value['revision'])

    def test_manual_mode_persists_requests_stop_and_blocks_automatic_start(self):
        current = self.manager.config('ima')
        self.assertTrue(current['data']['auto_sync_enabled'])
        saved = self.manager.save_config('ima', {'auto_sync_enabled': False}, current['revision'])
        self.assertFalse(saved['data']['auto_sync_enabled'])
        self.assertTrue((self.base / 'runtime/watch-stop.request').is_file())
        with patch.object(self.manager, 'python', return_value=sys.executable), \
             patch('subprocess.Popen', side_effect=AssertionError('must not launch')):
            with self.assertRaisesRegex(ValueError, '自动同步已关闭'):
                self.manager.action('ima', 'start')
        with self.assertRaisesRegex(ValueError, '布尔'):
            self.manager.save_config('ima', {'auto_sync_enabled': 'false'}, saved['revision'])
        self.assertFalse(self.manager.config('ima')['data']['auto_sync_enabled'])

    def test_manual_sync_can_run_when_automatic_sync_is_disabled(self):
        current = self.manager.config('ima')
        self.manager.save_config('ima', {'auto_sync_enabled': False}, current['revision'])
        with patch.object(self.manager, 'python', return_value=sys.executable), \
             patch('subprocess.Popen') as launch:
            launch.return_value.pid = 12345
            result = self.manager.action('ima', 'sync-once')
        self.assertEqual(result['state'], 'running')
        self.assertEqual(launch.call_args.args[0][-2:], ['sync', '--execute'])

    def test_portable_credentials_are_reported_ready_without_exposing_or_consuming_them(self):
        path = self.base / 'credentials.bootstrap.json'
        content = json.dumps({'client_id': 'private-client', 'api_key': 'private-key'})
        path.write_text(content, encoding='utf-8')
        value = ima_control.snapshot(self.manager)
        self.assertTrue(value['credentials_configured'])
        self.assertNotIn('private-', json.dumps(value))
        self.assertEqual(path.read_text(encoding='utf-8'), content)

    def test_all_ima_actions_use_headless_cli_and_unknown_action_is_rejected(self):
        with patch.object(self.manager, 'python', return_value=sys.executable):
            for action in ('start', 'stop', 'check', 'collect', 'plan', 'sync-once'):
                command, cwd = self.manager.command('ima', action)
                self.assertEqual(cwd, self.base)
                self.assertEqual(command[2:4], ['-m', 'ima_manager'])
                self.assertNotIn('gui', command)
            self.assertEqual(self.manager.command('ima', 'start')[0][-2:], ['watch', '--execute'])
            with self.assertRaises(ValueError):
                self.manager.command('ima', 'delete-all')

    def test_credentials_only_travel_over_stdin_and_never_return_to_browser(self):
        result = subprocess.CompletedProcess([], 0, '{"saved":true}', '')
        with patch.object(self.manager, 'python', return_value=sys.executable), \
             patch('ima_control.subprocess.run', return_value=result) as run:
            response = ima_control.query(self.manager, 'credentials', {'client_id': 'fixture-client', 'api_key': 'fixture-key'})
        self.assertEqual(response, {'saved': True})
        command = run.call_args.args[0]
        self.assertNotIn('fixture-key', ' '.join(command))
        self.assertEqual(json.loads(run.call_args.kwargs['input'])['data']['api_key'], 'fixture-key')
        failed = subprocess.CompletedProcess([], 1, '', 'fixture-key accidental validation output')
        with patch.object(self.manager, 'python', return_value=sys.executable), \
             patch('ima_control.subprocess.run', return_value=failed):
            with self.assertRaises(ValueError) as error:
                ima_control.query(self.manager, 'credentials', {'api_key': 'fixture-key'})
        self.assertNotIn('fixture-key', str(error.exception))

    def test_unknown_query_never_starts_a_process(self):
        with patch('subprocess.run', side_effect=AssertionError('unknown query executed')):
            with self.assertRaises(ValueError):
                ima_control.query(self.manager, 'delete-all', {})

    def test_plan_and_collection_page_past_old_limit_and_filter_before_paging(self):
        files = [dict(name=f'file-{index:04}.pdf', relative=f'项目/file-{index:04}.pdf',
                      status='error' if index % 3 == 0 else 'ready', reason='源文件缺失' if index % 3 == 0 else '')
                 for index in range(625)]
        for kind, filename in [('plan', 'latest-plan.json'), ('collection', 'collection-report.json')]:
            (self.base / 'runtime' / filename).write_text(json.dumps({'files': files}), encoding='utf-8')
            with patch('subprocess.run', side_effect=AssertionError('must remain local')):
                page = ima_control.records(self.manager, kind, 600, 20)
            self.assertEqual(page['total'], 625)
            self.assertEqual(page['items'][0]['name'], 'file-0600.pdf')
            self.assertEqual(len(page['items']), 20)
            selected = ima_control.records(self.manager, kind, 0, 20, 'FILE-06', 'error')
            self.assertEqual(selected['total'], 9)
            self.assertTrue(all(row['status'] == 'error' and row['name'].startswith('file-06') for row in selected['items']))
            last = ima_control.records(self.manager, kind, 10000, 20)
            self.assertEqual(last['offset'], 620)
            self.assertEqual(len(last['items']), 5)
        summary = ima_control.snapshot(self.manager)
        self.assertEqual(summary['plan']['total_files'], 625)
        self.assertEqual(sum(summary['plan']['counts'].values()), 625)
        self.assertEqual(len(summary['plan']['files']), 20)
        self.assertIn('updated_at', summary['plan'])

    def test_history_pages_past_200_without_creating_or_exposing_credentials(self):
        path = self.base / 'runtime/state.sqlite3'
        empty = ima_control.records(self.manager, 'history')
        self.assertEqual(empty['total'], 0)
        self.assertFalse(path.exists())
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE receipts (account TEXT,kb TEXT,folder TEXT,path TEXT,status TEXT,media_id TEXT,note TEXT,updated REAL)')
            db.executemany('INSERT INTO receipts VALUES (?,?,?,?,?,?,?,?)',
                [('private-account', 'kb', '', f'file-{index}.pdf', 'done' if index % 2 else 'failed', '', '', index)
                 for index in range(260)])
        result = ima_control.records(self.manager, 'history', 220, 20)
        self.assertEqual(result['total'], 260)
        self.assertEqual(result['items'][0]['path'], 'file-39.pdf')
        self.assertNotIn('private-account', json.dumps(result))
        self.assertEqual(ima_control.records(self.manager, 'history', 0, 20, status='failed')['total'], 130)
        self.assertEqual(ima_control.records(self.manager, 'history', 0, 20, "' OR 1=1 --")['total'], 0)

    def test_task_records_page_beyond_30_and_do_not_include_other_tools(self):
        with closing(sqlite3.connect(self.manager.tasks_path)) as db, db:
            db.executemany('INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?,?,?)',
                [(f'task-{index}', 'ima' if index < 65 else 'sync', 'plan', '预览上传计划', 'failed',
                  0, 1, '2026-11-04T19:00:00+08:00', None, f'task-{index}.log') for index in range(70)])
        result = ima_control.records(self.manager, 'tasks', 40, 20, status='failed')
        self.assertEqual(result['total'], 65)
        self.assertEqual(len(result['items']), 20)
        self.assertTrue(all(row['tool'] == 'ima' for row in result['items']))

    def test_journal_pagination_keeps_full_history_and_local_cloud_distinction(self):
        source = Path(self.manager.config('ima')['data']['source'])
        source.mkdir()
        path = source / '每日更新记录.txt'
        text = 'ima 每日文件更新记录（北京时间）\n格式：时间 | 操作 | 文件 | 结果与版本\n'
        text += ''.join(f'2026-11-04 12:00:00 | 提取新增 | file-{index}.pdf | 成功 | 版本=abc\n' for index in range(1000))
        text += '2026-11-04 13:00:00 | 同步任务 | （任务） | 失败 | ima HTTP 403\n'
        path.write_text(text, encoding='utf-8')
        before = path.read_bytes()
        result = ima_control.records(self.manager, 'journal')
        self.assertEqual(result['total'], 1001)
        self.assertEqual(result['items'][0]['operation'], '同步任务')
        self.assertEqual(ima_control.records(self.manager, 'journal', 1000, 20)['items'][0]['path'], 'file-0.pdf')
        self.assertEqual(ima_control.records(self.manager, 'journal', 0, 20, status='失败')['total'], 1)
        self.assertEqual(path.read_bytes(), before)

    def test_records_reject_unknown_kind_and_invalid_page(self):
        for kind, offset, limit in [('credentials', 0, 20), ('plan', -1, 20), ('plan', 0, 101)]:
            with self.assertRaises(ValueError):
                ima_control.records(self.manager, kind, offset, limit)

    def test_duplicate_mutating_action_is_blocked_while_watch_runs(self):
        with patch.object(self.manager, 'python', return_value=sys.executable), \
             patch.object(self.manager, 'live', return_value='external'), \
             patch('subprocess.Popen', side_effect=AssertionError('must not launch')):
            with self.assertRaisesRegex(ValueError, '正在运行'):
                self.manager.action('ima', 'sync-once')

    def test_ima_http_routes_require_session_and_csrf(self):
        static = self.root / 'static'
        static.mkdir()
        (static / 'index.html').write_text('fixture', encoding='utf-8')
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler(self.manager, 'fixture-session', static))
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        url = f'http://127.0.0.1:{server.server_port}'
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        try:
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(url + '/api/ima')
            self.assertEqual(error.exception.code, 403)
            opener.open(url + '/api/session?token=fixture-session').close()
            with opener.open(url + '/api/ima') as response:
                self.assertIn('credentials_configured', json.load(response))
            with opener.open(url + '/api/ima/records?kind=plan&offset=0&limit=20') as response:
                self.assertEqual(json.load(response)['total'], 0)
            with self.assertRaises(urllib.error.HTTPError) as invalid:
                opener.open(url + '/api/ima/records?kind=plan&limit=101')
            self.assertEqual(invalid.exception.code, 400)
            payload = json.dumps({'operation': 'scan', 'data': {}}).encode()
            with self.assertRaises(urllib.error.HTTPError) as error:
                opener.open(urllib.request.Request(url + '/api/ima/query', data=payload, method='POST'))
            self.assertEqual(error.exception.code, 403)
            with patch('ima_control.query', return_value={'items': []}) as query:
                request = urllib.request.Request(url + '/api/ima/query', data=payload,
                    headers={'X-Admin-Request': '1', 'Content-Type': 'application/json'}, method='POST')
                with opener.open(request) as response:
                    self.assertEqual(json.load(response), {'items': []})
                query.assert_called_once_with(self.manager, 'scan', {})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
