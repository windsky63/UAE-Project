from contextlib import closing
import hashlib
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import warnings
import zipfile

import py7zr

from local_analysis import extract_zip, extract_7z
from local_analysis import analyze
from tests.test_local_analysis import PCF, IDF


class ArchiveAndPcfTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base/'input'
        self.root.mkdir()
        self.block = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.block.start()
        self.addCleanup(self.block.stop)

    def analyze(self):
        return analyze(root=self.root, project='p', output=self.base/'output')

    def zip(self, entries):
        path = self.root/'H4-262-files.zip'
        with warnings.catch_warnings(), zipfile.ZipFile(path, 'w') as archive:
            warnings.simplefilter('ignore', UserWarning)
            for name, content in entries:
                archive.writestr(name, content)
        return path

    def seven(self, entries):
        path = self.root/'H4-262-files.7z'
        with py7zr.SevenZipFile(path, 'w') as archive:
            for name, content in entries:
                archive.writestr(content, name)
        return path

    def test_identical_duplicate_paths_are_crc_checked_and_deduplicated(self):
        path = self.zip([('a.pcf', PCF), ('a.pcf', PCF), ('A.pcf', PCF)])
        original = path.read_bytes()
        records = []
        files = extract_zip(path, self.base/'extract', audit=records)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_text(), PCF)
        self.assertEqual(sum(r['state']=='同路径同内容，去重' for r in records), 2)
        self.assertEqual(path.read_bytes(), original)

    def test_distinct_duplicate_contents_are_both_preserved_with_original_names_audited(self):
        path = self.zip([('a.pcf', PCF), ('A.pcf', PCF.replace('L001','L002'))])
        records = []
        files = extract_zip(path, self.base/'extract', audit=records)
        self.assertEqual(len(files), 2)
        self.assertEqual(len({str(p).casefold() for p in files}), 2)
        self.assertEqual({r['member'] for r in records}, {'a.pcf','A.pcf'})
        self.assertEqual(records[1]['state'], '同路径异内容，分别保留')
        groups, report, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'], 2)
        self.assertEqual(len(report['archive_members']), 2)

    def test_repeated_directory_records_and_file_directory_collision_are_preserved(self):
        path = self.zip([('dir/', b''), ('DIR/', b''), ('dir', b'file'), ('dir/a.pcf', PCF)])
        files = extract_zip(path, self.base/'extract')
        self.assertEqual(len(files), 2)
        self.assertEqual({p.read_bytes() for p in files}, {b'file', PCF.encode()})

    def test_duplicate_same_pipeline_different_geometry_still_requires_review(self):
        self.zip([('a.pcf', PCF), ('a.pcf', PCF.replace('1000','2000'))])
        groups, _, _ = self.analyze()
        self.assertIsNone(groups['p','H4-262','PCF']['管线数'])
        self.assertIsNone(groups['p','H4-262','PCF']['焊缝数'])

    def test_crc_failure_never_publishes_partial_extraction(self):
        path = self.zip([('a.pcf',PCF),('a.pcf',PCF)])
        with zipfile.ZipFile(path) as archive:
            last = archive.infolist()[1]
            offset = last.header_offset+30+len(last.filename.encode())+len(last.extra)
        data = bytearray(path.read_bytes())
        data[offset] ^= 1
        path.write_bytes(data)
        target = self.base/'extract'
        with self.assertRaises(zipfile.BadZipFile):extract_zip(path, target)
        self.assertFalse(target.exists())

    def test_sevenzip_extracts_and_analyzes_real_pcf_and_idf(self):
        path = self.seven([('H4-262/model.pcf', PCF), ('H2-233/model.idf', IDF)])
        original = path.read_bytes()
        groups, report, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'], 1)
        self.assertEqual(groups['p','H2-233','IDF']['管线数'], 1)
        self.assertEqual(report['files'][0]['state'], '已解压')
        self.assertEqual(path.read_bytes(), original)

    def test_sevenzip_duplicate_paths_are_preserved_and_deduplicated(self):
        path = self.seven([('a.pcf', PCF), ('a.pcf', PCF), ('A.pcf', PCF.replace('L001','L002'))])
        records = []
        files = extract_7z(path, self.base/'extract', audit=records)
        self.assertEqual(len(files), 2)
        self.assertEqual(sum(r['state']=='同路径同内容，去重' for r in records), 1)
        self.assertEqual(len(records), 3)

    def test_sevenzip_keeps_empty_regular_files(self):
        path = self.seven([('empty.txt',''),('model.pcf',PCF)])
        files = extract_7z(path,self.base/'extract')
        self.assertEqual(len(files),2)
        self.assertEqual(sum(p.stat().st_size==0 for p in files),1)

    def test_sevenzip_result_and_member_inventory_are_cached_by_worker(self):
        from analysis_worker import AnalysisQueue,cached_snapshot
        path = self.seven([('model.pcf',PCF)])
        database = self.base/'runtime/ledger.sqlite3'
        database.parent.mkdir()
        with closing(sqlite3.connect(database)) as db,db:
            db.execute('CREATE TABLE files(project,id,filename,path,status,bytes,sha256,active)')
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,1)',
                ('p','one',path.name,str(path),'done',path.stat().st_size,hashlib.sha256(path.read_bytes()).hexdigest()))
        queue=AnalysisQueue(database,base_dir=self.base)
        try:
            self.assertEqual(queue.discover(),1)
            self.assertTrue(queue.process_next())
            self.assertEqual(queue.db.execute('SELECT state FROM jobs').fetchone()[0],'done')
            with patch('local_analysis.extract_7z',side_effect=AssertionError('must reuse cache')):
                groups,report,_=cached_snapshot(database,base_dir=self.base,output=self.base/'snapshot')
            self.assertEqual(groups['p','H4-262','PCF']['管线数'],1)
            self.assertEqual(len(report['archive_members']),1)
        finally:
            queue.close()

    def test_sevenzip_library_generated_duplicate_suffix_collision_is_preserved(self):
        path = self.seven([('a.pcf', PCF), ('a.pcf', PCF.replace('L001','L002')),
                           ('a.pcf_0', 'third-entry')])
        records = []
        files = extract_7z(path, self.base/'extract', audit=records)
        self.assertEqual(len(files), 3)
        self.assertEqual({p.read_text() for p in files}, {PCF, PCF.replace('L001','L002'), 'third-entry'})

    def test_sevenzip_limits_and_password_failure_do_not_publish(self):
        path = self.seven([('a.pcf', PCF)])
        with self.assertRaises(ValueError):extract_7z(path,self.base/'limit',max_file_bytes=10)
        self.assertFalse((self.base/'limit').exists())
        path = self.root/'password.7z'
        with py7zr.SevenZipFile(path,'w',password='test-only') as archive:
            archive.writestr(PCF,'a.pcf')
        with self.assertRaisesRegex(ValueError,'密码'):extract_7z(path,self.base/'password')
        self.assertFalse((self.base/'password').exists())

    def test_mixed_zip_and_sevenzip_nesting_uses_shared_depth_limit(self):
        seven = self.seven([('a.pcf', PCF)])
        seven_bytes = seven.read_bytes()
        seven.unlink()
        content, name = seven_bytes, 'inner.7z'
        for i in range(4):
            stream = io.BytesIO()
            with zipfile.ZipFile(stream,'w') as archive:archive.writestr(name, content)
            content, name = stream.getvalue(), f'layer-{i}.zip'
        (self.root/name).write_bytes(content)
        groups, _, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'], 1)
        stream = io.BytesIO()
        with zipfile.ZipFile(stream,'w') as archive:archive.writestr(name, content)
        (self.root/name).unlink()
        (self.root/'six-layers.zip').write_bytes(stream.getvalue())
        _, report, _ = self.analyze()
        self.assertTrue(any('超过5层' in r.get('detail','') for r in report['files']))

    def test_pcf_indented_header_tabs_and_duplicate_same_reference_are_valid(self):
        text = PCF.replace('PIPELINE-REFERENCE H4-262-L001',
            '  pipeline-reference H4-262-L001\n\tPIPELINE-REFERENCE H4-262-L001')
        text = text.replace('    END-POINT','\tEND-POINT')
        path = self.root/'indented.pcf'
        path.write_text(text,encoding='utf-8-sig')
        original = path.read_bytes()
        groups, report, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'],1)
        self.assertEqual(groups['p','H4-262','PCF']['焊缝数'],1)
        self.assertEqual(path.read_bytes(), original)
        self.assertTrue(any('重复声明合并1次' in r.get('detail','') for r in report['files']))

    def test_utf16_pcf_is_decoded_and_normalized_without_changing_source(self):
        for encoding in ('utf-16','utf-16-le','utf-16-be'):
            path = self.root/(encoding+'.pcf')
            path.write_bytes(PCF.encode(encoding))
        groups, _, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'],1)

    def test_multiple_explicit_pipeline_sections_are_not_combined_geometrically(self):
        (self.root/'multi.pcf').write_text(PCF+PCF.replace('L001','L002'),encoding='utf-8')
        groups, report, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'],2)
        self.assertEqual(groups['p','H4-262','PCF']['焊缝数'],2)
        self.assertEqual(len(report['pipeline_results']),2)

    def test_same_reference_in_separate_different_sections_is_not_silently_dropped(self):
        (self.root/'parts.pcf').write_text(PCF+PCF.replace('1000','2000'),encoding='utf-8')
        groups, _, _ = self.analyze()
        self.assertIsNone(groups['p','H4-262','PCF']['管线数'])

    def test_repeated_identical_complete_pcf_sections_are_counted_once(self):
        (self.root/'repeated.pcf').write_text(PCF+PCF,encoding='utf-8')
        groups, _, _ = self.analyze()
        self.assertEqual(groups['p','H4-262','PCF']['管线数'],1)
        self.assertEqual(groups['p','H4-262','PCF']['焊缝数'],1)

    def test_missing_or_ambiguous_pipeline_headers_are_specific_failures(self):
        for name,text in [('missing',PCF.replace('PIPELINE-REFERENCE H4-262-L001\n','')),
                          ('ambiguous',PCF.replace('PIPELINE-REFERENCE H4-262-L001',
                              'PIPELINE-REFERENCE H4-262-L001\nPIPELINE-REFERENCE H4-262-L002'))]:
            (self.root/(name+'.pcf')).write_text(text,encoding='utf-8')
        _, report, _ = self.analyze()
        details = [r.get('detail','') for r in report['files']]
        self.assertTrue(any('未识别到有效PIPELINE-REFERENCE' in s for s in details))
        self.assertTrue(any('没有可分段的元件边界' in s for s in details))


if __name__ == '__main__':unittest.main()
