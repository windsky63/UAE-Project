"""Real offline journals/files; never touch production data or WeChat."""
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from ledger import Ledger
import file_layout as layout
from migration_recovery import recover
from analysis_worker import worker_lock
from briefing_schedule import DailyBriefing, LOCAL
from publication_worker import PublicationJob
from push_settings import DEFAULT, save_settings


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.runtime = self.base / 'runtime'
        self.ledger = Ledger(self.runtime / 'ledger.sqlite3', base_dir=self.base)
        self.addCleanup(self.ledger.close)
        self.clock = patch('migration_recovery.in_download_window', return_value=False)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.network = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def interrupted_replacement(self, identical=False):
        old_data, new_data = b'old', b'old' if identical else b'new'
        self.old = self.base / 'downloads/Project/Pipe/drawing.pdf'
        self.new = self.base / 'runtime/download_ready/new/content'
        self.history = self.base / 'history/version-1/Project/Pipe/drawing.pdf'
        for path, data in ((self.old, old_data), (self.new, new_data)):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        digest = lambda data: hashlib.sha256(data).hexdigest()
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO folders(project,id,name,path) VALUES('p','r','Pipe','Project/Pipe')")
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,filename,metadata,related_state) VALUES('p','f','r','drawing.pdf','{}','done')")
            self.ledger.db.execute("""INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,size,bytes,path,sha256,active)
                VALUES('p','f','f','r','drawing.pdf','{}','done',3,3,?,?,1)""",
                (self.new.relative_to(self.base).as_posix(), digest(new_data)))
            self.ledger.db.execute("INSERT INTO document_files VALUES('p','f','r','f',1)")
            history_id = self.ledger.db.execute("INSERT INTO file_history(project,file_id,path,bytes,sha256,archived) VALUES('p','f',?,3,?,'2026-10-31')",
                (self.old.relative_to(self.base).as_posix(), digest(old_data))).lastrowid
        layout.ensure_schema(self.ledger.db)
        moves = [dict(source=self.old.relative_to(self.base).as_posix(), target=self.history.relative_to(self.base).as_posix(),
                      refs=[['file_history', history_id]], bytes=3, sha256=digest(old_data)),
                 dict(source=self.new.relative_to(self.base).as_posix(), target=self.old.relative_to(self.base).as_posix(),
                      refs=[['files', 'p', 'f']], bytes=3, sha256=digest(new_data))]
        with self.ledger.db:
            for i, move in enumerate(moves):
                self.ledger.db.execute('INSERT INTO layout_moves VALUES(?,?,?)', (str(i), json.dumps(move), 'pending'))
        (self.runtime / 'layout.pending').write_text('interrupted at deadline')
        layout.pause_analysis_for_migration(self.base)
        return moves

    def test_two_pending_moves_recover_without_download_or_new_migration_plan(self):
        self.interrupted_replacement(identical=True)
        with patch('file_layout.plan', side_effect=AssertionError('must not start unrelated moves')):
            result = recover(self.base)
        self.assertEqual(result['phase'], 'complete')
        self.assertEqual(result['recovered_moves'], 2)
        self.assertEqual(self.old.read_bytes(), b'old')
        self.assertEqual(self.history.read_bytes(), b'old')
        self.assertFalse(self.new.exists())
        self.assertFalse((self.runtime / 'layout.pending').exists())
        self.assertFalse((self.runtime / 'file_analysis/stop.request').exists())
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state!='done'")[0], 0)
        self.assertEqual(self.ledger.one("SELECT path FROM files WHERE id='f'")[0], self.old.relative_to(self.base).as_posix())
        self.assertTrue(list((self.runtime / 'backups').glob('before-auto-layout-*.sqlite3')))
        self.assertTrue((self.runtime / 'path_migration.log').is_file())

    def test_recovery_preserves_manual_analysis_stop(self):
        self.interrupted_replacement()
        stop = self.runtime / 'file_analysis/stop.request'
        stop.write_text('stop after current local file')  # User explicitly stops after migration paused it.
        self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertEqual(stop.read_text(), 'stop after current local file')
        self.assertEqual(self.old.read_bytes(), b'new')
        self.assertEqual(self.history.read_bytes(), b'old')

    def test_original_file_recovery_does_not_wait_for_published_image_delivery(self):
        self.interrupted_replacement()
        with worker_lock(self.runtime / 'wechat_delivery') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertEqual(self.old.read_bytes(), b'new')

    def test_real_recovery_child_completes_recorded_batch(self):
        from analysis_worker import in_download_window
        if in_download_window():
            self.skipTest('real worker respects the download window')
        self.interrupted_replacement()
        job = PublicationJob(self.base, 'recovery', {})
        self.addCleanup(job.close)
        deadline = time.monotonic() + 15
        outcome = None
        while outcome is None and time.monotonic() < deadline:
            outcome = job.poll()
            time.sleep(.02)
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome['result']['phase'], 'complete', outcome)
        self.assertFalse((self.runtime / 'layout.pending').exists())
        self.assertEqual(self.old.read_bytes(), b'new')
        self.assertEqual(self.history.read_bytes(), b'old')

    def test_failure_preserves_journal_and_reuses_backup_before_successful_retry(self):
        self.interrupted_replacement()
        self.new.write_bytes(b'bad')
        result = recover(self.base)
        self.assertEqual(result['phase'], 'failed')
        self.assertEqual(self.old.read_bytes(), b'old')
        self.assertFalse(self.history.exists())
        self.assertTrue((self.runtime / 'layout.pending').exists())
        self.assertTrue((self.runtime / 'file_analysis/stop.request').exists())
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state!='done'")[0], 2)
        self.new.write_bytes(b'new')
        self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertEqual(len(list((self.runtime / 'backups').glob('before-auto-layout-*.sqlite3'))), 1)

    def test_recorded_batch_resumes_after_publishing_before_commit(self):
        self.interrupted_replacement()
        real_publish = layout.publish
        def interrupted(*args, **kwargs):
            real_publish(*args, **kwargs)
            raise OSError('simulated crash after publish')
        with patch('file_layout.publish', side_effect=interrupted):
            self.assertEqual(recover(self.base)['phase'], 'failed')
        self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertEqual(self.old.read_bytes(), b'new')
        self.assertEqual(self.history.read_bytes(), b'old')

    def test_busy_sync_and_download_window_defer_without_touching_files(self):
        self.interrupted_replacement()
        with worker_lock(self.runtime, 'sync.lock') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(recover(self.base)['phase'], 'deferred')
        with patch('migration_recovery.in_download_window', return_value=True):
            self.assertEqual(recover(self.base)['phase'], 'deferred')
        self.assertEqual(self.old.read_bytes(), b'old')
        self.assertEqual(self.new.read_bytes(), b'new')

    def test_live_parser_is_asked_to_finish_before_recovery_retries(self):
        self.interrupted_replacement()
        (self.runtime / 'file_analysis/stop.request').unlink()
        with worker_lock(self.runtime / 'file_analysis') as acquired:
            self.assertTrue(acquired)
            self.assertEqual(recover(self.base)['phase'], 'deferred')
            self.assertTrue((self.runtime / 'file_analysis/stop.request').exists())
            self.assertEqual(self.old.read_bytes(), b'old')
        self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertFalse((self.runtime / 'file_analysis/stop.request').exists())

    def test_all_moves_done_but_mapping_cleanup_interrupted_is_recoverable(self):
        self.interrupted_replacement()
        real_replace = layout.os.replace
        def fail_mapping(source, target):
            if Path(target).name == 'path_mapping.json':
                raise PermissionError('simulated mapping write failure')
            return real_replace(source, target)
        with patch('file_layout.os.replace', side_effect=fail_mapping):
            self.assertEqual(recover(self.base)['phase'], 'failed')
        self.assertTrue((self.runtime / 'layout.pending').exists())
        self.assertEqual(self.ledger.one("SELECT COUNT(*) FROM layout_moves WHERE state!='done'")[0], 0)
        self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertFalse((self.runtime / 'layout.pending').exists())
        self.assertFalse((self.runtime / 'file_analysis/stop.request').exists())

    def test_full_exit_during_recovery_preserves_owned_pause_for_next_start(self):
        self.interrupted_replacement()
        real_apply = layout.apply
        def finish(*args, **kwargs):
            result = real_apply(*args, **kwargs)
            (self.runtime / 'scheduler.exit.request').touch()
            return result
        with patch('migration_recovery.apply', side_effect=finish):
            self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertTrue((self.runtime / 'layout.analysis_pause.json').exists())
        self.assertTrue((self.runtime / 'file_analysis/stop.request').exists())
        (self.runtime / 'scheduler.exit.request').unlink()
        self.assertEqual(recover(self.base)['phase'], 'complete')
        self.assertFalse((self.runtime / 'file_analysis/stop.request').exists())


class RecoverySchedulingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        (self.base / 'runtime').mkdir()
        (self.base / 'runtime/ledger.sqlite3').touch()
        (self.base / 'runtime/layout.pending').touch()
        save_settings({**DEFAULT, 'recipients': ['fixture']}, self.base)
        self.at = datetime(2026, 10, 31, 10, 40, tzinfo=LOCAL)
        self.job = Mock()
        self.job.poll.return_value = None
        self.factory = Mock(return_value=self.job)
        notice = Mock()
        notice.poll.return_value = None
        self.service = DailyBriefing(self.base, Mock(), async_jobs=True, recovery_factory=self.factory,
                                    job_factory=Mock(return_value=notice), analysis_start=Mock())
        self.addCleanup(self.service.close)

    def test_recovery_runs_once_without_blocking_prepare_and_resumes_analysis(self):
        self.service.tick(self.at)
        self.service.tick(self.at + timedelta(seconds=1))
        self.factory.assert_called_once()
        self.assertIsNotNone(self.service.notice_job)
        self.assertFalse((self.service.marker('2026-10-31').parent / '微信推送文快照.json').exists())
        self.service.analysis_start.assert_not_called()
        (self.base / 'runtime/layout.pending').unlink()
        self.job.poll.return_value = dict(result=dict(phase='complete', reason='finished'))
        self.service.tick(self.at + timedelta(seconds=2))
        self.service.analysis_start.assert_not_called()
        self.service.tick(self.at.replace(hour=11, minute=0))
        self.service.analysis_start.assert_called_once()

    def test_failed_recovery_retries_after_sixty_seconds_even_after_publication(self):
        self.service.state = dict(date='2026-10-31', phase='submitted')
        self.service.tick(self.at)
        self.job.poll.return_value = dict(result=dict(phase='failed', reason='fixture conflict'))
        self.service.tick(self.at + timedelta(seconds=1))
        self.service.tick(self.at + timedelta(seconds=30))
        self.factory.assert_called_once()
        self.job.poll.return_value = None
        self.service.tick(self.at + timedelta(seconds=61))
        self.assertEqual(self.factory.call_count, 2)

    def test_eleven_oclock_delivery_starts_while_recovery_is_running(self):
        marker = self.service.marker('2026-10-31')
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps(dict(date='2026-10-31', ready=True, demo=False,
            generated_at=self.at.isoformat(), images=[dict(path='fixture.png', sha256='fixture')])), encoding='utf-8')
        self.service.tick(self.at)
        self.service.tick(self.at.replace(hour=11, minute=0))
        self.assertIs(self.service.recovery_job, self.job)
        self.assertEqual(self.service.state['phase'], 'sending')
        self.assertEqual(self.service.job_factory.call_args.args[1], 'delivery')

    def test_recovery_child_waits_for_journal_instead_of_being_terminated(self):
        job = PublicationJob.__new__(PublicationJob)
        job.kind = 'recovery'
        job.folder = self.base
        job.log = Mock()
        job.child = Mock(pid=1)
        job.child.poll.side_effect = [None, None, 0]
        with patch('publication_worker.time.sleep'):
            job.close(Mock())
        job.child.terminate.assert_not_called()
        job.child.kill.assert_not_called()
