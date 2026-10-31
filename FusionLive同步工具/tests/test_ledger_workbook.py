import csv
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import load_workbook
from ledger_workbook import export_workbook, GREEN


class LedgerWorkbookTests(unittest.TestCase):
    def test_ready_to_open_layout_types_and_nonduplicated_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);source=root/'input.csv'
            fields=['工作区','内容ID','文件名','记录类型','业务类型','业务专业','下载状态',
                    '文件大小','下载日期（北京）','业务类型识别状态','所属压缩包文件名','包内完整路径']
            row={'工作区':'p','内容ID':'000000012345678901','文件名':'=HYPERLINK("bad")',
                 '记录类型':'平台文件','业务类型':'管道单线图','业务专业':'管道','下载状态':'done',
                 '文件大小':'1048576','下载日期（北京）':'2026-10-29T01:02:03+00:00',
                 '业务类型识别状态':'recognized'}
            with source.open('w',encoding='utf-8-sig',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=fields);writer.writeheader();writer.writerow(row);writer.writerow(row)
                writer.writerow({**row,'内容ID':'member','文件名':'1-43-52-1167-FLC-AAE_0.pdf','记录类型':'解压文件',
                                 '所属压缩包文件名':'package.zip','包内完整路径':'nested.zip!/ISO/drawing.pdf'})
            target=export_workbook(source)
            book=load_workbook(target)
            try:
                self.assertEqual(book.sheetnames,['文件台账','台账概览','完整字段'])
                sheet=book.active
                self.assertEqual(sheet.freeze_panes,'D6')
                self.assertFalse(sheet.sheet_view.showGridLines)
                self.assertEqual(sheet.auto_filter.ref,'A5:Q8')
                self.assertGreaterEqual(sheet.column_dimensions['B'].width,50)
                self.assertGreaterEqual(sheet.row_dimensions[6].height,32)
                self.assertEqual(sheet['F6'].value,'已完成')
                self.assertEqual(sheet['F6'].fill.fgColor.rgb[-6:],GREEN)
                self.assertEqual(sheet['H6'].value,1)
                self.assertEqual(sheet['I6'].value,datetime(2026,10,29,9,2,3))
                self.assertEqual(sheet['B6'].data_type,'s')
                self.assertIn('平台内容 1 个',sheet['A2'].value)
                self.assertIn('解压条目 1 个',sheet['A2'].value)
                self.assertEqual(book['完整字段']['B6'].value,'000000012345678901')
                self.assertTrue(all(cell.data_type!='f' for s in book for row in s for cell in row))
            finally:book.close()

    def test_open_workbook_is_saved_to_an_alternative_without_overwriting_it(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'input.csv'
            source.write_text('文件名,内容ID,记录类型\nfile.pdf,001,平台文件\n',encoding='utf-8-sig')
            target=source.with_suffix('.xlsx');target.write_bytes(b'open workbook placeholder')
            import ledger_workbook
            original=ledger_workbook.os.replace
            def replace(src,dest):
                if Path(dest)==target:raise PermissionError('fixture Excel lock')
                return original(src,dest)
            with patch('ledger_workbook.os.replace',side_effect=replace):
                result=export_workbook(source,target)
            self.assertNotEqual(result,target)
            self.assertEqual(target.read_bytes(),b'open workbook placeholder')
            book=load_workbook(result,read_only=True);book.close()


if __name__=='__main__':unittest.main()
