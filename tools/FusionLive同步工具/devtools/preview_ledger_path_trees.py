"""Generate an offline example using fictional ledger entries."""
import csv
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
from ledger_workbook import export_workbook
from openpyxl import load_workbook


def main():
    target = Path(__file__).resolve().parents[3] / 'exports' / '文件台账-目录路径树示例.xlsx'
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ['工作区','目录','文件名','内容ID','记录类型','下载状态','业务类型','文件大小','本地路径','所属压缩包文件名','包内完整路径']
    rows = [
        ['示例项目','工程文件/管道/单线图','ISO-001.pdf','001','平台文件','done','管道单线图',1048576,r'D:\项目资料\管道\单线图\ISO-001.pdf','',''],
        ['示例项目','工程文件/管道/布置图','LAYOUT-001.pdf','002','平台文件','done','管道布置图',2097152,r'D:\项目资料\管道\布置图\LAYOUT-001.pdf','',''],
        ['示例项目','工程文件/电气/施工图','ELE-001.pdf','003','平台文件','pending','电气施工图',524288,'','',''],
        ['示例项目','工程文件/管道/单线图','图纸包.zip','004','平台文件','done','管道单线图',4194304,r'D:\项目资料\压缩包\图纸包.zip','',''],
        ['示例项目','工程文件/管道/单线图；工程文件/管道/归档','ISO-002.pdf','005','解压文件','done','管道单线图',1048576,r'D:\项目资料\解压文件\图纸包\ISO\ISO-002.pdf','图纸包.zip','ISO/ISO-002.pdf'],
        ['示例项目','工程文件/设备/厂家资料','设备说明书.pdf','006','平台文件','done','设备资料',3145728,r'\\NAS\项目共享\设备\设备说明书.pdf','',''],
        ['示例项目','工程文件/管道/归档','ISO-001.pdf','001','平台文件','done','管道单线图',1048576,r'D:\项目资料\管道\单线图\ISO-001.pdf','',''],
        ['示例项目','','待核对资料.pdf','007','平台文件','failed','待分类',0,'','',''],
    ]
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / 'sample.csv'
        with source.open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(fields)
            writer.writerows(rows)
        result = export_workbook(source, target)
    book = load_workbook(result)
    try:
        assert book.sheetnames == ['文件台账', '目录路径树', '台账概览', '完整字段']
        for title, count in [('目录路径树',9)]:
            sheet = book[title]
            assert sum(isinstance(row[4], int) for row in sheet.iter_rows(min_row=6, values_only=True)) == count
            assert any(d.outlineLevel for d in sheet.row_dimensions.values())
        print(result)
    finally:
        book.close()


if __name__ == '__main__':
    main()
