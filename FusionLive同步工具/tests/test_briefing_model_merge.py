"""Actual workbook headers, ISO filename recognition and cross-format dedup."""
from pathlib import Path
import tempfile
import unittest

from openpyxl import load_workbook
from briefing import create_workbook, prepare_model_summary, METRICS
from business_type import classify_pdf
from local_analysis import analyze, discovered_files


def pipeline(source, name='L1', *, kind='shop', nps=4, conflict=False, unit='H4-262', project='p'):
    return dict(project=project, unit=unit, source=source, pipeline=name, conflict=conflict,
        rows=[{'寸径':nps, '_classification_engine':'fixture', '_classification':kind}])


def report(pipes, files=None):
    return dict(pipeline_results=pipes, prefab_rule='none', files=[], ledger_inventory=[], welds=[],
                file_inventory=files if files is not None else [dict(project=p['project'], unit=p['unit'], kind=p['source']) for p in pipes])


class ModelMergeTests(unittest.TestCase):
    def test_only_one_format_has_only_that_header_and_weld_results(self):
        for source in ('PCF', 'IDF'):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                groups = {('p','H4-262',source): {metric:1 for metric in METRICS}}
                path = Path(directory) / 'briefing.xlsx'
                create_workbook(path, groups, [], 'test', '2026-10-28', report([pipeline(source)]), layout_template=False)
                wb = load_workbook(path)
                try:
                    values = [cell.value for row in wb['简报'] for cell in row]
                    other = 'IDF' if source == 'PCF' else 'PCF'
                    self.assertIn(source+'文件数', values)
                    self.assertIn(source+'管线数', values)
                    self.assertIn('三、'+source+' 解析焊接量（设计量）', values)
                    self.assertNotIn(other+'文件数', values)
                    self.assertNotIn('PCF/IDF文件数', values)
                    self.assertEqual(wb['简报']['B5'].value, 1)
                finally:
                    wb.close()

    def test_mixed_formats_deduplicate_same_pipeline_and_add_idf_only(self):
        pipes = [pipeline('PCF','L1',nps=4), pipeline('IDF','l1',nps=99), pipeline('IDF','L2',kind='field',nps=2)]
        data = report(pipes)
        groups, label, audit = prepare_model_summary({}, data)
        record = groups['p','H4-262',label]
        self.assertEqual(label, 'PCF/IDF')
        self.assertEqual(record['管线数'], 2)
        self.assertEqual(record['焊缝数'], 2)
        self.assertEqual(record['寸口量'], 6)
        self.assertEqual(record['预制寸口量'], 4)
        self.assertEqual(record['安装寸口量'], 2)
        self.assertEqual(audit[0][3], 'PCF')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'mixed.xlsx'
            create_workbook(path, {}, [], 'test', '2026-10-28', data, layout_template=False)
            wb = load_workbook(path)
            try:
                self.assertEqual(wb['简报']['B4'].value, 'PCF/IDF文件数')
                self.assertEqual(wb['简报']['B5'].value, 3)  # Files remain three, pipelines are two.
                self.assertEqual(wb['简报']['B9'].value, 2)
                rows = [row for row in wb['简报'].iter_rows(values_only=True) if row[0] == 'H4']
                self.assertEqual(rows[2][1:7], (2,6,1,4,1,2))
                self.assertEqual(wb['PCF-IDF合并核对'].max_row, 2)
            finally:
                wb.close()

    def test_conflicting_pcf_does_not_hide_valid_idf(self):
        groups, label, _ = prepare_model_summary({}, report([pipeline('PCF',conflict=True,nps=99), pipeline('IDF',nps=2)]))
        self.assertEqual(groups['p','H4-262',label]['寸口量'], 2)

    def test_same_number_in_different_units_or_projects_is_not_deduplicated(self):
        pipes = [pipeline('PCF'), pipeline('IDF',unit='H1-001'), pipeline('IDF',project='other')]
        groups, _, _ = prepare_model_summary({}, report(pipes))
        self.assertEqual(sum(row['管线数'] for row in groups.values()), 3)

    def test_unknown_bore_and_classification_are_not_false_zero(self):
        groups, label, _ = prepare_model_summary({}, report([pipeline('PCF',nps=None,kind=None), pipeline('IDF',name='L2',kind='field',nps=2)]))
        record = groups['p','H4-262',label]
        self.assertEqual(record['焊缝数'], 2)
        self.assertEqual(record['寸口量'], 2)
        self.assertEqual(record['未分类焊缝数'], 1)
        self.assertIsNone(record['预制焊缝数'])

    def test_no_pipeline_details_does_not_blindly_add_overlapping_totals(self):
        groups = {('p','H4-262',source): {'管线数':1,'焊缝数':10} for source in ('PCF','IDF')}
        merged, label, _ = prepare_model_summary(groups, None)
        self.assertIsNone(merged['p','H4-262',label]['焊缝数'])


class IsoFileCountTests(unittest.TestCase):
    def test_download_suffix_and_exclusions(self):
        self.assertEqual(classify_pdf('5-521-52-0141-HCX-001-01__rev_A__21249112__a67431f298.pdf')[0], '单线图')
        for code in ('43','44'):
            self.assertEqual(classify_pdf(f'30201_50150_LT_HE-H4-262-13-{code}-001.pdf')[0], '其他PDF')
        self.assertEqual(classify_pdf('5-521-45-0141-HCX-001-01.pdf')[0], '其他PDF')
        self.assertEqual(classify_pdf('5-521-52-0141-HCX-001-01/not-an-iso.pdf')[0], '待分类PDF')

    def test_pdf_count_reaches_real_workbook_without_content_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            inputs = base/'input'
            inputs.mkdir()
            for name in ('5-521-52-0141-HCX-001-01__rev_A__21249112__a67431f298.pdf',
                         '5-521-53-0141-HCX-001-02.pdf', '30201_50150_LT_HE-H5-521-13-61-001.pdf',
                         '30201_50150_LT_HE-H5-521-13-43-001.pdf'):
                (inputs/name).write_bytes(b'%PDF filename classification only')
            groups, data, _ = analyze(root=inputs, output=base/'output')
            iso = [row for row in data['file_inventory'] if row['kind']=='单线图']
            self.assertEqual(len(iso), 2)
            self.assertEqual({row['unit'] for row in iso}, {'H5-521'})
            target = base/'report.xlsx'
            create_workbook(target, groups, [], 'test', '2026-10-28', data, layout_template=False)
            wb = load_workbook(target)
            try:
                self.assertEqual(wb['简报']['C5'].value, 2)
            finally:
                wb.close()

    def test_missing_or_unfinished_ledger_pdf_is_not_counted(self):
        item = dict(project='p',filename='5-521-52-0141-HCX-001-01.pdf',path='missing.pdf',
                    status='done',state='本地文件未完成或大小不符，未分析')
        self.assertEqual(discovered_files([], [item]), [])
