"""Exercise large declared sizes without allocating multi-gigabyte files."""
import inspect
import unittest

from local_analysis import (
    MAX_ARCHIVE_FILE_BYTES, MAX_ARCHIVE_EXPANDED_BYTES, MAX_ARCHIVE_ENTRIES,
    _plan, extract_zip, extract_7z,
)


class ArchiveLimitTests(unittest.TestCase):
    def plan(self, size, budget=None):
        return _plan([dict(member='large.pcf', directory=False, size=size,
                           compressed=size // 10)], budget,
                     MAX_ARCHIVE_EXPANDED_BYTES, MAX_ARCHIVE_ENTRIES,
                     MAX_ARCHIVE_FILE_BYTES)

    def test_zip_and_7z_defaults_allow_more_than_eight_gib(self):
        for extractor in (extract_zip, extract_7z):
            params = inspect.signature(extractor).parameters
            self.assertEqual(params['max_file_bytes'].default, 16 * 1024**3)
            self.assertEqual(params['max_bytes'].default, 64 * 1024**3)
        self.assertEqual(len(self.plan(8 * 1024**3 + 1)), 1)
        self.assertEqual(len(self.plan(16 * 1024**3)), 1)
        with self.assertRaisesRegex(ValueError, '单文件超过'):
            self.plan(16 * 1024**3 + 1)

    def test_nested_archives_share_total_budget(self):
        budget = dict(bytes=MAX_ARCHIVE_EXPANDED_BYTES, files=MAX_ARCHIVE_ENTRIES)
        for _ in range(4):
            self.plan(16 * 1024**3, budget)
        self.assertEqual(budget['bytes'], 0)
        with self.assertRaisesRegex(ValueError, '展开总大小'):
            self.plan(1, budget)
