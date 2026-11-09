"""Queue concurrency tests simulate file publication, never execute downloads."""
from contextlib import closing
from contextlib import redirect_stdout
import io
import csv
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import zipfile
from datetime import datetime, timezone, timedelta

from analysis_worker import AnalysisQueue, ProgressLog, activity, cached_snapshot, worker_lock, notify_download_completed, ensure_background_worker
from briefing import generate
from tests.test_local_analysis import PCF


class WorkerTests(unittest.TestCase):
    def test_unchanged_discovery_does_not_rewrite_jobs_or_reexport_after_restart(self):
        self.publish()
        self.queue.discover()
        self.queue.process_next()
        self.queue.mark_exported('ledger')
        self.queue.mark_exported()
        self.queue._export_dirty = False
        before = self.queue.db.total_changes
        self.queue.discover()
        self.assertEqual(self.queue.db.total_changes, before)
        self.assertFalse(self.queue.snapshot_pending())
        reopened = AnalysisQueue(self.database)
        try:
            reopened.discover()
            self.assertFalse(reopened._export_dirty)
            self.assertFalse(reopened.snapshot_pending())
            self.assertEqual(reopened.counts(), {'done': 1})
        finally:
            reopened.close()

    def test_busy_initial_discovery_defers_workbook_in_favour_of_parsing(self):
        reader = Mock()
        reader.execute.return_value = [('documents',), ('folders',), ('document_files',)]
        clock = [100]
        self.queue._export_dirty = True
        with patch('archive_index.readonly', return_value=reader), \
             patch.object(self.queue, 'counts', return_value={'pending': 1}), \
             patch('analysis_worker.time.monotonic', side_effect=lambda: clock[0]), \
             patch('sync.export_csv', return_value='fixture.xlsx') as export:
            self.queue.refresh_ledger_export()
            export.assert_not_called()
            clock[0] = 1001
            self.queue.refresh_ledger_export()
            export.assert_called_once()

    def test_unavailable_source_changes_invalidate_snapshot_even_without_new_jobs(self):
        self.publish(state='pending')
        self.assertEqual(self.queue.discover(), 0)
        self.queue.mark_exported()
        self.queue.mark_exported('ledger')
        self.queue._export_dirty = False
        self.assertFalse(self.queue.snapshot_pending())
        self.publish('not-yet-downloaded', state='pending')
        self.assertEqual(self.queue.discover(), 0)
        self.assertTrue(self.queue.snapshot_pending())

    def test_snapshot_looks_up_current_keys_without_scanning_historical_jobs(self):
        self.publish()
        self.queue.discover()
        self.queue.process_next()
        with self.queue.db:
            self.queue.db.executemany('''INSERT INTO jobs(key,project,file_id,path,origin,state,active,updated)
                VALUES(?,'history',?,'missing','history','done',0,0)''',
                [(f'{i:064x}', str(i)) for i in range(30000)])
        connect = sqlite3.connect
        def bounded(*args, **kwargs):
            db = connect(*args, **kwargs)
            if 'queue.sqlite3' in str(args[0]):
                ticks = [0]
                def budget():
                    ticks[0] += 1
                    return int(ticks[0] > 10)
                db.set_progress_handler(budget, 1000)
            return db
        with patch('analysis_worker.sqlite3.connect', side_effect=bounded):
            _, report, _ = self.snapshot()
        self.assertEqual(report['completed_analysis_files'], 1)
        self.assertEqual(report['pending_analysis_files'], 0)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = self.base / 'runtime/ledger.sqlite3'
        self.database.parent.mkdir()
        self.downloads = self.base / 'downloads'
        self.downloads.mkdir()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE files(project,id,filename,path,status,bytes,sha256,active,PRIMARY KEY(project,id))')
            db.execute('CREATE TABLE documents(project,id,reference,filename,title,metadata,active)')
        self.queue = AnalysisQueue(self.database)
        self.addCleanup(self.queue.close)
        self.socket = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.socket.start()
        self.addCleanup(self.socket.stop)

    def publish(self, identifier='one', content=PCF, state='done'):
        path = self.downloads / f'{identifier}.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('pipe.pcf', content)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,1)',
                       ('p', identifier, path.name, f'downloads/{path.name}', state,
                        path.stat().st_size, hashlib.sha256(path.read_bytes()).hexdigest()))
        return path

    def test_failed_workbook_export_is_throttled_while_analysis_continues(self):
        from unittest.mock import Mock
        reader=Mock()
        reader.execute.return_value=[('documents',),('folders',),('document_files',)]
        clock=[100]
        self.queue._export_dirty=True
        self.queue._last_export=-1000  # Already past the initial busy grace interval.
        with patch('archive_index.readonly',return_value=reader), \
             patch.object(self.queue,'counts',return_value={'pending':1}), \
             patch('analysis_worker.time.monotonic',side_effect=lambda:clock[0]), \
             patch('sync.export_csv',side_effect=PermissionError('unavailable output folder')) as export:
            self.queue.refresh_ledger_export()
            clock[0]=101
            self.queue.refresh_ledger_export()
            self.assertEqual(export.call_count,1)
            self.assertTrue(self.queue._export_dirty)
            clock[0]=1001
            self.queue.refresh_ledger_export()
            self.assertEqual(export.call_count,2)

    def test_unchanged_ledger_poll_skips_full_scan_but_new_commit_is_discovered(self):
        import local_analysis
        self.publish()
        with patch('local_analysis.ledger_sources', wraps=local_analysis.ledger_sources) as scan:
            self.assertEqual(self.queue.discover_if_changed(), 1)
            self.assertEqual(self.queue.discover_if_changed(), 0)
            self.assertEqual(scan.call_count, 1)
            self.publish('new')
            self.assertEqual(self.queue.discover_if_changed(), 1)
            self.assertEqual(scan.call_count, 2)
            self.queue._last_full_scan = float('-inf')
            self.queue.discover_if_changed()
            self.assertEqual(scan.call_count, 3)

    def snapshot(self):
        return cached_snapshot(self.database, output=self.base / 'snapshots')

    def stage(self, filename, data):
        path = self.base / 'runtime/download_ready/one/content'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,1)',
                       ('p', 'one', filename, path.relative_to(self.base).as_posix(),
                        'done', len(data), hashlib.sha256(data).hexdigest()))
        return path

    def test_staged_pdf_worker_recovers_failed_job_without_idf_parser(self):
        self.stage('drawing.PDF', b'%PDF-1.7\nfixture')
        self.queue.discover()
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET state='failed',error='IDF未识别到-6管线记录。',attempts=3,retryable=0")
        self.queue.discover()
        with patch('local_analysis.parse_file', side_effect=AssertionError('PDF entered pipe parser')):
            self.assertTrue(self.queue.process_next())
        job = self.queue.db.execute('SELECT * FROM jobs WHERE active=1').fetchone()
        self.assertEqual(job['state'], 'done', job['error'])
        report = json.loads((self.queue.cache / job['result_path']).read_text(encoding='utf-8'))
        self.assertEqual(report['files'][0]['format'], '.pdf')

    def test_staged_pcf_worker_uses_pcf_parser(self):
        self.stage('pipe.pcf', PCF.encode('utf-8'))
        self.queue.discover()
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET state='failed',error='IDF未识别到-6管线记录。',attempts=3,retryable=0")
        self.queue.discover()
        self.assertTrue(self.queue.process_next())
        job = self.queue.db.execute('SELECT * FROM jobs WHERE active=1').fetchone()
        self.assertEqual(job['state'], 'done', job['error'])
        self.assertEqual(self.queue.db.execute('SELECT source FROM pipeline_inventory').fetchone()[0], 'PCF')

    def test_staged_zip_worker_uses_archive_parser_after_queue_reopen(self):
        content = io.BytesIO()
        with zipfile.ZipFile(content, 'w') as archive:
            archive.writestr('pipe.pcf', PCF)
        self.stage('drawing.zip', content.getvalue())
        self.queue.discover()
        reopened = AnalysisQueue(self.database)
        try:
            self.assertTrue(reopened.process_next())
            job = reopened.db.execute('SELECT * FROM jobs WHERE active=1').fetchone()
            self.assertEqual(job['state'], 'done', job['error'])
            self.assertEqual(reopened.db.execute('SELECT source FROM pipeline_inventory').fetchone()[0], 'PCF')
        finally:
            reopened.close()

    def test_pipe_parser_rejects_pdf_and_unknown_extension_before_idf_read(self):
        from local_analysis import parse_file
        path = self.stage('drawing.pdf', b'%PDF-1.7\nfixture')
        for name in ('drawing.pdf', 'content'):
            with self.subTest(name=name), patch('local_analysis.idf.read_text', side_effect=AssertionError('IDF read PDF')):
                with self.assertRaisesRegex(ValueError, '不支持的管线文件格式'):
                    parse_file(path, self.base / 'split', logical_name=name)

    def test_weld_export_uses_current_deduplicated_cached_totals(self):
        self.publish()
        self.publish('duplicate')
        self.queue.discover()
        while self.queue.process_next():
            pass
        with patch('local_analysis.parse_file', side_effect=AssertionError('export reparsed input')):
            groups, report, _ = self.snapshot()
        target = self.base / 'exports/焊口表.csv'
        self.assertEqual(Path(report['weld_table_file']), target)
        self.assertTrue(target.read_bytes().startswith(b'\xef\xbb\xbf'))
        with target.open(encoding='utf-8-sig', newline='') as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual(len(rows), groups['p', 'H4-262', 'PCF']['焊缝数'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['焊口号'], report['welds'][0]['焊口号'])
        self.assertEqual(rows[0]['分类规则'], report['welds'][0]['_classification_rule'])
        self.assertEqual(rows[0]['焊点类型'], {'shop': 'S', 'field': 'F'}[report['welds'][0]['分类']])
        self.assertEqual(rows[0]['汇总状态'], '完整')
        # A new version invalidates both cached inputs; stale rows must disappear.
        self.publish(content=PCF.replace('L001', 'L002'))
        self.publish('duplicate', content=PCF.replace('L001', 'L002'))
        self.queue.discover()
        _, report, _ = self.snapshot()
        self.assertFalse(report['analysis_complete'])
        with target.open(encoding='utf-8-sig', newline='') as stream:
            self.assertEqual(list(csv.DictReader(stream)), [])

    def test_new_files_can_arrive_while_another_archive_is_being_extracted(self):
        self.publish()
        self.queue.discover()
        import local_analysis
        original_extract = local_analysis.extract_zip
        started, release = threading.Event(), threading.Event()
        errors = []
        def slow_extract(*args, **kwargs):
            started.set()
            if not release.wait(10):
                raise AssertionError('test extraction was not released')
            return original_extract(*args, **kwargs)
        def process():
            queue = AnalysisQueue(self.database)
            try:
                queue.process_next()
            except BaseException as exc:
                errors.append(exc)
            finally:
                queue.close()
        with patch('local_analysis.extract_zip', side_effect=slow_extract):
            worker = threading.Thread(target=process)
            worker.start()
            try:
                self.assertTrue(started.wait(5))
                # A separate connection publishes a complete file while extraction blocks.
                self.publish('two', PCF.replace('L001', 'L002'))
                self.assertEqual(self.queue.discover(), 1)
                self.assertEqual(self.queue.db.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0], 1)
                _, pending, _ = self.snapshot()
                self.assertEqual(pending['pending_analysis_files'], 2)
                self.assertFalse(pending['analysis_complete'])
            finally:
                release.set()
                worker.join(10)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors, [])
        self.assertTrue(self.queue.process_next())
        self.assertFalse(self.queue.process_next())
        groups, report, _ = self.snapshot()
        self.assertEqual(groups['p', 'H4-262', 'PCF']['管线数'], 2)
        self.assertTrue(report['analysis_complete'])

    def test_unchanged_files_are_cached_and_report_never_reextracts(self):
        original = self.publish().read_bytes()
        self.assertEqual(self.queue.discover(), 1)
        self.assertTrue(self.queue.process_next())
        self.assertEqual(self.queue.discover(), 0)
        self.assertFalse(self.queue.process_next())
        with patch('local_analysis.extract_zip', side_effect=AssertionError('report tried to extract')), \
             patch('local_analysis.parse_file', side_effect=AssertionError('report tried to parse')):
            groups, report, _ = self.snapshot()
            state = generate(self.database, self.base / 'reports', use_cache=True)
        self.assertEqual(groups['p', 'H4-262', 'PCF']['焊缝数'], 1)
        self.assertTrue(state['ready'])
        self.assertTrue(state['analysis_complete'])
        self.assertEqual((self.downloads / 'one.zip').read_bytes(), original)

    def test_updated_file_invalidates_previous_result_but_partial_report_is_sendable(self):
        self.publish()
        self.queue.discover()
        self.queue.process_next()
        self.publish(content=PCF.replace('L001', 'L002'))
        self.assertEqual(self.queue.discover(), 1)
        groups, report, _ = self.snapshot()
        self.assertEqual(groups, {})
        self.assertFalse(report['analysis_complete'])
        state = generate(self.database, self.base / 'reports', use_cache=True)
        self.assertTrue(state['ready'])
        self.assertFalse(state['analysis_complete'])
        rerender = generate(None, self.base / 'rerender', workbook=state['workbook'])
        self.assertTrue(rerender['ready'])
        self.assertFalse(rerender['analysis_complete'])
        self.queue.process_next()
        groups, report, _ = self.snapshot()
        self.assertTrue(report['analysis_complete'])
        self.assertEqual(groups['p', 'H4-262', 'PCF']['管线数'], 1)

    def test_partial_and_downloading_files_are_not_processed(self):
        self.publish(state='downloading')
        self.assertEqual(self.queue.discover(), 0)
        self.assertFalse(self.queue.process_next())
        _, report, _ = self.snapshot()
        self.assertFalse(report['analysis_complete'])
        self.assertEqual(report['unavailable_local_files'], 1)

    def test_interrupted_job_is_recovered_and_corrupt_zip_is_not_ready(self):
        self.publish()
        self.queue.discover()
        with self.queue.db:
            self.queue.db.execute("UPDATE jobs SET state='running'")
        self.queue.recover()
        self.assertTrue(self.queue.process_next())
        (self.downloads / 'bad.zip').write_bytes(b'not a zip')
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("INSERT INTO files VALUES('p','bad','bad.zip','downloads/bad.zip','done',9,NULL,1)")
        self.queue.discover()
        self.queue.process_next()
        _, report, _ = self.snapshot()
        self.assertEqual(report['failed_analysis_files'], 1)
        self.assertFalse(report['analysis_complete'])

    def test_single_process_lock_and_isolated_spawn(self):
        cache = self.base / 'runtime/file_analysis'
        with worker_lock(cache) as first:
            self.assertTrue(first)
            with worker_lock(cache) as second:
                self.assertFalse(second)
        with patch('analysis_worker._child', None), patch('analysis_worker.subprocess.Popen', return_value=Mock()) as spawn, \
             patch('analysis_worker.analysis_paused_for_schedule', return_value=False), \
             patch.dict('os.environ', {'FUSIONLIVE_PASSWORD': 'test-secret', 'FUSIONLIVE_USERNAME': 'test-user'}):
            ensure_background_worker(self.base, self.database)
            self.assertNotIn('FUSIONLIVE_PASSWORD', spawn.call_args.kwargs['env'])
            self.assertNotIn('FUSIONLIVE_USERNAME', spawn.call_args.kwargs['env'])
        with patch('analysis_worker.ensure_background_worker', side_effect=OSError('simulated')):
            notify_download_completed(self.base, self.database)  # must not raise

    def test_deleted_cache_is_requeued(self):
        self.publish()
        self.queue.discover()
        self.queue.process_next()
        saved = self.queue.db.execute('SELECT result_path FROM jobs').fetchone()[0]
        (self.queue.cache / saved).unlink()
        self.queue.discover()
        self.assertTrue(self.queue.process_next())
        self.assertTrue(self.snapshot()[1]['analysis_complete'])

    def test_old_identityless_cache_is_not_used_after_context_fix(self):
        self.publish()
        current = self.queue.version
        legacy = '47acc31762ac0f0449bf0a4dfefc61a14ba58c5307ec98576b832a9d307d2130'
        self.assertNotEqual(current, legacy)
        self.queue.version = legacy
        self.queue.discover()
        self.queue.process_next()
        self.queue.version = current
        self.assertEqual(self.queue.discover(), 1)
        _, report, _ = self.snapshot()
        self.assertEqual(report['completed_analysis_files'], 0)
        self.assertEqual(report['pending_analysis_files'], 1)
        self.assertEqual(report['welds'], [])

    def test_cache_combines_duplicates_and_reports_cross_archive_conflicts(self):
        self.publish()
        self.publish('copy')
        self.queue.discover()
        while self.queue.process_next():
            pass
        groups, report, _ = self.snapshot()
        self.assertEqual(groups['p', 'H4-262', 'PCF']['管线数'], 1)
        self.assertEqual(groups['p', 'H4-262', 'PCF']['焊缝数'], 1)
        self.publish('revision', PCF.replace('1000', '2000'))
        self.queue.discover()
        self.queue.process_next()
        groups, report, _ = self.snapshot()
        self.assertIsNone(groups['p', 'H4-262', 'PCF']['焊缝数'])
        self.assertFalse(report['analysis_complete'])
        self.assertGreater(report['unresolved_analysis_groups'], 0)

    def test_source_hash_mismatch_does_not_publish_cached_result(self):
        self.publish()
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("UPDATE files SET sha256='wrong'")
        self.queue.discover()
        self.queue.process_next()
        groups, report, _ = self.snapshot()
        self.assertEqual(groups, {})
        self.assertFalse(report['analysis_complete'])

    def test_worker_cli_processes_only_the_temporary_fixture_ledger(self):
        self.publish()
        env = {k: v for k, v in os.environ.items() if k not in ('FUSIONLIVE_USERNAME', 'FUSIONLIVE_PASSWORD')}
        env['PYTHONIOENCODING'] = 'utf-8'
        # Freeze only the subprocess clock; production CLI has no window bypass.
        launcher = str(Path(__file__).resolve().parents[1] / 'main.py')
        cli = [sys.executable, '-B', '-c',
               "import sys,runpy; from pathlib import Path; from datetime import datetime,timezone,timedelta; "
               "sys.path.insert(0,str(Path(sys.argv[1]).parent/'app')); import fusion_client; "
               "fusion_client.now=lambda:datetime(2026,10,31,12,tzinfo=timezone(timedelta(hours=8))); "
               "runpy.run_path(sys.argv.pop(1),run_name='__main__')", launcher, 'analysis_worker']
        result = subprocess.run([*cli,
                                 '--database', str(self.database), '--base-dir', str(self.base)],
                                capture_output=True, text=True, encoding='utf-8', env=env, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.snapshot()[1]['analysis_complete'])
        persisted = (self.queue.cache / 'progress.log').read_text(encoding='utf-8')
        self.assertEqual(result.stdout.replace('\r\n','\n'), persisted)
        for message in ('解析程序启动', '开始扫描下载台账', '解析缓存版本', '开始文件', '开始解压 ZIP',
                        '包内进度 1/1', '解析 PCF', '分类小计', '文件结果', '本次运行结束'):
            self.assertIn(message, persisted)
        # A second startup must explain cached completion without parsing again.
        second = subprocess.run([*cli,
                                 '--database', str(self.database), '--base-dir', str(self.base), '--watch',
                                 '--idle-exit', '.15', '--poll-seconds', '.1'],
                                capture_output=True, text=True, encoding='utf-8', env=env, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn('已完成/可复用缓存 1', second.stdout)
        self.assertIn('空闲等待', second.stdout)
        self.assertNotIn('开始文件', second.stdout)

    def test_failed_file_logs_reason_and_retry_but_keeps_queue_recoverable(self):
        self.publish()
        messages=[]
        self.queue.log=messages.append
        self.queue.discover()
        with patch('local_analysis.analyze', side_effect=OSError('fixture parse failure')):
            self.assertTrue(self.queue.process_next())
        self.assertTrue(any('失败原因' in line and 'fixture parse failure' in line and '60 秒' in line for line in messages))
        self.assertEqual(self.queue.counts(), {'failed':1})

    def test_permanent_error_is_not_retried_after_reopen(self):
        self.publish()
        self.queue.discover()
        with patch('local_analysis.analyze', side_effect=ValueError('压缩包展开总大小或文件数超过本次分析上限。')) as analyze:
            self.assertTrue(self.queue.process_next())
            self.queue.db.execute('UPDATE jobs SET retry_at=0')
            self.queue.db.commit()
            self.assertFalse(self.queue.process_next())
            self.assertEqual(analyze.call_count, 1)
        self.assertEqual(self.queue.db.execute('SELECT retryable FROM jobs').fetchone()[0], 0)
        reopened = AnalysisQueue(self.database)
        self.addCleanup(reopened.close)
        reopened.discover()
        self.assertFalse(reopened.process_next())

    def test_archive_member_permanent_error_is_not_retried(self):
        self.publish()
        self.queue.discover()
        with patch('local_analysis.extract_zip', side_effect=ValueError('压缩包内单文件超过限制')):
            self.assertTrue(self.queue.process_next())
        self.queue.db.execute('UPDATE jobs SET retry_at=0')
        self.queue.db.commit()
        self.assertFalse(self.queue.process_next())

    def test_transient_error_remains_retryable(self):
        self.publish()
        self.queue.discover()
        with patch('local_analysis.analyze', side_effect=OSError('temporary read error')) as analyze:
            self.assertTrue(self.queue.process_next())
            self.queue.db.execute('UPDATE jobs SET retry_at=0')
            self.queue.db.commit()
            self.assertTrue(self.queue.process_next())
            self.assertEqual(analyze.call_count, 2)

    def test_long_operation_reports_liveness_and_heartbeat_stops_on_exit(self):
        messages=[]
        reported=threading.Event()
        def log(message):
            messages.append(message)
            if '仍在处理' in message: reported.set()
        with activity(log, 'fixture archive', interval=.01) as phase:
            phase('fixture PCF')
            self.assertTrue(reported.wait(2))
        self.assertIn('fixture PCF',messages[-1])
        self.assertIn('不代表解析已完成',messages[-1])
        self.assertFalse(any(t.name=='analysis-progress' and t.is_alive() for t in threading.enumerate()))


class AnalysisScheduleDefaultsTests(unittest.TestCase):
    def test_download_window_exclusion_is_enabled_by_default(self):
        from analysis_worker import PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW
        self.assertTrue(PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW)


class DownloadWindowTests(unittest.TestCase):
    def setUp(self):
        guard = patch('analysis_worker.PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW', True)
        guard.start()
        self.addCleanup(guard.stop)

    def at(self, hour, minute=0):
        return datetime(2026, 10, 31, hour, minute, tzinfo=timezone(timedelta(hours=8)))

    def test_beijing_boundaries_and_utc_conversion(self):
        from analysis_worker import in_download_window
        for hour, minute, expected in ((1, 59, False), (2, 0, True),
                                       (10, 29, True), (10, 30, False), (23, 59, False)):
            with self.subTest(hour=hour, minute=minute):
                self.assertEqual(in_download_window(self.at(hour, minute)), expected)
                self.assertEqual(in_download_window(self.at(hour, minute).astimezone(timezone.utc)), expected)

    def test_analysis_follows_briefing_time_and_reloads_settings(self):
        from analysis_worker import analysis_paused_for_schedule
        from push_settings import DEFAULT, save_settings
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            for send_time in ('11:00', '12:15'):
                save_settings({**DEFAULT, 'briefing_send_time_beijing': send_time,
                               'text_send_time_beijing': '10:45'}, base)
                hour, minute = map(int, send_time.split(':'))
                boundary = self.at(hour, minute)
                for at, paused in ((boundary - timedelta(seconds=1), True),
                                   (boundary, False), (self.at(0), True), (self.at(23, 59), False)):
                    with self.subTest(send_time=send_time, at=at):
                        self.assertEqual(analysis_paused_for_schedule(base, at), paused)
                        self.assertEqual(analysis_paused_for_schedule(base, at.astimezone(timezone.utc)), paused)

    def test_download_hook_waits_after_download_window_until_briefing(self):
        from push_settings import DEFAULT, save_settings
        with tempfile.TemporaryDirectory() as folder, \
             patch('analysis_worker._child', None), \
             patch('analysis_worker.subprocess.Popen') as spawn:
            base = Path(folder)
            save_settings({**DEFAULT, 'briefing_send_time_beijing': '12:00'}, base)
            for at in (self.at(10, 30), self.at(11, 59)):
                with patch('analysis_worker.now', return_value=at):
                    notify_download_completed(base, base/'runtime/ledger.sqlite3')
            spawn.assert_not_called()
            with patch('analysis_worker.now', return_value=self.at(12)):
                notify_download_completed(base, base/'runtime/ledger.sqlite3')
            spawn.assert_called_once()

    def test_download_hook_does_not_spawn_during_download_window(self):
        with patch('analysis_worker.now', return_value=self.at(9)), \
             patch('analysis_worker.subprocess.Popen') as spawn:
            notify_download_completed(Path('unused'), Path('unused.db'))
            spawn.assert_not_called()

    def test_disabled_gate_allows_download_hook_during_download_window(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch('analysis_worker.PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW', False), \
             patch('analysis_worker.now', return_value=self.at(9)), \
             patch('analysis_worker._child', None), \
             patch('analysis_worker.subprocess.Popen') as spawn:
            base = Path(folder)
            notify_download_completed(base, base / 'runtime/ledger.sqlite3')
            spawn.assert_called_once()

    def test_disabled_gate_runs_queue_and_exports_during_download_window(self):
        from analysis_worker import main, in_download_window
        queue = Mock()
        queue.counts.return_value = {'done': 1}
        queue.process_next.side_effect = [True, False]
        with tempfile.TemporaryDirectory() as folder, \
             patch('analysis_worker.PAUSE_ANALYSIS_DURING_DOWNLOAD_WINDOW', False), \
             patch('analysis_worker.now', return_value=self.at(9)), \
             patch('analysis_worker.AnalysisQueue', return_value=queue), \
             patch('analysis_worker.cached_snapshot', return_value=({}, {'weld_table_file': 'fixture.csv', 'welds': []}, None)) as snapshot:
            base = Path(folder)
            self.assertTrue(in_download_window())
            self.assertEqual(main(['--database', str(base/'runtime/ledger.sqlite3'),
                                   '--base-dir', str(base)]), 0)
            queue.discover_if_changed.assert_called_once()
            self.assertEqual(queue.process_next.call_count, 2)
            snapshot.assert_called_once()
            queue.close.assert_called_once_with(defer_exports=False)

    def test_migration_still_blocks_original_file_analysis_outside_download_window(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            marker = base / 'runtime/layout.pending'
            marker.parent.mkdir()
            marker.write_text('unfinished migration')
            with patch('analysis_worker._child', None), \
                 patch('analysis_worker.now', return_value=self.at(11)), \
                 patch('analysis_worker.subprocess.Popen') as spawn:
                ensure_background_worker(base, base / 'runtime/ledger.sqlite3')
                spawn.assert_not_called()
            self.assertEqual(marker.read_text(), 'unfinished migration')

    def test_manual_resume_cannot_bypass_window_or_clear_stop(self):
        from analysis_worker import main
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            stop = base / 'runtime/file_analysis/stop.request'
            stop.parent.mkdir(parents=True)
            stop.write_text('manual stop')
            with patch('analysis_worker.now', return_value=self.at(9)), \
                 patch('analysis_worker.AnalysisQueue') as queue:
                self.assertEqual(main(['--database', str(base/'runtime/ledger.sqlite3'),
                                       '--base-dir', str(base), '--watch', '--resume']), 0)
                queue.assert_not_called()
            self.assertEqual(stop.read_text(), 'manual stop')

    def test_running_worker_finishes_current_task_then_exits_without_exports(self):
        from analysis_worker import main
        clock = [self.at(23, 59)]
        queue = Mock()
        queue.counts.return_value = {'done': 1}

        def finish_current():
            clock[0] = self.at(0)
            return True

        queue.process_next.side_effect = finish_current
        with tempfile.TemporaryDirectory() as folder, \
             patch('analysis_worker.now', side_effect=lambda: clock[0]), \
             patch('analysis_worker.AnalysisQueue', return_value=queue), \
             patch('analysis_worker.cached_snapshot') as snapshot:
            base = Path(folder)
            self.assertEqual(main(['--database', str(base/'runtime/ledger.sqlite3'),
                                   '--base-dir', str(base), '--watch']), 0)
            queue.process_next.assert_called_once()
            queue.close.assert_called_once_with(defer_exports=True)
            snapshot.assert_not_called()
            self.assertFalse((base/'runtime/file_analysis/stop.request').exists())


if __name__ == '__main__':
    unittest.main()
