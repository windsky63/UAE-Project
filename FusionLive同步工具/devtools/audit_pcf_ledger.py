"""Read-only PCF review: filename recognition is separate from content parsing."""
import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sys

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME / 'app'))
from business_type import classify, load_rules
from local_analysis import unit_from
from openpyxl import load_workbook, Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def naming_reason(name, result):
    if result['status'] == 'recognized':
        return '已识别'
    stem = re.sub(r'-pcf(?=\.pcf$)', '', name, flags=re.I)
    if re.match(r'^\d{2,3}-\d{4}[A-Z]*-[A-Z0-9]+', stem, re.I):
        return '短编号：缺少厂区/单线图类型，不能仅凭文件名确定业务细类'
    full = re.match(r'^([0-5BXL])-(\d{2,3})-(\d{2})-(\d{4})-([A-Z0-9]+)-([A-Z0-9]+)(.*)\.pcf$', stem, re.I)
    if full:
        # With an extra field, the last token may still be a valid CWA. Do not
        # mislabel the preceding material/class/fluid token as an invalid CWA.
        if full[7] and not re.fullmatch(r'-\d{2}', full[7]):
            if re.fullmatch(r'-[A-Z0-9]+(?:-\d{2})?', full[7], re.I):
                return '额外编号段：现规则不支持这类多段编号，需核对命名依据'
            return '施工编号附加尾缀/图页格式未命中规则'
        if full[3] not in load_rules()['construction_isometrics']:
            return '施工编号的类型编码不在已确认规则中'
        if len(full[5]) not in (2, 3):
            return '介质代码长度不在当前规则中'
        if full[6].isdigit() and not re.fullmatch(r'-\d{2}', full[7]):
            return '数字CWA格式缺少有效两位图页号'
        cwa = load_rules()['construction_iso_naming']['alphabetic_cwa']
        if not full[6].isdigit() and (len(full[6]) != 3 or any(ch not in cwa[key] for ch,key in zip(full[6].upper(), ('first','second','third')))):
            return '字母CWA不在已核对的索引编码范围中'
        if len(full[2]) == 2 and not any(r['plant'] == 'H' + full[1] and r['original_unit'] == full[2] for r in load_rules()['units']):
            return '两位单元号未在同厂区原单元映射表中确认'
        return '施工编号附加尾缀/图页格式未命中规则'
    return '其他文件名格式未命中已确认编号规则'


def review(source):
    book = load_workbook(source, read_only=True, data_only=True)
    try:
        iterator = book['完整字段'].iter_rows(values_only=True)
        intro = [next(iterator) for _ in range(5)]
        headers = intro[-1]
        records, seen, duplicate_rows = [], set(), 0
        for excel_row, values in enumerate(iterator, 6):
            row = dict(zip(headers, values))
            name = str(row.get('文件名') or '')
            if not name.lower().endswith('.pcf'):
                continue
            key = (row.get('工作区'), row.get('记录类型'), row.get('内容ID') or row.get('本地路径'))
            if key in seen:
                duplicate_rows += 1
                continue
            seen.add(key)
            result = classify(filename=name)
            records.append(dict(台账行号=excel_row, 文件名=name, 记录类型=row.get('记录类型'),
                原内容解析状态=row.get('内容解析状态') or '台账未提供', 原业务类型=row.get('业务类型'),
                原业务识别状态=row.get('业务类型识别状态'), 当前业务类型=result['business_type'],
                当前业务识别状态=result['status'], 文件名单元=unit_from(name),
                编号复核原因=naming_reason(name, result), 命中编号=result['matched_number'],
                当前规则依据=result['basis'], 原待核对原因=row.get('业务类型待核对原因'),
                所属压缩包=row.get('所属压缩包文件名'), 包内完整路径=row.get('包内完整路径'),
                工作区=row.get('工作区'), 内容ID=row.get('内容ID'), 父包内容ID=row.get('所属压缩包内容ID'),
                文档编号=row.get('文档编号'), 本地路径=row.get('本地路径'), SHA256=row.get('SHA256'),
                台账错误=row.get('错误') or '', 文件字节=row.get('已保存字节')))
        return str(intro[1][0]), records, duplicate_rows
    finally:
        book.close()


def save_sheet(book, name, records, columns):
    sheet = book.create_sheet(name)
    sheet.append(columns)
    for record in records:
        values = [record.get(key, '') for key in columns]
        sheet.append(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v for v in values])
    sheet.freeze_panes = 'B2'
    sheet.auto_filter.ref = sheet.dimensions
    sheet.row_dimensions[1].height = 30
    for cell in sheet[1]:
        cell.font = Font(color='FFFFFF', bold=True)
        cell.fill = PatternFill('solid', fgColor='245B78')
        cell.alignment = Alignment(wrap_text=True, vertical='center')
    for index, key in enumerate(columns, 1):
        width = 52 if key in ('文件名', '所属压缩包', '命中编号') else 70 if key in ('包内完整路径', '本地路径', '当前规则依据') else 38 if '原因' in key or '状态' in key else 22
        sheet.column_dimensions[get_column_letter(index)].width = width


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=HOME.parent / '文件台账.xlsx')
    parser.add_argument('--output', type=Path, default=HOME.parent / 'PCF台账复核.xlsx')
    args = parser.parse_args()
    if args.source.resolve() == args.output.resolve():
        parser.error('复核输出不能覆盖原始台账')
    exported, records, duplicate_rows = review(args.source)
    failures = [r for r in records if r['原内容解析状态'] == '分析失败']
    conflicts = [r for r in records if '同管线不同内容' in r['原内容解析状态']]
    unknown = [r for r in records if r['当前业务识别状态'] != 'recognized']
    suffix_fixed = [r for r in records if 'PCF-EXPORT-01' in r['当前规则依据']]
    source_hash = hashlib.sha256(args.source.read_bytes()).hexdigest()
    summary = dict(source=str(args.source.resolve()), source_sha256=source_hash, export=exported,
        pcf_entries=len(records), duplicate_association_rows=duplicate_rows,
        original_parse_states=dict(Counter(r['原内容解析状态'] for r in records)),
        current_business_states=dict(Counter(r['当前业务识别状态'] for r in records)),
        unrecognized_reasons=dict(Counter(r['编号复核原因'] for r in unknown)),
        suffix_rule_entries=len(suffix_fixed), analysis_failures=len(failures), content_conflicts=len(conflicts),
        unknown_filename_units=sum(r['文件名单元'] == '未识别单元' for r in records),
        original_pcf_files_locally_available=sum(Path(r['本地路径']).is_file() for r in records if r['本地路径']),
        failure_files=[{k:r[k] for k in ('文件名','所属压缩包','包内完整路径','台账错误')} for r in failures])
    book = Workbook()
    book.remove(book.active)
    notes = [dict(项目='来源', 结果=str(args.source.resolve())), dict(项目='台账快照', 结果=exported),
        dict(项目='复核时间（北京）', 结果=datetime.now(timezone(timedelta(hours=8))).isoformat()),
        dict(项目='范围', 结果='按工作区、记录类型、内容ID去重PCF条目；不按同名文件或内容哈希合并'),
        dict(项目='内容状态依据', 结果='原台账记录的内容解析状态；没有重新读取或解析服务器原PCF'),
        dict(项目='编号规则', 结果='当前已确认编号规则及特殊规则PCF-EXPORT-01；未识别业务类型不等于内容解析失败'),
        dict(项目='失败原因限制', 结果='解压成员的台账错误字段可能为空，准确失败原因需读取对应服务器analysis.json或进度日志'),
        dict(项目='原台账完整性', 结果=source_hash)]
    notes.extend(dict(项目=key, 结果=json.dumps(value, ensure_ascii=False) if isinstance(value, dict) else value)
                 for key,value in summary.items() if key not in ('source','source_sha256','export','failure_files'))
    save_sheet(book, '检查说明', notes, ['项目', '结果'])
    columns = list(records[0]) if records else ['文件名']
    for name, items in [('内容分析失败',failures), ('管线内容冲突',conflicts), ('仍未识别编号',unknown),
                        ('尾缀规则已识别',suffix_fixed), ('全部PCF复核',records)]:
        save_sheet(book, name, items, columns)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    book.save(args.output)
    summary_path = HOME / 'runtime/pcf-ledger-audit-summary.json'
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print('OUTPUT', args.output.resolve())


if __name__ == '__main__':
    main()
