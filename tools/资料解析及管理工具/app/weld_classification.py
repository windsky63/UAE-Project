"""Integrate the supplied equipment reader and 260818 S/F rules without Excel I/O.

The original rule script is shipped verbatim. Only its workbook inputs and report
outputs are adapted; rule order and rule bodies remain unchanged. Each invocation
has its own namespace, so intermediate rule state cannot cross pipelines/jobs.
"""
import ast
from contextlib import redirect_stdout
from functools import lru_cache
import io
from pathlib import Path
import warnings

VERSION = 'pcf-equipment-v5+prefab-260818-v2-context'


@lru_cache(maxsize=1)
def rule_code():
    path = Path(__file__).with_name('prefab_rules_260818.py')
    tree = ast.parse(path.read_text(encoding='utf-8-sig'), filename=str(path))
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = {target.id for target in node.targets if isinstance(target, ast.Name)}
            if 'new_excel_file_path' in names:
                break  # Classification is complete; omit workbook/statistics export.
            replacements = {'rule_order': '[]', 'rule_category_map': '{}',
                            'rule_metadata_df': 'pd.DataFrame()',
                            'file_path2': "''", 'rule_summary_md_path': "''"}
            for name, value in replacements.items():
                if name in names:
                    node.value = ast.parse(value, mode='eval').body
        nodes.append(node)

    class Inputs(ast.NodeTransformer):
        def visit_Call(self, node):
            if isinstance(node.func, ast.Attribute) and node.func.attr == 'read_excel':
                sheet = next((keyword.value.value for keyword in node.keywords
                              if keyword.arg == 'sheet_name' and isinstance(keyword.value, ast.Constant)), None)
                name = {None: '_input_rows', '安装口坐标': '_install_points',
                        '设备连接信息': '_equipment_connections'}.get(sheet)
                if name is None:
                    raise ValueError(f'未适配的算法输入工作表：{sheet}')
                return ast.copy_location(ast.parse(name + '.copy(deep=True)', mode='eval').body, node)
            return self.generic_visit(node)

    adapted = ast.fix_missing_locations(Inputs().visit(ast.Module(body=nodes, type_ignores=[])))
    return compile(adapted, str(path), 'exec')


@lru_cache(maxsize=1)
def rule_fields():
    source_tree = ast.parse(Path(__file__).with_name('prefab_rules_260818.py').read_text(encoding='utf-8-sig'))
    return next(ast.literal_eval(node.value) for node in source_tree.body if isinstance(node, ast.Assign)
                  and any(isinstance(target, ast.Name) and target.id == 'new_column_names' for target in node.targets))


def apply_rules(rows, install_points, equipment_connections):
    import pandas as pd
    # The supplied script expects precisely this legacy workbook layout.
    code = rule_code()
    fields = rule_fields()
    log = io.StringIO()
    namespace = {'__name__': 'embedded_prefab_rules', '_input_rows': pd.DataFrame(rows).reindex(columns=fields).replace('', float('nan')),
                 'print': lambda *args, **kwargs: print(*args, **{**kwargs, 'file': log}),
                 '_install_points': install_points, '_equipment_connections': equipment_connections}
    with warnings.catch_warnings():
        # The legacy script prints percentage statistics; a missing bore yields
        # 0/0 there. Quantities still remain unknown in the final project report.
        warnings.filterwarnings('ignore', message='invalid value encountered in scalar divide', category=RuntimeWarning)
        exec(code, namespace)
    return namespace['df'], namespace['rule_trigger_records'], log.getvalue()


def classify_pcf(model, path, rows):
    """Classify the existing geometry-derived weld rows; never inherit old S/F flags."""
    import equipment_reader
    import parse_pcf_to_json as pcf
    import parse_idf_to_json as idf
    if not rows:
        return rows
    for row in rows:
        if not str(row.get('单元号') or '').strip():
            raise ValueError('PCF分类缺少单元上下文，不能使用默认预制结果')
        for side in (1, 2):
            if row.get(f'材料代码{side}') and not row.get(f'材料唯一码{side}'):
                raise ValueError(f'PCF焊口{row.get("焊口号")}的材料唯一码{side}未生成，不能使用默认预制结果')
    with redirect_stdout(io.StringIO()):
        device, install = equipment_reader.analyze_files([str(path)], str(model.get('unitName') or ''))
    header = pcf.load_pcf_structure(Path(path))['header_meta']
    scale = pcf.coord_scale(header) / idf.IDF_UNITS_PER_MM
    for frame in (device, install):
        if not frame.empty:
            frame['设备法兰口坐标'] = frame['设备法兰口坐标'].map(
                lambda value: ','.join(f'{float(part)*scale:.2f}' for part in str(value).split(',')))
            frame['规格'] = frame['规格'].map(lambda value: pcf.point_size_dn(value, header))
    # Preserve the geometry parser's material identity and mm coordinates.
    legacy = []
    for row in rows:
        dn = idf.safe_float(row.get('公称直径'))
        nps = idf.safe_float(row.get('寸径'))
        value = {'管线号': row['管线号'], '单元号': row['单元号'], '焊点编号': row['焊口号'],
                 '公称直径': dn, '焊点外径': idf.get_outer_diameter_mm(dn), '英制': nps, '吋径': nps,
                 '焊点位置': row['焊点坐标'], '焊接类型': row['焊接类型'], '焊点类型': '', '管段编号': ''}
        for side in (1, 2):
            value.update({f'描述{side}': row.get(f'材料描述{side}', ''),
                          f'焊接材料{side}': row.get(f'材料代码{side}', ''),
                          f'材料唯一码{side}': row.get(f'材料唯一码{side}', ''),
                          f'数量{side}': row.get(f'数量{side}', ''),
                          f'单位{side}': row.get(f'单位{side}', '')})
        legacy.append(value)
    result, triggers, _ = apply_rules(legacy, install, device)
    if len(result) != len(rows) or set(result.index) != set(range(len(rows))):
        raise ValueError('预制率算法改变了焊口行标识，拒绝错配分类结果')
    trace_by_weld = {}
    for entry in triggers:
        trace_by_weld.setdefault((str(entry['管线号']), str(entry['焊点编号'])), []).append(entry)
    for index, row in enumerate(rows):
        value = result.loc[index]
        kind = {'S': 'shop', 'F': 'field'}.get(str(value['焊点类型']))
        row.update(_classification=kind, _classification_engine=VERSION,
                   _classification_rule=str(value['规则']), _location_original=row.pop('_location', None),
                   _classification_trace=trace_by_weld.get((str(row['管线号']), str(row['焊口号'])), []))
    return rows
