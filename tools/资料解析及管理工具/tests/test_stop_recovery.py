"""Offline regression tests for stopped downloads and isolated emergency actions."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import contextmanager
import os
from briefing_schedule import DailyBriefing
import subprocess


class StopRecoveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.runtime = self.base / 'runtime'
        self.runtime.mkdir()





    def test_report_child_escalates_termination_with_bounded_wait(self):
        service = DailyBriefing(self.base, Mock(), analysis_start=Mock())
        service.state = {'date': '2026-10-29'}
        child = Mock()
        child.poll.return_value = None
        child.wait.side_effect = [subprocess.TimeoutExpired('fixture', 10), 0]
        service.process = child
        service.close()
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertIsNone(service.process)


