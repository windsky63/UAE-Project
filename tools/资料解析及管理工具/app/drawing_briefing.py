"""Offline drawing change briefing, using the pipeline briefing renderer."""
import argparse
import sys
from collections import defaultdict
from contextlib import closing
from datetime import datetime, date
import json
from pathlib import Path
import sqlite3
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment
from business_type import classify
from briefing import LOCAL, TEAL, literal, render
from paths import HOME

KINDS = {'added':'新增', 'updated':'更新', 'moved_or_renamed':'移动/更名', 'not_visible':'删除/不可见'}

def ima_scope():
    """Managed scope is explicit; standalone scope is owned by this tool."""
    import os
    configured=os.environ.get('PROJECT_MANAGER_HOME')
    if configured:
        centre=Path(configured).resolve()
        if not (centre/'drawing_scope.py').is_file():
            raise RuntimeError('管理中心图纸范围服务不可用')
        if str(centre) not in sys.path:sys.path.insert(0,str(centre))
        from drawing_scope import load
        return [load(centre/'config/drawing_scope.json')]
    path=HOME/'runtime/drawing_scope.json'
    if not path.is_file():path=Path(__file__).resolve().parent.parent/'defaults/drawing_scope.json'
    return [json.loads(path.read_text(encoding='utf-8'))] if path.is_file() else []


def bucket(discipline, unit, rules, drawing_type=None, file_format='PDF'):
    if discipline == '未识别专业' or unit == '未识别单元':
        return '待识别'
    if not rules:
        return '范围未配置'
    def matches(code):
        if '-' in code:
            return unit == code
        return unit.rsplit('-',1)[-1].lstrip('0') == code.lstrip('0')
    return '负责范围' if any(discipline in r['disciplines'] and any(matches(code) for code in r['units'])
        and (r.get('drawing_types') is None or drawing_type in r['drawing_types'])
        and file_format in r.get('file_formats',['PDF']) for r in rules) else '其他图纸'

def collect(database, day, rules):
    result = {}
    with closing(sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        with db:
            docs = {(r['project'],r['id'],r['folder_id']):dict(r) for r in db.execute('SELECT * FROM documents')}
            def register(d):
                b = classify(filename=d['filename'] or '', reference=d['reference'] or '')
                metadata = json.loads(d['metadata'] or '{}')
                dtype = str(metadata.get('documentType','')).split(' - ',1)[0].strip().upper()
                if '图' not in b['business_type'] and dtype not in ('ISO','DWG','DRAWING'):
                    return None
                unit = b['plant']+'-'+b['unit'] if b['plant'] and b['unit'] else '未识别单元'
                identity = (d['project'],(d['reference'] or d['id']).casefold())
                row = result.setdefault(identity, dict(project=d['project'], reference=d['reference'] or d['id'], discipline=b['discipline'], unit=unit, revision=d['revision'] or '—', kinds=set(), active=False, bucket=bucket(b['discipline'],unit,rules,b['business_type'],Path(d['filename'] or '').suffix[1:].upper())))
                current = bool(d.get('active', 1)) and metadata.get('isLatest') not in (False, 'false') and str(metadata.get('pmStatus','')).upper() != 'CANCELLED'
                row['active'] = row['active'] or current
                return row
            for d in docs.values():
                if d.get('active', 1):
                    register(d)
            links = defaultdict(set)
            for r in db.execute('SELECT * FROM document_files'):
                links[r['project'],r['file_id']].add((r['project'],r['document_id'],r['folder_id']))
            for e in db.execute('SELECT * FROM changes ORDER BY id'):
                at = datetime.fromisoformat(e['created'])
                at = at.replace(tzinfo=LOCAL) if at.tzinfo is None else at.astimezone(LOCAL)
                if at.date().isoformat() != day or e['kind'] not in KINDS:
                    continue
                keys = [(e['project'],e['entity_id'],e['folder_id'])] if e['entity']=='document' else links[e['project'],e['entity_id']] if e['entity']=='file' else []
                for key in keys:
                    d = docs.get(key)
                    if not d:
                        continue
                    row = register(d)
                    if row is not None:
                        row['kinds'].add(e['kind'])
    return [r for r in result.values() if r['active'] or r['kinds']]

def generate(rows, output, day, rules, demo=False):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    sheet = wb.active
    sheet.title = '简报'
    def wide(text):
        sheet.append([text])
        sheet.merge_cells(start_row=sheet.max_row,start_column=1,end_row=sheet.max_row,end_column=7)
    wide(f'图纸文件变更简报 · {day}'+(' · 模拟示例' if demo else ''))
    sheet.append([])
    sheet.append(['单元','专业','文件数',*KINDS.values()])
    groups = defaultdict(list)
    for r in rows:
        if r['bucket'] == '负责范围':
            groups[r['unit'],r['discipline']].append(r)
    # Include configured responsibility pairs even on days without changes.
    for rule in rules:
        for unit in rule['units']:
            for discipline in rule['disciplines']:
                if '-' in unit:
                    groups[unit,discipline]
    def summary(unit, discipline, items):
        sheet.append([unit,discipline,sum(r.get('active',True) for r in items),*(sum(k in r['kinds'] for r in items) for k in KINDS)])
    for (unit, discipline), items in sorted(groups.items()):
        summary(unit,discipline,items)
    summary('其他单元和专业','—',[r for r in rows if r['bucket']=='其他图纸'])
    summary('未识别','—',[r for r in rows if r['bucket']=='待识别'])
    if not rules:
        summary('范围未配置','—',[r for r in rows if r['bucket']=='范围未配置'])
    wide('备注：文件数为当前台账有效图纸数，按图号去重；变更列为当日北京日期的事件数。删除/不可见代表图纸可能被业主平台删除。'+('全部为模拟数据。' if demo else '本地清单可能不完整。'))
    detail = wb.create_sheet('变更明细')
    detail.append(['范围','专业','单元','图号','当前版本','变更类型'])
    for r in sorted(rows,key=lambda r:(r['bucket'],r['project'],r['unit'],r['reference'])):
        if not r['kinds']:
            continue
        detail.append([r['bucket'],r['discipline'],r['unit'],r['reference'],r['revision'],'、'.join(KINDS[k] for k in KINDS if k in r['kinds'])])
    detail.freeze_panes = 'A2'
    detail.auto_filter.ref = detail.dimensions
    for ws in wb:
        for row in ws:
            for c in row:
                if isinstance(c.value,str):
                    literal(c,c.value)
                head = c.row==1 or (ws == sheet and c.row==3)
                c.font = Font(name='微软雅黑',size=12,bold=head,color='FFFFFF' if head else '334155')
                c.fill = PatternFill('solid',fgColor=TEAL if head else 'FFFFFF')
                c.alignment = Alignment(horizontal='center',vertical='center',wrap_text=True)
            ws.row_dimensions[row[0].row].height = 40
        for col in 'ABCDEFG':
            ws.column_dimensions[col].width = 22
    detail.column_dimensions['D'].width = 60
    sheet.row_dimensions[2].height = 12
    path = output/'图纸变更简报.xlsx'
    wb.save(path)
    wb.close()
    return path, render(path,output)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date',default=datetime.now(LOCAL).date().isoformat())
    parser.add_argument('--database',type=Path,default=HOME/'runtime/ledger.sqlite3')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--demo',action='store_true')
    args = parser.parse_args()
    date.fromisoformat(args.date)
    rules = ima_scope()
    if args.demo:
        rows = [dict(project='模拟项目',reference=f'DEMO-DRAWING-{i:03}',discipline=d,unit=u,revision='B',kinds={k},bucket=bucket(d,u,rules)) for i,(d,u,k) in enumerate([('管道','H4-262','added'),('管道','H4-262','updated'),('管道','H4-263','updated'),('电气','H4-262','added'),('土建','H4-264','not_visible'),('未识别专业','未识别单元','updated')],1)]
        for row in rows:
            row['active'] = 'not_visible' not in row['kinds']
        for unit, discipline, count in [('H4-262','管道',18),('H4-263','管道',9),('H4-262','电气',7)]:
            rows.extend(dict(project='模拟项目',reference=f'DEMO-STABLE-{unit}-{discipline}-{i}',discipline=discipline,unit=unit,revision='A',kinds=set(),active=True,bucket=bucket(discipline,unit,rules)) for i in range(count))
        for row in rows:
            row['unit'] = {'H4-262':'H4-213','H4-263':'H4-027'}.get(row['unit'],row['unit'])
            row['bucket'] = bucket(row['discipline'],row['unit'],rules)
    else:
        rows = collect(args.database,args.date,rules)
    output = args.output or HOME/'logs'/args.date/('图纸变更简报示例' if args.demo else '图纸变更简报')
    path, images = generate(rows,output,args.date,rules,args.demo)
    print(path)
    for image in images:
        print(image)

if __name__=='__main__':
    main()
