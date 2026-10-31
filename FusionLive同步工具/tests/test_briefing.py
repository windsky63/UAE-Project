"""Only offline briefing code is exercised; network creation is forbidden."""
import hashlib
from copy import copy
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from briefing import catalogue, create_workbook, generate, HEADERS, import_metrics, METRICS, write_template, write_layout_template, screenshot_pages, layout_role


class BriefingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.database = self.base / 'ledger.sqlite3'
        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute('CREATE TABLE documents(project,id,reference,filename,title,metadata,active)')
            for project, identifier, reference, kind, active in (
                ('p', 'one', 'LT_HE-H4-262-01', 'ISO', 1),
                ('p', 'one', 'LT_HE-H4-262-01', 'ISO', 1),
                ('p', 'old', 'LT_HE-H4-262-02', 'ISO', 0),
                ('p', 'electrical', 'LT_HE-H4-262-03', 'SLD', 1),
                ('other', 'one', 'LT_HE-H4-262-01', 'ISO', 1),
                ('p', 'unknown', 'no-unit', 'ISO', 1)):
                db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                           (project, identifier, reference, '', 'title', json.dumps({'documentType': kind}), active))
        self.network = patch('socket.socket', side_effect=AssertionError('Network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def test_catalogue_excludes_duplicates_inactive_and_electrical(self):
        before = self.database.read_bytes()
        groups, evidence = catalogue(self.database)
        self.assertEqual(len(evidence), 3)
        self.assertIsNone(groups['p', 'H4-262', '单线图']['图纸数'])
        self.assertIsNone(groups['p', 'H4-262', '单线图']['管线数'])
        self.assertEqual(self.database.read_bytes(), before)

    def test_generation_failure_logs_details_and_preserves_last_publication(self):
        output = self.base / 'output'
        output.mkdir()
        marker = output / '简报推送状态.json'
        marker.write_text('previous publication', encoding='utf-8')
        with patch('briefing.render', side_effect=OSError('render unavailable')):
            with self.assertRaisesRegex(OSError, 'render unavailable'):
                generate(self.database, output, title='测试', day='2026-10-28')
        content = (output / '生成过程.log').read_text(encoding='utf-8')
        self.assertIn('管道简报 开始', content)
        self.assertIn('结束：失败', content)
        self.assertIn('OSError: render unavailable', content)
        self.assertIn('Traceback', content)
        self.assertEqual(marker.read_text(encoding='utf-8'), 'previous publication')

    def test_plants_sum_units_and_keep_separate_unit_sheets(self):
        groups = {}
        for unit, n in (('H1-001', 2), ('H1-002', 3), ('H2-001', 7), ('未识别单元', 1)):
            groups['p', unit, 'PCF'] = {metric: n for metric in METRICS}
        report = dict(file_inventory=[dict(project='p', unit=unit, kind='PCF')
                                     for _, unit, _ in groups], files=[], ledger_inventory=[], welds=[])
        path = self.base / 'plants.xlsx'
        create_workbook(path, groups, [], '厂区简报', '2026-10-26', report)
        wb = load_workbook(path)
        try:
            self.assertEqual(wb.sheetnames[:2], ['简报', '单元汇总'])
            self.assertNotIn('H1-001', wb.sheetnames)
            main = wb['简报']
            rows = [row for row in main.iter_rows(values_only=True) if row[0] == 'H1']
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0][1:4], (2, 0, 0))
            self.assertEqual(rows[1][1], 5)
            self.assertEqual(rows[2][1:7], (5, 5, 5, 5, 5, 5))
            self.assertEqual(main.max_column, 7)
            self.assertFalse(any(c.value in ('统计范围', '解析口径', '解析范围', '解析状态') for row in main for c in row))
            self.assertIn('简报说明', wb.sheetnames)
            self.assertFalse(any(c.value in ('英文名称', '中文名称', 'H1-001', 'H1-002') for row in main for c in row))
            for unit, count in (('H1-001', 2), ('H1-002', 3)):
                rows = [row for row in wb['单元汇总'].iter_rows(values_only=True) if row[0] == unit]
                self.assertEqual(rows[0][3], 1)
                self.assertEqual(rows[1][3], count)
                self.assertEqual(rows[2][3], count)
            # Unknown unit values must not erase the other unit's valid subtotal.
            groups['p', 'H1-002', 'PCF']['寸口量'] = None
        finally:
            wb.close()
        create_workbook(path, groups, [], '厂区简报', '2026-10-26', report)
        wb = load_workbook(path)
        try:
            weld_row = [row for row in wb['简报'].iter_rows(values_only=True) if row[0] == 'H1'][2]
            self.assertEqual(weld_row[1], 5)
            self.assertEqual(weld_row[2], 2)
        finally:
            wb.close()

    def test_page_breaks_keep_section_headers_with_data(self):
        groups = {('p', f'H{i}-001', 'PCF'): dict.fromkeys(METRICS, 1) for i in range(1, 11)}
        path = self.base / 'pages.xlsx'
        create_workbook(path, groups, [], '分页测试', '2026-10-27', dict(file_inventory=[], files=[], ledger_inventory=[], welds=[]))
        wb = load_workbook(path)
        try:
            sheet = wb['简报']
            for capacity in range(3, sheet.max_row + 1):
                pages = screenshot_pages(sheet, capacity)
                self.assertEqual([i for page in pages for i in page], list(range(3, sheet.max_row + 1)))
                for page in pages[:-1]:
                    self.assertNotIn(layout_role(sheet, page[-1]), ('header', 'section'))
                for page in pages:
                    if layout_role(sheet, page[0]) == 'section':
                        self.assertEqual(layout_role(sheet, page[1]), 'header')
                        self.assertIn(layout_role(sheet, page[2]), ('body', 'total'))
        finally:
            wb.close()

    def supplement(self, values):
        path = self.base / 'input.xlsx'
        write_template(path, {('p', 'H4-262')})
        wb = load_workbook(path)
        sheet = wb['数据录入']
        sheet.delete_rows(2, sheet.max_row)
        for col, value in enumerate(values, 1):
            sheet.cell(2, col).value = value
        wb.save(path)
        wb.close()
        return path

    def test_import_and_handoff_hashes_and_workbook_rerender(self):
        source = self.supplement(['p', 'H4-262', 'PCF', 12, None, 10, 25.5, 6, 15.5, 4, 10, '本地文件解析表'])
        state = generate(self.database, self.base / 'out', source, day='2026-10-23')
        log = (self.base / 'out/生成过程.log').read_text(encoding='utf-8')
        self.assertIn('结束：成功', log)
        self.assertIn('图片渲染完成', log)
        self.assertIn('总耗时=', log)
        self.assertTrue(state['ready'])
        self.assertFalse(state['sent'])
        for item in state['images']:
            self.assertEqual(hashlib.sha256(Path(item['path']).read_bytes()).hexdigest(), item['sha256'])
        wb = load_workbook(state['workbook'])
        sheet = wb['统计依据']
        row = next(row for row in sheet.iter_rows(values_only=True) if row[:3] == ('p', 'H4-262', 'PCF'))
        self.assertEqual(row[3:7], (12, None, 10, 25.5))
        wb['简报']['A1'] = '人工修改的标题'
        wb.save(state['workbook'])
        wb.close()
        second = generate(None, self.base / 'rerender', workbook=state['workbook'])
        self.assertTrue(second['ready'])

    def test_duplicate_formula_negative_and_inconsistent_values_rejected(self):
        cases = [(-1, 10, 6, 4), ('=1+2', 10, 6, 4), (1.5, 10, 6, 4), (1, 11, 6, 4)]
        for pipes, welds, shop, field in cases:
            path = self.supplement(['p', 'H4-262', 'PCF', pipes, None, welds, None, shop, None, field, None, '依据'])
            with self.assertRaises(ValueError):
                import_metrics(path)
        path = self.supplement(['p', 'H4-262', '单线图', 1, None, None, None, None, None, None, None, '依据'])
        wb = load_workbook(path)
        wb['数据录入'].append(['p', 'H4-262', '单线图'])
        wb.save(path)
        wb.close()
        with self.assertRaises(ValueError):
            import_metrics(path)

    def test_single_line_welding_metrics_are_rejected(self):
        path = self.supplement(['p', 'H4-262', '单线图', None, None, 10, None, None, None, None, None, '依据'])
        with self.assertRaisesRegex(ValueError, '仅PCF'):
            import_metrics(path)

    def test_render_failure_preserves_last_successful_publication(self):
        output = self.base / 'out'
        state = generate(self.database, output)
        before = Path(state['workbook']).read_bytes()
        with patch('briefing.render', side_effect=OSError('locked')):
            with self.assertRaises(OSError):
                generate(self.database, output)
        self.assertEqual(json.loads((output / '简报推送状态.json').read_text(encoding='utf-8')), state)
        self.assertEqual(Path(state['workbook']).read_bytes(), before)

    def test_multipage_and_excel_formula_not_published(self):
        with closing(sqlite3.connect(self.database)) as db, db:
            for i in range(30):
                db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',
                           ('p', str(i), f'LT_HE-H{10+i}-001-01', '', '', '{"documentType":"ISO"}', 1))
        groups, evidence = catalogue(self.database)
        for record in groups.values():
            record['管线数'] = 1
        with patch('briefing.catalogue', return_value=(groups, evidence)):
            state = generate(self.database, self.base / 'out')
        self.assertGreater(len(state['images']), 1)
        wb = load_workbook(state['workbook'])
        wb['简报']['A1'] = '=SUM(1,2)'
        wb.save(state['workbook'])
        wb.close()
        with self.assertRaises(ValueError):
            generate(None, self.base / 'reject', workbook=state['workbook'])

    def test_report_uses_layout_template_and_has_one_note_without_progress_paragraph(self):
        template = write_layout_template(self.base / 'layout.xlsx')
        source = load_workbook(template)
        title_font = copy(source['简报']['A1'].font)
        title_font.color = 'FF112233'
        source['简报']['A1'].font = title_font
        source['简报']['A' + str(source['简报'].max_row)] = '备注：项目统计口径。'
        source.save(template)
        source.close()
        report = dict(from_cache=True, analysis_complete=False, pending_analysis_files=42, failed_analysis_files=0,
                      unavailable_local_files=0, files=[], ledger_inventory=[], welds=[])
        with patch('briefing.LAYOUT_TEMPLATE', template), \
             patch('analysis_worker.cached_snapshot', return_value=({}, report, self.base / 'analysis.json')):
            state = generate(self.database, self.base / 'out', title='项目管道简报', use_cache=True)
        self.assertFalse(state['analysis_complete'])
        workbook = load_workbook(state['workbook'])
        try:
            sheet = workbook['简报']
            texts = [cell.value for row in sheet for cell in row if isinstance(cell.value, str)]
            self.assertTrue(sheet['A1'].value.startswith('项目管道简报（'))
            self.assertEqual(sheet['A1'].font.color.rgb, 'FF112233')
            self.assertEqual([value for value in texts if value.startswith('备注：')], ['备注：项目统计口径。'])
            self.assertFalse(any('仅统计已完成部分' in value for value in texts))
            self.assertIsNone(sheet['A2'].value)
        finally:
            workbook.close()


if __name__ == '__main__':
    unittest.main()
