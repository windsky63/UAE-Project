"""Procedure selection/classification derived from package_management_docs.py.

Keep the runtime independent of the one-off packaging/reference script.
"""
from pathlib import Path, PurePosixPath
import re


def normalized(value):
    return str(value or '').replace('\\', '/').strip()


def relative_path(value):
    parts = PurePosixPath(normalized(value)).parts
    if parts and parts[0].lower() == 'downloads':
        parts = parts[1:]
    if not parts or any(p in ('..', '/') or ':' in p for p in parts):
        return None
    return Path(*parts)


def inside(root, path):
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def selected(row):
    name = str(row.get('文件名') or '')
    is_pp = name.upper().startswith('PP-') or row.get('业务类型') == '项目程序文件'
    if not is_pp:
        return False
    if name not in ('', '未命名') and not name.upper().startswith('PP-'):
        return False
    return True


def classify(row):
    professional = str(row.get('业务专业') or '')
    if '/Procedure_Guide' in normalized(row.get('目录')) and row.get('业务类型') != '项目程序文件':
        return '09_指南与培训'
    if professional == '项目管理':
        return '01_项目管理'
    if '质量' in professional:
        return '02_质量管理'
    if '施工' in professional:
        return '03_施工管理'
    if any(word in professional.upper() for word in ('安全', '环境', '环保', 'HSE')):
        return '04_安全环保'
    if any(word in professional for word in ('采购', '物资', '材料')):
        return '05_采购与物资管理'
    if any(word in professional for word in ('调试', '试运', '开车')):
        return '06_调试与试运行'
    code = re.search(r'-(\d{2})-\d{2}-\d{3}(?:\b|_)', str(row.get('文档编号') or ''))
    if not code:
        code = re.search(r'-(\d{2})-\d{2}-\d{3}(?:\b|_)', str(row.get('文件名') or ''))
    mapping = {'50': '01_项目管理', '51': '02_质量管理', '69': '03_施工管理',
               '61': '04_安全环保', '55': '05_采购与物资管理', '53': '06_调试与试运行'}
    if code and code.group(1) in mapping:
        return mapping[code.group(1)]
    if professional and not professional.startswith('未识别'):
        safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', professional).strip(' .')
        if safe:
            return '07_专业技术程序'
    return '99_待核对'
