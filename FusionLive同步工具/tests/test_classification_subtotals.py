from pathlib import Path
import tempfile
import unittest
from openpyxl import load_workbook
from briefing import create_workbook, METRICS, write_layout_template
from local_analysis import analyze


class ClassificationSubtotalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def report(self, rows):
        cached = dict(files=[], pipeline_results=[dict(project='p', unit='H4-262', source='PCF',
            pipeline='H4-262-L001', sha256='a', origin='sample.pcf', conflict=False, rows=rows)])
        return analyze(sources_override=[], cached_reports=[cached], output=self.root/'out')

    def test_unknown_weld_does_not_hide_known_shop_and_field(self):
        groups, report, _ = self.report([{'寸径':2,'_location':'Shop weld'},
                                       {'寸径':3,'_location':'field'}, {'寸径':4}])
        record = groups['p','H4-262','PCF']
        self.assertEqual(record['焊缝数'],3)
        self.assertEqual(record['预制焊缝数'],1)
        self.assertEqual(record['安装焊缝数'],1)
        self.assertEqual(record['预制寸口量'],2)
        self.assertEqual(record['安装寸口量'],3)
        self.assertEqual(record['未分类焊缝数'],1)
        self.assertEqual(len(report['welds']),3)
        target = self.root/'classified.xlsx'
        create_workbook(target,groups,[],'分类核对','2026-10-27',report,layout_template=False)
        wb = load_workbook(target)
        self.addCleanup(wb.close)
        self.assertEqual(wb['焊口分类核对']['G2'].value,1)
        main = [row for row in wb['简报'].iter_rows(values_only=True) if row[0]=='H4'][-1]
        self.assertEqual(main[1:7],(3,9,1,2,1,3))

    def test_absent_kind_is_unknown_when_unclassified_welds_exist(self):
        groups, _, _ = self.report([{'寸径':2,'_location':'shop'}, {'寸径':3}])
        record = groups['p','H4-262','PCF']
        self.assertEqual(record['预制焊缝数'],1)
        self.assertIsNone(record['安装焊缝数'])
        self.assertIsNone(record['安装寸口量'])

    def test_known_diameters_are_subtotaled_without_turning_unknown_into_zero(self):
        groups, _, _ = self.report([{'寸径':2,'_location':'shop'}, {'寸径':None,'_location':'shop'}])
        record = groups['p','H4-262','PCF']
        self.assertEqual(record['预制焊缝数'],2)
        self.assertEqual(record['预制寸口量'],2)
        self.assertEqual(record['口径未知焊缝数'],1)
        self.assertEqual(record['安装焊缝数'],0)
        groups, _, _ = self.report([{'寸径':None,'_location':'shop'}])
        self.assertIsNone(groups['p','H4-262','PCF']['预制寸口量'])

    def test_one_unit_sheet_keeps_projects_separate_and_total_metrics_aligned(self):
        groups = {(project, 'H4-262', 'PCF'):dict.fromkeys(METRICS,n)
                  for project,n in (('p1',2),('p2',3))}
        template = write_layout_template(self.root/'layout.xlsx')
        target = self.root/'units.xlsx'
        create_workbook(target,groups,[],'单元合并','2026-10-27',layout_template=template)
        wb = load_workbook(target)
        self.addCleanup(wb.close)
        self.assertEqual(wb.sheetnames[:2],['简报','单元汇总'])
        self.assertNotIn('H4-262',wb.sheetnames)
        sheet = wb['单元汇总']
        self.assertEqual(sheet.max_column,13)
        self.assertEqual(sheet.freeze_panes,'E5')
        rows = [r for r in sheet.iter_rows(values_only=True) if r[0]=='p1' and r[1]=='H4-262']
        self.assertEqual(len(rows),2)  # Unknown file inventory is omitted.
        self.assertEqual(rows[0][4],2)
        self.assertEqual(rows[1][4:10],(2,2,2,2,2,2))
        total = [r for r in sheet.iter_rows(values_only=True) if r[1]=='合计'][-1]
        self.assertEqual(total[4:10],(5,5,5,5,5,5))
