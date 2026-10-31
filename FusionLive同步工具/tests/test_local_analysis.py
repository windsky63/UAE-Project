"""Offline fixtures: real PCF syntax in ZIPs; socket creation is forbidden."""
from contextlib import closing
import io
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from local_analysis import analyze, extract_zip, ledger_sources, parse_file, model_weld_rows, unit_from
from briefing import generate

PCF = '''UNITS-BORE MM
UNITS-CO-ORDS MM
PIPELINE-REFERENCE H4-262-L001
PIPE
    END-POINT 0 0 0 50
    END-POINT 1000 0 0 50
    ITEM-CODE P1
    DESCRIPTION PIPE BW
WELD
    CO-ORDS 1000 0 0 50
    SKEY WELD
    WELD-LOCATION SHOP
PIPE
    END-POINT 1000 0 0 50
    END-POINT 1000 1000 0 50
    ITEM-CODE P2
    DESCRIPTION PIPE BW
'''
IDF = '''-6 H2-233-L001
100 0 0 0 100000 0 0 50 1 0 0 0 0 0
999
'''


class LocalAnalysisTests(unittest.TestCase):
    def test_construction_pcf_unit_precedes_archive_directory(self):
        name = '2-272-52-0001-AGX-WAB-pcf.pcf'
        self.assertEqual(unit_from(name), 'H2-272')
        self.assertEqual(unit_from('H4-262/package.zip!/models/'+name), 'H2-272')
        self.assertEqual(unit_from('H4-262-L001', name), 'H4-262')
        self.assertEqual(unit_from('2-272-52-0001-AGX-WAB-extra.pcf'), '未识别单元')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / 'downloaded'
        self.root.mkdir()
        self.block = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.block.start()
        self.addCleanup(self.block.stop)

    def run_analysis(self, **kwargs):
        return analyze(root=self.root, project='p', output=self.base / 'analysis', **kwargs)

    def test_nested_zip_and_loose_duplicate_are_counted_once(self):
        inner = io.BytesIO()
        with zipfile.ZipFile(inner, 'w') as z:
            z.writestr('H4-262/a.pcf', PCF)
        with zipfile.ZipFile(self.root / 'native.zip', 'w') as z:
            z.writestr('inner.zip', inner.getvalue())
            z.writestr('copy.pcf', PCF)
        groups, report, path = self.run_analysis()
        r = groups['p', 'H4-262', 'PCF']
        self.assertEqual(r['管线数'], 1)
        self.assertEqual(r['焊缝数'], 1)
        self.assertEqual(r['寸口量'], 2)
        self.assertEqual(r['预制焊缝数'], 1)
        self.assertEqual(r['安装焊缝数'], 0)
        self.assertTrue(path.exists())
        self.assertFalse(report['network_accessed'])
        self.assertTrue(any(v['state'] == '内容重复，跳过' for v in report['files']))

    def test_pcf_export_suffix_does_not_block_full_content_analysis(self):
        name = '2-272-52-0001-AGX-WAB-pcf.pcf'
        pipeline = '2-272-52-0001-AGX-WAB'
        (self.root / name).write_text(PCF.replace('H4-262-L001', pipeline), encoding='utf-8')
        groups, report, _ = self.run_analysis()
        self.assertEqual(groups['p', 'H2-272', 'PCF']['管线数'], 1)
        self.assertEqual(groups['p', 'H2-272', 'PCF']['焊缝数'], 1)
        self.assertEqual(report['pipeline_results'][0]['pipeline'], pipeline.lower())
        self.assertEqual(report['files'][0]['state'], '已解析')
        self.assertEqual(report['files'][0]['unit'], 'H2-272')
        self.assertTrue((self.root / name).is_file())

    def test_unknown_pcf_location_uses_embedded_algorithm(self):
        (self.root / 'a.pcf').write_text(PCF.replace('WELD-LOCATION SHOP', 'COMMENT UNKNOWN'), encoding='utf-8')
        groups, _, _ = self.run_analysis()
        self.assertEqual(groups['p', 'H4-262', 'PCF']['预制焊缝数'], 1)
        groups, _, _ = self.run_analysis(prefab_rule='no-small')
        self.assertEqual(groups['p', 'H4-262', 'PCF']['预制焊缝数'], 1)

    def test_different_contents_same_pipeline_never_inflate_welds(self):
        (self.root / 'a.pcf').write_text(PCF, encoding='utf-8')
        (self.root / 'revision.pcf').write_text(PCF.replace('1000', '2000'), encoding='utf-8')
        groups, report, _ = self.run_analysis()
        self.assertIsNone(groups['p', 'H4-262', 'PCF']['焊缝数'])
        self.assertIn('版本', str(report['files']))

    def test_multi_pipeline_idf_is_split_in_output_not_source(self):
        path = self.root / 'multi.idf'
        path.write_text(IDF.replace('999\n', '') + IDF.replace('L001', 'L002'), encoding='utf-8')
        original = path.read_bytes()
        groups, _, _ = self.run_analysis()
        self.assertEqual(groups['p', 'H2-233', 'IDF']['管线数'], 2)
        self.assertNotIn(('p', 'H2-233', 'PCF'), groups)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse((self.root / 'newidf').exists())

    def test_five_zip_layers_are_analyzed_and_sixth_layer_is_reported(self):
        for layers in (5, 6):
            content = PCF.encode('utf-8')
            filename = 'model.pcf'
            for level in range(layers):
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as archive:
                    archive.writestr(filename, content)
                content, filename = stream.getvalue(), f'level-{level}.zip'
            root = self.root / str(layers)
            root.mkdir()
            (root / 'H4-262-native.zip').write_bytes(content)
            groups, report, _ = analyze(root=root, project='p', output=self.base / f'result-{layers}')
            if layers == 5:
                self.assertFalse(any(r['state'] == '分析失败' for r in report['files']))
                self.assertEqual(groups['p', 'H4-262', 'PCF']['管线数'], 1)
            else:
                self.assertTrue(any('嵌套ZIP/7z超过5层' in r.get('detail', '') for r in report['files']))
                self.assertIsNone(groups['p', 'H4-262', 'PCF']['管线数'])

    def test_zip_rejects_traversal_links_duplicate_and_resource_limits(self):
        for i, entries in enumerate(([('../escape.pcf', PCF)], [('C:/escape.pcf', PCF)])):
            path = self.base / f'{i}.zip'
            with zipfile.ZipFile(path, 'w') as z:
                for name, content in entries:
                    z.writestr(name, content)
            target = self.base / f'extract-{i}'
            with self.assertRaises(ValueError):
                extract_zip(path, target)
            self.assertFalse(target.exists())
        link = self.base / 'link.zip'
        with zipfile.ZipFile(link, 'w') as z:
            info = zipfile.ZipInfo('link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            z.writestr(info, '../outside')
        with self.assertRaises(ValueError):
            extract_zip(link, self.base / 'link-out')
        path = self.base / 'limited.zip'
        with zipfile.ZipFile(path, 'w') as z:
            z.writestr('a.pcf', PCF)
        with self.assertRaises(ValueError):
            extract_zip(path, self.base / 'limited', max_bytes=10)

    def test_missing_server_files_do_not_trigger_download_or_zero_counts(self):
        database = self.base / 'runtime/ledger.sqlite3'
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE files(project,id,filename,path,status,bytes,active)')
            db.execute("INSERT INTO files VALUES('p','a','native.zip','downloads/native.zip','done',200,1)")
        before = database.read_bytes()
        sources, inventory = ledger_sources(database)
        self.assertEqual(sources, [])
        self.assertEqual(len(inventory), 1)
        groups, report, _ = analyze(database=database, output=self.base / 'output')
        self.assertEqual(groups, {})
        self.assertEqual(database.read_bytes(), before)

    def test_bad_archive_and_invalid_pcf_are_audited_as_missing_not_zero(self):
        (self.root / 'H4-262-broken.zip').write_bytes(b'not a zip')
        (self.root / 'H4-262-bad.pcf').write_text('PIPELINE-REFERENCE H4-262-L001\n', encoding='utf-8')
        groups, report, _ = self.run_analysis()
        self.assertTrue(any(row['state'] == '分析失败' for row in report['files']))
        self.assertIsNone(groups['p', 'H4-262', 'PCF']['管线数'])

    def test_missing_diameter_does_not_report_zero_inches(self):
        (self.root / 'a.pcf').write_text(PCF.replace(' 50', ' 91'), encoding='utf-8')
        groups, _, _ = self.run_analysis()
        self.assertEqual(groups['p', 'H4-262', 'PCF']['焊缝数'], 1)
        self.assertIsNone(groups['p', 'H4-262', 'PCF']['寸口量'])

    def test_generated_files_cannot_be_written_in_download_or_input_directories(self):
        with self.assertRaises(ValueError):
            analyze(root=self.root, output=self.root / 'generated')
        self.assertFalse((self.root / 'generated').exists())
        database = self.base / 'runtime/ledger.sqlite3'
        download_output = self.base / 'downloads/reports'
        with self.assertRaises(ValueError):
            generate(database, download_output)
        self.assertFalse(download_output.exists())
        with self.assertRaises(ValueError):
            analyze(database=database, output=self.base / 'downloads/extracted')
        self.assertFalse((self.base / 'downloads').exists())

    def test_analysis_keeps_entire_input_tree_unchanged(self):
        with zipfile.ZipFile(self.root / 'native.zip', 'w') as z:
            z.writestr('multi.idf', IDF.replace('999\n', '') + IDF.replace('L001', 'L002'))
            z.writestr('model.pcf', PCF)
        before = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                  for p in self.root.rglob('*') if p.is_file()}
        self.run_analysis()
        after = {p.relative_to(self.root): (p.read_bytes(), p.stat().st_mtime_ns)
                 for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before, after)

    def test_report_can_analyze_zip_and_include_audit(self):
        with zipfile.ZipFile(self.root / 'native.zip', 'w') as z:
            z.writestr('model.pcf', PCF)
            z.writestr('model.idf', IDF)
        dbpath = self.base / 'empty.sqlite3'
        with closing(sqlite3.connect(dbpath)) as db, db:
            db.execute('CREATE TABLE documents(project,id,reference,filename,title,metadata,active)')
        state = generate(dbpath, self.base / 'brief', files_root=self.root, project='p')
        from openpyxl import load_workbook
        wb = load_workbook(state['workbook'], read_only=True)
        try:
            self.assertIn('文件分析记录', wb.sheetnames)
            self.assertIn('解析焊口明细', wb.sheetnames)
            self.assertEqual(wb['解析焊口明细'].max_row, 2)
            self.assertTrue(any('二、各厂区已解析管线数量' in str(row[0]) for row in wb['简报'].iter_rows(values_only=True)))
            self.assertTrue(any('PCF/IDF管线数' in row for row in wb['简报'].iter_rows(values_only=True)))
        finally:
            wb.close()
        self.assertTrue(Path(state['analysis_file']).exists())


if __name__ == '__main__':
    unittest.main()
