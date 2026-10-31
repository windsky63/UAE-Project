"""Build the unified naming/coding reference, without ledger or trial data.

PyMuPDF is needed only here, for source-page transcription, not by the parser.
The executable rules remain in document_coding_rules.json.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import sys

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME/'app'))
from business_type import RULES_PATH, load_rules
from business_type import unit_names
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


def build(source_root, output):
    import fitz
    source_root, output = Path(source_root), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    rules = load_rules()
    wb = Workbook()
    wb.remove(wb.active)

    def sheet(name, headers, rows, widths=None):
        tab = wb.create_sheet(name)
        tab.append(headers)
        for row in rows:
            tab.append(row)
        for row in tab:
            for cell in row:
                if isinstance(cell.value, str):
                    cell.data_type = 's'
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                cell.font = Font(name='Microsoft YaHei', size=11,
                                 bold=cell.row == 1, color='FFFFFF' if cell.row == 1 else '1E293B')
                if cell.row == 1:
                    cell.fill = PatternFill('solid', fgColor='104F60')
        for i in range(1, len(headers) + 1):
            tab.column_dimensions[get_column_letter(i)].width = widths[i-1] if widths else 30
        tab.freeze_panes = 'A2'
        tab.auto_filter.ref = tab.dimensions
        return tab

    sheet('使用说明', ['项目', '说明'], [
        ['用途', '统一文件命名编码、业务类型和单元中英文名称；单元名称已用于管道简报。'],
        ['执行规则', 'document_coding_rules.json是代码读取的规则源；本表由同一JSON生成。修改表格不会自动修改规则。'],
        ['规则文件SHA256', hashlib.sha256(RULES_PATH.read_bytes()).hexdigest()],
        ['分类顺序', '实际文件名中的完整编号或性质前缀优先，其次本地路径的文件名，最后关联文档编号。'],
        ['关联附件', '自身无编码的附件可按关联文档归类，结果标记inherited；不能据此证明附件内容。'],
        ['状态', 'recognized=按自身编号识别；inherited=按关联文档继承；discipline_only=仅识别专业；unrecognized=未识别；conflict=多种类型待核对。'],
        ['边界', '不读取PDF业务内容，不解压文件，不按扩展名、标题关键词或目录名称推断业务类型。'],
        ['细分覆盖', f"已确认并执行{len(rules['document_types'])}条子类型范围；其他专业的原表仍保留在原表逐页中，尚未全部转成执行规则。"],
        ['序号', '保留原表范围；子类型未命中范围时留待核对，不能仅按中间两位数字套用。'],
        *[['说明', note] for note in rules['notes']],
        *[['单元名称说明', note] for note in rules['unit_notes']],
    ], [26, 115])
    sheet('编号结构', ['字段', '示例', '含义'], [
        ['性质前缀', 'PP', '可选；项目程序文件'], ['项目编号', '30201_50150_LT_HE', '项目固定编号'],
        ['厂区', 'GG / H0–H5 / PL等', '厂区标识，不是业务类型'],
        ['单元', '000 / 262', '三位单元编号；旧设施可能保留原编号'],
        ['专业', '13', '管道'], ['文档类型', '20', '须联合专业及序号解释'],
        ['序号', '001', '须核对附件2范围'], ['完整示例', '30201_50150_LT_HE-H4-262-13-20-001', '管道布置平面及立面图'],
    ], [24, 65, 65])
    sheet('厂区编码', ['厂区代码', '英文名称', '依据'],
          [[k, v, 'TE-PR-063 附件4；002 Rev.2 PDF第56页'] for k, v in rules['plants'].items()], [18, 65, 70])
    sheet('单元中英文名称', ['单元号', '厂区', '原单元号', '英文名称', '中文名称（译名）', '区域', '名称状态', '依据'],
          [[r['plant']+'-'+r['unit'], rules['plants'][r['plant']], r['original_unit'], r['english'],
            r['chinese'], r['area'], unit_names(r['plant']+'-'+r['unit'])['status'], r['basis']]
           for r in rules['units']], [20, 20, 14, 76, 58, 20, 24, 76])
    unique_codes = list(dict.fromkeys(r['plant']+'-'+r['unit'] for r in rules['units']))
    sheet('单元名称待核对', ['单元号', '状态', '候选英文名称', '候选中文译名', '处理方式'],
          [[code, info['status'], '\n'.join(r['english'] for r in info['candidates']),
            '\n'.join(r['chinese'] for r in info['candidates']), '简报标记待核对，不猜选名称']
           for code in unique_codes if (info := unit_names(code))['status'] != 'listed'], [20, 24, 90, 65, 45])
    sheet('专业编码', ['专业码', '专业', '依据'],
          [[k, v, 'TE-PR-063 附件1；002 Rev.2 PDF第43–44页'] for k, v in rules['disciplines'].items()], [18, 46, 70])
    sheet('业务性质前缀', ['前缀', '业务类型', '依据'],
          [[k, v, 'TE-PR-063 附件5；002 Rev.2 PDF第57页'] for k, v in rules['prefixes'].items()], [18, 40, 70])
    sheet('业务子类型', ['专业码', '类型码', '序号起', '序号止', '业务类型', '规则依据'],
          [[r[k] for k in ('discipline', 'type', 'start', 'end', 'label', 'basis')] for r in rules['document_types']],
          [14, 14, 14, 14, 48, 78])
    sheet('施工单线图', ['类型码', '业务类型', '编号结构', '依据'],
          [[k, v, '区域-单元-类型-管线序号-流体-CWA-页号', '编号规定§7.6；002 Rev.2 PDF第22–23页']
           for k, v in rules['construction_isometrics'].items()] +
          [[k, v, '区域-单元原编号-类型-四位管线号-两至三位流体代码-三字母CWA[-两位页号]',
            rules['construction_iso_naming']['alphabetic_cwa_basis']]
           for k, v in rules['construction_isometrics'].items()] +
          [['两位单元', '保留原编号', rules['construction_iso_naming']['short_unit'],
            '002 Rev.2 Appendix 2，PDF第150–152页'],
           ['CWA', '字母范围', '第一位ABCLPRSUWF；第二位ABCDENU；第三位字母排除I/O/Q',
            rules['construction_iso_naming']['alphabetic_cwa_basis']]], [18, 40, 65, 70])
    sheet('待核对规则', ['项目', '处理'], [
        ['51专业代码', '原PDF文字存在Commissioning/Procurement排版歧义，暂不执行。'],
        ['13-61', '原表注明单线图目录(List of isometrics)，且出现13-61-00；不能把其任意三位序号直接认定为单张单线图。'],
        ['17-50桩基图', '原表101 thru 099上下限矛盾，暂不执行。'],
        ['设备扩展编号', 'XXXXX设备号、XXX-YY扩展号等须另加结构解析；当前保留待核对。'],
        ['供应商与旧设施编号', '不套用三位单元的普通项目编号结构；无已确认前缀时保留待核对。'],
    ], [30, 110])
    sources, raw_pages, original_rows = [], [], []
    # Preserve the original complete attachments for review, including unimplemented rows.
    for source in rules['source_documents']:
        path = source_root / source['file']
        sources.append([path.name, source['revision'], source['description'], hashlib.sha256(path.read_bytes()).hexdigest()])
        with fitz.open(path) as doc:
            recording = False
            for page_number, page in enumerate(doc, 1):
                text = page.get_text().replace('\xa0', ' ')
                if 'DISCIPLINE CODES' in text and 'ATTACHMENT' in text and 'PROCESS / SYSTEMS' in text:
                    recording = True
                if recording:
                    raw_pages.append([path.name, page_number, text])
                    for block in page.get_text('blocks'):
                        content = block[4].replace('\xa0', ' ').strip()
                        if re.search(r'\b\d{2}-\d{2}-', content):
                            original_rows.append([path.name, page_number, content])
                if recording and 'NATURE OF DOCUMENT/ PREFIX' in text:
                    break
    sheet('规则来源', ['文件', '修订', '用途', 'SHA256'], sources, [88, 12, 65, 75])
    sheet('原表逐页', ['源文件', 'PDF页码', '原始表格文字（含未执行条目）'], raw_pages, [88, 14, 125])
    sheet('原表编码条目', ['源文件', 'PDF页码', '原表条目及说明（未清洗，供核对）'], original_rows, [88, 14, 125])
    for source in rules['source_documents'][:1]:
        with fitz.open(source_root / source['file']) as doc:
            for page in (150, 151, 152):
                wb['原表逐页'].append([source['file'], page, doc[page-1].get_text()])
    target = output / '文件命名编码规则（含业务类型）.xlsx'
    if target.exists():
        raise FileExistsError(target)
    wb.save(target)
    return dict(workbook=str(target), unit_rows=len(rules['units']), source_pages=len(raw_pages)+3)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--source-root', type=Path, default=HOME.parent)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source_root, args.output), ensure_ascii=False, indent=2))
