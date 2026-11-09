from contextlib import closing
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile
from openpyxl import load_workbook

from archive_index import current_members, verified_member
from analysis_worker import AnalysisQueue
from ledger import Ledger
from sync import export_csv


class ArchiveIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        (self.base/'downloads').mkdir()
        self.database=self.base/'runtime/ledger.sqlite3'
        self.ledger=Ledger(self.database,base_dir=self.base)
        self.addCleanup(self.ledger.close)
        nested=io.BytesIO()
        with zipfile.ZipFile(nested,'w') as z:z.writestr('notes/内部说明.txt','nested evidence')
        self.path=self.base/'downloads/package.zip'
        with zipfile.ZipFile(self.path,'w') as z:
            z.writestr('ISO/1-43-52-1167-FLC-AAE_0.pdf',b'%PDF fixture')
            z.writestr('Office/材料说明.docx',b'office fixture')
            z.writestr('CAD/layout.dwg',b'cad fixture')
            z.writestr('nested.zip',nested.getvalue())
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO folders(project,id,name,path,active) VALUES('p','folder','ISO','Engineering/PIP/ISO',1)")
            self.ledger.db.execute("INSERT INTO documents(project,id,folder_id,reference,title,metadata,active) VALUES('p','doc','folder','30201_50150_LT_HE-H1-000-13-61-001','PIPING ISOMETRIC INDEX','{}',1)")
            self.ledger.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,size,bytes,path,sha256,status,metadata,active)
                VALUES('p','zip','doc','folder','package.zip',?,?,?,?, 'done','{}',1)''',
                (self.path.stat().st_size,self.path.stat().st_size,'downloads/package.zip',hashlib.sha256(self.path.read_bytes()).hexdigest()))
            self.ledger.db.execute("INSERT INTO document_files VALUES('p','doc','folder','zip',1)")
        self.queue=AnalysisQueue(self.database,log=lambda _:None)
        self.addCleanup(self.queue.close)
        self.queue.discover()
        self.assertTrue(self.queue.process_next())

    def test_all_formats_nested_paths_and_physical_reads(self):
        rows=current_members(self.base)
        self.assertEqual(len(rows),5)
        self.assertTrue(any(r['filename'].endswith('.docx') for r in rows))
        child=next(r for r in rows if r['filename'].endswith('.txt'))
        self.assertIn('nested.zip!/',child['member_path'])
        self.assertEqual(child['parent_file_id'],'zip')
        _,path=verified_member(self.base,child['id'])
        self.assertEqual(path.read_text(),'nested evidence')
        self.assertEqual(len(list(self.ledger.db.execute('SELECT * FROM files'))),1)
        self.assertEqual(self.queue.db.execute('SELECT count(*) FROM current_archive_inventory').fetchone()[0],5)

    def test_excel_contains_children_and_worker_refreshes_after_extraction(self):
        self.queue.refresh_ledger_export(force=True)
        export=self.base/'exports/文件台账.xlsx'
        workbook=load_workbook(export,read_only=True)
        sheet=workbook['完整字段']
        headers=next(sheet.iter_rows(min_row=5,max_row=5,values_only=True))
        rows=[dict(zip(headers,row)) for row in sheet.iter_rows(min_row=6,values_only=True)]
        workbook.close()
        self.assertEqual(len(rows),6)
        children=[r for r in rows if r['记录类型']=='解压文件']
        self.assertEqual(len(children),5)
        iso=next(r for r in children if r['文件名'].endswith('.pdf'))
        self.assertEqual(iso['业务类型'],'地上新建管道单线图')
        self.assertEqual(iso['所属压缩包内容ID'],'zip')
        self.assertIn('ISO/',iso['包内完整路径'])
        self.assertTrue(Path(iso['本地路径']).is_file())

    def test_deleted_or_updated_parent_hides_stale_members(self):
        with self.ledger.db:self.ledger.db.execute("UPDATE files SET sha256='new-version' WHERE id='zip'")
        self.assertEqual(current_members(self.base),[])
        with self.ledger.db:self.ledger.db.execute("UPDATE files SET active=0")
        self.assertEqual(current_members(self.base),[])
        export_csv(self.ledger,supplemental=False,quiet=True)

    def test_explicit_ledger_source_is_read_only_and_does_not_use_default_database(self):
        alternate = self.base / 'runtime/alternate.sqlite3'
        with closing(sqlite3.connect(alternate)) as db:
            self.ledger.db.backup(db)
            with db:
                db.execute('UPDATE files SET active=0')
        before = alternate.read_bytes()
        self.assertEqual(current_members(self.base, ledger_path=alternate), [])
        self.assertEqual(len(current_members(self.base)), 5)
        self.assertEqual(alternate.read_bytes(), before)

    def test_local_collection_can_read_inactive_packages_but_never_history(self):
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET active=0')
        with self.queue.db:
            self.queue.db.execute('UPDATE jobs SET active=0')
        self.assertEqual(current_members(self.base), [])
        self.assertEqual(len(current_members(self.base, include_inactive=True)), 5)
        history = self.base / 'history'
        history.mkdir()
        archived = history / self.path.name
        self.path.rename(archived)
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET path=?', ('history/' + archived.name,))
        self.assertEqual(current_members(self.base, include_inactive=True), [])
        self.assertTrue(archived.is_file())

    def test_member_or_parent_tampering_and_cache_escape_are_rejected(self):
        member=next(r for r in current_members(self.base) if r['filename'].endswith('.dwg'))
        path=Path(member['path']);original=path.read_bytes()
        path.write_bytes(b'x'*len(original))
        with self.assertRaisesRegex(ValueError,'SHA256'):verified_member(self.base,member['id'])
        path.write_bytes(original)
        data=self.path.read_bytes();self.path.write_bytes(b'x'*len(data))
        with self.assertRaisesRegex(ValueError,'SHA256'):verified_member(self.base,member['id'])
        self.path.write_bytes(data)
        with self.queue.db:
            rows=list(self.queue.db.execute('SELECT rowid,payload FROM archive_inventory'))
            for row in rows:
                payload=json.loads(row['payload']);payload['path']=str(self.base/'secret.txt')
                self.queue.db.execute('UPDATE archive_inventory SET payload=? WHERE rowid=?',(json.dumps(payload),row['rowid']))
        self.assertEqual(current_members(self.base),[])

    def test_legacy_cache_is_read_without_reextracting(self):
        with self.queue.db:self.queue.db.execute('DELETE FROM archive_inventory')
        result=self.queue.db.execute('SELECT result_path FROM jobs').fetchone()[0]
        path=self.queue.cache/result
        report=json.loads(path.read_text(encoding='utf-8'))
        for row in report['archive_members']:row.pop('extracted_path',None)
        path.write_text(json.dumps(report),encoding='utf-8')
        self.assertEqual(len(current_members(self.base)),5)


if __name__=='__main__':unittest.main()
