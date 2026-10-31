import tempfile
from pathlib import Path
import unittest

from openpyxl import load_workbook

from briefing import create_workbook, METRICS, render
from business_type import load_rules
from business_type import unit_names


class UnitReferenceTests(unittest.TestCase):
    def test_named_unit_and_short_original_number_lookup(self):
        self.assertEqual(unit_names('H4-262')['english'], 'Steam, Condensate & BFW System')
        self.assertEqual(unit_names('H4-262')['chinese'], '蒸汽、凝结水及锅炉给水系统')
        self.assertEqual(unit_names('H0-30')['english'], unit_names('H0-030')['english'])
        self.assertEqual(unit_names('HB-011')['english'], 'Booster Compression Station')

    def test_ambiguous_and_unconfirmed_source_definitions_are_not_guessed(self):
        for code in ('H1-012', 'H4-113', 'H5-530'):
            self.assertEqual(unit_names(code)['status'], 'ambiguous')
            self.assertGreater(len(unit_names(code)['candidates']), 1)
        self.assertEqual(unit_names('H2-069')['status'], 'pending_definition')
        self.assertEqual(unit_names('H4-245')['status'], 'pending_definition')
        self.assertEqual(unit_names('H4-999')['status'], 'unlisted')
        self.assertEqual(unit_names('H5-577')['status'], 'listed')

    def test_page_continuations_and_first_row_on_last_page_are_retained(self):
        self.assertEqual(len(load_rules()['units']), 122)
        self.assertEqual(unit_names('H2-233')['chinese'], '酸性气富集装置')
        self.assertEqual(unit_names('H5-599')['english'], 'P5.0 Project Interconnecting Piperack')
        self.assertTrue(all(r['source_page'] in (150, 151, 152) for r in load_rules()['units']))

    def test_report_names_in_all_sections_and_metrics_keep_their_meaning(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            groups = {('p', 'H4-262', source): {**dict.fromkeys(METRICS),
                '管线数': 12, '焊缝数': 48, '寸口量': 186, '状态': '已解析'}
                for source in ('单线图', 'PCF')}
            target = folder / 'report.xlsx'
            create_workbook(target, groups, [], '管道简报', '2026-10-26')
            wb = load_workbook(target)
            try:
                sheet = wb['单元汇总']
                headers = [row for row in sheet if row[0].value == '单元号']
                self.assertEqual(len(headers), 3)
                self.assertTrue(all([c.value for c in row[:3]] == ['单元号', '英文名称', '中文名称'] for row in headers))
                rows = [row for row in sheet if row[0].value == 'H4-262']
                self.assertEqual(len(rows), 2)  # Unknown file inventory has no visible data row.
                for row in rows:
                    self.assertEqual(row[1].value, 'Steam, Condensate & BFW System')
                    self.assertEqual(row[2].value, '蒸汽、凝结水及锅炉给水系统')
                self.assertEqual(rows[0][3].value, 12)
                self.assertEqual(rows[1][3].value, 48)
                self.assertEqual(rows[1][4].value, 186)
                self.assertEqual(sheet.max_column, 12)
                self.assertEqual(sheet.freeze_panes, 'D5')
            finally:
                wb.close()
            self.assertTrue(all(p.is_file() for p in render(target, folder)))


if __name__ == '__main__':
    unittest.main()
