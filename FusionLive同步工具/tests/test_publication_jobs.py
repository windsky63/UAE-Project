"""Process isolation and non-blocking scheduling, with no real WeChat delivery."""
from datetime import datetime
from pathlib import Path
import json
import queue
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from briefing_schedule import DailyBriefing, LOCAL
from publication_worker import PublicationJob
from push_settings import DEFAULT, save_settings
import scheduler


class PublicationJobsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        (self.base/'runtime').mkdir()
        self.at = datetime(2026, 10, 29, 10, 40, tzinfo=LOCAL)
        save_settings({**DEFAULT, 'recipients': ['fixture']}, self.base)

    def test_notice_and_full_report_are_separate_jobs(self):
        job, factory = Mock(), Mock()
        job.poll.return_value = None
        process = Mock()
        process.poll.return_value = 0
        factory.return_value = process
        service = DailyBriefing(self.base, Mock(), async_jobs=True, job_factory=Mock(return_value=job),
                                process_factory=factory, analysis_start=Mock())
        self.addCleanup(service.close)
        with patch('briefing.generate_pending_notice', side_effect=AssertionError('timer must not render')):
            service.tick(self.at)
            service.tick(self.at.replace(second=1))
        factory.assert_not_called()
        self.assertIs(service.notice_job, job)
        job.poll.return_value = dict(result=True)
        service.tick(self.at.replace(second=2))
        factory.assert_called_once()

    def test_delivery_is_polled_without_duplicate_submission(self):
        job = Mock()
        job.poll.return_value = None
        job_factory = Mock(return_value=job)
        service = DailyBriefing(self.base, Mock(), async_jobs=True, job_factory=job_factory,
                                analysis_start=Mock(), delivery=Mock(side_effect=AssertionError('timer must not send')))
        self.addCleanup(service.close)
        day = self.at.date().isoformat()
        marker = service.marker(day)
        marker.parent.mkdir(parents=True)
        marker.write_text(json.dumps(dict(date=day, ready=True, demo=False,
            generated_at=self.at.isoformat(), images=[dict(path='fixture.png', sha256='fixture')])), encoding='utf-8')
        at = self.at.replace(hour=11, minute=0)
        service.tick(at)
        service.tick(at.replace(second=1))
        self.assertEqual(service.state['phase'], 'sending')
        job_factory.assert_called_once()
        # Migration may start while a publication child is still running.
        # Its result must still be polled and recorded without resubmission.
        (self.base/'runtime/layout.pending').touch()
        job.poll.return_value = dict(result=dict(complete=True, uncertain=0))
        service.tick(at.replace(second=2))
        self.assertEqual(service.state['phase'], 'submitted')
        job_factory.assert_called_once()

    def test_stopped_delivery_child_never_initializes_sender(self):
        (self.base/'runtime/scheduler.exit.request').touch()
        job = PublicationJob(self.base, 'delivery', dict(marker='not-a-real-report', settings={}, at=self.at.isoformat()))
        self.addCleanup(job.close)
        deadline = time.monotonic() + 15
        result = None
        while result is None and time.monotonic() < deadline:
            result = job.poll()
            time.sleep(.02)
        self.assertIsNotNone(result)
        self.assertTrue(result['not_sent'])
        self.assertFalse((self.base/'runtime/wechat_delivery').exists())

    def test_sync_monitor_does_not_block_main_command_handler(self):
        started, release, exiting = threading.Event(), threading.Event(), threading.Event()
        def worker(*args):
            started.set()
            release.wait(5)
        with patch('scheduler.get_credentials', return_value=('fixture', 'fixture')), \
             patch('scheduler.run_worker', side_effect=worker), patch('scheduler.log'), patch('scheduler.request_full_stop'), \
             patch('scheduler.RUNTIME', self.base/'runtime'):
            task = scheduler.start_sync_task(exiting)
            try:
                self.assertTrue(started.wait(2))
                commands = queue.Queue()
                commands.put('q')
                scheduler.consume_commands(commands, exiting)
                self.assertTrue(exiting.is_set())
                self.assertTrue(task.is_alive())
            finally:
                release.set()
                task.join(5)
