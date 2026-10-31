"""File counts stay independent of pipeline parsing and drawing classification."""
from collections import Counter
from contextlib import closing
from pathlib import Path
import hashlib
import sqlite3
import tempfile
import unittest
import zipfile

from openpyxl import load_workbook
from analysis_worker import AnalysisQueue, cached_snapshot
from briefing import create_workbook, METRICS, write_layout_template
from business_type import classify_pdf
from local_analysis import analyze
from tests.test_local_analysis import PCF, IDF


class FileStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'downloads'
        self.root.mkdir()

    def test_pdf_classification_excludes_electrical_and_ambiguous_index(self):
        self.assertEqual(classify_pdf('4-262-52-0001-ABC-001-01.pdf')[0], '单线图')
        self.assertEqual(classify_pdf('30201_50150_LT_HE-H4-262-16-33-012.pdf')[0], '其他PDF')
        self.assertEqual(classify_pdf('30201_50150_LT_HE-H4-262-13-61-001.pdf')[0], '待分类PDF')
        self.assertEqual(classify_pdf('30201_50150_LT_HE-H4-262-13-20-001.pdf')[0], '其他PDF')

    def test_file_count_is_not_pipeline_count_and_bad_files_still_count(self):
        (self.root / 'H4-262-multi.idf').write_text(IDF.replace('999\n', '') + IDF.replace('L001', 'L002'), encoding='utf-8')
        with zipfile.ZipFile(self.root / 'H4-262.zip', 'w') as z:
            z.writestr('one.pcf', PCF)
            z.writestr('copy.pcf', PCF)
            z.writestr('bad.pcf', 'not a valid pcf')
            z.writestr('4-262-52-0001-ABC-001-01.pdf', b'%PDF fake, content parsing is not requested')
            z.writestr('30201_50150_LT_HE-H4-262-16-33-012.pdf', b'%PDF electrical')
            z.writestr('30201_50150_LT_HE-H4-262-13-61-001.pdf', b'%PDF index or unidentified')
            z.writestr('one.sha', b'unknown format')
        groups, report, _ = analyze(root=self.root, project='p', output=self.base/'out')
        counts = Counter(r['kind'] for r in report['file_inventory'])
        self.assertEqual(counts, {'PCF': 3, 'IDF': 1, '单线图': 1, '其他PDF': 1, '待分类PDF': 1, 'SHA': 1})
        self.assertEqual(groups['p', 'H2-233', 'IDF']['管线数'], 2)
        self.assertTrue(any(r['kind'] == 'PCF' and r['state'] == '分析失败' for r in report['file_inventory']))
        self.assertNotIn(('p', 'H4-262', 'SHA'), groups)

    def test_database_inventory_survives_failed_parsing_and_excludes_old_jobs(self):
        database = self.base/'runtime/ledger.sqlite3'
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE files(project,id,filename,path,status,bytes,sha256,active,PRIMARY KEY(project,id))')
            for identifier, content in (('H4-262-bad.pcf', b'invalid'), ('H4-262-good.pcf', PCF.encode()), ('H4-262.sha', b'future')):
                path = self.root / identifier
                path.write_bytes(content)
                db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,1)', ('p', identifier, identifier,
                    'downloads/'+identifier, 'done', len(content), hashlib.sha256(content).hexdigest()))
        queue = AnalysisQueue(database)
        self.addCleanup(queue.close)
        queue.discover()
        _, pending, _ = cached_snapshot(database, output=self.base/'snapshots')
        self.assertEqual(Counter(r['kind'] for r in pending['file_inventory']), {'PCF': 2, 'SHA': 1})
        while queue.process_next():
            pass
        counts = dict(queue.db.execute('SELECT kind,count(*) FROM current_file_inventory GROUP BY kind'))
        self.assertEqual(counts, {'PCF': 2, 'SHA': 1})
        _, snapshot, _ = cached_snapshot(database, output=self.base/'snapshots')
        self.assertEqual(Counter(r['kind'] for r in snapshot['file_inventory']), {'PCF': 2, 'SHA': 1})
        self.assertEqual(queue.db.execute('SELECT count(*) FROM current_pipeline_inventory').fetchone()[0], 1)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE files SET active=0')
        queue.discover()
        self.assertEqual(queue.db.execute('SELECT count(*) FROM current_file_inventory').fetchone()[0], 0)
        self.assertEqual(queue.db.execute('SELECT count(*) FROM file_inventory').fetchone()[0], 3)

    def test_duplicate_loose_files_keep_file_counts_and_share_the_parsed_unit(self):
        for name in ('one.pcf', 'copy.pcf'):
            (self.root/name).write_text(PCF, encoding='utf-8')
        groups, report, _ = analyze(root=self.root, project='p', output=self.base/'out')
        self.assertEqual(len(report['file_inventory']), 2)
        self.assertEqual({r['unit'] for r in report['file_inventory']}, {'H4-262'})
        self.assertEqual(groups['p', 'H4-262', 'PCF']['管线数'], 1)

    def test_main_sheet_has_three_sections_and_only_other_sheet_has_differences(self):
        groups = {('p', 'H4-262', s): {**dict.fromkeys(METRICS), '管线数': n}
                  for s, n in (('PCF', 2), ('单线图', 3))}
        report = dict(file_inventory=[dict(project='p', unit='H4-262', origin='one', kind='PCF')],
                      files=[], ledger_inventory=[], welds=[])
        path = self.base/'report.xlsx'
        template = write_layout_template(self.base/'template.xlsx')
        create_workbook(path, groups, [], '管道简报', '2026-10-26', report, layout_template=template)
        wb = load_workbook(path)
        self.addCleanup(wb.close)
        main = wb['简报']
        self.assertEqual([main.cell(4, i).value for i in range(1, 5)],
                         ['厂区', 'PCF文件数', '单线图PDF文件数', 'SHA文件数'])
        self.assertEqual(main['A5'].value, 'H4')
        self.assertEqual(main['B5'].value, 1)
        self.assertEqual(main['B8'].value, 'PCF管线数')
        self.assertEqual(main['B9'].value, 2)
        self.assertEqual(main['D9'].value, '—')
        self.assertEqual(wb['单元汇总']['D5'].value, 1)
        self.assertFalse(any('差值' in str(c.value) for row in main for c in row))
        self.assertEqual(wb['管线差值']['C2'].value, 1)
        self.assertIsNone(wb['管线差值']['D2'].value)
        weld_header = next(row for row in main if row[1].value == '焊缝数')
        self.assertEqual(weld_header[6].fill.fgColor.rgb, weld_header[1].fill.fgColor.rgb)
        self.assertEqual(weld_header[6].font.color.rgb, weld_header[1].font.color.rgb)


if __name__ == '__main__':
    unittest.main()
