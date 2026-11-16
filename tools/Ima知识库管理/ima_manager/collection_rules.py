"""Collect procedures and confirmed drawings into sibling knowledge-base folders."""
from functools import lru_cache
from pathlib import Path
import re
import sys

from .api import ImaError
from . import document_rules
from .document_rules import inside, normalized, relative_path
from .database_source import sync_module


# The drawing collection is intentionally PDF-only.
DRAWING_EXTENSIONS = {'.pdf'}
LIST_CATEGORY = '设备、材料或碰口清单'
LIST_EXTENSIONS = {'.pdf', '.xls', '.xlsx', '.csv'}
COLLECTION_ROOTS = ('项目管理规定', '图纸', '图纸（其他）', LIST_CATEGORY, LIST_CATEGORY + '（其他）')
RELOCATABLE_ROOTS = frozenset(COLLECTION_ROOTS[1:]) | {'其他图纸'}
def responsibility_scope():
    import os,json
    configured=os.environ.get('PROJECT_MANAGER_HOME')
    if configured:
        centre=Path(configured).resolve()
        if not (centre/'drawing_scope.py').is_file():
            raise ImaError('管理中心图纸范围服务不可用')
        if str(centre) not in sys.path:sys.path.insert(0,str(centre))
        from drawing_scope import load
        return load(centre/'config/drawing_scope.json')
    from .config import ROOT
    path=ROOT/'runtime/drawing_scope.json'
    if not path.is_file():path=ROOT/'defaults/drawing_scope.json'
    return json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {'disciplines':[],'units':[]}

REVIEW_ATTACHMENT = re.compile(r'(?:^|[-_\s])(ATR|CC|WTRAN)(?:$|[-_\s\d])', re.I)
LIST_REVIEW_ATTACHMENT = re.compile(r'(?:^|[-_\s])(ATR|CC|WTRAN|CRS|CRSR)(?:$|[-_\s\d.])', re.I)


@lru_cache(maxsize=1)
def drawing_types():
    rules = sync_module('business_type').load_rules()
    labels = {item['label'] for item in rules['document_types']}
    labels.update(rules['prefixes'].values())
    labels.update(rules['construction_isometrics'].values())
    return frozenset(label for label in labels if '图' in label)


def is_drawing(row):
    name = str(row.get('文件名') or '')
    if Path(name).suffix.lower() not in DRAWING_EXTENSIONS:
        return False
    if REVIEW_ATTACHMENT.search(Path(name).stem):
        return False
    if row.get('业务类型') not in drawing_types():
        return False
    # A transmittal or other attachment may inherit its parent's drawing label.
    # Require the file's own confirmed number, including for old XLSX snapshots.
    result = sync_module('business_type').parse_number(name)
    return result['status'] == 'recognized' and result['business_type'] == row['业务类型']


def is_equipment_list(row):
    name = str(row.get('文件名') or '')
    if (Path(name).suffix.lower() not in LIST_EXTENSIONS
            or row.get('业务类型') != LIST_CATEGORY
            or LIST_REVIEW_ATTACHMENT.search(Path(name).stem)):
        return False
    result = sync_module('business_type').parse_number(name)
    return result['status'] == 'recognized' and result['business_type'] == LIST_CATEGORY


def selected(row):
    return document_rules.selected(row) or is_drawing(row) or is_equipment_list(row)


def classify(row):
    if document_rules.selected(row):
        return document_rules.classify(row)
    if not (is_drawing(row) or is_equipment_list(row)):
        raise ValueError('文件没有已确认的图纸或清单类型，或不是可提取的格式')
    # Type names such as 地上/地下碰口管道单线图 must remain one folder.
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', row['业务类型']).strip(' .')


def relative_destination(row, category, filename):
    if document_rules.selected(row):
        return Path('项目管理规定') / category / filename
    location = drawing_location(row)
    result = sync_module('business_type').parse_number(str(row.get('文件名') or ''))
    discipline = result.get('discipline') or '未识别专业'
    scope = responsibility_scope()
    primary = (location is not None and discipline in scope['disciplines']
               and location[1].removeprefix('单元').zfill(3) in scope['units']
               and (not is_drawing(row) or ((scope.get('drawing_types') is None or row['业务类型'] in scope['drawing_types'])
                    and Path(filename).suffix[1:].upper() in scope.get('file_formats',['PDF']))))
    discipline = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', discipline).strip(' .')
    if is_equipment_list(row):
        root = Path(LIST_CATEGORY if primary else LIST_CATEGORY + '（其他）') / discipline
    else:
        root = Path('图纸' if primary else '图纸（其他）') / discipline / category
    if location is not None:
        plant, unit = location
        root = root / plant / unit
    return root / filename


def drawing_location(row):
    """Use only the PDF's own unambiguous, reviewed plant and unit numbering."""
    business = sync_module('business_type')
    result = business.parse_number(str(row.get('文件名') or ''))
    if result['status'] != 'recognized':
        return None
    # The classifier can confirm a common type across several numbers. Location
    # must agree as well; never let the first number choose a plant or unit.
    locations = set()
    for number in result['matched_number'].split('；'):
        item = business.parse_number(number)
        plant, unit = item['plant'], item['unit']
        if plant not in business.load_rules()['plants'] or not re.fullmatch(r'\d{2,3}', unit):
            return None
        locations.add((plant, unit))
    if len(locations) != 1:
        return None
    plant, unit = locations.pop()
    name = '跨厂区通用' if plant == 'GG' else business.load_rules()['plants'][plant]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(' .')
    return f'{plant}_{name}', f'单元{unit}'
