"""Offline Excel briefings and image handoff. No FusionLive or credential imports."""

from paths import HOME, CODE
import argparse
from contextlib import closing, contextmanager
from copy import copy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import traceback

from openpyxl import Workbook, load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.cell.cell import MergedCell
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from PIL import Image, ImageDraw, ImageFont
from paths import check_output_directory
from business_type import unit_names

LOCAL = timezone(timedelta(hours=8))
SOURCES = ('单线图', 'PCF', 'SHA', 'IDF')
METRICS = ('管线数', '图纸数', '焊缝数', '寸口量', '预制焊缝数', '预制寸口量', '安装焊缝数', '安装寸口量')
HEADERS = ('项目', '单元', '来源', *METRICS, '依据')
NOTE = '备注：文件数按已发现路径统计，未展开包不估算；管线和焊接量为成功解析且排除冲突后的有效小计，合计仅累加已知值；—表示未知，全未知行不显示。寸口为焊口公称英寸之和；PCF预制/安装采用设备连接及260818规则分类，IDF沿用原分类口径，不代表施工进度。'
LAYOUT_TEMPLATE = next((p for p in (HOME/'templates/管道简报模板.xlsx', HOME/'data/管道简报模板.xlsx')
                        if p.is_file()), HOME/'data/管道简报模板.xlsx')
TEAL, DARK, PALE, BG = '104F60', '334155', 'DDECEF', 'F1F5F9'


def literal(cell, value):
    cell.value = value
    if isinstance(value, str):
        cell.data_type = 's'  # Never interpret imported labels as Excel formulas.


def catalogue(database):
    """Read a consistent, read-only snapshot; count references, never attachments."""
    groups, seen, evidence = {}, set(), []
    with closing(sqlite3.connect(Path(database).resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        with db:
            for row in db.execute('SELECT project,id,reference,filename,title,metadata FROM documents WHERE active=1'):
                data = json.loads(row['metadata'])
                if data.get('documentType', '').split(' - ', 1)[0].strip().upper() != 'ISO':
                    continue
                if data.get('isLatest') in (False, 'false') or str(data.get('pmStatus', '')).upper() == 'CANCELLED':
                    continue
                reference = (row['reference'] or '').strip()
                identity = (row['project'], reference.casefold() or row['id'])
                if identity in seen:
                    continue
                seen.add(identity)
                match = re.search(r'LT_HE-((?:GG|H\d+)-\d{2,3})(?=[_-]|$)', reference, re.I)
                unit = match[1].upper() if match else '未识别单元'
                key = (row['project'], unit, '单线图')
                record = groups.setdefault(key, dict.fromkeys(METRICS))
                record['依据'] = '仅发现台账 ISO 记录；下载文件尚未分析，不作为图纸数量'
                evidence.append((row['project'], unit, reference, row['id'], row['title']))
    return groups, evidence


def import_metrics(path):
    result = {}
    wb = load_workbook(path, data_only=False, read_only=True)
    try:
        sheet = wb['数据录入']
        rows = sheet.iter_rows(values_only=True)
        if tuple(next(rows, ())) != HEADERS:
            raise ValueError('数据录入表头不匹配，请使用生成的补充模板。')
        for number, values in enumerate(rows, 2):
            if all(v is None for v in values):
                continue
            values = tuple(values) + (None,) * max(0, len(HEADERS) - len(values))
            project, unit, source = values[:3]
            if not all(isinstance(v, str) and v.strip() for v in (project, unit, source)) or source not in SOURCES:
                raise ValueError(f'第 {number} 行项目、单元或来源无效。')
            key = (project.strip(), unit.strip(), source)
            if key in result:
                raise ValueError(f'第 {number} 行重复：每项目/单元/来源只能一行汇总值。')
            record = {}
            for metric, value in zip(METRICS, values[3:11]):
                if value in (None, '', '—'):
                    value = None
                elif isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                    raise ValueError(f'第 {number} 行 {metric} 必须为非负数或留空，不能使用公式。')
                elif '寸口' not in metric and value != int(value):
                    raise ValueError(f'第 {number} 行 {metric} 必须为整数。')
                record[metric] = value
            if source not in ('PCF', 'IDF') and any(record[m] is not None for m in METRICS[2:]):
                raise ValueError(f'第 {number} 行仅PCF/IDF来源支持焊接统计；单线图焊接指标不纳入简报。')
            for total, shop, field in (('焊缝数', '预制焊缝数', '安装焊缝数'), ('寸口量', '预制寸口量', '安装寸口量')):
                a, b, c = (record[k] for k in (total, shop, field))
                if a is not None and b is not None and c is not None and not math.isclose(a, b + c, abs_tol=0.001):
                    raise ValueError(f'第 {number} 行 {total} 与预制、安装合计不一致。')
            if any(v is not None for v in record.values()) and not str(values[11] or '').strip():
                raise ValueError(f'第 {number} 行请填写数据依据。')
            record['依据'] = str(values[11] or '未获取')
            result[key] = record
    finally:
        wb.close()
    return result


def write_template(path, keys):
    wb = Workbook()
    sheet = wb.active
    sheet.title = '数据录入'
    sheet.append(HEADERS)
    for project, unit in sorted(keys):
        for source in SOURCES:
            sheet.append([project, unit, source, *([None] * len(METRICS)), None])
    for row in sheet:
        for cell in row:
            if isinstance(cell.value, str):
                literal(cell, cell.value)
    sheet.freeze_panes = 'D2'
    sheet.auto_filter.ref = sheet.dimensions
    for col in sheet.columns:
        sheet.column_dimensions[col[0].column_letter].width = 20
    help_sheet = wb.create_sheet('填写说明')
    for text in ('每项目、单元、来源填写一行汇总数据；来源为单线图、PCF、SHA或IDF。',
                 '未知留空；确认无数量时填写0；不要把不同修订版本累加。',
                 '寸口量以英寸计；预制/安装分别填写焊缝数和寸口量；不得填写施工完成量。',
                 'SHA仅填写已确认解析的图纸数，不要按SHA附件个数填写图纸数。',
                 '填写依据（原始文件、解析结果或统计表），不使用公式；重复行会被拒绝。',
                 'PCF/IDF按原始格式分别录入；简报合并时须有逐管线明细去重，不能直接累加同单元重叠汇总；单线图不填写焊接指标。',
                 '管线数、图纸数必须来自下载后文件分析，不得用台账目录数量代替。'):
        help_sheet.append([text])
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)


def valid_sum(values):
    values = [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)]
    return round(sum(values), 6) if values else None


def prepare_model_summary(groups, analysis_report):
    """One display source, with cross-format pipeline dedup before plant totals."""
    report = analysis_report or {}
    formats = {key[2] for key in groups if key[2] in ('PCF', 'IDF')}
    formats.update(row['kind'] for row in report.get('file_inventory', []) if row['kind'] in ('PCF', 'IDF'))
    formats.update(row['source'] for row in report.get('pipeline_results', []) if row['source'] in ('PCF', 'IDF'))
    label = next(iter(formats)) if len(formats) == 1 else 'PCF/IDF'
    result = {key: dict(value) for key, value in groups.items() if key[2] not in ('PCF', 'IDF')}
    units = {key[:2] for key in groups if key[2] in ('PCF', 'IDF')}
    pipeline_units = {}
    for item in report.get('pipeline_results', []):
        if item['source'] not in ('PCF', 'IDF') or item.get('conflict'):
            continue
        unit = (item['project'], item['unit'])
        units.add(unit)
        pipeline_units.setdefault(unit, {}).setdefault(str(item['pipeline']).strip().casefold(), []).append(item)
    audit = []
    for project, unit in sorted(units):
        raw = [groups[project, unit, source] for source in ('PCF', 'IDF') if (project, unit, source) in groups]
        pipes = pipeline_units.get((project, unit))
        if pipes is None:
            if len(raw) == 1:
                result[project, unit, label] = dict(raw[0])
            elif raw:
                result[project, unit, label] = {**dict.fromkeys(METRICS),
                    '状态':'缺少逐管线明细，跨格式去重待核对', '依据':'不直接相加可能重叠的PCF和IDF分组汇总'}
            continue
        # Existing single-format aggregates may include manual supplements.
        if len(formats) == 1 and raw:
            result[project, unit, label] = dict(raw[0])
            continue
        from local_analysis import classify
        rows = []
        selected_count = 0
        for pipeline, candidates in sorted(pipes.items()):
            candidates.sort(key=lambda row: 0 if row['source'] == 'PCF' else 1)
            chosen = candidates[0]
            selected_count += 1
            if len(candidates) > 1:
                audit.append([project, unit, pipeline, chosen['source'],
                              '；'.join(row['source'] for row in candidates),
                              '；'.join(f'{row["source"]}: {len(row.get("rows", []))}道焊口' for row in candidates),
                              '同管线PCF优先，IDF不重复累计；差异需核对原文件'])
            for row in chosen.get('rows', []):
                nps = row.get('寸径')
                nps = nps if isinstance(nps, (int, float)) and not isinstance(nps, bool) and math.isfinite(nps) and nps > 0 else None
                rows.append((nps, classify(row, report.get('prefab_rule', 'none'))))
        record = dict.fromkeys(METRICS)
        record.update(管线数=selected_count, 焊缝数=len(rows),
                      未分类焊缝数=sum(kind is None for _, kind in rows),
                      口径未知焊缝数=sum(nps is None for nps, _ in rows))
        known = [nps for nps, _ in rows if nps is not None]
        record['寸口量'] = round(sum(known), 3) if known or not rows else None
        for kind, count, amount in (('shop', '预制焊缝数', '预制寸口量'), ('field', '安装焊缝数', '安装寸口量')):
            chosen = [nps for nps, value in rows if value == kind]
            if chosen or record['未分类焊缝数'] == 0:
                record[count] = len(chosen)
                known = [nps for nps in chosen if nps is not None]
                record[amount] = round(sum(known), 3) if known or not chosen else None
        record['状态'] = '有效解析小计；PCF优先，IDF补充；未知分类留空'
        record['依据'] = '按项目、单元、管线号跨PCF/IDF去重；排除冲突管线；寸口仅累加已知口径'
        result[project, unit, label] = record
    return result, label, audit


def summary_sheet(wb, groups, title, day, analysis_report=None, layout_template=None, *, name='简报', plant=False, include_project=False, model_source='PCF'):
    sheet = wb.create_sheet(name)
    columns = 7 if plant else 13 if include_project else 12
    sheet.append([f'{title}（{day}）'])
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=columns)
    sheet.append([''])
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=columns)
    sheet.append(['一、各厂区文件数量' if plant else '一、各单元文件数量'])
    sheet.merge_cells(start_row=3, start_column=1, end_row=3, end_column=columns)
    table_headers = set()
    def header(values):
        if plant:
            values = values[:-1]  # Explanations remain on unit and audit sheets.
        sheet.append(['厂区', *values[1:]] if plant else [*(['项目'] if include_project else []), '单元号', '英文名称', '中文名称', *values[1:]])
        table_headers.add(sheet.max_row)
        sheet.merge_cells(start_row=sheet.max_row, start_column=len(values) + (0 if plant else 3 if include_project else 2), end_row=sheet.max_row, end_column=columns)
    def data_row(values, project=''):
        if valid_sum(values[1:-1]) is None:
            return
        if plant:
            values = values[:-1]
        info = unit_names(values[0]) if not plant and values[0] != '合计' else None
        if not plant:
            values = [values[0], info['english'] if info else '', info['chinese'] if info else '', *values[1:]]
            if include_project:
                values = [project, *values]
        sheet.append([v if v is not None else '—' for v in values])
        sheet.merge_cells(start_row=sheet.max_row, start_column=len(values), end_row=sheet.max_row, end_column=columns)
    def diff(a, b):
        return a - b if a is not None and b is not None else None
    file_rows = (analysis_report or {}).get('file_inventory', [])
    units = sorted({key[:2] for key in groups} | {(r['project'], r['unit']) for r in file_rows})
    file_counts = {}
    for r in file_rows:
        key = (r['project'], r['unit'], r['kind'])
        file_counts[key] = file_counts.get(key, 0) + 1
    known_files = analysis_report is not None and 'file_inventory' in analysis_report
    model_formats = ('PCF', 'IDF') if model_source == 'PCF/IDF' else (model_source,)
    header(['单元', f'{model_source}文件数', '单线图PDF文件数', 'SHA文件数', '统计范围'])
    for project, unit in units:
        values = ([sum(file_counts.get((project, unit, source), 0) for source in model_formats)] +
                  [file_counts.get((project, unit, source), 0) for source in ('单线图', 'SHA')]) if known_files else [None] * 3
        unknown = file_counts.get((project, unit, '待分类PDF'), 0)
        data_row([unit, *values, '模拟文件数据' if '模拟' in title else
                  f'已发现文件；待分类PDF {unknown}；未展开包不计入' if known_files else '文件清单待登记'], project)
    if units:
        totals = ([sum(file_counts.get((p, u, source), 0) for p, u in units for source in model_formats)] +
                  [sum(file_counts.get((p, u, source), 0) for p, u in units) for source in ('单线图', 'SHA')]) if known_files else [None] * 3
        data_row(['合计', *totals, '已发现文件小计，非全部压缩包预计数量'])
    sheet.append(['二、各厂区已解析管线数量' if plant else '二、各单元已解析管线数量'])
    sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=columns)
    header(['单元', f'{model_source}管线数', '单线图管线数', 'SHA管线数', '解析口径'])
    for project, unit in units:
        values = [groups.get((project, unit, source), {}).get('管线数') for source in (model_source, '单线图', 'SHA')]
        data_row([unit, *values, '管线标识去重；PDF、SHA内容解析待接入'], project)
    if units:
        totals = []
        for source in (model_source, '单线图', 'SHA'):
            values = [groups.get((p, u, source), {}).get('管线数') for p, u in units]
            totals.append(valid_sum(values))
        data_row(['合计', *totals, '仅累加有效解析数据，未知不计入'])
    sheet.append([f'三、{model_source} 解析焊接量（设计量）'])
    sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=columns)
    header(['单元', '焊缝数', '寸口量', '预制焊缝数', '预制寸口量', '安装焊缝数', '安装寸口量', '解析状态'])
    for project, unit in units:
        record = groups.get((project, unit, model_source), {})
        data_row([unit, *(record.get(k) for k in METRICS[2:]),
                  '模拟解析结果 · 非真实数据' if '模拟' in title else record.get('状态', f'待分析{model_source}文件' if all(record.get(k) is None for k in METRICS[2:]) else '已导入解析结果')], project)
    if units:
        totals = []
        for metric in METRICS[2:]:
            values = [groups.get((p, u, model_source), {}).get(metric) for p, u in units]
            totals.append(valid_sum(values))
        data_row(['合计', *totals, '模拟汇总' if '模拟' in title else '仅累加有效解析数据，未知不计入'])
    sheet.append([NOTE])
    sheet.merge_cells(start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=columns)
    border = Border(*( [Side(style='thin', color='CBD5E1')] * 4 ))
    merged = {r.min_row for r in sheet.merged_cells.ranges if r.min_col == 1}
    for row in sheet:
        i = row[0].row
        section = i in merged and 3 <= i < sheet.max_row
        color = TEAL if i == 1 else DARK if i in table_headers else PALE if section else ('FFFFFF' if i % 2 else 'F8FAFC')
        for cell in row:
            cell.fill = PatternFill('solid', fgColor=color)
            cell.font = Font(name='Microsoft YaHei', size=20 if i == 1 else 11, bold=i in table_headers or i == 1 or section or row[0].value == '合计' or (include_project and row[1].value == '合计'),
                             color='FFFFFF' if i == 1 or i in table_headers else TEAL if section else '1E293B')
            cell.alignment = Alignment(horizontal='center' if i not in merged or i == 1 else 'left', vertical='center', wrap_text=True)
            cell.border = border
            if isinstance(cell.value, str):
                literal(cell, cell.value)
            elif isinstance(cell.value, (int, float)):
                cell.number_format = '#,##0.##'
        sheet.row_dimensions[i].height = 56 if i == 1 else 40 if i in (2, sheet.max_row) else 34
    for col in 'ABCDEFGHIJKLM'[:columns]:
        sheet.column_dimensions[col].width = ({'A': 22}.get(col, 16) if plant else
            {'A': 18, 'B': 18, 'C': 42, 'D': 32}.get(col, 16) if include_project else {'A': 18, 'B': 42, 'C': 32}.get(col, 16))
    sheet.freeze_panes = 'B5' if plant else 'E5' if include_project else 'D5'
    sheet.sheet_view.showGridLines = False
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = 'landscape'
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A3
    sheet.page_setup.fitToWidth = 1
    sheet.print_title_rows = '1:2'
    sheet.print_area = sheet.dimensions
    template = LAYOUT_TEMPLATE if layout_template is None else layout_template
    if template and Path(template).is_file():
        apply_layout_template(sheet, template)
    for row in range(3, sheet.max_row):
        if not plant and layout_role(sheet, row) == 'body':
            lines = max(math.ceil(len(str(sheet.cell(row, 3 if include_project else 2).value or '')) / 38),
                        math.ceil(len(str(sheet.cell(row, 4 if include_project else 3).value or '')) / 20))
            sheet.row_dimensions[row].height = max(sheet.row_dimensions[row].height or 34, lines * 16)
    sheet.row_dimensions[2].height = 12
    return sheet


def plant_code(unit):
    code = str(unit).strip().upper()
    match = re.fullmatch(r'([A-Z][A-Z0-9]*)-\d{1,3}', code)
    return match[1] if match else unit


def plant_summary(groups, analysis_report):
    """Sum known unit metrics as valid subtotals; keep wholly unknown metrics blank."""
    file_rows = (analysis_report or {}).get('file_inventory', [])
    units = sorted({key[:2] for key in groups} | {(r['project'], r['unit']) for r in file_rows})
    members = {}
    for project, unit in units:
        members.setdefault((project, plant_code(unit)), []).append(unit)
    result = {}
    for (project, plant), children in members.items():
        for source in sorted({key[2] for key in groups}):
            records = [groups.get((project, unit, source), {}) for unit in children]
            if not any(records):
                continue
            record = {}
            for metric in METRICS:
                values = [r.get(metric) for r in records]
                record[metric] = valid_sum(values)
            record['状态'] = '按所属单元有效数据汇总；未知不计入，非完整总量'
            result[project, plant, source] = record
    report = None if analysis_report is None else {**analysis_report,
        'file_inventory': [{**r, 'unit': plant_code(r['unit'])} for r in file_rows]}
    return result, report, units


def create_workbook(path, groups, evidence, title, day, analysis_report=None, layout_template=None):
    wb = Workbook()
    wb.remove(wb.active)
    view_groups, model_source, merge_audit = prepare_model_summary(groups, analysis_report)
    aggregated, report, units = plant_summary(view_groups, analysis_report)
    summary_sheet(wb, aggregated, title, day, report, layout_template, plant=True, model_source=model_source)
    summary_sheet(wb, view_groups, f'{title} · 单元汇总', day, analysis_report, layout_template,
                  name='单元汇总', include_project=len({p for p, _ in units}) > 1, model_source=model_source)
    notes = wb.create_sheet('简报说明')
    notes.append(['部分', '说明'])
    notes.append(['文件统计范围', '按当前有效下载文件及已发现包内文件路径统计；未展开包不计入，待分类PDF见文件数量明细。'])
    notes.append(['管线解析口径', '按管线标识去重，排除冲突管线；单元、厂区及合计均仅累加已知有效值，非完整总量；PDF、SHA内容解析待接入。'])
    notes.append(['焊接解析范围', f'{model_source}有效解析管线的设计量；同管线PCF优先、IDF补充，不重复累计。未知口径、未知分类不按0计算；原格式结果保留在统计依据。'])
    notes.append(['单线图PDF数量', '按文件自身施工编号52/53/54/58识别管道ISO，统计已发现文件路径；不计13-61目录、伴热图、电气图及土建45类。文件数不是PDF页数或管线数。'])
    notes.append(['原始格式明细', '统计依据和解析焊口明细保留各格式原始结果供核对；两种格式覆盖同一管线时，不直接相加原明细行数作为合并焊口数。重复来源见PCF-IDF合并核对。'])
    notes.append(['行显示规则', '每部分独立判断：所有指标未知的行（包括合计）不显示；0是有效值，部分指标已知时保留该行，其余指标显示—。'])
    notes.append(['焊口分类小计', '预制、安装分别统计明确分类的焊口；未分类不清空已分类数量，也不强行归入安装。分类覆盖情况见焊口分类核对。'])
    notes.column_dimensions['A'].width = 22
    notes.column_dimensions['B'].width = 100
    for row in notes:
        for cell in row:
            cell.alignment = Alignment(vertical='center', wrap_text=True)
    file_rows = (analysis_report or {}).get('file_inventory', [])
    def diff(a, b):
        return a - b if a is not None and b is not None else None
    audit = wb.create_sheet('统计依据')
    audit.append(HEADERS)
    for key, record in sorted(groups.items()):
        audit.append([*key, *(record.get(k) for k in METRICS), record.get('依据', '未获取')])
    coverage = wb.create_sheet('焊口分类核对')
    coverage.append(['项目', '单元', '来源', '有效焊缝数', '已明确预制焊缝数', '已明确安装焊缝数', '未分类焊缝数', '口径未知焊缝数', '说明'])
    for (p, u, source), record in sorted(groups.items()):
        if source in ('PCF', 'IDF'):
            coverage.append([p, u, source, record.get('焊缝数'), record.get('预制焊缝数'), record.get('安装焊缝数'),
                             record.get('未分类焊缝数'), record.get('口径未知焊缝数'), record.get('依据', '未获取')])
    coverage.freeze_panes = 'D2'
    coverage.auto_filter.ref = coverage.dimensions
    comparison = wb.create_sheet('管线差值')
    comparison.append(['项目', '单元', f'单线图−{model_source}管线数', '单线图−SHA管线数', '说明'])
    for p, u in units:
        iso, pcf, sha = (view_groups.get((p, u, source), {}).get('管线数') for source in ('单线图', model_source, 'SHA'))
        comparison.append([p, u, diff(iso, pcf), diff(iso, sha), '两侧均有解析结果才计算；不以文件数代替管线数'])
    comparison.freeze_panes = 'A2'
    merged = wb.create_sheet('PCF-IDF合并核对')
    merged.append(['项目', '单元', '管线号', '采用格式', '发现格式', '各格式焊口数', '说明'])
    for row in merge_audit:
        merged.append(row)
    merged.freeze_panes = 'A2'
    docs = wb.create_sheet('图号明细')
    docs.append(['项目', '单元', '图号', '文档ID', '标题'])
    for row in evidence:
        docs.append(row)
    names_sheet = wb.create_sheet('单元名称依据')
    names_sheet.append(['单元号', '英文名称', '中文名称（译名）', '区域', '名称状态', '依据'])
    for _, unit in units:
        info = unit_names(unit)
        if info['candidates']:
            for row in info['candidates']:
                names_sheet.append([unit, row['english'], row['chinese'], row['area'], info['status'], row['basis']])
        else:
            names_sheet.append([unit, info['english'], info['chinese'], '', info['status'], '编号规定Appendix 2未列明'])
    for tab in (audit, docs, names_sheet, coverage, merged):
        tab.freeze_panes = 'A2'
        for row in tab:
            for cell in row:
                if isinstance(cell.value, str):
                    literal(cell, cell.value)
    if analysis_report:
        if 'analysis_complete' in analysis_report:
            meta = wb.create_sheet('生成状态')
            for key in ('analysis_complete', 'completed_analysis_files', 'pending_analysis_files', 'failed_analysis_files', 'unavailable_local_files', 'unresolved_analysis_groups'):
                meta.append([key, analysis_report.get(key, '未知')])
            meta.sheet_state = 'hidden'
        for name, rows, fields in (
                ('文件数量明细', file_rows, ('project', 'unit', 'origin', 'kind', 'basis', 'sha256', 'state')),
                ('压缩包文件明细', analysis_report.get('archive_members', []), ('project', 'archive', 'format', 'entry', 'member', 'output', 'size', 'sha256', 'state', 'duplicate_of')),
                ('文件分析记录', analysis_report['files'], ('project', 'unit', 'origin', 'format', 'state', 'detail', 'pipeline', 'sha256')),
                ('台账本地文件检查', analysis_report['ledger_inventory'], ('project', 'id', 'filename', 'path', 'status', 'state', 'analysis_state')),
                ('解析焊口明细', analysis_report['welds'], ('项目', '单元', '来源格式', '管线号', '焊口号', '公称直径', '寸径', '焊接类型', '分类', '原文件'))):
            tab = wb.create_sheet(name)
            tab.append(fields)
            for row in rows:
                tab.append([row.get(k) for k in fields])
            tab.freeze_panes = 'A2'
            tab.auto_filter.ref = tab.dimensions
            for row in tab:
                for cell in row:
                    if isinstance(cell.value, str):
                        literal(cell, cell.value)
    wb.save(path)


def layout_role(sheet, row):
    value = str(sheet.cell(row, 1).value or '')
    if row == 1:
        return 'title'
    if row == 2:
        return 'spacer'
    if row == sheet.max_row:
        return 'note'
    if value in ('厂区', '单元号', '单元', '项目 / 单元', '项目'):
        return 'header'
    if value == '合计' or (sheet.max_column == 13 and sheet.cell(row, 2).value == '合计'):
        return 'total'
    if value.startswith(('一、', '二、', '三、')):
        return 'section'
    return 'body'


def apply_layout_template(sheet, template):
    """Reuse template row styles while filling a variable number of units."""
    source = load_workbook(template, data_only=False)
    try:
        tab = source['简报']
        rows = {}
        for i in range(1, tab.max_row + 1):
            rows.setdefault(layout_role(tab, i), i)
        for i in range(1, sheet.max_row + 1):
            original = rows.get(layout_role(sheet, i))
            if original is None:
                continue
            sheet.row_dimensions[i].height = tab.row_dimensions[original].height
            for col in range(1, sheet.max_column + 1):
                # Older templates have one identity column; preserve metric styles
                # while the new name columns use the former identity-column style.
                target_ids = ['项目', '单元', '英文', '中文'] if sheet.max_column == 13 else ['单元', '英文', '中文'] if sheet.max_column >= 12 else ['单元']
                source_ids = ['项目', '单元', '英文', '中文'] if tab.max_column == 13 else ['单元', '英文', '中文'] if tab.max_column >= 12 else ['单元']
                if col <= len(target_ids):
                    identity = target_ids[col - 1]
                    source_col = source_ids.index(identity) + 1 if identity in source_ids else source_ids.index('单元') + 1
                else:
                    source_col = col - len(target_ids) + len(source_ids)
                target, origin = sheet.cell(i, col), tab.cell(original, source_col)
                if isinstance(origin, MergedCell):
                    # File/pipeline sections have fewer metric columns than the
                    # weld section. Merged placeholders carry no usable style.
                    origin = tab.cell(original, len(source_ids) + 1)
                if isinstance(origin, MergedCell):
                    # File/pipeline sections have fewer metric columns than the
                    # weld section. Merged placeholders carry no usable style.
                    origin = tab.cell(original, len(source_ids) + 1)
                # Style IDs belong to their workbook; register actual objects.
                for attribute in ('font', 'fill', 'border', 'alignment', 'protection'):
                    setattr(target, attribute, copy(getattr(origin, attribute)))
                target.number_format = origin.number_format
        for col in 'ABCDEFGHIJKLM'[:sheet.max_column]:
            if tab.max_column == sheet.max_column and sheet.max_column >= 12:
                sheet.column_dimensions[col].width = tab.column_dimensions[col].width
        sheet.page_setup = copy(tab.page_setup)
        sheet.page_margins = copy(tab.page_margins)
        note = tab.cell(rows['note'], 1).value
        if isinstance(note, str) and note.startswith('备注：文件数按当前有效下载文件'):
            note = NOTE
        if isinstance(note, str) and note.strip() and not note.startswith('=') and '差值=单线图' not in note:
            literal(sheet.cell(sheet.max_row, 1), note)
    finally:
        source.close()


def write_layout_template(path=LAYOUT_TEMPLATE):
    """Blank report layout, separate from the metrics supplement workbook."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    groups = {}
    for source in SOURCES:
        groups['{项目}', '{单元}', source] = {**dict.fromkeys(METRICS), '依据': '待填写', '状态': '待填写'}
    create_workbook(path, groups, [], '{简报标题}', '{日期}', layout_template=False)
    return path


def font(size, bold=False):
    folder = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    for name in (('msyhbd.ttc', 'msyh.ttc', 'simhei.ttf') if bold else ('msyh.ttc', 'simhei.ttf')):
        if (folder / name).exists():
            return ImageFont.truetype(str(folder / name), size)
    raise RuntimeError('未找到中文字体，请安装微软雅黑或黑体。')


def screenshot_pages(sheet, rows_per_page=24):
    """Keep section titles and column headers with at least one data row."""
    if rows_per_page < 3:
        raise ValueError('每页至少需要3行，以容纳标题、表头及数据。')
    pages, start = [], 3
    while start <= sheet.max_row:
        end = min(start + rows_per_page, sheet.max_row + 1)
        if end <= sheet.max_row:
            while end > start and layout_role(sheet, end - 1) in ('section', 'header'):
                end -= 1
        if end == start:
            # Empty sections have no data row to carry onto the next page.
            end = min(start + 2, sheet.max_row + 1)
        pages.append(list(range(start, end)))
        start = end
    return pages or [[]]


def render(workbook, folder, rows_per_page=24):
    """Render the saved 简报 sheet, including manual edits, without Excel/COM."""
    wb = load_workbook(workbook, data_only=False)
    try:
        sheet = wb['简报']
        for row in sheet:
            if any(cell.data_type == 'f' for cell in row):
                raise ValueError('简报不支持公式，请先在Excel中粘贴为数值再转图片。')
        pages = screenshot_pages(sheet, rows_per_page)
        merged = {(r.min_row, r.min_col):r.max_col for r in sheet.merged_cells.ranges}
        widths = ([170, 400, 300] + [140] * 6 + [95] * 3
                  if sheet.max_column >= 12 else [260] + [155] * 6 + [95] * 3)[:sheet.max_column]
        def cell_lines(text, face, width):
            lines = []
            for paragraph in text.split('\n'):
                line = ''
                for char in paragraph:
                    if line and draw.textlength(line + char, font=face) > width - 20:
                        cut = line.rfind(' ')
                        if cut > 0:
                            lines.append(line[:cut])
                            line = line[cut + 1:] + char
                        else:
                            lines.append(line)
                            line = char
                    else:
                        line += char
                lines.append(line.strip())
            return lines
        paths = []
        for page, indices in enumerate(pages, 1):
            if page > 1 and indices and layout_role(sheet, indices[0]) in ('body', 'total'):
                previous = [i for i in range(3, indices[0]) if layout_role(sheet, i) == 'header']
                if previous:
                    h = previous[-1]
                    context = [h - 1] if layout_role(sheet, h - 1) == 'section' else []
                    indices = [*context, h, *indices]
            indices = [1, *([2] if sheet.cell(2, 1).value else []), *indices]
            heights = [max(64, round((sheet.row_dimensions[i].height or 34) * 1.65)) for i in indices]
            draw = ImageDraw.Draw(Image.new('RGB', (1, 1)))
            cells = {}
            for row_index, i in enumerate(indices):
                for col in range(1, len(widths) + 1):
                    if any(r.min_row == i and r.min_col < col <= r.max_col for r in sheet.merged_cells.ranges):
                        continue
                    cell = sheet.cell(i, col)
                    width = sum(widths[col - 1:merged.get((i, col), col)])
                    value = cell.value
                    text = f'{value:,.2f}'.rstrip('0').rstrip('.') if isinstance(value, float) else str(value if value is not None else '—')
                    face = font(32 if i == 1 else 23, cell.font.bold)
                    wrapped = '\n'.join(cell_lines(text, face, width))
                    box = draw.multiline_textbbox((0, 0), wrapped, font=face, spacing=4)
                    heights[row_index] = max(heights[row_index], box[3] - box[1] + 24)
                    cells[i, col] = (wrapped, face)
            canvas = Image.new('RGB', (sum(widths) + 64, sum(heights) + 100), '#' + BG)
            draw = ImageDraw.Draw(canvas)
            y = 32
            for i, height in zip(indices, heights):
                x = 32
                cols = [col for col in range(1, len(widths) + 1) if (i, col) in cells]
                for col in cols:
                    cell = sheet.cell(i, col)
                    width = sum(widths[col - 1:merged.get((i, col), col)])
                    fill = cell.fill.fgColor.rgb
                    fill = '#' + fill[-6:] if isinstance(fill, str) else '#FFFFFF'
                    draw.rectangle((x, y, x + width, y + height), fill=fill, outline='#CBD5E1')
                    text, face = cells[i, col]
                    raw_color = cell.font.color.rgb if cell.font.color and cell.font.color.type == 'rgb' else '1E293B'
                    color = '#' + raw_color[-6:]
                    draw.multiline_text((x + width / 2, y + height / 2), text, font=face,
                                        anchor='mm', align='center', spacing=4, fill=color)
                    x += width
                y += height
            draw.text((32, y + 18), f'第 {page}/{len(pages)} 页', font=font(18), fill='#64748B')
            path = folder / f'简报-{page:02d}.png'
            canvas.save(path)
            paths.append(path)
        return paths
    finally:
        wb.close()


def atomic_json(path, data):
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def generate(database, output, supplement=None, title=None, day=None, workbook=None,
             analyze_files=False, files_root=None, project='本地项目', prefab_rule='none', use_cache=False):
    output = check_output_directory(output, database=database, source_root=files_root)
    with generation_log(output, '管道简报', day) as progress:
        return _generate(database, output, supplement, title, day, workbook, analyze_files,
                         files_root, project, prefab_rule, use_cache, progress)


@contextmanager
def generation_log(output, task, day):
    """Persist diagnostics for manual and scheduled calls, including failures."""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    started = previous = time.monotonic()
    run_id = f'{os.getpid()}-{time.time_ns()}'
    def progress(message):
        nonlocal previous
        now = time.monotonic()
        line = (f'[{datetime.now(LOCAL).isoformat(timespec="seconds")}] [{run_id}] '
                f'{task} {message}；阶段耗时={now - previous:.3f}秒；总耗时={now - started:.3f}秒')
        previous = now
        with (output / '生成过程.log').open('a', encoding='utf-8') as stream:
            stream.write(line + '\n')
        print(line, flush=True)
    progress('开始；统计日期=' + str(day or datetime.now(LOCAL).date()))
    try:
        yield progress
    except Exception as exc:
        progress(f'结束：失败；{type(exc).__name__}: {exc}\n{traceback.format_exc()}')
        raise
    else:
        progress('结束：成功')


def _generate(database, output, supplement, title, day, workbook,
              analyze_files, files_root, project, prefab_rule, use_cache, progress):
    day = day or datetime.now(LOCAL).date().isoformat()
    if title is None:
        from push_settings import load_settings
        title = load_settings()['briefing_title']
    output = check_output_directory(output, database=database, source_root=files_root)
    output.mkdir(parents=True, exist_ok=True)
    marker = output / '简报推送状态.json'
    state = dict(ready=False, date=day, sent=False, network_accessed=False, images=[])
    # Keep the last successful publication available while refreshing.
    # Each publication has unique paths: RPA cannot accidentally read older images.
    with tempfile.TemporaryDirectory(prefix='.briefing-', dir=output) as temporary:
        staging = Path(temporary)
        target = staging / '管道资料简报.xlsx'
        if workbook:
            target.write_bytes(Path(workbook).read_bytes())
        else:
            groups, evidence = catalogue(database)
            progress(f'台账读取完成；分组={len(groups)}；依据记录={len(evidence)}')
            analysis_report = None
            if use_cache and not files_root:
                from analysis_worker import cached_snapshot
                analyzed, analysis_report, analysis_path = cached_snapshot(database,
                    output=output / 'analysis_snapshots', prefab_rule=prefab_rule)
                groups.update(analyzed)
                state['analysis_file'] = str(analysis_path)
                progress('缓存汇总完成')
            elif analyze_files or files_root:
                from local_analysis import analyze
                analyzed, analysis_report, analysis_path = analyze(database=database, root=files_root,
                    project=project, output=output / 'file_analysis', prefab_rule=prefab_rule)
                groups.update(analyzed)
                state['analysis_file'] = str(analysis_path)
                progress('本地文件分析完成')
            if supplement:
                for key, record in import_metrics(supplement).items():
                    existing = groups.setdefault(key, dict.fromkeys(METRICS))
                    for metric, value in record.items():
                        if value is not None:
                            existing[metric] = value
            template = output / '简报数据补充模板.xlsx'
            if not template.exists():
                write_template(template, {key[:2] for key in groups} or {('项目编号', '单元编号')})
            create_workbook(target, groups, evidence, title, day, analysis_report)
        progress('Excel就绪')
        images = render(target, staging)
        progress(f'图片渲染完成；图片={len(images)}')
        check = load_workbook(target, read_only=True)
        try:
            is_demo = '模拟' in str(check['简报']['A1'].value)
            if '生成状态' in check.sheetnames:
                state.update({row[0]: row[1] for row in check['生成状态'].iter_rows(values_only=True)})
        finally:
            check.close()
        publication = output / ('版本-' + datetime.now(LOCAL).strftime('%Y%m%d-%H%M%S-%f'))
        staging.rename(publication)
        target = publication / target.name
        state.update(ready=not is_demo, demo=is_demo, generated_at=datetime.now(LOCAL).isoformat(), workbook=str(target.resolve()),
                     workbook_sha256=hashlib.sha256(target.read_bytes()).hexdigest(),
                     images=[dict(path=str((publication / p.name).resolve()), sha256=hashlib.sha256((publication / p.name).read_bytes()).hexdigest()) for p in images])
        atomic_json(marker, state)
        progress('生成状态已保存')
    return state


def generate_pending_notice(output, day, title=None):
    """Small independent baseline: no ledger scan, decompression or parsing."""
    output = check_output_directory(output)
    with generation_log(output, '待分析通知', day):
        return _generate_pending_notice(output, day, title)


def _generate_pending_notice(output, day, title):
    output.mkdir(parents=True, exist_ok=True)
    folder = Path(tempfile.mkdtemp(prefix='待分析通知-', dir=output))
    workbook = folder / '管道资料简报.xlsx'
    report = dict(from_cache=True, analysis_complete=False, pending_analysis_files='未知',
                  failed_analysis_files='未知', unavailable_local_files='未知', unresolved_analysis_groups='未知',
                  files=[], ledger_inventory=[], welds=[])
    if title is None:
        from push_settings import load_settings
        title = load_settings()['briefing_title']
    create_workbook(workbook, {}, [], title, day, report)
    images = render(workbook, folder)
    state = dict(ready=True, demo=False, date=day, sent=False, network_accessed=False,
        **report, fallback_notice=True, generated_at=datetime.now(LOCAL).isoformat(),
        workbook=str(workbook.resolve()), workbook_sha256=hashlib.sha256(workbook.read_bytes()).hexdigest(),
        images=[dict(path=str(p.resolve()), sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in images])
    atomic_json(output / '简报推送状态.json', state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线生成Excel简报并转PNG，不登录、不下载、不发送消息')
    parser.add_argument('--database', type=Path, default=HOME / 'runtime/ledger.sqlite3')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--supplement', type=Path, help='填写后的补充模板')
    parser.add_argument('--render', type=Path, help='将已编辑的简报Excel重新转图片')
    parser.add_argument('--title', help='覆盖启动设置中保存的简报标题')
    parser.add_argument('--daily-text', action='store_true', help='兼容旧参数；文字日报改为发送时实时生成')
    parser.add_argument('--date', default=datetime.now(LOCAL).date().isoformat())
    parser.add_argument('--files-root', type=Path, help='可选：分析工具外的独立样本目录；正常使用自动定位工具downloads目录及台账路径，无需填写')
    parser.add_argument('--project', default='本地项目', help='手动指定目录对应的项目编号')
    parser.add_argument('--prefab-rule', choices=['none', 'no-small', 'all'], default='none', help='预制/安装分类规则；默认不估算')
    args = parser.parse_args(argv)
    datetime.strptime(args.date, '%Y-%m-%d')
    state = generate(args.database, args.output or HOME / 'logs' / args.date / '管道简报',
                     args.supplement, args.title, args.date, args.render,
                     files_root=args.files_root, project=args.project, prefab_rule=args.prefab_rule, use_cache=True)
    print('离线简报已生成：' + state['workbook'])
    print('推送图片：' + str(len(state['images'])) + (' 张；推送文件已就绪，统计范围见简报说明，尚未发送。' if state['ready'] else ' 张；模拟预览不推送。'))


if __name__ == '__main__':
    main()
