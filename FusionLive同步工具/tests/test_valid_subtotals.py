"""Valid parsed results survive missing units, failures and pipeline conflicts."""
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
from briefing import create_workbook, layout_role, METRICS, valid_sum
from local_analysis import analyze


class ValidSubtotalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_sections_filter_independently_and_totals_sum_only_known_values(self):
        groups = {
            ('p', 'H1-001', 'PCF'): {'管线数': 3, '焊缝数': 5, '寸口量': 10},
            ('p', 'H1-002', 'PCF'): dict.fromkeys(METRICS),
            ('p', 'H2-001', 'PCF'): {'管线数': 0, '焊缝数': 0},
            ('p', 'H3-001', 'PCF'): dict.fromkeys(METRICS),
            ('p', 'H4-001', 'PCF'): {'管线数': 2},
        }
        report = dict(files=[], ledger_inventory=[], welds=[], file_inventory=[
            dict(project='p', unit='H1-002', kind='单线图')])
        path = self.root / 'report.xlsx'
        create_workbook(path, groups, [], '有效小计测试', '2026-10-27', report, layout_template=False)
        wb = load_workbook(path)
        self.addCleanup(wb.close)
        sections, section = {}, None
        for row in wb['简报']:
            role = layout_role(wb['简报'], row[0].row)
            if role == 'section':
                section = str(row[0].value)[0]
                sections[section] = {}
            elif role in ('body', 'total'):
                sections[section][row[0].value] = [cell.value for cell in row]
        self.assertIn('H3', sections['一'])  # Known zero file counts are valid.
        self.assertNotIn('H3', sections['二'])
        self.assertNotIn('H3', sections['三'])
        self.assertIn('H4', sections['二'])
        self.assertNotIn('H4', sections['三'])
        self.assertEqual(sections['二']['H1'][1:4], [3, '—', '—'])
        self.assertEqual(sections['二']['H2'][1], 0)
        self.assertEqual(sections['二']['合计'][1:4], [5, '—', '—'])
        self.assertEqual(sections['三']['合计'][1:7], [5, 10, '—', '—', '—', '—'])
        self.assertFalse(any(row[0].value == 'H1-002' for row in wb['单元汇总']
                             if layout_role(wb['单元汇总'], row[0].row) in ('body', 'total')
                             and row[0].row > 6))

    def test_wholly_unknown_totals_are_omitted(self):
        path = self.root / 'unknown.xlsx'
        create_workbook(path, {('p', 'H1-001', 'PCF'): dict.fromkeys(METRICS)}, [],
                        '无数据', '2026-10-27', layout_template=False)
        wb = load_workbook(path)
        self.addCleanup(wb.close)
        for name in ('简报', '单元汇总'):
            self.assertFalse(any(layout_role(wb[name], row[0].row) in ('body', 'total') for row in wb[name]))
        self.assertEqual(valid_sum([None, 0]), 0)
        self.assertIsNone(valid_sum([None]))

    def pipeline(self, name, sha, conflict=False):
        return dict(project='p', unit='H1-001', source='PCF', pipeline=name,
                    sha256=sha, conflict=conflict, origin=name,
                    rows=[{'寸径': 2, '分类': 'shop'}])

    def test_cached_failure_and_conflict_preserve_unrelated_valid_pipeline(self):
        reports = [dict(files=[], incomplete=[['p', 'H1-001', 'PCF']], pipeline_results=[
            self.pipeline('good', 'a'), self.pipeline('conflicted', 'b')]),
            dict(files=[], pipeline_results=[self.pipeline('conflicted', 'c')])]
        groups, report, _ = analyze(sources_override=[], cached_reports=reports, output=self.root / 'out')
        record = groups['p', 'H1-001', 'PCF']
        self.assertEqual(record['管线数'], 1)
        self.assertEqual(record['焊缝数'], 1)
        self.assertEqual(record['寸口量'], 2)
        self.assertEqual(len(report['welds']), 1)
        self.assertIn(('p', 'H1-001', 'PCF'), report['incomplete'])

    def test_later_conflict_flag_is_not_lost_when_cached_hashes_match(self):
        reports = [dict(files=[], pipeline_results=[self.pipeline('same', 'a')]),
                   dict(files=[], pipeline_results=[self.pipeline('same', 'a', True)])]
        groups, report, _ = analyze(sources_override=[], cached_reports=reports, output=self.root / 'out')
        self.assertIsNone(groups['p', 'H1-001', 'PCF']['管线数'])
        self.assertIsNone(groups['p', 'H1-001', 'PCF']['焊缝数'])
        self.assertEqual(report['welds'], [])
