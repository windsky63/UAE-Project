"""Unit names from the same reviewed rules file as the coding reference workbook."""

import re
import argparse
import csv
from functools import lru_cache
import json
from pathlib import Path, PureWindowsPath

HOME = Path(__file__).resolve().parents[1]


def unit_names(code):
    """Keep ambiguous source names for audit; never select by title keywords."""
    code = str(code).strip().upper()
    match = re.fullmatch(r'([A-Z0-9]{2})-(\d{1,3})', code)
    rows = [] if not match else [r for r in load_rules()['units']
        if r['plant'] == match[1] and r['unit'] == match[2].zfill(3)]
    names = {(r['english'].casefold(), r['chinese']) for r in rows}
    if not rows:
        english, chinese, status = 'Not listed', '未列入单元表', 'unlisted'
    elif len(names) > 1:
        english, chinese, status = 'Pending confirmation', '同号多义，待核对', 'ambiguous'
    elif any(r['status'] == 'pending_definition' for r in rows):
        english, chinese, status = 'Pending confirmation', '名称待业主确认', 'pending_definition'
    else:
        english, chinese, status = rows[0]['english'], rows[0]['chinese'], 'listed'
    return dict(code=code, english=english, chinese=chinese, status=status, candidates=rows)


RULES_PATH = HOME/'document_coding_rules.json'
NUMBER = re.compile(
    r'(?<![A-Z0-9_])(?:(?P<prefix>[A-Z]{1,5})-)?30201_50150_LT_HE-'
    r'(?P<plant>[A-Z0-9]{2})-(?P<unit>\d{3})-'
    r'(?P<discipline>\d{2})-(?P<type>\d{2})-(?P<serial>\d{3})'
    r'(?=$|[^A-Z0-9])', re.I)
# Index 13-61-021, PDF pp.3-8: alphabetic CWA and a separate sheet column.
# Keep capture positions for existing consumers; validate short original units below.
# PCF-EXPORT-01 (user-confirmed filename exception): ignore only the export
# marker before the .pcf extension. Never rename files or relax other ISO fields.
PCF_EXPORT_SUFFIX = r'-PCF\.PCF(?:$|[^A-Z0-9])'
ISO = re.compile(r'(?<![A-Z0-9])([0-5BXL])-(\d{2,3})-(52|53|54|58|45)-'
                 r'(\d{4})-([A-Z0-9]{2,3})-(\d{3}|[ABCLPRSUWF][ABCDENU][A-HJ-NPR-Z])'
                 r'(?:-(\d{2}))?(?:(?![A-Z0-9-])|(?=' + PCF_EXPORT_SUFFIX + r'))', re.I)
PREFIX = re.compile(r'(?<![A-Z0-9_])([A-Z]{1,5})-30201_50150_LT_HE-(?=[A-Z0-9])', re.I)
PREFIX = re.compile(r'(?<![A-Z0-9_])([A-Z]{1,5})-30201_50150_LT_HE-(?=[A-Z0-9])', re.I)


@lru_cache(maxsize=1)
def load_rules():
    return json.loads(RULES_PATH.read_text(encoding='utf-8'))


def empty_result(reason='未命中已确认的编码规则'):
    return dict(business_type='未识别业务类型', discipline='未识别专业',
                discipline_code='', document_type_code='', serial='', prefix='',
                plant='', unit='', matched_number='', basis='', status='unrecognized',
                reason=reason, source='')


def parse_number(text):
    """Classify explicit project numbers only, preserving unknowns and conflicts."""
    rules = load_rules()
    candidates = []
    for match in NUMBER.finditer(str(text or '').upper()):
        fields = match.groupdict()
        result = empty_result()
        result.update(discipline_code=fields['discipline'], document_type_code=fields['type'],
                      serial=fields['serial'], prefix=fields['prefix'] or '',
                      plant=fields['plant'], unit=fields['unit'], matched_number=match[0])
        result['discipline'] = rules['disciplines'].get(fields['discipline'], '未识别专业')
        prefix = rules['prefixes'].get(fields['prefix'])
        if prefix:
            result.update(business_type=prefix, status='recognized', reason='',
                          basis='TE-PR-063 附件5；项目编号规定 §7.6（文件性质前缀）')
        else:
            hits = [rule for rule in rules['document_types']
                    if rule['discipline'] == fields['discipline'] and rule['type'] == fields['type']
                    and rule['start'] <= int(fields['serial']) <= rule['end']]
            if hits and len({rule['label'] for rule in hits}) == 1:
                result.update(business_type=hits[0]['label'], status='recognized', reason='',
                              basis='；'.join(dict.fromkeys(rule['basis'] for rule in hits)))
            elif hits:
                result.update(status='conflict', reason='编码规则存在重叠且类型不同，待核对')
            elif result['discipline'] != '未识别专业':
                result.update(status='discipline_only', reason='专业已识别；文档子类型或序号不在已确认规则中',
                              basis='TE-PR-063 附件1')
        candidates.append(result)
    for match in PREFIX.finditer(str(text or '').upper()):
        if any(r['matched_number'].startswith(match[0]) for r in candidates):
            continue
        label = rules['prefixes'].get(match[1])
        if label:
            candidates.append(dict(empty_result(), prefix=match[1], business_type=label,
                matched_number=match[0], status='recognized', reason='',
                basis='TE-PR-063 附件5；仅解析文件性质前缀，其余编号未按新编码解释'))
    for match in PREFIX.finditer(str(text or '').upper()):
        if any(r['matched_number'].startswith(match[0]) for r in candidates):
            continue
        label = rules['prefixes'].get(match[1])
        if label:
            candidates.append(dict(empty_result(), prefix=match[1], business_type=label,
                matched_number=match[0], status='recognized', reason='',
                basis='TE-PR-063 附件5；仅解析文件性质前缀，其余编号未按新编码解释'))
    for match in ISO.finditer(str(text or '').upper()):
        # The numeric CWA format in procedure 002 includes a two-digit sheet.
        if match[6].isdigit() and not match[7]:
            continue
        original_units = []
        if len(match[2]) == 2:
            original_units = [r for r in rules['units']
                if r['plant'] == 'H' + match[1] and r['original_unit'] == match[2]]
            if not original_units:
                continue
        basis = ('项目编号规定 §7.6，施工单线图编号（PDF第22–23页）'
                 if match[6].isdigit() else
                 rules['construction_iso_naming']['alphabetic_cwa_basis'])
        if original_units:
            basis += '；既有单元原编号：' + original_units[0]['basis']
        if re.match(PCF_EXPORT_SUFFIX, str(text)[match.end():], re.I):
            basis += '；特殊规则PCF-EXPORT-01：忽略文件名导出尾缀-pcf，原文件名不变'
        label = rules['construction_isometrics'][match[3]]
        candidates.append(dict(empty_result(), business_type=label,
            discipline='土建' if match[3] == '45' else '管道', unit=match[2],
            plant='H' + match[1] if match[1] in '012345' else '',
            document_type_code=match[3], matched_number=match[0], status='recognized', reason='',
            basis=basis))
    if not candidates:
        return empty_result()
    identities = {(r['business_type'], r['discipline'], r['status']) for r in candidates}
    if len(identities) != 1:
        result = empty_result('同一字段包含多种业务类型或专业，待核对')
        result.update(status='conflict', matched_number='；'.join(r['matched_number'] for r in candidates))
        return result
    result = candidates[0]
    result['matched_number'] = '；'.join(dict.fromkeys(r['matched_number'] for r in candidates))
    return result


def classify(*, filename='', reference='', path='', parent_references=()):
    """Prefer the actual file's number; inherit its document's type only explicitly.

    Directory names and titles are not classification evidence. Parent inheritance
    describes the associated document, not proof of an attachment's contents.
    """
    for source, text in (('文件名', filename), ('本地文件名', PureWindowsPath(path).name if path else '')):
        result = parse_number(text)
        if result['matched_number']:
            result['source'] = source
            return result
    results = [parse_number(value) for value in (reference, *parent_references) if value]
    results = [r for r in results if r['matched_number']]
    if not results:
        return empty_result()
    if any(r['status'] == 'conflict' for r in results) or len({
            (r['business_type'], r['discipline'], r['status']) for r in results}) != 1:
        result = empty_result('关联文档的业务类型不一致，不能确定附件业务类型')
        result.update(status='conflict', source='关联文档编号',
                      matched_number='；'.join(r['matched_number'] for r in results))
        return result
    result = dict(results[0], source='关联文档编号')
    if result['status'] == 'recognized':
        result.update(status='inherited', reason='按关联文档编号归类；尚未核验附件内容')
    return result


def ledger_file_contexts(db, *, include_inactive=False):
    """Preserve multiple parents; local collection can include previous versions."""
    def records(sql):
        cursor = db.execute(sql)
        columns = [column[0] for column in cursor.description]
        return [dict(zip(columns, row)) for row in cursor]
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'documents' not in tables:
        return {}
    documents = records('SELECT * FROM documents')
    documents = [r for r in documents if include_inactive or r.get('active', 1)]
    folders = {(r['project'], r['id']): r for r in records('SELECT * FROM folders')} if 'folders' in tables else {}
    indexed = {}
    for row in documents:
        folder = folders.get((row['project'], row.get('folder_id')), {})
        if include_inactive or folder.get('active', 1):
            indexed[row['project'], row['id'], row.get('folder_id')] = dict(row, folder_path=folder.get('path', ''))
    links = records('SELECT * FROM document_files') if 'document_files' in tables else [
        dict(project=r['project'], file_id=r['id'], document_id=r.get('document_id'), folder_id=r.get('folder_id'))
        for r in records('SELECT * FROM files')]
    contexts = {}
    for row in links:
        if not include_inactive and not row.get('active', 1):
            continue
        parent = indexed.get((row['project'], row.get('document_id'), row.get('folder_id')))
        if parent:
            contexts.setdefault((row['project'], row['file_id']), []).append(parent)
    return contexts


def file_business(row, parents=()):
    return classify(filename=row.get('filename') or '', path=row.get('path') or '',
                    parent_references=tuple(p.get('reference') or '' for p in parents))


EXPORT_FIELDS = {
    '业务类型': 'business_type', '业务专业': 'discipline', '专业编码': 'discipline_code',
    '文档类型编码': 'document_type_code', '序号': 'serial', '文件性质前缀': 'prefix',
    '业务类型识别状态': 'status', '业务类型来源': 'source', '命中编号': 'matched_number',
    '编码规则依据': 'basis', '业务类型待核对原因': 'reason',
}


def classify_pdf(filename):
    """Classify the file's own number, never infer drawings from a package title."""
    name = PureWindowsPath(str(filename)).name
    result = parse_number(name)
    if (result['status'] == 'recognized' and result['discipline'] == '管道'
            and ISO.search(name) and result['document_type_code'] in ('52', '53', '54', '58')):
        return '单线图', result['basis']
    if result['status'] == 'recognized':
        return '其他PDF', result['business_type'] + '；' + result['basis']
    return '待分类PDF', result['reason'] + '；不按关联目录或ISO标签直接认定单线图'


def pdf_unit(filename, fallback):
    """Numbered Habshan construction areas map to H0-H5; keep other areas unresolved."""
    name = PureWindowsPath(str(filename)).name
    if classify_pdf(name)[0] != '单线图':
        return fallback
    match = ISO.search(name)
    if match and match[1] in '012345':
        return f'H{match[1]}-{match[2]}'
    return fallback


def classify_csv(source, output):
    """Append audit columns to a separate ledger copy, retaining every input row."""
    source, output = Path(source).resolve(), Path(output).resolve()
    if source == output:
        raise ValueError('输出必须为新文件，不能覆盖原始台账。')
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    counts = {}
    with source.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.DictReader(stream)
        headers = reader.fieldnames or []
        if not {'文件名', '文档编号'}.issubset(headers):
            raise ValueError('输入须包含文件名、文档编号列。')
        with output.open('x', encoding='utf-8-sig', newline='') as target:
            writer = csv.DictWriter(target, fieldnames=headers + [k for k in EXPORT_FIELDS if k not in headers])
            writer.writeheader()
            for row in reader:
                result = classify(filename=row.get('文件名', ''), reference=row.get('文档编号', ''),
                                  path=row.get('本地路径', ''))
                row.update({key: result[value] for key, value in EXPORT_FIELDS.items()})
                # Escape newly derived text while preserving the original columns.
                for key in EXPORT_FIELDS:
                    if row[key].startswith(('=', '+', '-', '@', '\t', '\r')):
                        row[key] = "'" + row[key]
                writer.writerow(row)
                counts[result['status']] = counts.get(result['status'], 0) + 1
    return counts


def main():
    parser = argparse.ArgumentParser(description='离线解析文件业务类型；不改动推送文、简报或原台账')
    parser.add_argument('--csv', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--filename', default='')
    parser.add_argument('--reference', default='')
    args = parser.parse_args()
    if args.csv:
        if not args.output:
            parser.error('--csv 须同时指定 --output')
        result = classify_csv(args.csv, args.output)
    else:
        result = classify(filename=args.filename, reference=args.reference)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
