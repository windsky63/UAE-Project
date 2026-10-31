"""Readable, streaming Excel ledger from a stable database export snapshot."""
from collections import defaultdict
import csv
from datetime import datetime, timedelta, timezone
import math
import os
from pathlib import Path
import shutil
import tempfile
import unicodedata
import uuid

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet


LOCAL = timezone(timedelta(hours=8))
BLUE, TEAL, GRAY = '17324D', 'DDEEF0', 'F2F5F9'
GREEN, AMBER, RED = 'E1F0E5', 'FFF0CF', 'FBE3E3'
FONT = Font(name='Microsoft YaHei', size=10, color='24384B')
HEADER = Font(name='Microsoft YaHei', size=10, bold=True, color='FFFFFF')
WRAP = Alignment(vertical='center', wrap_text=True)
FILLS = {color:PatternFill('solid',fgColor=color) for color in
         (BLUE,TEAL,GRAY,GREEN,AMBER,RED,'E7EEF5','FFFFFF')}
STATUSES = {'done':'已完成', 'pending':'待下载', 'downloading':'下载中', 'failed':'失败',
            'restricted':'无读取权限', 'not_enumerated':'待枚举', 'no_content':'未上传内容'}
MAIN = [
    ('序号',8), ('文件名',52), ('记录类型',13), ('业务类型',27), ('业务专业',12),
    ('下载状态',23), ('成员解析状态',27), ('文件大小（MiB）',17), ('下载日期（北京）',23),
    ('文档编号',48), ('文档名称',58), ('修订',9), ('所属压缩包文件名',50),
    ('包内完整路径',62), ('本地路径',65), ('错误',48), ('目录',62),
]


def cell(sheet, value, *, fill=None, bold=False, header=False, number_format=None):
    if isinstance(value,str):value=ILLEGAL_CHARACTERS_RE.sub('',value)
    result = WriteOnlyCell(sheet, value)
    if isinstance(value, str):
        # Text IDs keep leading zeros and never become formulas or links.
        result.data_type = 's'
    result.font = HEADER if header else (Font(name='Microsoft YaHei',size=10,bold=True,color='24384B') if bold else FONT)
    result.alignment = WRAP
    if fill:
        result.fill = FILLS[fill]
    if number_format:
        result.number_format = number_format
    return result


def setup(sheet, columns, title, subtitle, note, *, freeze='D6'):
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = 85
    sheet.freeze_panes = freeze
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = Worksheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth, sheet.page_setup.fitToHeight = 1, 0
    sheet.print_title_rows = '1:5'
    for index, (_, width) in enumerate(columns, 1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    end = get_column_letter(len(columns))
    for row, text, color in ((1,title,BLUE),(2,subtitle,'E7EEF5'),(3,note,'FFFFFF')):
        sheet.merged_cells.add(f'A{row}:{end}{row}')
        item = cell(sheet,text,fill=color,header=row==1)
        if row==1:item.font=Font(name='Microsoft YaHei',size=18,bold=True,color='FFFFFF')
        sheet.row_dimensions[row].height = 34 if row==1 else 28
        sheet.append([item])
    sheet.row_dimensions[4].height = 10
    sheet.append([])
    sheet.row_dimensions[5].height = 32
    sheet.append([cell(sheet,label,fill=BLUE,header=True) for label,_ in columns])


def width_units(value):
    return sum(2 if unicodedata.east_asian_width(c) in ('W','F') else 1 for c in str(value or ''))


def row_height(values, columns):
    lines=max((max(1,math.ceil(width_units(v)/max(1,width-2))) for v,(_,width) in zip(values,columns)),default=1)
    return min(90,max(32,lines*15+8))


def local_datetime(value):
    if not value:
        return None
    try:
        at=datetime.fromisoformat(value.replace('Z','+00:00'))
        return at.astimezone(LOCAL).replace(tzinfo=None) if at.tzinfo else at
    except ValueError:
        return value


def export_workbook(source, target=None):
    source=Path(source)
    target=Path(target or source.with_suffix('.xlsx'))
    # Freeze one CSV version while an ongoing download may publish a newer one.
    with tempfile.TemporaryDirectory(prefix='.ledger-',dir=target.parent) as temporary:
        stable=Path(temporary)/source.name
        shutil.copyfile(source,stable)
        return _export_workbook(stable,target)


def _export_workbook(source, target):
    source=Path(source)
    target=Path(target or source.with_suffix('.xlsx'))
    totals=defaultdict(set)
    groups=defaultdict(set)
    count=placeholders=0
    with source.open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream)
        fields=reader.fieldnames or []
        for row in reader:
            count+=1
            kind=row.get('记录类型') or '平台文件'
            identity=(row.get('工作区'),row.get('内容ID'))
            if not identity[1]:
                placeholders+=1
                continue
            totals[kind].add(identity)
            groups[kind,row.get('业务类型') or '待分类',STATUSES.get(row.get('下载状态'),row.get('下载状态') or '未登记')].add(identity)
    generated=datetime.now(LOCAL).strftime('%Y-%m-%d %H:%M:%S')
    subtitle=(f'导出时间（北京）：{generated}    明细 {count:,} 行（含关联重复行）    '
              f'平台内容 {len(totals["平台文件"]):,} 个    解压条目 {len(totals["解压文件"]):,} 个')
    book=Workbook(write_only=True)
    book.properties.title='项目文件台账'
    book.properties.subject='平台文件与解压成员，含业务分类和文件来源'
    main=book.create_sheet('文件台账')
    setup(main,MAIN,'项目文件台账',subtitle,
          '可直接筛选表头；向右查看压缩包来源和路径。绿色：完成；黄色：待处理/待核对；红色：异常。解压条目不计入平台下载数量。')
    complete=book.create_sheet('完整字段')
    full_columns=[(name,60 if any(k in name for k in ('路径','目录','原因','依据','文档名称')) else
                       52 if name in ('文件名','所属压缩包文件名','SHA256','命中编号') else 22) for name in fields]
    setup(complete,full_columns,'文件台账 · 完整字段',subtitle,
          '保留数据库导出全部字段和原始状态码；内容ID、版本ID及SHA256按文本保存。记录类型区分平台文件和解压成员。',freeze='C6')
    with source.open(encoding='utf-8-sig',newline='') as stream:
        for index,row in enumerate(csv.DictReader(stream),1):
            kind=row.get('记录类型') or '平台文件'
            status=STATUSES.get(row.get('下载状态'),row.get('下载状态') or '未登记')
            try:size=int(row['文件大小'])/(1024**2) if row.get('文件大小') else None
            except ValueError:size=None
            values=[index,row.get('文件名') or '未命名',kind,row.get('业务类型') or '待分类',
                row.get('业务专业'),status,row.get('内容解析状态') or '—',size,
                local_datetime(row.get('下载日期（北京）')),row.get('文档编号'),row.get('文档名称'),
                row.get('修订'),row.get('所属压缩包文件名'),row.get('包内完整路径'),row.get('本地路径'),
                row.get('错误'),row.get('目录')]
            fill=GRAY if index%2==0 else 'FFFFFF'
            main.row_dimensions[index+5].height=row_height(values,MAIN)
            cells=[cell(main,value,fill=fill) for value in values]
            cells[0].alignment=Alignment(horizontal='center',vertical='center')
            cells[2].fill=PatternFill('solid',fgColor=TEAL if kind=='解压文件' else 'E7EEF5')
            status_color=RED if any(k in status for k in ('失败','无读取权限','异常')) else GREEN if status=='已完成' else AMBER
            cells[5].fill=PatternFill('solid',fgColor=status_color)
            if row.get('业务类型识别状态') in ('unrecognized','conflict','discipline_only','unavailable'):
                cells[3].fill=PatternFill('solid',fgColor=AMBER)
            if row.get('错误'):cells[15].fill=PatternFill('solid',fgColor=RED)
            cells[7].number_format='[>=1]#,##0.00;[>0]0.000000;0'
            cells[8].number_format='yyyy-mm-dd hh:mm'
            main.append(cells)
            # Stream all original fields without duplicating them in memory.
            complete.row_dimensions[index+5].height=row_height([row.get(name) for name in fields],full_columns)
            complete.append([cell(complete,row.get(name),fill=fill) for name in fields])
    for sheet,columns in ((main,MAIN),(complete,full_columns)):
        sheet.auto_filter.ref=f'A5:{get_column_letter(len(columns))}{max(5,count+5)}'
    overview=book.create_sheet('台账概览')
    overview_columns=[('记录类型',18),('业务类型',52),('下载状态',30),('去重内容/条目数',22)]
    setup(overview,overview_columns,'文件台账 · 分类概览',subtitle,
          f'按工作区和内容ID去重；无内容ID的占位行另有 {placeholders:,} 行。平台内容与解压条目分别统计。',freeze='A6')
    for index,((kind,business,status),identities) in enumerate(sorted(groups.items()),1):
        overview.row_dimensions[index+5].height=34
        overview.append([cell(overview,value,fill=GRAY if index%2==0 else 'FFFFFF')
                         for value in (kind,business,status,len(identities))])
    overview.auto_filter.ref=f'A5:D{max(5,len(groups)+5)}'
    book.move_sheet(overview.title,offset=-1)
    book.active=0
    temporary=target.with_name(target.stem+'.'+uuid.uuid4().hex+'.xlsx')
    try:
        book.save(temporary)
        try:
            os.replace(temporary,target)
        except PermissionError:
            # An open Excel workbook must not stop download/analysis progress.
            alternative=target.with_name(target.stem+'-最新.xlsx')
            try:
                os.replace(temporary,alternative)
            except PermissionError:
                alternative=target.with_name(target.stem+'-'+datetime.now(LOCAL).strftime('%Y%m%d-%H%M%S-%f')+'.xlsx')
                os.replace(temporary,alternative)
            return alternative
        return target
    finally:
        temporary.unlink(missing_ok=True)
