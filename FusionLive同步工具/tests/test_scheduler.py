import csv
from contextlib import closing, contextmanager
from datetime import datetime
import hashlib
import importlib
import io
import itertools
from pathlib import Path
import queue
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from fusion_client import LOCAL, WindowClosed
from ledger import Ledger
from paths import relative_path
import scheduler
from reporting import record_download, volume, write_summary
from sync import download
from progress import Progress, DownloadProgress, PREFIX
import json


class ProgressTests(unittest.TestCase):
    def test_stop_wait_reports_specific_lock_and_does_not_wait_when_free(self):
        checks = [0]
        @contextmanager
        def probe(folder, name):
            checks[0] += 1
            yield not (checks[0] <= 4 and folder.name == 'file_analysis')
        with patch('scheduler.worker_lock', side_effect=probe), patch('scheduler.log') as output, \
             patch('scheduler.time.sleep'):
            scheduler.wait_for_full_exit()
        message = output.call_args.args[0]
        self.assertIn('后台解压解析', message)
        self.assertIn('worker.lock', message)
        self.assertNotIn('下载/注销', message)
        self.assertEqual(checks[0], 8)

    def test_publisher_wait_reports_status_without_thread_stack(self):
        publisher = Mock(ident=123)
        publisher.is_alive.side_effect = [True, False]
        with patch.dict('os.environ', {'FUSIONLIVE_LOG_DETAIL': '0'}), \
             patch('scheduler.sys._current_frames') as frames, patch('scheduler.log') as output:
            scheduler.wait_for_publisher(publisher)
        publisher.join.assert_called_once_with(timeout=.5)
        self.assertIn('等待简报线程安全退出', output.call_args.args[0])
        self.assertNotIn('\n', output.call_args.args[0])
        frames.assert_not_called()
        publisher.terminate.assert_not_called()

    def test_thread_stack_is_opt_in_and_saved_outside_operational_log(self):
        with tempfile.TemporaryDirectory() as temp, \
             patch('scheduler.RUNTIME', Path(temp)), \
             patch.dict('os.environ', {'FUSIONLIVE_LOG_DETAIL': '1'}), \
             patch('scheduler.sys._current_frames', return_value={}), patch('scheduler.log') as output:
            publisher = Mock(ident=123)
            publisher.is_alive.side_effect = [True, False]
            scheduler.wait_for_publisher(publisher, label='同步监控')
            diagnostic = Path(temp)/'scheduler-thread-stacks.txt'
            self.assertIn('同步监控', diagnostic.read_text(encoding='utf-8'))
            self.assertNotIn('\n', output.call_args.args[0])

    def test_pages_records_and_associations_are_distinct(self):
        state = Progress()
        state.accept(PREFIX + json.dumps({'phase': '文档清单', 'directories': 380, 'pages': 410, 'documents': 6179}))
        state.accept(PREFIX + json.dumps({'phase': '原件/附件核对', 'associated_done': 340, 'associated_total': 6179}))
        self.assertIn('清单页 410', state.message())
        self.assertIn('文档记录 6179', state.message())
        self.assertIn('关联已核对 340/6179', state.message())
        state.accept(PREFIX + json.dumps({'associated_done': 400, 'processed': 60, 'elapsed': 60}))
        self.assertIn('关联已核对 400/6179', state.message())
        self.assertIn('估计剩余约 96 分钟', state.message())
        self.assertFalse(state.accept('原件/附件核对：60'))

    def test_stale_operation_is_visible_in_console(self):
        state = Progress()
        state.last_update = 100
        with patch('progress.time.monotonic', return_value=165):
            self.assertIn('距上次进度更新 65 秒', state.message())

    def test_partial_retries_count_transfer_but_only_verified_file_counts_as_done(self):
        clock, events = [100], []
        ledger = Mock()
        ledger.one.return_value = {'total':2,'total_bytes':200000000,'done':1,'done_bytes':100000000}
        with patch('progress.time.monotonic', side_effect=lambda:clock[0]), patch('progress.emit', side_effect=lambda phase,**v:events.append(v)):
            meter = DownloadProgress(ledger, 'p')
            meter.begin({'filename':'large.zip','size':100000000})
            clock[0] = 106
            meter.update(50000000,100000000)
            self.assertEqual(events[-1]['download_done_bytes'],100000000)
            self.assertEqual(events[-1]['current_bytes'],50000000)
            self.assertAlmostEqual(events[-1]['recent_bytes_per_second'],50000000/6)
            clock[0] = 112
            meter.failed()
            self.assertEqual(events[-1]['current_bytes'],0)
            meter.begin({'filename':'large.zip','size':100000000})
            clock[0] = 118
            meter.update(30000000,100000000)
            clock[0] = 124
            meter.complete(100000000)
            self.assertEqual(events[-1]['transfer_bytes'],150000000)
            self.assertEqual(events[-1]['download_done_bytes'],200000000)
            self.assertEqual(events[-1]['download_done_files'],2)
            self.assertEqual(events[-1]['downloads'],1)
            self.assertEqual(events[-1]['current_bytes'],0)
            self.assertAlmostEqual(events[-1]['average_bytes_per_second'],150000000/24)

    def test_chunk_sampling_is_throttled_and_restarts_do_not_reset_cumulative_completion(self):
        ledger = Mock()
        ledger.one.return_value = {'total':10,'total_bytes':1000,'done':4,'done_bytes':400}
        with patch('progress.time.monotonic', return_value=100), patch('progress.emit') as emit:
            meter = DownloadProgress(ledger,'p')
            meter.begin({'filename':'a.pdf','size':100})
            calls = emit.call_count
            for size in range(1,50):
                meter.update(size,100)
            self.assertEqual(emit.call_count,calls)
            self.assertEqual(meter.done_bytes,400)
            self.assertEqual(meter.wire_bytes,49)

    def test_download_console_displays_totals_partial_bytes_and_speed(self):
        state = Progress()
        state.accept(PREFIX+json.dumps({'phase':'文件下载','download_total_files':15421,
            'download_done_files':100,'download_total_bytes':192614468968,'download_done_bytes':2000000000,
            'current_file':'large.zip','current_bytes':500000000,'current_total':1000000000,
            'recent_bytes_per_second':2500000,'average_bytes_per_second':2000000}))
        message = state.message()
        for expected in ('100/15421','2.00/192.61 GB','1.0%','近期 2.50 MB/s','均速 2.00 MB/s','large.zip 500.0/1000.0 MB'):
            self.assertIn(expected,message)

    def test_no_bytes_and_stalled_sampling_do_not_report_a_false_speed(self):
        state = Progress()
        with patch('progress.time.monotonic',return_value=100):
            state.accept(PREFIX+json.dumps({'phase':'文件下载','download_total_files':0,
                'download_done_files':0,'download_total_bytes':0,'download_done_bytes':0,
                'recent_bytes_per_second':10000000,'average_bytes_per_second':0}))
        with patch('progress.time.monotonic',return_value=165):
            self.assertIn('近期 0.00 MB/s',state.message())
            self.assertIn('0.0%',state.message())


class SchedulerTests(unittest.TestCase):
    def test_yesterdays_completion_observed_today_does_not_skip_today(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        at = datetime(2026, 11, 3, 9, tzinfo=LOCAL)
        calls = []
        def start(exiting):
            calls.append(1)
            if len(calls) == 2:
                exiting.set()
            return SimpleNamespace(is_alive=lambda: False, result=0, started_day='2026-11-02')
        sleeps = []
        def sleep(_):
            sleeps.append(1)
            if len(sleeps) > 4:
                self.fail('Yesterday completion prevented today from starting')
        with patch('scheduler.sys.argv', ['scheduler.py']), \
             patch('scheduler.RunLock', return_value=nullcontext()), \
             patch('scheduler.threading.Thread'), patch('scheduler.signal.signal'), \
             patch('scheduler.resume_background_analysis'), patch('scheduler.request_full_stop'), \
             patch('scheduler.wait_for_publisher'), patch('scheduler.wait_for_analysis_exit'), \
             patch('scheduler.log'), patch('scheduler.now', return_value=at), \
             patch('scheduler.time.monotonic', side_effect=itertools.count(0, 61)), \
             patch('scheduler.time.sleep', side_effect=sleep), patch('scheduler.start_sync_task', side_effect=start):
            self.assertEqual(scheduler.main(), 0)
        self.assertEqual(len(calls), 2)

    def test_stop_marker_is_scoped_to_beijing_day(self):
        yesterday = datetime(2026, 11, 2, 23, tzinfo=LOCAL)
        today = datetime(2026, 11, 3, 2, tzinfo=LOCAL)
        with patch('scheduler.now', return_value=yesterday), patch('scheduler.log'):
            scheduler.request_stop()
            self.assertTrue(scheduler.stopped_today(yesterday))
            self.assertFalse(scheduler.stopped_today(today))
            self.assertFalse((scheduler.RUNTIME/'stop.request').exists())
        with patch('scheduler.now', return_value=today):
            scheduler.request_stop()
        self.assertTrue(scheduler.stopped_today(today))

    def test_scheduler_continues_when_its_log_cannot_be_written(self):
        with patch('runtime_logging.BoundedLog.write', side_effect=PermissionError('busy log')), \
             patch('scheduler._log_warned', False), patch('scheduler.sys.stderr', io.StringIO()), \
             patch('scheduler.sys.stdout', io.StringIO()):
            scheduler.log('still scheduling')
            self.assertTrue(scheduler._log_warned)

    def test_resident_scheduler_retries_failed_worker_in_same_window(self):
        from contextlib import nullcontext
        from types import SimpleNamespace
        at = datetime(2026, 11, 2, 9, tzinfo=LOCAL)
        calls = []
        def start(exiting):
            calls.append(1)
            if len(calls) == 2:
                exiting.set()
            return SimpleNamespace(is_alive=lambda: False, result=1)
        with patch('scheduler.sys.argv', ['scheduler.py']), \
             patch('scheduler.RunLock', return_value=nullcontext()), \
             patch('scheduler.threading.Thread'), patch('scheduler.signal.signal'), \
             patch('scheduler.resume_background_analysis'), patch('scheduler.request_full_stop'), \
             patch('scheduler.wait_for_publisher'), patch('scheduler.wait_for_analysis_exit'), \
             patch('scheduler.log'), patch('scheduler.now', return_value=at), \
             patch('scheduler.time.monotonic', side_effect=itertools.count(0, 61)), \
             patch('scheduler.time.sleep'), patch('scheduler.start_sync_task', side_effect=start):
            self.assertEqual(scheduler.main(), 0)
        self.assertEqual(len(calls), 2)

    def setUp(self):
        temporary=tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base=Path(temporary.name)
        for target,value in (('scheduler.HOME',base),('scheduler.RUNTIME',base/'runtime')):
            mocked=patch(target,value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_window_immediate_start_and_one_run_per_day(self):
        for hour, minute, allowed in [(1, 59, False), (2, 0, True), (9, 0, True), (10, 29, True), (10, 30, False), (23, 59, False)]:
            at = datetime(2026, 10, 20, hour, minute, tzinfo=LOCAL)
            self.assertEqual(scheduler.eligible(at), allowed)
            self.assertFalse(scheduler.eligible(at, '2026-10-20'))
        self.assertTrue(scheduler.eligible(datetime(2026, 10, 21, 2, tzinfo=LOCAL), '2026-10-20'))

    def test_next_start_at_month_boundary(self):
        at = datetime(2026, 10, 31, 11, tzinfo=LOCAL)
        self.assertEqual(scheduler.next_start(at), datetime(2026, 11, 1, 2, tzinfo=LOCAL))
        at = datetime(2026, 10, 20, 1, 59, tzinfo=LOCAL)
        self.assertEqual(scheduler.next_start(at), datetime(2026, 10, 20, 2, tzinfo=LOCAL))

    def test_safe_exit_commands_do_not_kill_worker(self):
        exiting, commands = threading.Event(), queue.Queue()
        commands.put('s\n')
        with patch('scheduler.request_stop') as stop, patch('scheduler.log'):
            scheduler.consume_commands(commands, exiting)
            stop.assert_called_once()
            self.assertFalse(exiting.is_set())
            commands.put('q\n')
            scheduler.consume_commands(commands, exiting)
            self.assertTrue(exiting.is_set())

    def run_fake_worker(self, hour, minute, code=2, manual=False, review=False, action='auto'):
        with tempfile.TemporaryDirectory() as temp:
            if review:
                import sqlite3
                with closing(sqlite3.connect(Path(temp) / 'ledger.sqlite3')) as db:
                    db.executescript("CREATE TABLE runs(id INTEGER,status TEXT); INSERT INTO runs VALUES(1,'awaiting_scope'); CREATE TABLE workflows(stage TEXT); INSERT INTO workflows VALUES('scope_review');")
            child = Mock(stdout=io.StringIO('已注销；同一会话访问目录被拒绝。\n'))
            child.poll.side_effect = itertools.chain([None], itertools.repeat(code))
            child.wait.return_value = code
            commands, exiting = queue.Queue(), threading.Event()
            if manual:
                commands.put('q')
            with patch('scheduler.RUNTIME', Path(temp)), patch('scheduler.now', return_value=datetime(2026, 10, 20, hour, minute, tzinfo=LOCAL)), patch('scheduler.subprocess.Popen', return_value=child) as launched, patch('scheduler.log'), patch('scheduler.check_before_login'):
                scheduler.run_worker('fake', 'secret', exiting, commands, action=action)
                self.assertIn(action, launched.call_args.args[0])
                self.assertEqual((Path(temp) / 'login.paused').exists(), code == 3)
            stopped = (Path(temp) / 'stop.request').exists()
            child.terminate.assert_not_called()
            child.kill.assert_not_called()
            child.wait.assert_called()
            return stopped, exiting.is_set()

    def test_deadline_requests_stop_and_waits(self):
        self.assertEqual(self.run_fake_worker(10, 30), (True, False))

    def test_manual_quit_requests_stop_and_waits(self):
        self.assertEqual(self.run_fake_worker(9, 0, manual=True), (True, True))

    def test_unconfirmed_logout_disables_next_login(self):
        self.assertEqual(self.run_fake_worker(9, 0, code=3), (False, False))

    def test_unexpected_worker_exit_does_not_permanently_pause_login(self):
        self.assertEqual(self.run_fake_worker(9, 0, code=5), (False, False))

    def test_scope_review_keeps_publication_scheduler(self):
        self.assertEqual(self.run_fake_worker(9, 0, code=0, review=True), (False, False))

    def test_resume_worker_uses_inventory_only_action(self):
        self.assertEqual(self.run_fake_worker(9, 0, code=0, action='resume-scan'), (False, False))

    def test_failed_integrity_check_never_starts_login_worker(self):
        exiting = threading.Event()
        with patch('scheduler.check_before_login', side_effect=RuntimeError('SHA mismatch')), patch('scheduler.subprocess.Popen') as popen, patch('scheduler.log'):
            self.assertEqual(scheduler.run_worker('fake', 'secret', exiting, queue.Queue()), 4)
            self.assertFalse(exiting.is_set())
            popen.assert_not_called()

    def test_cancelled_startup_check_does_not_launch_or_pause_login(self):
        exiting = threading.Event()
        with patch('scheduler.check_before_login', side_effect=scheduler.CheckCancelled('cancelled')), \
             patch('scheduler.subprocess.Popen') as popen, patch('scheduler.log'):
            self.assertEqual(scheduler.run_worker('fake','secret',exiting,queue.Queue()),2)
            popen.assert_not_called()
        self.assertFalse((scheduler.RUNTIME/'login.paused').exists())


class DownloadAuditTests(unittest.TestCase):
    def setUp(self):
        background=patch('analysis_worker.ensure_background_worker',return_value=None)
        background.start()
        self.addCleanup(background.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base / 'runtime' / 'ledger.sqlite3', base_dir=self.base)
        self.client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(self.client)
        self.ledger.save_page('p', 'r', 1, [{'id': 'v', 'hasContent': 'true', 'contentSize': '3'}], 1, True)
        self.ledger.save_related(self.ledger.one('SELECT * FROM documents'), [{'id': 'v', 'filename': 'x.pdf', 'readAccessStatus': 'Available', 'contentSize': '3'}])

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def test_stream_callback_reports_partial_before_verified_database_completion(self):
        clock, events = [100], []
        def stream(item, target, progress=None, verified=None):
            self.assertIsNotNone(progress)
            clock[0] = 106
            progress(1,3)
            self.assertEqual(self.ledger.one('SELECT status FROM files')[0],'downloading')
            clock[0] = 112
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(b'pdf')
            progress(3,3)
            return {'path':str(target),'bytes':3,'sha256':hashlib.sha256(b'pdf').hexdigest()}
        self.client.download.side_effect = stream
        with patch('sync.check_window'), patch('progress.time.monotonic',side_effect=lambda:clock[0]), patch('progress.emit',side_effect=lambda phase,**v:events.append(v)):
            download(self.client,self.ledger)
        self.assertTrue(any(e['current_bytes']==1 and e['download_done_bytes']==0 for e in events))
        self.assertEqual(events[-1]['download_done_files'],1)
        self.assertEqual(events[-1]['download_done_bytes'],3)
        self.assertEqual(self.ledger.one('SELECT status FROM files')[0],'done')

    def test_interrupted_partial_deleted_target_reused_and_date_recorded(self):
        targets = []
        def interrupted(item, target, progress=None, verified=None):
            targets.append(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.with_name(target.name + '.part').write_bytes(b'incomplete')
            raise WindowClosed('stop')
        self.client.download.side_effect = interrupted
        with patch('sync.check_window'):
            with self.assertRaises(WindowClosed):
                download(self.client, self.ledger)
        self.assertFalse(targets[0].with_name(targets[0].name + '.part').exists())
        self.assertEqual(self.ledger.one('SELECT status FROM files')['status'], 'pending')
        def complete(item, target, progress=None, verified=None):
            targets.append(target)
            target.write_bytes(b'pdf')
            return {'path': str(target), 'bytes': 3, 'sha256': hashlib.sha256(b'pdf').hexdigest()}
        self.client.download.side_effect = complete
        with patch('sync.check_window'):
            download(self.client, self.ledger)
        self.assertEqual(targets[0], targets[1])
        row = self.ledger.one('SELECT * FROM files')
        self.assertTrue(row['downloaded_at'])
        self.assertEqual(row['download_date_source'], '下载完成实录')
        self.assertFalse(Path(row['path']).is_absolute())
        log = next((self.base / 'logs').glob('*/downloads.csv'))
        with log.open(encoding='utf-8-sig', newline='') as stream:
            records = list(csv.reader(stream))
        self.assertEqual([r[4] for r in records[1:]], ['stopped', 'done'])

    def test_startup_removes_partial_after_process_was_killed(self):
        target = self.base / 'downloads' / 'x.pdf'
        target.parent.mkdir()
        part = target.with_name('x.pdf.part')
        part.write_bytes(b'partial')
        target.write_bytes(b'finished')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET target_path=?,status='downloading'", (relative_path(target, self.base),))
        self.ledger.start_run('sync')
        self.assertFalse(part.exists())
        self.assertEqual(target.read_bytes(), b'finished')
        self.assertEqual(self.ledger.one('SELECT status FROM files')['status'], 'pending')

    def test_size_union_deduplicates_main_and_content(self):
        data = volume(self.ledger)
        self.assertEqual(data['main_documents']['bytes'], 3)
        self.assertEqual(data['known_main_and_attachments']['bytes'], 3)

    def test_download_date_survives_rescan_and_clears_for_updated_content(self):
        item = {'id': 'v', 'filename': 'x.pdf', 'readAccessStatus': 'Available', 'contentSize': '3'}
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET downloaded_at='2026-10-20T09:00:00+08:00',download_date_source='下载完成实录'")
        doc = self.ledger.one('SELECT * FROM documents')
        self.ledger.save_related(doc, [item])
        self.assertEqual(self.ledger.one('SELECT downloaded_at FROM files')['downloaded_at'], '2026-10-20T09:00:00+08:00')
        item['revision'] = 'new'
        self.ledger.save_related(doc, [item])
        row = self.ledger.one('SELECT downloaded_at,download_date_source,status FROM files')
        self.assertEqual(tuple(row), (None, None, 'pending'))

    def test_gui_queries_filters_dates_and_placeholders_offline(self):
        gui = importlib.import_module('台账')
        app = gui.App.__new__(gui.App)
        app.folder = None
        app.file_paths = {}
        app.search = Mock(get=Mock(return_value=''))
        app.filter = Mock()
        app.download_since = Mock()
        app.session = Mock(get=Mock(return_value=''))
        class Table:
            def __init__(self): self.rows = []
            def get_children(self): return ()
            def delete(self, *args): self.rows.clear()
            def insert(self, *args, values): self.rows.append(values); return str(len(self.rows))
        for name in ('docs', 'files', 'runs', 'changes'):
            setattr(app, name, Table())
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='done',downloaded_at='2026-10-20T09:00:00+08:00'")
        with patch.object(gui, 'DB_PATH', self.base / 'runtime' / 'ledger.sqlite3'):
            for state in ['全部', '未下载', '已完成', '失败', '附件待枚举', '未上传内容', '本轮新增或变更']:
                for since in ['', '2026-10-20', '2026-10-21']:
                    app.filter.get.return_value = state
                    app.download_since.get.return_value = since
                    app.refresh_rows()
                    if since == '2026-10-21':
                        self.assertEqual(app.files.rows, [])
            app.filter.get.return_value = '未上传内容'
            app.download_since.get.return_value = ''
            with self.ledger.db:
                self.ledger.db.execute('UPDATE documents SET metadata=?', ('{"hasContent":"false","hasAttachments":"false"}',))
            app.refresh_rows()
            self.assertIn('未上传内容', app.docs.rows[0][3])


if __name__ == '__main__':
    unittest.main()
