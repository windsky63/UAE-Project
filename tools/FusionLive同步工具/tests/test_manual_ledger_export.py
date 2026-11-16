import io
from contextlib import redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock
from openpyxl import load_workbook
from ledger import Ledger
from paths import exports_dir

class ManualExportTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.base=Path(temporary.name)
        ledger=Ledger(self.base/'runtime/ledger.sqlite3',base_dir=self.base)
        ledger.root(Mock(project_id='p',cabinet_id='root',project_name='fixture'))
        ledger.close()

    def test_manual_export_replaces_original_excel_without_network_or_ledger_initialization(self):
        from ledger_export import export
        target=exports_dir(self.base)/'文件台账.xlsx'
        target.write_bytes(b'previous export')
        with patch('socket.socket',side_effect=AssertionError('network forbidden')), \
             patch('ledger.Ledger.__init__',side_effect=AssertionError('writable ledger forbidden')), \
             redirect_stdout(io.StringIO()):
            result=export(self.base)
        self.assertEqual(result,target)
        self.assertEqual({p.name for p in target.parent.iterdir()},{'文件台账.xlsx'})
        book=load_workbook(target,read_only=True)
        try:
            self.assertEqual(book.sheetnames,['文件台账','目录路径树','台账概览','完整字段'])
        finally:book.close()

    def test_manual_export_requires_existing_database(self):
        from ledger_export import export
        with self.assertRaisesRegex(ValueError,'未找到文件台账数据库'):
            export(self.base/'missing')
        self.assertFalse((self.base/'missing').exists())
