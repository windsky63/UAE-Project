"""Exercise only offline export functions; login and download are blocked."""
from contextlib import redirect_stdout
import io
import importlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from openpyxl import load_workbook

from ledger import Ledger
from paths import exports_dir
from scope_review import export_scope_review
from sync import export_csv


class ExportLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base / 'runtime/ledger.sqlite3', base_dir=self.base)
        self.addCleanup(self.ledger.close)
        self.ledger.root(Mock(project_id='p', cabinet_id='root', project_name='offline fixture'))

    def test_all_offline_exports_use_exports_directory(self):
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), \
             patch('sync.download', side_effect=AssertionError('download forbidden')), \
             patch('fusion_client.FusionClient.login', side_effect=AssertionError('login forbidden')), \
             redirect_stdout(io.StringIO()):
            export_csv(self.ledger)
            export_scope_review(self.ledger, 'p')
        expected = {'文件台账.xlsx', '项目总体文件目录.txt', '本次盘点变更.csv', '关联核对异常.csv',
                    '目录范围汇总.csv', '人工范围确认说明.txt'}
        self.assertEqual({p.name for p in (self.base / 'exports').iterdir()}, expected)
        self.assertTrue(all(not (self.base / name).exists() for name in expected))
        self.assertFalse((self.base / 'downloads').exists())

    def test_export_directory_is_relative_to_the_tool_directory(self):
        self.assertEqual(exports_dir(self.base), self.base / 'exports')

    def test_business_columns_are_in_excel_and_desktop_ledger(self):
        layout='30201_50150_LT_HE-H4-262-13-20-001'
        electric='30201_50150_LT_HE-H1-001-16-60-100'
        with self.ledger.db:
            for identity,reference in [('layout',layout),('electric',electric)]:
                self.ledger.db.execute('''INSERT INTO documents(project,id,folder_id,reference,filename,title,metadata,active,related_state)
                    VALUES('p',?,'root',?,?,'fixture','{}',1,'done')''',(identity,reference,reference+'.pdf'))
            for identity,name,parent in [('own',layout+'.pdf','electric'),('inherit','vendor.zip','layout'),('conflict','shared.zip','layout')]:
                self.ledger.db.execute('''INSERT INTO files(project,id,document_id,folder_id,filename,metadata,status,active)
                    VALUES('p',?,?,'root',?,'{}','pending',1)''',(identity,parent,name))
                self.ledger.db.execute("INSERT INTO document_files VALUES('p',?,'root',?,1)",(parent,identity))
            self.ledger.db.execute("INSERT INTO document_files VALUES('p','electric','root','conflict',1)")
        with redirect_stdout(io.StringIO()):export_csv(self.ledger)
        workbook=load_workbook(self.base/'exports/文件台账.xlsx',read_only=True)
        sheet=workbook['完整字段']
        headers=next(sheet.iter_rows(min_row=5,max_row=5,values_only=True))
        rows=[dict(zip(headers,row)) for row in sheet.iter_rows(min_row=6,values_only=True)]
        workbook.close()
        own=next(r for r in rows if r['内容ID']=='own')
        inherited=next(r for r in rows if r['内容ID']=='inherit')
        conflicts=[r for r in rows if r['内容ID']=='conflict']
        self.assertEqual(own['业务类型'],'管道布置平面及立面图')
        self.assertEqual(own['业务类型来源'],'文件名')
        self.assertEqual(inherited['业务类型识别状态'],'inherited')
        self.assertEqual(len(conflicts),2)
        self.assertTrue(all(r['业务类型识别状态']=='conflict' for r in conflicts))
        gui=importlib.import_module('台账')
        app=gui.App.__new__(gui.App)
        app.folder=None;app.file_paths={}
        app.search=Mock(get=Mock(return_value=''));app.filter=Mock(get=Mock(return_value='全部'))
        app.download_since=Mock(get=Mock(return_value=''));app.session=Mock(get=Mock(return_value=''))
        class Table:
            def __init__(self):self.rows=[]
            def get_children(self):return ()
            def delete(self,*args):self.rows.clear()
            def insert(self,*args,values):self.rows.append(values);return str(len(self.rows))
        for name in ('docs','files','runs','changes'):setattr(app,name,Table())
        with patch.object(gui,'DB_PATH',self.base/'runtime/ledger.sqlite3'):app.refresh_rows()
        self.assertTrue(any(row[0]==layout+'.pdf' and row[9]=='管道布置平面及立面图' for row in app.files.rows))
        self.assertTrue(any(row[0]=='shared.zip' and row[11]=='conflict' for row in app.files.rows))


if __name__ == '__main__':
    unittest.main()
