"""Shared responsibility settings owned by the management centre."""
import json
from pathlib import Path
import re

CONFIG_PATH = Path(__file__).resolve().parent/'config/drawing_scope.json'

def validate(data):
    if not isinstance(data, dict) or not {'disciplines','units'} <= set(data) or set(data)-{'disciplines','units','drawing_types','file_formats'}:
        raise ValueError('负责范围须包含 disciplines 和 units')
    for key in ('disciplines', 'units'):
        if not isinstance(data[key], list) or not all(isinstance(v, str) and v.strip() for v in data[key]):
            raise ValueError('负责专业和单元须为字符串列表')
    if any(not re.fullmatch(r'\d{2,3}', v.strip()) for v in data['units']):
        raise ValueError('负责单元须为两位或三位数字，不限定厂区')
    types=data.get('drawing_types')
    if types is not None and (not isinstance(types,list) or any(not isinstance(v,str) or not v.strip() for v in types)):
        raise ValueError('负责图纸类型须为名称列表')
    formats=data.get('file_formats',['PDF'])
    if not isinstance(formats,list) or any(not isinstance(v,str) or v.upper() not in {'PDF','DWG','DXF'} for v in formats):
        raise ValueError('文件格式须为 PDF、DWG、DXF 的列表')
    return dict(disciplines=list(dict.fromkeys(v.strip() for v in data['disciplines'])),
                units=sorted({v.strip().zfill(3) for v in data['units']}),
                drawing_types=None if types is None else list(dict.fromkeys(v.strip() for v in types)),
                file_formats=list(dict.fromkeys(v.upper() for v in formats)))


def options(path):
    path=Path(path)
    if not path.is_file():return dict(disciplines=[],units=[],type_catalog={},file_formats=['PDF','DWG','DXF'])
    rules=json.loads(path.read_text(encoding='utf-8'))
    common={v for v in rules['prefixes'].values() if '图' in v}
    catalog={name:set(common) for name in rules['disciplines'].values()}
    for item in rules['document_types']:
        if '图' in item['label']:catalog[rules['disciplines'][item['discipline']]].add(item['label'])
    for code,label in rules['construction_isometrics'].items():
        catalog['土建' if code=='45' else '管道'].add(label)
    return dict(disciplines=list(catalog),units=sorted({str(r['unit']).zfill(3) for r in rules['units']}),
                type_catalog={name:sorted(labels) for name,labels in catalog.items()},file_formats=['PDF','DWG','DXF'])

def load(path=None):
    return validate(json.loads(Path(path or CONFIG_PATH).read_text(encoding='utf-8-sig')))
