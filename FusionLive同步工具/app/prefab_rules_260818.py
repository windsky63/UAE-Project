import pandas as pd
import re
import numpy as np
from itertools import count
from itertools import product
from collections import defaultdict
from string import ascii_uppercase
from itertools import count

# 定义文件名变量
file_name = '焊口表'
file_path2 = r"D:\研发工作\python\IDF文件读取研究\设备法兰口坐标\IDF设备连接信息-北海项目.xlsx"
rule_summary_md_path = '预制率提高算法规则清单260818.md'
rule_category_order = ['基础规则', '主规则', '功能规则', '停用规则']

# 小管径预制规则开关：
# True：DN40及以下小管径可以预制，S-F036不运行；
# False：DN40及以下小管径不预制，S-F036运行。
ENABLE_SMALL_DIAMETER_PREFAB = True

# 规则分析文件输出开关：
# True：输出规则生效统计表和规则轨迹表；
# False：不输出上述分析文件，只生成运行后的焊口表。
OUTPUT_RULE_ANALYSIS_FILES = False

# 材料描述关键词统一配置：
# 后续所有基于“描述1/描述2”的元件类型判断均引用这里，新增项目描述写法时只需维护此处。
MATERIAL_DESCRIPTION_PATTERNS = {
    'pipe': (
        r'管子|钢管|直管|\bPIPES?\b|'
        r'(?=.*\bSMLS\b)(?=.*\b(?:BE|PE)\b)(?=.*HG/T20553)'
    ),
    'elbow': r'弯头|\bELB(?:OW)?\b|\bE(?:45|90)\b',
    'flange': r'法兰|\bFLANGE\b|\bFLG\b|\bWN\b',
    'tee': r'三通|\bTEE\b|\bEQ\s*TE\b|\bRED\s*TE\b',
    'branch_outlet': (
        r'管台|管接台|支管台|支管座|管箍|'
        r'\bHALF\s*COUPLING\b|\bWELDOLET\b|\bSOCKOLET\b|'
        r'\bHALF\s*COUP\b|\bRED\s*COUP\b|\bOLET\b'
    ),
    'cap': r'管帽|\bCAP\b',
    'valve': r'阀|\bVALVE\b',
    'instrument': r'仪表|表|计|\bINSTRUMENT\b',
    'eccentric_reducer': (
        r'偏心异径|ECC\.\s*REDUCER|ECC\s+REDUCER|RED\s+ECC|'
        r'REDUCER\s*\(RE\)|ECCENTRIC\s+REDUCER'
    ),
    'concentric_reducer': r'同心异径|CONC\.?\s*REDUCER|CONCENTRIC\s+REDUCER',
    'reducer': r'异径|大小头|\bREDUCER\b',
    'npt_or_pvc': r'\bNPT\b|\bPVC\b',
}

# 材料类型识别模式：
# 'A' = 材料描述模式；'B' = 材料代码模式；'C' = 兼容模式（描述或代码命中即认定）。
MATERIAL_IDENTIFICATION_MODE = 'A'

# 材料代码按“材料唯一码1/材料唯一码2”的开头识别。
MATERIAL_CODE_PREFIX_PATTERNS = {
    'pipe': r'^P',
    'flange': r'^F',
    'elbow': r'^(?:45EL|90EL)',
    'tee': r'^(?:RT|T)',
    'branch_outlet': r'^(?:HC|O)',
    'cap': r'^CAP',
    'valve': r'^V',
    'eccentric_reducer': r'^RE',
    'concentric_reducer': r'^RC',
    'reducer': r'^(?:RE|RC)',
}

PIPE_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['pipe']
ELBOW_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['elbow']
FLANGE_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['flange']
TEE_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['tee']
BRANCH_OUTLET_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['branch_outlet']
CAP_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['cap']
VALVE_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['valve']
INSTRUMENT_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['instrument']
VALVE_OR_INSTRUMENT_DESC_PATTERN = (
    rf'(?:{VALVE_DESC_PATTERN})|(?:{INSTRUMENT_DESC_PATTERN})'
)
ECCENTRIC_REDUCER_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['eccentric_reducer']
CONCENTRIC_REDUCER_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['concentric_reducer']
REDUCER_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['reducer']
NPT_OR_PVC_DESC_PATTERN = MATERIAL_DESCRIPTION_PATTERNS['npt_or_pvc']

# 走向识别配置。方向按无正反的中心线轴向判断，并保留XY平面内的两种45°方向。
DIRECTION_MIN_SEGMENT_LENGTH = 50.0
DIRECTION_MAX_ANGLE_DEG = 3.0

def description_matches(value, pattern):
    """判断一个材料描述是否命中统一配置的正则表达式。"""
    if pd.isna(value):
        return False
    return bool(re.search(pattern, str(value), re.IGNORECASE))

def description_series_matches(series, pattern):
    """矢量化判断材料描述列是否命中统一配置的正则表达式。"""
    return series.astype('string').str.contains(
        pattern, na=False, case=False, regex=True
    )

def material_code_matches(value, component_type):
    """判断材料唯一码是否命中指定元件类型的代码前缀。"""
    pattern = MATERIAL_CODE_PREFIX_PATTERNS.get(component_type)
    if pattern is None or pd.isna(value):
        return False
    return bool(re.search(pattern, str(value).strip(), re.IGNORECASE))

def material_code_series_matches(series, component_type):
    """矢量化判断材料唯一码列的元件类型。"""
    pattern = MATERIAL_CODE_PREFIX_PATTERNS.get(component_type)
    if pattern is None:
        return pd.Series(False, index=series.index, dtype=bool)
    return series.astype('string').str.strip().str.contains(
        pattern, na=False, case=False, regex=True
    )

def material_matches(description, material_code, component_type):
    """按当前A/B/C模式判断单个元件的材料类型。"""
    mode = str(MATERIAL_IDENTIFICATION_MODE).strip().upper()
    if mode not in {'A', 'B', 'C'}:
        raise ValueError(f'无效的材料类型识别模式: {MATERIAL_IDENTIFICATION_MODE}')
    desc_pattern = MATERIAL_DESCRIPTION_PATTERNS.get(component_type)
    desc_matched = bool(desc_pattern) and description_matches(description, desc_pattern)
    code_matched = material_code_matches(material_code, component_type)
    if mode == 'A':
        return desc_matched
    if mode == 'B':
        return code_matched
    return desc_matched or code_matched

def material_series_matches(description_series, code_series, component_type):
    """按当前A/B/C模式矢量化判断元件类型。"""
    mode = str(MATERIAL_IDENTIFICATION_MODE).strip().upper()
    if mode not in {'A', 'B', 'C'}:
        raise ValueError(f'无效的材料类型识别模式: {MATERIAL_IDENTIFICATION_MODE}')
    desc_pattern = MATERIAL_DESCRIPTION_PATTERNS.get(component_type)
    desc_mask = (
        description_series_matches(description_series, desc_pattern)
        if desc_pattern else pd.Series(False, index=description_series.index, dtype=bool)
    )
    code_mask = material_code_series_matches(code_series, component_type)
    if mode == 'A':
        return desc_mask
    if mode == 'B':
        return code_mask
    return desc_mask | code_mask

def row_material_matches(row, side, component_type):
    """按行及侧别（1/2）判断元件类型。"""
    return material_matches(
        row.get(f'描述{side}'), row.get(f'材料唯一码{side}'), component_type
    )

def dataframe_side_material_matches(df_context, side, component_type):
    """按DataFrame及侧别（1/2）矢量化判断元件类型。"""
    return material_series_matches(
        df_context[f'描述{side}'], df_context[f'材料唯一码{side}'], component_type
    )

def classify_component(description, material_code):
    """按当前识别模式将元件归一为拓扑判向所需的类型。"""
    component_types = (
        'elbow', 'tee', 'branch_outlet', 'cap', 'pipe', 'flange',
        'eccentric_reducer', 'concentric_reducer', 'reducer',
        'valve', 'instrument'
    )
    for component_type in component_types:
        if material_matches(description, material_code, component_type):
            if component_type in {'eccentric_reducer', 'concentric_reducer'}:
                return 'reducer'
            return component_type
    return 'other'

def load_rule_order_from_md(md_path):
    """从规则清单Markdown中提取规则代号顺序，作为统计表列顺序。"""
    try:
        with open(md_path, 'r', encoding='utf-8') as f:
            content = f.read()
        rules = re.findall(r'`(S-F\d{3}(?:-\d)?)`', content)
        # 去重但保持顺序
        return list(dict.fromkeys(rules))
    except Exception as e:
        print(f"读取规则清单失败，将使用结果数据中的规则顺序兜底: {e}")
        return []

def load_rule_category_map_from_md(md_path):
    """从规则清单Markdown总览表中提取规则分类映射。"""
    try:
        rule_category_map = {}
        with open(md_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('|'):
                    continue
                parts = [part.strip() for part in line.strip('|').split('|')]
                if len(parts) != 5 or parts[0] in ('序号', '---'):
                    continue
                rule_code = parts[1]
                category = parts[2]
                if re.fullmatch(r'S-F\d{3}(?:-\d)?', rule_code):
                    rule_category_map[rule_code] = category
        return rule_category_map
    except Exception as e:
        print(f"读取规则分类失败，将使用默认分类兜底: {e}")
        return {}

def load_rule_metadata_from_md(md_path):
    """从规则清单Markdown总览表中提取规则说明元数据。"""
    try:
        records = []
        with open(md_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line.startswith('|'):
                    continue
                parts = [part.strip() for part in line.strip('|').split('|')]
                if len(parts) != 5 or parts[0] in ('序号', '---'):
                    continue
                seq_no, rule_code, category, enabled, desc = parts
                if re.fullmatch(r'S-F\d{3}(?:-\d)?', rule_code):
                    records.append({
                        '序号': pd.to_numeric(seq_no, errors='coerce'),
                        '规则代号': rule_code,
                        '分类': category,
                        '是否运行': enabled,
                        '说明': desc
                    })
        return pd.DataFrame(records)
    except Exception as e:
        print(f"读取规则说明失败，将使用默认说明兜底: {e}")
        return pd.DataFrame(columns=['序号', '规则代号', '分类', '是否运行', '说明'])

rule_order = load_rule_order_from_md(rule_summary_md_path)
rule_category_map = load_rule_category_map_from_md(rule_summary_md_path)
rule_metadata_df = load_rule_metadata_from_md(rule_summary_md_path)
rule_trigger_records = []
rule_execution_step = 0

def snapshot_rule_state(df):
    """拍摄规则执行前的轻量快照，用于统计规则触发次数。"""
    snapshot = pd.DataFrame(index=df.index)
    snapshot['规则'] = df['规则'] if '规则' in df.columns else ''
    snapshot['焊点类型'] = df['焊点类型'] if '焊点类型' in df.columns else ''
    return snapshot

def record_rule_triggers(df, before_state, rule_code):
    """
    记录某条规则在本轮执行中新影响到的焊口。
    口径：本轮执行后，规则为当前rule_code，且规则或焊点类型相较执行前发生变化。
    """
    global rule_execution_step
    rule_execution_step += 1

    if before_state is None or before_state.empty:
        changed_mask = df['规则'] == rule_code
    else:
        changed_mask = (
            (df['规则'] == rule_code) &
            (
                before_state['规则'].ne(df['规则']) |
                before_state['焊点类型'].ne(df['焊点类型'])
            )
        )

    changed_rows = df.loc[changed_mask, ['单元号', '管线号', '焊点编号']].copy()
    if changed_rows.empty:
        return

    if before_state is not None and not before_state.empty:
        changed_rows['来源规则'] = before_state.loc[changed_rows.index, '规则'].fillna('').astype(str).values
        changed_rows['来源焊点类型'] = before_state.loc[changed_rows.index, '焊点类型'].fillna('').astype(str).values
    else:
        changed_rows['来源规则'] = ''
        changed_rows['来源焊点类型'] = ''

    changed_rows['规则'] = rule_code
    changed_rows['目标焊点类型'] = df.loc[changed_rows.index, '焊点类型'].fillna('').astype(str).values
    changed_rows['步骤序号'] = rule_execution_step
    changed_rows['是否改规则'] = changed_rows['来源规则'] != changed_rows['规则']
    changed_rows['是否改SF'] = changed_rows['来源焊点类型'] != changed_rows['目标焊点类型']
    rule_trigger_records.extend(changed_rows.to_dict('records'))

# 读取Excel文件（使用相对路径，文件需与代码在同一目录）
excel_file_path = f'{file_name}.xlsx'
df = pd.read_excel(excel_file_path)

# 读取Excel文件-IDF设备法兰口坐标.xlsx，指定读取'安装口坐标'sheet
df2 = pd.read_excel(file_path2, sheet_name='安装口坐标')

# 读取Excel文件-IDF设备连接信息，指定读取'设备连接信息'sheet（用于设备多口规则）
try:
    df_device_conn = pd.read_excel(file_path2, sheet_name='设备连接信息')
except Exception as e:
    df_device_conn = None
    print(f"读取设备连接信息sheet失败（将跳过依赖该表的规则）: {e}")

# Excel表格式处理
# 新的列名列表
new_column_names = ['管线号', '页码', '焊点编号','管段编号','公称直径','焊点外径','英制','吋径','壁厚系列1','壁厚系列2','材质1','材质2','焊点类型','焊接类型','管路等级','热处理','管道级别','焊接材料1','描述1','数量1','单位1','材料唯一码1','元件1坐标','焊接材料2','描述2','数量2','单位2','材料唯一码2','元件2坐标','焊点位置','焊点坐标(焊缝两侧管子坐标)','焊口位置信息标识符','单元号']

# 修改列名
df.columns = new_column_names

# 定义英制单位到小数值的映射（输入会先去除空格再匹配）
imperial_map = {
    '1/8': 0.125,
    '1/4': 0.25,
    '3/8': 0.375,
    '1/2': 0.5,
    '3/4': 0.75,
    '11/4': 1.25,   # 对应 1 1/4 英寸
    '11/2': 1.5,    # 对应 1 1/2 英寸
    '21/2': 2.5,    # 对应 2 1/2 英寸
    '31/2': 3.5     # 对应 3 1/2 英寸
}

def convert_imperial(value):
    """将英制分数值转换为小数"""
    # 转换为字符串并去除空格
    str_val = str(value).strip().replace(' ', '')
    # 尝试从映射表获取
    if str_val in imperial_map:
        return imperial_map[str_val]
    # 尝试直接转换为数值
    try:
        return float(str_val)
    except (ValueError, TypeError):
        return np.nan

# 应用转换函数，直接得到float类型结果
df['英制'] = df['英制'].apply(convert_imperial)

# 使用正则表达式去除 '焊点位置' 列中的所有空白字符
df['焊点位置'] = df['焊点位置'].replace(to_replace=r'\s', value='', regex=True)

# 将 '数量1'和'数量2' 列转换为数值类型
df['数量1'] = pd.to_numeric(df['数量1'], errors='coerce')
df['数量2'] = pd.to_numeric(df['数量2'], errors='coerce')

# 将 '公称直径' 列转换为数值类型
df['公称直径'] = pd.to_numeric(df['公称直径'], errors='coerce')

# 将 '焊点位置' 列转换为字符串类型
df['焊点位置'] = df['焊点位置'].astype(str)

# 将 '管段编号' 列转换为字符串类型
df['管段编号'] = df['管段编号'].astype(str)

# 将多个列同时转换为 float64
cols_to_float = ['吋径', '壁厚系列1', '材质2', '热处理', '管道级别']
df[cols_to_float] = df[cols_to_float].apply(pd.to_numeric, errors='coerce')

# 使用Pandas可空字符串类型，避免将真正的空单元格转换为字符串"nan"。
# 写回Excel时pd.NA会保持为空单元格。
df['描述1'] = df['描述1'].astype('string')
df['描述2'] = df['描述2'].astype('string')

# 将'焊点位置'分割成x, y, z三个列；无法解析为三维坐标的行直接跳过
weld_position_coords = (
    df['焊点位置']
    .str.split(',', n=2, expand=True)
    .reindex(columns=range(3))
    .apply(pd.to_numeric, errors='coerce')
)
df[['x', 'y', 'z']] = weld_position_coords.to_numpy()

# 计算所有行中'英制'列的总和
total_sum_metric = df['英制'].sum()

def determine_direction(df_pipeline):
    """
    按“共享材料唯一码 + 元件拓扑”确定焊口走向。

    - 同一根直管的两端焊口共享材料唯一码，以两端焊口坐标向量作为高置信度方向；
    - 弯头、三通、支管台和管帽属于变向/分支/终止元件，绝不使用其端口连线判向；
    - 异径管、法兰、阀门和仪表等双端直通元件可作为无直管时的低一级方向来源；
    - 保留XY平面内整体45°安装管线的xy_pos和xy_neg方向；
    - 无可靠拓扑方向来源时保持空值，不再按焊点编号相邻关系强行推测。
    """
    if df_pipeline.empty:
        return pd.Series(dtype=object)

    def classify_direction(point1, point2):
        """按与标准轴向的夹角分类，方向不区分正反。"""
        vector = np.asarray(point2, dtype=float) - np.asarray(point1, dtype=float)
        if not np.isfinite(vector).all():
            return None

        length = float(np.linalg.norm(vector))
        if length <= DIRECTION_MIN_SEGMENT_LENGTH:
            return None

        unit_vector = vector / length
        sqrt_half = np.sqrt(0.5)
        standard_directions = {
            'x': np.array([1.0, 0.0, 0.0]),
            'y': np.array([0.0, 1.0, 0.0]),
            'z': np.array([0.0, 0.0, 1.0]),
            'xy_pos': np.array([sqrt_half, sqrt_half, 0.0]),
            'xy_neg': np.array([sqrt_half, -sqrt_half, 0.0]),
        }

        best_direction = None
        best_angle = float('inf')
        for direction_name, standard_vector in standard_directions.items():
            # abs使正向和反向视为同一个管道走向。
            cosine = float(np.clip(abs(np.dot(unit_vector, standard_vector)), 0.0, 1.0))
            angle = float(np.degrees(np.arccos(cosine)))
            if angle < best_angle:
                best_direction = direction_name
                best_angle = angle

        if best_angle <= DIRECTION_MAX_ANGLE_DEG:
            return best_direction
        return None

    def normalize_component_code(value):
        if pd.isna(value):
            return None
        code = str(value).strip()
        if not code or code.lower() == 'nan':
            return None
        return code

    # 每个元件由“单元号 + 材料唯一码”唯一标识，防止不同单元重复编码串线。
    component_occurrences = defaultdict(list)
    for row_index, row in df_pipeline.iterrows():
        unit_no = row.get('单元号')
        unit_key = '' if pd.isna(unit_no) else str(unit_no).strip()
        for side in (1, 2):
            component_code = normalize_component_code(row.get(f'材料唯一码{side}'))
            if component_code is None:
                continue
            component_occurrences[(unit_key, component_code)].append({
                'index': row_index,
                'description': row.get(f'描述{side}'),
                'material_code': component_code,
            })

    # index -> (方向, 置信度, 来源元件)。直管优先级最高，避免被其他元件覆盖。
    direction_state = {}
    ambiguous_indices = set()

    def assign_direction(row_index, direction, confidence, source_key):
        if direction is None or row_index in ambiguous_indices:
            return
        current = direction_state.get(row_index)
        if current is None or confidence > current[1]:
            direction_state[row_index] = (direction, confidence, source_key)
        elif confidence == current[1] and direction != current[0]:
            # 同置信度出现互相矛盾的方向时宁可留空，不强行选择。
            direction_state.pop(row_index, None)
            ambiguous_indices.add(row_index)

    inline_component_confidence = {
        'pipe': 300,
        'reducer': 220,
        'flange': 200,
        'valve': 180,
        'instrument': 180,
    }
    direction_changing_components = {'elbow', 'tee', 'branch_outlet', 'cap'}

    for component_key, occurrences in component_occurrences.items():
        # 同一个元件在同一道焊口两侧重复出现时只保留一条。
        unique_occurrences = {}
        component_values = []
        for occurrence in occurrences:
            unique_occurrences.setdefault(occurrence['index'], occurrence)
            component_values.append((
                occurrence['description'], occurrence['material_code']
            ))

        # 描述可能在不同焊口存在语言/缩写差异，优先采用已识别出的明确元件类型。
        component_types = [
            classify_component(description, material_code)
            for description, material_code in component_values
        ]
        component_type = next(
            (item for item in component_types if item != 'other'),
            'other'
        )

        if component_type in direction_changing_components:
            continue
        if component_type not in inline_component_confidence:
            continue
        row_indices = list(unique_occurrences)
        if len(row_indices) > 2 and component_type == 'pipe':
            # 一根主管出现三个及以上焊口时，通常包含支管开孔。
            # 仅用距离最远的两个端部焊口恢复主管方向，不把主管方向赋给中间支管口；
            # 支管口可继续由其自身连接的另一根双端直管取得更高置信度方向。
            farthest_pair = None
            farthest_distance = -1.0
            for first_pos in range(len(row_indices) - 1):
                for second_pos in range(first_pos + 1, len(row_indices)):
                    first_index = row_indices[first_pos]
                    second_index = row_indices[second_pos]
                    first_point = df_pipeline.loc[first_index, ['x', 'y', 'z']].to_numpy(dtype=float)
                    second_point = df_pipeline.loc[second_index, ['x', 'y', 'z']].to_numpy(dtype=float)
                    if not np.isfinite(first_point).all() or not np.isfinite(second_point).all():
                        continue
                    distance = float(np.linalg.norm(second_point - first_point))
                    if distance > farthest_distance:
                        farthest_distance = distance
                        farthest_pair = (first_index, second_index, first_point, second_point)

            if farthest_pair is not None:
                first_index, second_index, first_point, second_point = farthest_pair
                direction = classify_direction(first_point, second_point)
                for row_index in (first_index, second_index):
                    assign_direction(row_index, direction, 260, component_key)
            continue

        if len(row_indices) != 2:
            continue

        point1 = df_pipeline.loc[row_indices[0], ['x', 'y', 'z']].to_numpy(dtype=float)
        point2 = df_pipeline.loc[row_indices[1], ['x', 'y', 'z']].to_numpy(dtype=float)
        direction = classify_direction(point1, point2)
        confidence = inline_component_confidence[component_type]
        for row_index in row_indices:
            assign_direction(row_index, direction, confidence, component_key)

    directions = pd.Series(index=df_pipeline.index, dtype=object)
    for row_index, (direction, _, _) in direction_state.items():
        directions.loc[row_index] = direction
    return directions

# 应用函数并创建'走向'列
df['走向'] = None
for pipeline_no in df['管线号'].unique():
    mask = df['管线号'] == pipeline_no
    pipeline_subset = df[mask]
    if not pipeline_subset.empty:
        df.loc[mask, '走向'] = determine_direction(pipeline_subset)

rule_state_before = snapshot_rule_state(df)

# 规则S-F000
# 全部焊口初始化为预制口
df.loc[:, '规则'] = 'S-F000'
df.loc[:, '焊点类型'] = 'S'

# 计算规则'S-F000'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F000') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F000') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F000改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F000')


rule_state_before = snapshot_rule_state(df)

# 规则S-F001
# 所有DN40及以下焊口全部初始化为现场口
df.loc[df['公称直径'] <= 40, '焊点类型'] = 'F'
df.loc[df['公称直径'] <= 40, '规则'] = 'S-F001'

# 计算规则'S-F001'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F001') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F001') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F001改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F001')


rule_state_before = snapshot_rule_state(df)

# 规则S-F002
# 管件与管件相连的焊缝初始化为预制口
# 筛选出'单位1'和'单位2'都是'个'（表示管件）的焊缝
mask = (df['公称直径'] > 40) & (df['单位1'] == '个') & (df['单位2'] == '个')
df.loc[mask, '焊点类型'] = 'S'
df.loc[mask, '规则'] = 'S-F002'

# 计算规则'S-F002'改为预制焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F002') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F002') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_S - total_english_units_F

print(f"规则S-F002改为预制焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F002')


rule_state_before = snapshot_rule_state(df)

# 规则S-F005
# 焊口左右两侧不是同一条管线的，该口改为现场口
mask = (df['公称直径'] > 40)

# 检测'焊接材料1'或'焊接材料2'为空白的行
condition = mask & (pd.isna(df['焊接材料1']) | pd.isna(df['焊接材料2']))

# 将这些行的'焊点类型'改为'F'
df.loc[condition, '焊点类型'] = 'F'

# 对于这些行，'规则'列写入'S-F005'
df.loc[condition, '规则'] = 'S-F005'

# 计算规则'S-F005'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F005') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F005') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F005改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F005')


rule_state_before = snapshot_rule_state(df)

# 规则S-F006
# 遍历与直管相连的焊缝，如果出现直管前后的焊缝坐标连接的直线存在坡度(两个焊缝xyz均不相等)则该预制口均改为现场口
# 初始化记录唯一码的集合
unique_codes = set()

# 遍历每一行
for index, row in df.iterrows():
    # 跳过 '公称直径' 列小于等于40的行
    if row['公称直径'] <= 40:
        continue

    # 判断描述1列是否包含'管子'字符串
    if row_material_matches(row, 1, 'pipe'):
        # 记录唯一码1
        unique_codes.add(row['材料唯一码1'])
    
    # 判断描述2列是否包含'管子'字符串
    if row_material_matches(row, 2, 'pipe'):
        # 记录唯一码2
        unique_codes.add(row['材料唯一码2'])

# 去重唯一码集合
unique_codes = list(set(unique_codes))

# 遍历唯一码集合
for code in unique_codes:
    # 先找到包含该管子的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == code) | (df['材料唯一码2'] == code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找唯一码对应的行数
    same_unit_df = df[df['单元号'] == unit_no]
    indices = same_unit_df[(same_unit_df['材料唯一码1'] == code) | (same_unit_df['材料唯一码2'] == code)].index
    
    # 如果找到了两行，继续处理
    if len(indices) == 2:
        coord1 = df.loc[indices[0], ['x', 'y', 'z']].to_numpy(dtype=np.float64)
        coord2 = df.loc[indices[1], ['x', 'y', 'z']].to_numpy(dtype=np.float64)
        if not (np.isfinite(coord1).all() and np.isfinite(coord2).all()):
            continue
        
        # 判断两个坐标的 x、y、z 是否均不相同
        if all(coord1[i] != coord2[i] for i in range(3)):
            # 修改相应的列
            df.at[indices[0], '焊点类型'] = 'F'
            df.at[indices[1], '焊点类型'] = 'F'
            df.at[indices[0], '规则'] = 'S-F006'
            df.at[indices[1], '规则'] = 'S-F006'

# 计算规则'S-F006'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F006') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F006') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F006改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F006')


rule_state_before = snapshot_rule_state(df)

# 规则S-F007
# 焊接阀门或焊接仪表跟管子连接的焊缝，设置成现场口
# 筛选出满足条件的行（阀门或仪表：描述中包含'阀'、'VALVE'、'表'或'计'）
condition = ((df['公称直径'] > 40)) & \
            (dataframe_side_material_matches(df, 1, 'valve') |
             dataframe_side_material_matches(df, 1, 'instrument') |
             dataframe_side_material_matches(df, 2, 'valve') |
             dataframe_side_material_matches(df, 2, 'instrument'))

# 更新这些行的'焊点类型'和'规则'列
df.loc[condition, '焊点类型'] = 'F'
df.loc[condition, '规则'] = 'S-F007'

# 计算规则'S-F007'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F007') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F007') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F007改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F007')


rule_state_before = snapshot_rule_state(df)

# 规则S-F008
# 补强板焊口设置为现场口
# 筛选出满足条件的行
condition = ((df['公称直径'] > 40)) & \
            (df['焊接类型'].str.lower().isin(['br', 'pad']))

# 更新这些行的'焊点类型'和'规则'列
df.loc[condition, '焊点类型'] = 'F'
df.loc[condition, '规则'] = 'S-F008'

# 计算规则'S-F008'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F008') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F008') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F008改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F008')


rule_state_before = snapshot_rule_state(df)

# 规则S-F009
# 螺纹口设置为现场口；NPT和PVC材料焊口设置为现场口
condition = (df['公称直径'] > 40) & (df['焊接类型'] == 'scw') & (df['焊点类型'] == 'S')
df.loc[condition, '焊点类型'] = 'F'
df.loc[condition, '规则'] = 'S-F009'

condition = (df['公称直径'] > 40) & (df['焊接类型'] == 'seton') & (df['焊点类型'] == 'S')
df.loc[condition, '焊点类型'] = 'F'
df.loc[condition, '规则'] = 'S-F009'

# 使用 str.contains 方法检查 '描述1' 和 '描述2' 是否包含 'NPT' 或 'PVC'
mask = (df['公称直径'] > 40) & (
    description_series_matches(df['描述1'], NPT_OR_PVC_DESC_PATTERN) |
    description_series_matches(df['描述2'], NPT_OR_PVC_DESC_PATTERN)
)

# 更新符合条件的行的 '焊点类型' 和 '规则'
df.loc[mask, '焊点类型'] = 'F'
df.loc[mask, '规则'] = 'S-F009'

# 计算规则'S-F009'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F009') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F009') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F009改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F009')


rule_state_before = snapshot_rule_state(df)

# 规则S-F010
# 连接设备的法兰口、随设备和仪表元件的虚线法兰设置为现场口
# 备注列包含：'设备法兰'、'设备法兰（虚线）'、'设备焊缝'、'虚线法兰'、'虚线异径管'
# 不再限制规格小于DN400
try:
    filtered_df2 = df2

    # 获取'设备法兰口坐标'和'备注'列的内容
    法兰_coordinates = filtered_df2['设备法兰口坐标'].tolist()
    备注_list = filtered_df2['备注'].tolist() if '备注' in filtered_df2.columns else [None] * len(法兰_coordinates)

    # 定义一个函数用于解析坐标并四舍五入取整
    def parse_and_round_coordinates(coord_str):
        try:
            # 提取坐标值 (x,y,z)
            coords = coord_str.strip('()').split(',')
            # 确保有3个坐标值
            if len(coords) == 3:
                # 四舍五入取整
                rounded_coords = [round(float(c.strip())) for c in coords]
                return tuple(rounded_coords)
            return None
        except:
            return None

    # 处理设备法兰口坐标列表，将其转换为四舍五入取整后的坐标元组列表，同时保留备注信息
    rounded_法兰_coordinates = []
    coord_to_备注 = {}  # 坐标到备注的映射
    for coord, 备注 in zip(法兰_coordinates, 备注_list):
        rounded_coord = parse_and_round_coordinates(coord)
        if rounded_coord:
            rounded_法兰_coordinates.append(rounded_coord)
            coord_to_备注[rounded_coord] = 备注

    # 遍历df，根据'焊点位置'更新'焊点类型'和'规则'
    for index, row in df.iterrows():
        # 解析并四舍五入当前行的焊点位置坐标
        rounded_weld_position = parse_and_round_coordinates(row['焊点位置'])
        # 如果坐标解析成功且在四舍五入取整后的法兰坐标列表中
        if rounded_weld_position and rounded_weld_position in rounded_法兰_coordinates:
            # 获取该坐标对应的备注
            当前备注 = coord_to_备注.get(rounded_weld_position, None)
            
            # S-F010-1已停用：安装口坐标表中的所有匹配口（包括备注为“设备法兰”）
            # 均按设备口安装焊口处理，不再为S-F010-1保留分支。
            df.at[index, '焊点类型'] = 'F'
            df.at[index, '规则'] = 'S-F010'
except Exception as e:
    print(f"规则S-F010执行出错: {e}")
    # 可选：打印列名辅助调试
    print(f"df2列名: {df2.columns.tolist() if 'df2' in locals() else 'df2未定义'}")

# 计算规则'S-F010'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F010') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F010') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F010改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F010')


rule_state_before = snapshot_rule_state(df)

# 规则S-F011
# 偏心大小头的小头为安装口
# 初始化记录唯一码的集合（向量化提取）
mask_diam = df['公称直径'] > 40
mask_red1 = dataframe_side_material_matches(df, 1, 'eccentric_reducer')
mask_red2 = dataframe_side_material_matches(df, 2, 'eccentric_reducer')
codes1 = set(df.loc[mask_diam & mask_red1, '材料唯一码1'].dropna())
codes2 = set(df.loc[mask_diam & mask_red2, '材料唯一码2'].dropna())
unique_codes = list(codes1.union(codes2))

# 去重唯一码集合
unique_codes = list(set(unique_codes))

# 遍历唯一码集合
for code in unique_codes:
    # 先找到包含该大小头的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == code) | (df['材料唯一码2'] == code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找唯一码对应的行数
    same_unit_df = df[df['单元号'] == unit_no]
    indices = same_unit_df[(same_unit_df['材料唯一码1'] == code) | (same_unit_df['材料唯一码2'] == code)].index
    
    # 如果找到了两行，继续处理
    if len(indices) == 2:
        # 获取两行的公称直径值
        diameter1 = df.loc[indices[0], '公称直径']
        diameter2 = df.loc[indices[1], '公称直径']
        
        # 比较公称直径大小
        if diameter1 < diameter2:
            # 更新小的一行的 '焊点类型' 和 '规则' 列
            df.at[indices[0], '焊点类型'] = 'F'
            df.at[indices[0], '规则'] = 'S-F011'
        elif diameter1 > diameter2:
            # 更新小的一行的 '焊点类型' 和 '规则' 列
            df.at[indices[1], '焊点类型'] = 'F'
            df.at[indices[1], '规则'] = 'S-F011'

# 计算规则'S-F011'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F011') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F011') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F011改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F011')


rule_state_before = snapshot_rule_state(df)

# 规则S-F013
# 遍历同一预制管段内的连续两道以上预制口的坐标，如果预制口的坐标出现不在同一个平面的情况，那么将最后一个被判断为在同一平面上的焊缝的改为现场口
# 生成管段编号
def generate_segment_labels():
    """ 生成连续段标记，如A、B、C、...、Z、AA、AB、... """
    for label_number in count(1):
        result = ""
        while label_number:
            label_number, remainder = divmod(label_number - 1, 26)
            result = ascii_uppercase[remainder] + result
        yield result

def find_connected_segments_advanced(group):
    """
    在给定的数据组中找到所有连续的段，并为每个新的'管线号'从'F001'开始分配标记。
    """
    segment_label_gen = generate_segment_labels()
    group = group.reset_index(drop=True)
    connected_segments = {}
    visited = set()

    def dfs(row_index, label):
        """ 深度优先搜索来找到所有连续的行 """
        if row_index in visited:
            return
        visited.add(row_index)
        connected_segments[row_index] = label
        row = group.loc[row_index]
        connected_rows = group[(group['材料唯一码1'] == row['材料唯一码1']) | 
                               (group['材料唯一码1'] == row['材料唯一码2']) |
                               (group['材料唯一码2'] == row['材料唯一码1']) | 
                               (group['材料唯一码2'] == row['材料唯一码2'])].index
        for idx in connected_rows:
            dfs(idx, label)

    for index, _ in group.iterrows():
        if index not in visited:
            label = next(segment_label_gen)
            dfs(index, label)

    return connected_segments

def find_and_label_connected_segments(df):
    """
    对'焊点类型'为'S'的数据进行处理，按'管线号'分组，并为每组相连的行分配管段编号。
    """
    df_filtered = df[df['焊点类型'] == 'S'].copy()  # 筛选'焊点类型'为'S'的数据
    df_filtered['管段编号'] = None  # 初始化'管段编号'列

    for pipeline, group in df_filtered.groupby('管线号'):
        connected_segments = find_connected_segments_advanced(group)
        for index, label in connected_segments.items():
            df_filtered.at[group.index[index], '管段编号'] = label

    return df_filtered

# 应用函数并显示结果
df_labeled = find_and_label_connected_segments(df)
df_labeled  # 显示结果，用于验证逻辑是否正确

# 将新的管段编号合并回原始的df DataFrame
df['管段编号'] = df_labeled['管段编号']

# 定义公称直径的条件
diameter_condition = (df['公称直径'] > 40)

# 筛选出符合条件的行
filtered_df = df[diameter_condition]

# 按'管线号'和'管段编号'分组
for (pipeline_no, segment_no), group in filtered_df.groupby(['管线号', '管段编号']):
    # 合并'材料唯一码1'和'材料唯一码2'，去重后查找
    unique_material_codes = pd.unique(group[['材料唯一码1', '材料唯一码2']].values.ravel('K'))
    filtered_group = group[group['材料唯一码1'].isin(unique_material_codes) | group['材料唯一码2'].isin(unique_material_codes)]

    # 初始化计数器和列表
    counter = 0
    positions = []
    s_indices = []

    # 步骤3: 遍历筛选后的DataFrame
    for index, row in filtered_group.iterrows():
        if row['焊点类型'] == 'S':
            current_position = row[['x', 'y', 'z']].to_numpy(dtype=np.float64)
            if not np.isfinite(current_position).all():
                counter = 0
                positions = []
                s_indices = []
                continue

            counter += 1
            positions.append(current_position)
            s_indices.append(index)

            if counter >= 3:
                xs, ys, zs = zip(*positions)

                if not ((len(set(xs)) == 1) or (len(set(ys)) == 1) or (len(set(zs)) == 1)):
                    # 步骤4: 修改不合理的焊点类型
                    update_index = s_indices[-2]  # 倒数第二个S的索引
                    df.at[update_index, '焊点类型'] = 'F'
                    df.at[update_index, '规则'] = 'S-F013'

                    counter = 1
                    positions = [current_position]
                    s_indices = [index]

        else:
            counter = 0
            positions = []
            s_indices = []

# 计算规则'S-F013'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F013') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F013') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F013改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F013')


rule_state_before = snapshot_rule_state(df)

# 规则S-F018
# 判断每个预制管段的空间跨度，如果超过12米×2.5米的限制，则将导致管段超限的第一道焊口改为现场口
def sf018_build_context(df):
    """预计算S-F018会反复用到的静态索引，避免在大表上重复全表扫描。"""
    context = {
        'index_codes': {},
        'index_unit': {},
        'coords': {},
        'unit_material_to_welds': {},
    }

    positions = df['焊点位置'].astype(str).str.split(',', expand=True)
    if positions.shape[1] >= 3:
        positions = positions.iloc[:, :3].apply(pd.to_numeric, errors='coerce')
    else:
        positions = pd.DataFrame(index=df.index, columns=[0, 1, 2], dtype=float)

    for idx in df.index:
        code1 = df.at[idx, '材料唯一码1']
        code2 = df.at[idx, '材料唯一码2']
        unit_no = df.at[idx, '单元号']

        codes = tuple(code for code in (code1, code2) if pd.notna(code))
        context['index_codes'][idx] = codes
        context['index_unit'][idx] = unit_no

        try:
            context['coords'][idx] = (
                float(positions.at[idx, 0]),
                float(positions.at[idx, 1]),
                float(positions.at[idx, 2])
            )
        except Exception:
            context['coords'][idx] = None

        for code in codes:
            context['unit_material_to_welds'].setdefault((unit_no, code), []).append(idx)

    return context

def sf018_generate_segment_labels():
    """生成连续段标记，如A、B、C、...、Z、AA、AB、..."""
    for label_number in count(1):
        result = ""
        while label_number:
            label_number, remainder = divmod(label_number - 1, 26)
            result = ascii_uppercase[remainder] + result
        yield result

def sf018_find_connected_segments(group):
    """在同一管线号内，按材料唯一码连通关系识别连续预制管段。"""
    segment_label_gen = sf018_generate_segment_labels()
    group = group.reset_index(drop=True)
    connected_segments = {}
    visited = set()
    code_to_rows = {}

    for row_index, row in group.iterrows():
        for code in (row['材料唯一码1'], row['材料唯一码2']):
            if pd.notna(code):
                code_to_rows.setdefault(code, set()).add(row_index)

    def dfs(row_index, label):
        if row_index in visited:
            return
        visited.add(row_index)
        connected_segments[row_index] = label
        row = group.loc[row_index]
        connected_rows = set()
        for code in (row['材料唯一码1'], row['材料唯一码2']):
            if pd.notna(code):
                connected_rows.update(code_to_rows.get(code, set()))
        for idx in connected_rows:
            dfs(idx, label)

    for index, _ in group.iterrows():
        if index not in visited:
            dfs(index, next(segment_label_gen))

    return connected_segments

def sf018_label_core_segments(df):
    """仅对当前预制口打标签，得到连续预制口组成的核心管段。"""
    df_filtered = df[(df['焊点类型'] == 'S') & (df['公称直径'] > 40)].copy()
    df_filtered['管段编号'] = None

    for _, group in df_filtered.groupby('管线号'):
        connected_segments = sf018_find_connected_segments(group)
        for index, label in connected_segments.items():
            df_filtered.at[group.index[index], '管段编号'] = label

    return df_filtered

def sf018_label_pipeline_segments(df, pipeline_no):
    """仅重建单条管线当前预制口的连续管段，用于增量更新。"""
    mask = (df['焊点类型'] == 'S') & (df['公称直径'] > 40) & (df['管线号'] == pipeline_no)
    pipeline_df = df.loc[mask].copy()
    pipeline_df['管段编号'] = None

    if pipeline_df.empty:
        return pipeline_df

    connected_segments = sf018_find_connected_segments(pipeline_df)
    for index, label in connected_segments.items():
        pipeline_df.at[pipeline_df.index[index], '管段编号'] = label

    return pipeline_df

def sf018_get_material_codes(context, weld_indices):
    """收集一组焊缝涉及的全部材料唯一码。"""
    if not weld_indices:
        return set()

    material_codes = set()
    for idx in weld_indices:
        material_codes.update(context['index_codes'].get(idx, ()))
    return material_codes

def sf018_expand_segment_welds(context, unit_no, core_indices, cache):
    """
    根据核心预制口涉及的材料唯一码，反查同一单元内的全部相关焊缝。
    这样得到的集合包含管段边界焊缝。
    """
    material_codes = sf018_get_material_codes(context, core_indices)
    if not material_codes:
        return []

    cache_key = (unit_no, frozenset(material_codes))
    cached = cache.get(cache_key)
    if cached is not None:
        return cached

    related_indices = set()
    for code in material_codes:
        related_indices.update(context['unit_material_to_welds'].get((unit_no, code), []))

    result = sorted(related_indices)
    cache[cache_key] = result
    return result

def sf018_count_segment_components(context, weld_indices):
    """统计一段焊缝集合涉及的有效元件数量。"""
    return len(sf018_get_material_codes(context, weld_indices))

def sf018_build_core_graph(context, core_indices):
    """构建核心预制口之间的连通图，并记录共享材料。"""
    adjacency = {idx: set() for idx in core_indices}

    for i, idx_a in enumerate(core_indices):
        codes_a = set(context['index_codes'].get(idx_a, ()))
        for idx_b in core_indices[i + 1:]:
            codes_b = set(context['index_codes'].get(idx_b, ()))
            common_codes = codes_a.intersection(codes_b)
            if common_codes:
                adjacency[idx_a].add(idx_b)
                adjacency[idx_b].add(idx_a)

    return adjacency

def sf018_order_core_segment(context, core_indices):
    """
    将连续预制口按拓扑顺序排成链。
    遇到非线性分支时，退回到按原始索引排序，避免误改。
    """
    if len(core_indices) <= 1:
        return list(core_indices)

    adjacency = sf018_build_core_graph(context, core_indices)
    degrees = {idx: len(neighbors) for idx, neighbors in adjacency.items()}
    branch_nodes = [idx for idx, degree in degrees.items() if degree > 2]
    if branch_nodes:
        return sorted(core_indices)

    endpoints = sorted([idx for idx, degree in degrees.items() if degree <= 1])
    start = endpoints[0] if endpoints else min(core_indices)

    ordered = []
    visited = set()
    current = start
    previous = None

    while current is not None and current not in visited:
        ordered.append(current)
        visited.add(current)
        next_candidates = sorted(idx for idx in adjacency[current] if idx != previous and idx not in visited)
        previous, current = current, (next_candidates[0] if next_candidates else None)

    if len(ordered) != len(core_indices):
        remaining = sorted(idx for idx in core_indices if idx not in visited)
        ordered.extend(remaining)

    return ordered

def sf018_parse_positions(context, weld_indices):
    """解析焊点坐标，并计算包络跨度。"""
    if not weld_indices:
        return None

    coords = [context['coords'].get(idx) for idx in weld_indices]
    coords = [coord for coord in coords if coord is not None and not any(pd.isna(value) for value in coord)]
    if not coords:
        return None

    xs = [coord[0] for coord in coords]
    ys = [coord[1] for coord in coords]
    zs = [coord[2] for coord in coords]
    span_x = max(xs) - min(xs)
    span_y = max(ys) - min(ys)
    span_z = max(zs) - min(zs)
    return span_x, span_y, span_z

def sf018_is_span_valid(spans):
    """校验12米×2.5米限制。"""
    if spans is None:
        return True
    ordered_spans = sorted(spans)
    return ordered_spans[-1] < 12000 and ordered_spans[-2] < 2500

def sf018_span_score(spans):
    """
    将跨度压缩为可比较的评分。
    先比较最大跨度，再比较第二大跨度，用于判断打断后是否真的变小。
    """
    if spans is None:
        return (0.0, 0.0)
    ordered_spans = sorted(float(value) for value in spans)
    return (ordered_spans[-1], ordered_spans[-2])

def sf018_find_first_break_index(context, ordered_core_indices, unit_no, expand_cache):
    """
    从链的一端逐道扩展核心预制口。
    一旦完整管段包络首次超限，当前这道预制口就是应被打断的内侧预制口。
    """
    if not ordered_core_indices:
        return None

    for end in (ordered_core_indices, list(reversed(ordered_core_indices))):
        prefix = []
        for core_idx in end:
            prefix.append(core_idx)
            expanded_indices = sf018_expand_segment_welds(context, unit_no, prefix, expand_cache)
            spans = sf018_parse_positions(context, expanded_indices)
            if not sf018_is_span_valid(spans):
                return core_idx

    return None

def sf018_apply_rule(df):
    """按单条管线增量更新，循环打断超限预制管段直到满足跨度限制。"""
    seen_signatures = set()
    stalled_segments = {}
    context = sf018_build_context(df)
    expand_cache = {}
    candidate_pipelines = [
        pipeline_no for pipeline_no in
        df.loc[(df['焊点类型'] == 'S') & (df['公称直径'] > 40), '管线号'].dropna().drop_duplicates().tolist()
    ]

    df['管段编号'] = None

    for pipeline_no in candidate_pipelines:
        while True:
            pipeline_mask = df['管线号'] == pipeline_no
            df.loc[pipeline_mask, '管段编号'] = None
            df_labeled = sf018_label_pipeline_segments(df, pipeline_no)
            if df_labeled.empty:
                break

            df.loc[df_labeled.index, '管段编号'] = df_labeled['管段编号']
            changed = False

            for segment_number, group in df_labeled.groupby('管段编号', dropna=True):
                if pd.isna(segment_number):
                    continue
                core_indices = group.index.tolist()
                if not core_indices:
                    continue

                # 保护条件1：只剩一道核心预制口时，不再继续按S-F018打断。
                if len(core_indices) <= 1:
                    continue

                unit_no = group.iloc[0]['单元号']
                full_indices = sf018_expand_segment_welds(context, unit_no, core_indices, expand_cache)

                # 保护条件2：只剩一个元件时，不再继续计算超限切分。
                if sf018_count_segment_components(context, full_indices) <= 1:
                    continue

                # 保护条件3：若同一核心段模式重复出现，说明继续打断已无法推进，直接跳过。
                segment_signature = (
                    pipeline_no,
                    unit_no,
                    tuple(sorted(core_indices)),
                    tuple(sorted(full_indices))
                )
                if segment_signature in seen_signatures:
                    continue

                spans = sf018_parse_positions(context, full_indices)
                current_score = sf018_span_score(spans)
                if sf018_is_span_valid(spans):
                    seen_signatures.add(segment_signature)
                    continue

                stalled_key = (pipeline_no, unit_no, sf018_count_segment_components(context, full_indices))
                previous_score = stalled_segments.get(stalled_key)
                if previous_score is not None and current_score >= previous_score:
                    seen_signatures.add(segment_signature)
                    continue

                ordered_core_indices = sf018_order_core_segment(context, core_indices)
                break_idx = sf018_find_first_break_index(context, ordered_core_indices, unit_no, expand_cache)
                if break_idx is None:
                    seen_signatures.add(segment_signature)
                    continue

                if df.at[break_idx, '焊点类型'] != 'F':
                    df.at[break_idx, '焊点类型'] = 'F'
                    df.at[break_idx, '规则'] = 'S-F018'
                    stalled_segments[stalled_key] = current_score
                    changed = True
                    break

                seen_signatures.add(segment_signature)

            if not changed:
                break

sf018_apply_rule(df)

# 计算规则'S-F018'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F018') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F018') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F018改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F018')


rule_state_before = snapshot_rule_state(df)

# 规则S-F014
# 遍历同一管段的连续两道以上预制口的信息，如果预制口所连接的管线长度超过15米则最后一道连续焊缝改为现场口
# 生成管段编号
def generate_segment_labels():
    """ 生成连续段标记，如A、B、C、...、Z、AA、AB、... """
    for label_number in count(1):
        result = ""
        while label_number:
            label_number, remainder = divmod(label_number - 1, 26)
            result = ascii_uppercase[remainder] + result
        yield result

def find_connected_segments_advanced(group):
    """
    在给定的数据组中找到所有连续的段，并为每个新的'管线号'从'F001'开始分配标记。
    """
    segment_label_gen = generate_segment_labels()
    group = group.reset_index(drop=True)
    connected_segments = {}
    visited = set()

    def dfs(row_index, label):
        """ 深度优先搜索来找到所有连续的行 """
        if row_index in visited:
            return
        visited.add(row_index)
        connected_segments[row_index] = label
        row = group.loc[row_index]
        connected_rows = group[(group['材料唯一码1'] == row['材料唯一码1']) | 
                               (group['材料唯一码1'] == row['材料唯一码2']) |
                               (group['材料唯一码2'] == row['材料唯一码1']) | 
                               (group['材料唯一码2'] == row['材料唯一码2'])].index
        for idx in connected_rows:
            dfs(idx, label)

    for index, _ in group.iterrows():
        if index not in visited:
            label = next(segment_label_gen)
            dfs(index, label)

    return connected_segments

def find_and_label_connected_segments(df):
    """
    对'焊点类型'为'S'的数据进行处理，按'管线号'分组，并为每组相连的行分配管段编号。
    """
    df_filtered = df[df['焊点类型'] == 'S'].copy()  # 筛选'焊点类型'为'S'的数据
    df_filtered['管段编号'] = None  # 初始化'管段编号'列

    for pipeline, group in df_filtered.groupby('管线号'):
        connected_segments = find_connected_segments_advanced(group)
        for index, label in connected_segments.items():
            df_filtered.at[group.index[index], '管段编号'] = label

    return df_filtered

# 应用函数并显示结果
df_labeled = find_and_label_connected_segments(df)
df_labeled  # 显示结果，用于验证逻辑是否正确

# 将新的管段编号合并回原始的df DataFrame
df['管段编号'] = df_labeled['管段编号']

# 提取管线号和管段编号的唯一组合
unique_combinations = df[['管线号', '管段编号']].drop_duplicates()

# 遍历每个唯一的管线号和管段编号组合
for _, row in unique_combinations.iterrows():
    pipeline_number = row['管线号']
    segment_number = row['管段编号']

    # 根据管线号和管段编号筛选数据
    filtered_df = df[(df['管线号'] == pipeline_number) & (df['管段编号'] == segment_number) & (df['公称直径'] > 40)]

    # 初始化变量
    current_total = 0
    s_count = 0
    recorded_codes = set()

    # 遍历每一行
    for sub_index, sub_row in filtered_df.iterrows():
        # 获取相关列的值
        code1 = sub_row['材料唯一码1']
        unit1 = sub_row['单位1']
        diameter1 = sub_row['公称直径']
        quantity1 = sub_row['数量1']

        code2 = sub_row['材料唯一码2']
        unit2 = sub_row['单位2']
        diameter2 = sub_row['公称直径']
        quantity2 = sub_row['数量2']

        # 处理材料唯一码1
        if code1 not in recorded_codes:
            if unit1 == '个':
                current_total += diameter1 * 0.005
            else:
                current_total += quantity1
            recorded_codes.add(code1)

        # 处理材料唯一码2
        if code2 not in recorded_codes:
            if unit2 == '个':
                current_total += diameter2 * 0.005
            else:
                current_total += quantity2
            recorded_codes.add(code2)

        # 处理焊点类型
        if sub_row['焊点类型'] == 'S':
            s_count += 1
        else:
            s_count = 0
            current_total = 0
            recorded_codes = set()

        # 判断是否有连续3行的焊点类型为 'S'，并检查统计数值是否超过15
        if s_count >= 2 and current_total > 15:
            df.at[sub_index, '焊点类型'] = 'F'
            df.at[sub_index, '规则'] = 'S-F014'
            current_total = 0
            s_count = 0
            recorded_codes = set()
            
# 计算规则'S-F014'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F014') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F014') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F014改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F014')


rule_state_before = snapshot_rule_state(df)

# 规则S-F016
# 一条管线如果在一段连续相同走向上的焊口全部为预制口，且超过1道，则修改其中一道为现场口作为调节段
# 更新'焊点类型'和'规则'
def update_weld_type_by_direction(df):
    # 筛选'公称直径'大于40的行
    df_filtered = df[(df['公称直径'] > 40)]

    # 遍历每个唯一的'管线号'
    for pipeline_no in df_filtered['管线号'].unique():
        # 筛选当前管线号的行并创建副本，避免切片修改警告
        pipeline_df = df_filtered[df_filtered['管线号'] == pipeline_no].copy()
        
        # 为连续相同的'走向'分组
        pipeline_df['走向_group'] = (pipeline_df['走向'] != pipeline_df['走向'].shift()).cumsum()

        # 遍历每个分组
        for group_id, group in pipeline_df.groupby('走向_group'):
            # 排除'走向'为空值的分组
            if group['走向'].isnull().all():
                continue
            
            # 获取当前分组的'走向'
            direction = group['走向'].iloc[0]

            # 如果连续相同的行数量大于1，且焊点类型均为'S'
            if len(group) > 1 and group['焊点类型'].eq('S').all():
                # 优先在规则不为S-F003的焊缝中选择
                non_sf003_group = group[group['规则'] != 'S-F003']
                
                # 如果全部都是S-F003规则，则不进行调整
                if non_sf003_group.empty:
                    continue
                
                # 使用非S-F003的焊缝组作为候选
                candidate_group = non_sf003_group
                
                if direction == 'z':
                    # 优先查找'描述1'或'描述2'包含'管子'的行
                    pipe_group = candidate_group[
                        dataframe_side_material_matches(candidate_group, 1, 'pipe') |
                        dataframe_side_material_matches(candidate_group, 2, 'pipe')
                    ]
                    if not pipe_group.empty:
                        # 修改'z'列值最接近1300的一行
                        target_index = (pipe_group['z'] - 1300).abs().idxmin()
                    else:
                        # 如果不存在符合条件的行，修改'z'列值最接近1300的一行
                        target_index = (candidate_group['z'] - 1300).abs().idxmin()

                    # 更新焊点类型和规则
                    df.loc[target_index, '焊点类型'] = 'F'
                    df.loc[target_index, '规则'] = 'S-F016z'

                elif direction in ['x', 'y', 'xy_pos', 'xy_neg']:
                    # 对于所有水平或斜向管段，按坐标顺序确定一道焊口作为调节口
                    # 优先查找'描述1'或'描述2'包含'管子'的行
                    pipe_group = candidate_group[
                        dataframe_side_material_matches(candidate_group, 1, 'pipe') |
                        dataframe_side_material_matches(candidate_group, 2, 'pipe')
                    ]
                    if not pipe_group.empty:
                        # 在包含'管子'的焊口中，按 x、y、z 的优先级选择坐标偏小的焊口
                        pipe_group_sorted = pipe_group.sort_values(['x', 'y', 'z'])
                        target_index = pipe_group_sorted.index[0]
                    else:
                        # 如果不存在包含'管子'的行，在所有候选焊口中按 x、y、z 的优先级选择坐标偏小的焊口
                        candidate_group_sorted = candidate_group.sort_values(['x', 'y', 'z'])
                        target_index = candidate_group_sorted.index[0]

                    # 更新焊点类型和规则
                    df.loc[target_index, '焊点类型'] = 'F'
                    df.loc[target_index, '规则'] = f'S-F016{direction}'

# 调用函数更新数据框
update_weld_type_by_direction(df)

# 计算规则'S-F016x'、'S-F016y'、'S-F016z'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'].str.contains('S-F016', na=False)) & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'].str.contains('S-F016', na=False)) & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F016改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F016')


rule_state_before = snapshot_rule_state(df)

# 规则S-F003
# 判定一个三通的三道焊缝，将其支管焊缝改为现场口；若两道主管焊缝均连接整根直管且均为预制口，则打断其中一道为现场口。
# 新逻辑采用距离法，不受管道方向影响，适用于正交及倾斜管道。

# 筛选出可能与三通相关的焊口
df_tees = df[(df['公称直径'] > 40)].copy()

# 使用矢量化操作快速查找所有三通的唯一码
mask1 = dataframe_side_material_matches(df_tees, 1, 'tee')
mask2 = dataframe_side_material_matches(df_tees, 2, 'tee')
codes1 = df_tees.loc[mask1, '材料唯一码1']
codes2 = df_tees.loc[mask2, '材料唯一码2']
unique_codes = pd.unique(pd.concat([codes1, codes2]).dropna())

# 遍历唯一的三通码
for code in unique_codes:
    # 先找到包含该三通的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == code) | (df['材料唯一码2'] == code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找与该三通相关的所有焊缝（应为3个）
    same_unit_df = df[df['单元号'] == unit_no]
    indices = same_unit_df.index[(same_unit_df['材料唯一码1'] == code) | (same_unit_df['材料唯一码2'] == code)].tolist()
    
    if len(indices) == 3:
        # 获取三个焊点的索引和坐标
        idx1, idx2, idx3 = indices[0], indices[1], indices[2]
        
        pos1_str = df.at[idx1, '焊点位置']
        pos2_str = df.at[idx2, '焊点位置']
        pos3_str = df.at[idx3, '焊点位置']

        # 确保坐标是有效字符串，且可以解析为包含3个浮点数的列表
        try:
            p1 = np.array([float(c) for c in pos1_str.split(',') if c.strip()])
            p2 = np.array([float(c) for c in pos2_str.split(',') if c.strip()])
            p3 = np.array([float(c) for c in pos3_str.split(',') if c.strip()])
            if not (
                p1.shape == (3,) and p2.shape == (3,) and p3.shape == (3,) and
                np.isfinite(p1).all() and np.isfinite(p2).all() and np.isfinite(p3).all()
            ):
                raise ValueError("无效的坐标维度")
        except (ValueError, AttributeError, IndexError):
            # 如果坐标格式不正确或不完整，跳过这个三通
            continue

        # 检查是否是三个不同的点，防止因数据问题导致的错误
        if np.array_equal(p1, p2) or np.array_equal(p1, p3) or np.array_equal(p2, p3):
            continue

        # 距离法：计算三点之间距离的平方。主管上的两个焊点距离最远。
        dist12_sq = np.sum((p1 - p2)**2)
        dist13_sq = np.sum((p1 - p3)**2)
        dist23_sq = np.sum((p2 - p3)**2)

        # 建立距离与支管点索引的映射
        # 距离d(P1,P2)最长时，P3是支管点
        distances = {
            dist12_sq: idx3,
            dist13_sq: idx2,
            dist23_sq: idx1
        }
        
        # 找到最大距离
        max_dist_sq = max(distances.keys())
        
        # 检查是否有多个最大距离（例如，等边三角形）。
        # 这种情况物理上少见且难以判断，为保证准确性，跳过此类情况。
        # 这也确保了一个三通最多只有一个焊缝被此规则修改。
        keys_list = list(distances.keys())
        if keys_list.count(max_dist_sq) > 1:
            continue

        # 距离最远的两个点在主管上，剩下的那个点是支管点
        branch_index = distances[max_dist_sq]
        
        # 新增逻辑：检查支管是否连接短管+法兰，若是则可以预制
        branch_row = df.loc[branch_index]
        branch_can_prefab = False  # 标记是否满足短管+法兰条件
        flange_weld_index = None   # 记录法兰口的索引
        
        # 判断支管焊缝的描述1或描述2是否包含管子
        desc1_is_pipe = row_material_matches(branch_row, 1, 'pipe')
        desc2_is_pipe = row_material_matches(branch_row, 2, 'pipe')
        
        if desc1_is_pipe or desc2_is_pipe:
            # 获取管子对应的数量和材料唯一码
            if desc1_is_pipe:
                pipe_quantity = branch_row['数量1']
                pipe_code = branch_row['材料唯一码1']
            else:
                pipe_quantity = branch_row['数量2']
                pipe_code = branch_row['材料唯一码2']
            
            # 检查管子长度是否≤0.3米
            if pd.notna(pipe_quantity) and float(pipe_quantity) <= 0.3:
                # 在相同单元号内查找这个管子的材料唯一码
                unit_no = branch_row['单元号']
                same_unit_df = df[df['单元号'] == unit_no]
                pipe_welds = same_unit_df[(same_unit_df['材料唯一码1'] == pipe_code) | 
                                          (same_unit_df['材料唯一码2'] == pipe_code)]
                
                # 如果找到两行（管子连接两道焊缝）
                if len(pipe_welds) == 2:
                    # 找到另一行（非支管焊缝行）
                    other_weld_indices = pipe_welds.index[pipe_welds.index != branch_index]
                    if len(other_weld_indices) == 1:
                        other_weld_index = other_weld_indices[0]
                        other_row = df.loc[other_weld_index]
                        
                        # 检查另一行是否一侧是管子，另一侧是法兰
                        other_desc1_is_pipe = row_material_matches(other_row, 1, 'pipe')
                        other_desc2_is_pipe = row_material_matches(other_row, 2, 'pipe')
                        other_desc1_is_flange = row_material_matches(other_row, 1, 'flange')
                        other_desc2_is_flange = row_material_matches(other_row, 2, 'flange')
                        
                        # 一侧是管子，另一侧是法兰
                        if (other_desc1_is_pipe and other_desc2_is_flange) or \
                           (other_desc2_is_pipe and other_desc1_is_flange):
                            branch_can_prefab = True
                            flange_weld_index = other_weld_index
        
        # 根据判断结果设置焊点类型
        if branch_can_prefab and flange_weld_index is not None:
            # 支管口和法兰口均可预制
            df.loc[branch_index, '焊点类型'] = 'S'
            df.loc[branch_index, '规则'] = 'S-F003'
            df.loc[flange_weld_index, '焊点类型'] = 'S'
            df.loc[flange_weld_index, '规则'] = 'S-F003'
        else:
            # 将支管焊缝标记为现场焊口 'F'
            df.loc[branch_index, '焊点类型'] = 'F'
            df.loc[branch_index, '规则'] = 'S-F003'

        # 增强逻辑：若两道主管焊缝均连接整根直管(6m/12m)且均为预制口，则打断其中一道为现场口
        main_indices = [i for i in indices if i != branch_index]
        if len(main_indices) == 2:
            def _is_connected_whole_pipe(weld_idx, tee_code):
                """
                判断某道焊缝在三通侧的另一端是否为整根直管（描述为管子/PIPE且长度为6或12）。
                返回 (是否整管, 整管长度[float或None])
                """
                r = df.loc[weld_idx]
                if r['材料唯一码1'] == tee_code:
                    other_desc = r['描述2']
                    other_code = r['材料唯一码2']
                    other_qty = r['数量2']
                elif r['材料唯一码2'] == tee_code:
                    other_desc = r['描述1']
                    other_code = r['材料唯一码1']
                    other_qty = r['数量1']
                else:
                    return False, None

                if not material_matches(other_desc, other_code, 'pipe'):
                    return False, None
                if pd.isna(other_qty):
                    return False, None
                try:
                    q = float(other_qty)
                except (ValueError, TypeError):
                    return False, None
                if q in (6.0, 12.0):
                    return True, q
                return False, None

            ok1, q1 = _is_connected_whole_pipe(main_indices[0], code)
            ok2, q2 = _is_connected_whole_pipe(main_indices[1], code)
            if ok1 and ok2 and (df.loc[main_indices[0], '焊点类型'] == 'S') and (df.loc[main_indices[1], '焊点类型'] == 'S'):
                # 选择打断哪一侧：优先打断连接12m整管的一侧；否则打断标高更低的一侧（保证确定性）
                if (q1 is not None) and (q2 is not None) and (q1 != q2):
                    idx_to_F = main_indices[0] if q1 > q2 else main_indices[1]
                else:
                    z1 = df.loc[main_indices[0], 'z']
                    z2 = df.loc[main_indices[1], 'z']
                    if pd.isna(z1) or pd.isna(z2):
                        continue
                    idx_to_F = main_indices[0] if z1 <= z2 else main_indices[1]
                df.loc[idx_to_F, '焊点类型'] = 'F'
                df.loc[idx_to_F, '规则'] = 'S-F003'

# 计算规则'S-F003'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F003') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F003') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F003改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F003')


rule_state_before = snapshot_rule_state(df)

# 规则S-F004
# 对于直管焊直管的口，如果焊口两侧长度相加超过11米，那么该口改为现场口
# 使用矢量化操作替代iterrows以提高性能
diameter_mask = (df['公称直径'] > 40)
pipe_mask = dataframe_side_material_matches(df, 1, 'pipe') & \
            dataframe_side_material_matches(df, 2, 'pipe')
quantity_mask = (df['数量1'] + df['数量2']) > 11

update_mask = diameter_mask & pipe_mask & quantity_mask
df.loc[update_mask, '焊点类型'] = 'F'
df.loc[update_mask, '规则'] = 'S-F004'

# 计算规则'S-F004'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F004') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F004') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F004改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F004')


'''
# 规则S-F004-1
# 对于超过一根整管长度的管段，只预制第一根整管与相邻管件之间的焊口，剩余整管之间、整管与最后一根短管之间、最后一根短管与相邻管件之间的焊口全部为安装口
# 辅助函数，用于从一个起点（管子）沿一个方向（通过一个焊口）追溯整个元件链
def trace_chain_one_direction(start_pipe_code, start_weld_idx, df_context, direction_name):
    chain = []
    current_component_code = start_pipe_code
    current_weld_idx = start_weld_idx
    visited_welds = {start_weld_idx}

    while True:
        if current_weld_idx not in df_context.index: return None # 安全检查
        current_weld_row = df_context.loc[current_weld_idx]
        
        # 确定下一个元件的信息
        is_code1 = current_weld_row['材料唯一码1'] == current_component_code
        next_comp_code = current_weld_row['材料唯一码2'] if is_code1 else current_weld_row['材料唯一码1']
        
        if pd.isna(next_comp_code): return None

        next_comp_info = {
            'weld_idx': current_weld_idx,
            'comp_code': next_comp_code,
            'comp_desc': current_weld_row['描述2'] if is_code1 else current_weld_row['描述1'],
            'comp_unit': current_weld_row['单位2'] if is_code1 else current_weld_row['单位1'],
            'comp_quantity': current_weld_row['数量2'] if is_code1 else current_weld_row['数量1']
        }
        chain.append(next_comp_info)

        if next_comp_info['comp_unit'] == '个':
            return chain

        # 如果不是管件（即是管子），则查找它的另一个焊口以继续
        all_welds_for_next_comp = df_context[
            (df_context['材料唯一码1'] == next_comp_code) | (df_context['材料唯一码2'] == next_comp_code)
        ]
        
        other_weld_indices = all_welds_for_next_comp.index[all_welds_for_next_comp.index != current_weld_idx]

        if len(other_weld_indices) == 0:
            return None # 链条末端没有管件
        
        elif len(other_weld_indices) == 1:
            # 这是简单链条的预期情况（共2个焊缝，1个其他）
            next_weld_idx = other_weld_indices[0]
        else: # len(other_weld_indices) > 1，例如分支连接
            current_imperial_size = current_weld_row['英制']
            
            # 按英制尺寸筛选其他焊缝
            candidate_welds = df_context.loc[other_weld_indices]
            matching_candidates = candidate_welds[candidate_welds['英制'] == current_imperial_size]
            
            if len(matching_candidates) == 1:
                next_weld_idx = matching_candidates.index[0]
            else:
                return None
        
        if next_weld_idx in visited_welds: # 避免无限循环
            return None
            
        # 准备下一次循环
        current_component_code = next_comp_code
        current_weld_idx = next_weld_idx
        visited_welds.add(current_weld_idx)

# 用于存储已被本规则处理过的焊口，避免重复计算
processed_welds_sf004_1 = set()

# 筛选出所有长度为6米或12米的整管作为可能的起点
whole_pipes_mask1 = dataframe_side_material_matches(df, 1, 'pipe') & df['数量1'].isin([6, 12])
whole_pipes_mask2 = dataframe_side_material_matches(df, 2, 'pipe') & df['数量2'].isin([6, 12])
start_welds_df = df[
    (whole_pipes_mask1 | whole_pipes_mask2) &
    (df['公称直径'] > 40)
].copy()

for start_weld_idx, start_weld_row in start_welds_df.iterrows():
    if start_weld_idx in processed_welds_sf004_1:
        continue

    # 确定起始整管的唯一码和长度
    is_pipe1_whole = whole_pipes_mask1.get(start_weld_idx, False)
    start_pipe_code = start_weld_row['材料唯一码1'] if is_pipe1_whole else start_weld_row['材料唯一码2']
    whole_pipe_len = start_weld_row['数量1'] if is_pipe1_whole else start_weld_row['数量2']

    # 找到这个整管连接的两个焊口（在相同单元号内查找）
    unit_no = start_weld_row['单元号']
    same_unit_df = df[df['单元号'] == unit_no]
    all_welds_for_start_pipe = same_unit_df[(same_unit_df['材料唯一码1'] == start_pipe_code) | (same_unit_df['材料唯一码2'] == start_pipe_code)]
    
    weld1_idx, weld2_idx = None, None

    if len(all_welds_for_start_pipe) < 2:
        continue
    elif len(all_welds_for_start_pipe) == 2:
        weld1_idx, weld2_idx = all_welds_for_start_pipe.index
    else: # 长度 > 2
        weld1_idx = start_weld_idx # 触发搜索的焊缝是链条的一端
        
        other_weld_indices = all_welds_for_start_pipe.index[all_welds_for_start_pipe.index != weld1_idx]
        current_imperial_size = df.loc[weld1_idx, '英制']

        candidate_welds = df.loc[other_weld_indices]
        matching_candidates = candidate_welds[candidate_welds['英制'] == current_imperial_size]

        if len(matching_candidates) == 1:
            weld2_idx = matching_candidates.index[0]
        else:
            continue

    if weld1_idx is None or weld2_idx is None:
        continue

    # 确定追踪上下文为当前管线
    df_pipeline = df[df['管线号'] == start_weld_row['管线号']]

    # 从两个方向进行追踪
    chain_part1 = trace_chain_one_direction(start_pipe_code, weld1_idx, df_pipeline, "方向1")
    chain_part2 = trace_chain_one_direction(start_pipe_code, weld2_idx, df_pipeline, "方向2")

    if not chain_part1 or not chain_part2:
        continue

    # 合并并验证结构
    chain_part1.reverse()
    full_chain = chain_part1 + [{
        'comp_code': start_pipe_code,
        'comp_desc': '起始整管',
        'comp_quantity': whole_pipe_len
    }] + chain_part2
    
    # 验证1：链条两端必须是管件
    if full_chain[0].get('comp_unit') != '个' or full_chain[-1].get('comp_unit') != '个':
        continue
    
    # 验证2：检查管段结构是否为 '整管(s) + [短管]' 且所有整管长度均与起点一致
    pipes_in_chain = [elem for elem in full_chain if elem.get('comp_unit') != '个']
    
    # 检查2.1：所有应为整管的管子必须具有正确的长度
    whole_pipes_correct = all(p['comp_quantity'] == whole_pipe_len for p in pipes_in_chain if p['comp_quantity'] in [6, 12])
    if not whole_pipes_correct:
        continue

    # 检查2.2：最多只能有一根短管
    short_pipes = [p for p in pipes_in_chain if p['comp_quantity'] not in [6, 12]]
    if len(short_pipes) > 1:
        continue

    # 检查2.3：如果有一根短管，它必须位于管段序列的开头或末尾
    if len(short_pipes) == 1:
        # 我们需要找到短管在管子序列中的索引
        short_pipe_code = short_pipes[0]['comp_code']
        pipe_codes = [p['comp_code'] for p in pipes_in_chain]
        short_pipe_index = pipe_codes.index(short_pipe_code)
        
        if not (short_pipe_index == 0 or short_pipe_index == len(pipes_in_chain) - 1):
            continue
            
    # 执行修改
    all_weld_indices_in_chain = [elem['weld_idx'] for elem in full_chain if 'weld_idx' in elem]
    
    # 根据短管位置决定哪个焊口应为'S'
    weld_to_be_S = None
    
    # 情况1: 没有短管
    if len(short_pipes) == 0:
        weld_to_be_S = all_weld_indices_in_chain[0]
    
    # 情况2：有一个短管
    else: # 有一个短管
        short_pipe_code = short_pipes[0]['comp_code']
        pipe_codes = [p['comp_code'] for p in pipes_in_chain]
        short_pipe_index = pipe_codes.index(short_pipe_code)
        
        if short_pipe_index == 0: # 短管在管段起始端
            weld_to_be_S = all_weld_indices_in_chain[-1]
        else: # 短管在管段末端
            weld_to_be_S = all_weld_indices_in_chain[0]

    # 应用修改
    for weld_idx in all_weld_indices_in_chain:
        if weld_idx == weld_to_be_S:
            df.loc[weld_idx, '焊点类型'] = 'S'
        else:
            df.loc[weld_idx, '焊点类型'] = 'F'
        df.loc[weld_idx, '规则'] = 'S-F004-1'
            
    # 记录已处理的焊口
    processed_welds_sf004_1.update(all_weld_indices_in_chain)

# 计算规则'S-F004-1'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F004-1') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F004-1') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F004-1改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
'''

# 规则S-F010-1（已停用）
# 如果非虚线设备法兰连接管子，则此法兰口预制，管子另一端安装
'''
rule_state_before = snapshot_rule_state(df)

try:
    filtered_df2 = df2

    # 获取'设备法兰口坐标'和'备注'列的内容
    法兰_coordinates = filtered_df2['设备法兰口坐标'].tolist()
    备注_list = filtered_df2['备注'].tolist() if '备注' in filtered_df2.columns else [None] * len(法兰_coordinates)

    # 定义一个函数用于解析坐标并四舍五入取整
    def parse_and_round_coordinates(coord_str):
        try:
            # 提取坐标值 (x,y,z)
            coords = coord_str.strip('()').split(',')
            # 确保有3个坐标值
            if len(coords) == 3:
                # 四舍五入取整
                rounded_coords = [round(float(c.strip())) for c in coords]
                return tuple(rounded_coords)
            return None
        except:
            return None

    # 处理设备法兰口坐标列表，将其转换为四舍五入取整后的坐标元组列表，同时保留备注信息
    rounded_法兰_coordinates = []
    coord_to_备注 = {}  # 坐标到备注的映射
    for coord, 备注 in zip(法兰_coordinates, 备注_list):
        rounded_coord = parse_and_round_coordinates(coord)
        if rounded_coord:
            rounded_法兰_coordinates.append(rounded_coord)
            coord_to_备注[rounded_coord] = 备注

    # 遍历df，根据'焊点位置'更新'焊点类型'和'规则'
    # 仅处理备注为'设备法兰'的情况
    for index, row in df.iterrows():
        # 解析并四舍五入当前行的焊点位置坐标
        rounded_weld_position = parse_and_round_coordinates(row['焊点位置'])
        # 如果坐标解析成功且在四舍五入取整后的法兰坐标列表中
        if rounded_weld_position and rounded_weld_position in rounded_法兰_coordinates:
            # 获取该坐标对应的备注
            当前备注 = coord_to_备注.get(rounded_weld_position, None)
            
            # 规则S-F010-1：如果非虚线设备法兰连接管子，则此法兰口预制，管子另一端安装
            # 仅对备注为'设备法兰'的进行S-F010-1判断
            if 当前备注 == '设备法兰':
                handled_as_prefab = False
                pipe_code = None
                # 如果描述1对应法兰材料，则判断描述2是否为管子材料
                if row_material_matches(row, 1, 'flange') or pd.isna(row['描述1']) or str(row['描述1']) == 'nan':
                    if row_material_matches(row, 2, 'pipe'):
                        pipe_code = row['材料唯一码2']
                # 如果描述2对应法兰材料，则判断描述1是否为管子材料
                elif row_material_matches(row, 2, 'flange') or pd.isna(row['描述2']) or str(row['描述2']) == 'nan':
                    if row_material_matches(row, 1, 'pipe'):
                        pipe_code = row['材料唯一码1']
                
                # 如果找到了管子的材料唯一码
                if pipe_code is not None and pd.notna(pipe_code):
                    # 在该单元号内查找这个管子的材料唯一码
                    unit_no = row['单元号']
                    same_unit_df = df[df['单元号'] == unit_no]
                    pipe_welds = same_unit_df[(same_unit_df['材料唯一码1'] == pipe_code) | (same_unit_df['材料唯一码2'] == pipe_code)]
                    
                    # 如果找到了2行（管子连接两道焊缝）
                    if len(pipe_welds) == 2:
                        # 设备法兰口那行改为S（预制口），规则写入S-F010-1
                        df.at[index, '焊点类型'] = 'S'
                        df.at[index, '规则'] = 'S-F010-1'
                        
                        # 找到管子另一端那行，改为F（安装口），规则也写入S-F010-1
                        other_weld_idx = pipe_welds.index[pipe_welds.index != index]
                        if len(other_weld_idx) == 1:
                            df.at[other_weld_idx[0], '焊点类型'] = 'F'
                            df.at[other_weld_idx[0], '规则'] = 'S-F010-1'
                            handled_as_prefab = True

                # 如果设备法兰口没有满足“连接管子并能定位到管子另一端”的条件，
                # 则按安装口处理，而不是保留前序规则结果。
                if not handled_as_prefab:
                    df.at[index, '焊点类型'] = 'F'
                    df.at[index, '规则'] = 'S-F010'
except Exception as e:
    print(f"规则S-F010-1执行出错: {e}")
    # 可选：打印列名辅助调试
    print(f"df2列名: {df2.columns.tolist() if 'df2' in locals() else 'df2未定义'}")

# 计算规则'S-F010-1'改为安装焊口的寸径
total_english_units_F_1 = df.loc[(df['规则'] == 'S-F010-1') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S_1 = df.loc[(df['规则'] == 'S-F010-1') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter_1 = total_english_units_F_1 - total_english_units_S_1

print(f"规则S-F010-1改为安装焊口的寸径: {installation_weld_diameter_1}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F010-1')
'''


rule_state_before = snapshot_rule_state(df)

# 规则S-F010-2
# 对于连续的法兰焊口，若大于等于4个，则将首端和末端的焊口设置为现场口，中间设置为预制口。
# 连续的法兰焊口通过数字连续的焊点编号判断（按同一管线号内、法兰焊口的焊点编号数字部分排序后，差值为1视为连续）
def extract_weld_id_num_for_sf010_2(weld_id):
    """稳健地提取焊点编号中的数字部分，用于连续性判断。"""
    try:
        if pd.notna(weld_id):
            match = re.search(r'[0-9]+', str(weld_id))
            if match:
                return int(match.group(0))
    except (ValueError, TypeError):
        pass
    return np.nan

# 识别法兰焊口（任一侧描述包含法兰关键词），S-F010设备口保持现场口并跳过
flange_mask = dataframe_side_material_matches(df, 1, 'flange') | \
              dataframe_side_material_matches(df, 2, 'flange')
flange_mask &= df['规则'] != 'S-F010'

df_flange = df.loc[flange_mask, ['管线号', '焊点编号']].copy()
df_flange['焊点编号_数值'] = df_flange['焊点编号'].apply(extract_weld_id_num_for_sf010_2)
df_flange = df_flange.dropna(subset=['焊点编号_数值'])
df_flange['焊点编号_数值'] = df_flange['焊点编号_数值'].astype(int)

if not df_flange.empty:
    # 按管线号处理连续段
    for pipe_no, g in df_flange.groupby('管线号'):
        g_sorted = g.sort_values('焊点编号_数值')
        nums = g_sorted['焊点编号_数值'].to_numpy()
        idxs = g_sorted.index.to_numpy()

        # 切分连续段：相邻差值不为1即断开（包含重复号/跳号）
        if len(nums) < 4:
            continue
        breaks = np.where(np.diff(nums) != 1)[0] + 1
        segments = np.split(idxs, breaks)
        seg_nums = np.split(nums, breaks)

        for seg_idxs, seg_n in zip(segments, seg_nums):
            if len(seg_idxs) >= 4:
                # 首末为F，中间为S
                first_idx = seg_idxs[0]
                last_idx = seg_idxs[-1]
                middle_idxs = seg_idxs[1:-1]

                df.loc[first_idx, '焊点类型'] = 'F'
                df.loc[first_idx, '规则'] = 'S-F010-2'
                df.loc[last_idx, '焊点类型'] = 'F'
                df.loc[last_idx, '规则'] = 'S-F010-2'

                if len(middle_idxs) > 0:
                    df.loc[middle_idxs, '焊点类型'] = 'S'
                    df.loc[middle_idxs, '规则'] = 'S-F010-2'

# 计算规则'S-F010-2'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F010-2') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F010-2') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F010-2改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F010-2')


rule_state_before = snapshot_rule_state(df)

# 规则S-F015
# 遍历每根管子，如果两端均焊接弯头，且两端都为预制口，则改其中一道为现场口。
# 筛选出在预制范围内的所有管子
df_filtered = df[(df['公称直径'] > 40)]
pipe_mask1 = dataframe_side_material_matches(df_filtered, 1, 'pipe')
pipe_codes1 = df_filtered.loc[pipe_mask1, '材料唯一码1']
pipe_mask2 = dataframe_side_material_matches(df_filtered, 2, 'pipe')
pipe_codes2 = df_filtered.loc[pipe_mask2, '材料唯一码2']
all_pipe_codes = pd.unique(pd.concat([pipe_codes1, pipe_codes2]).dropna())

def get_elbow_other_side_status(elbow_code, current_weld_idx, df_context):
    """辅助函数：获取弯头另一端焊缝的'焊点类型'"""
    elbow_welds = df_context[((df_context['材料唯一码1'] == elbow_code) | (df_context['材料唯一码2'] == elbow_code))]
    if len(elbow_welds) != 2:
        return None  # 数据异常，不是一个标准的两端弯头
    
    other_weld_idx_arr = elbow_welds.index[elbow_welds.index != current_weld_idx]
    if len(other_weld_idx_arr) == 0:
        return None # 逻辑异常
        
    other_weld_idx = other_weld_idx_arr[0]
    return df_context.loc[other_weld_idx, '焊点类型']

# 遍历所有管子
for pipe_code in all_pipe_codes:
    # 先找到包含该管子的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == pipe_code) | (df['材料唯一码2'] == pipe_code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找管子焊缝
    same_unit_df = df[df['单元号'] == unit_no]
    pipe_welds = same_unit_df[((same_unit_df['材料唯一码1'] == pipe_code) | (same_unit_df['材料唯一码2'] == pipe_code))]

    # 确保管子连接着两个焊缝
    if len(pipe_welds) != 2:
        continue

    weld1_idx, weld2_idx = pipe_welds.index[0], pipe_welds.index[1]
    weld1, weld2 = df.loc[weld1_idx], df.loc[weld2_idx]
    
    # 条件1: 两个焊口都必须是预制口 'S'
    if not (weld1['焊点类型'] == 'S' and weld2['焊点类型'] == 'S'):
        continue

    # 条件2: 管子两端连接的都必须是弯头
    # 检查weld1连接的是否是弯头
    desc1_is_pipe = row_material_matches(weld1, 1, 'pipe')
    other_desc1 = weld1['描述2'] if desc1_is_pipe else weld1['描述1']
    elbow1_code = weld1['材料唯一码2'] if desc1_is_pipe else weld1['材料唯一码1']
    is_elbow1 = material_matches(other_desc1, elbow1_code, 'elbow')
    
    # 检查weld2连接的是否是弯头
    desc2_is_pipe = row_material_matches(weld2, 1, 'pipe')
    other_desc2 = weld2['描述2'] if desc2_is_pipe else weld2['描述1']
    elbow2_code = weld2['材料唯一码2'] if desc2_is_pipe else weld2['材料唯一码1']
    is_elbow2 = material_matches(other_desc2, elbow2_code, 'elbow')

    if not (is_elbow1 and is_elbow2):
        continue

    # 条件满足，开始判断修改哪一道焊缝
    status1 = get_elbow_other_side_status(elbow1_code, weld1_idx, df)
    status2 = get_elbow_other_side_status(elbow2_code, weld2_idx, df)
    
    # 如果任一弯头数据异常，则跳过
    if status1 is None or status2 is None:
        continue
    
    # 决策逻辑
    modify_idx = None
    if status1 == 'S' and status2 == 'F':
        modify_idx = weld1_idx  # 优先打断S-S链
    elif status1 == 'F' and status2 == 'S':
        modify_idx = weld2_idx  # 优先打断S-S链
    elif status1 == 'S' and status2 == 'S':
        # 两边都是S-S链，则根据标高决定
        if pd.isna(weld1['z']) or pd.isna(weld2['z']):
            continue
        if weld1['z'] < weld2['z']:
            modify_idx = weld1_idx
        else:  # z值更高或相等时，修改weld2
            modify_idx = weld2_idx
    # 如果 status1 == 'F' and status2 == 'F'，则不修改，modify_idx 保持为 None
    
    if modify_idx is not None:
        df.loc[modify_idx, '焊点类型'] = 'F'
        df.loc[modify_idx, '规则'] = 'S-F015'

# 计算规则'S-F015'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F015') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F015') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F015改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F015')


rule_state_before = snapshot_rule_state(df)

# 规则S-F012
# 分别计算每个弯头两边直管长度加2倍D的值，将大于11米的改为现场口
# 初始化存储材料唯一码的集合
unique_material_codes = set()

# 遍历描述1列
for index, row in df.iterrows():
    if row['公称直径'] > 40 and row_material_matches(row, 1, 'elbow'):
        unique_material_codes.add(str(row['材料唯一码1']))

# 遍历描述2列
for index, row in df.iterrows():
    if row['公称直径'] > 40 and row_material_matches(row, 2, 'elbow'):
        unique_material_codes.add(str(row['材料唯一码2']))

# 判断 '描述1' 和 '描述2' 是否包含 '管子'
if row_material_matches(row, 1, 'pipe'):
    material_code = row['材料唯一码1']
elif row_material_matches(row, 2, 'pipe'):
    material_code = row['材料唯一码2']

# 遍历唯一的材料唯一码
for material_code in unique_material_codes:
    # 先找到包含该弯头的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == material_code) | (df['材料唯一码2'] == material_code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内搜索材料唯一码对应的行
    same_unit_df = df[df['单元号'] == unit_no]
    search_result = same_unit_df[(same_unit_df['材料唯一码1'] == material_code) | (same_unit_df['材料唯一码2'] == material_code)]

    # 如果搜索结果有两行
    if len(search_result) == 2:
        # 获取两行的直管长度和公称直径数据
        quantity1 = search_result.iloc[0]['数量1'] if row_material_matches(search_result.iloc[0], 1, 'pipe') else search_result.iloc[0]['数量2']
        quantity2 = search_result.iloc[1]['数量1'] if row_material_matches(search_result.iloc[1], 1, 'pipe') else search_result.iloc[1]['数量2']
        diameter1 = search_result.iloc[0]['公称直径']
        diameter2 = search_result.iloc[1]['公称直径']

        # 判断直管长度加2倍公称直径是否大于等于12米
        if (float(quantity1) + 0.002 * diameter1 >= 12) or (float(quantity2) + 0.002 * diameter2 >= 12):
            if float(quantity1) + 0.002 * diameter1 >= 12:
                df.at[search_result.index[0], '焊点类型'] = 'F'
                df.at[search_result.index[0], '规则'] = 'S-F012'
            if float(quantity2) + 0.002 * diameter2 >= 12:
                df.at[search_result.index[1], '焊点类型'] = 'F'
                df.at[search_result.index[1], '规则'] = 'S-F012'

# 计算规则'S-F012'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F012') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F012') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F012改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F012')


rule_state_before = snapshot_rule_state(df)

# 规则S-F017
# 如果弯头的两个焊缝都是预制口，且两边的直管长度加2倍D都大于等于2.5米，那么将短边的焊口改为现场口
# 初始化存储材料唯一码的集合
unique_material_codes = set()

# 遍历描述1列
for index, row in df.iterrows():
    if row['公称直径'] > 40 and row_material_matches(row, 1, 'elbow'):
        unique_material_codes.add(str(row['材料唯一码1']))

# 遍历描述2列
for index, row in df.iterrows():
    if row['公称直径'] > 40 and row_material_matches(row, 2, 'elbow'):
        unique_material_codes.add(str(row['材料唯一码2']))

# 判断 '描述1' 和 '描述2' 是否包含 '管子'
if row_material_matches(row, 1, 'pipe'):
    material_code = row['材料唯一码1']
elif row_material_matches(row, 2, 'pipe'):
    material_code = row['材料唯一码2']

# 遍历唯一的材料唯一码
for material_code in unique_material_codes:
    # 先找到包含该弯头的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == material_code) | (df['材料唯一码2'] == material_code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内搜索材料唯一码对应的行
    same_unit_df = df[df['单元号'] == unit_no]
    search_result = same_unit_df[(same_unit_df['材料唯一码1'] == material_code) | (same_unit_df['材料唯一码2'] == material_code)]

    # 如果搜索结果有两行
    if len(search_result) == 2:
        # 获取两行的直管长度和公称直径数据
        quantity1 = search_result.iloc[0]['数量1'] if row_material_matches(search_result.iloc[0], 1, 'pipe') else search_result.iloc[0]['数量2']
        quantity2 = search_result.iloc[1]['数量1'] if row_material_matches(search_result.iloc[1], 1, 'pipe') else search_result.iloc[1]['数量2']
        diameter1 = search_result.iloc[0]['公称直径']
        diameter2 = search_result.iloc[1]['公称直径']

        # 检查两边是否都是'S'
        if search_result.iloc[0]['焊点类型'] == 'S' and search_result.iloc[1]['焊点类型'] == 'S':
            # 再判断两边的直管长度加2倍公称直径是否都大于等于2.5米
            if (float(quantity1) + 0.002 * diameter1 >= 2.5) and (float(quantity2) + 0.002 * diameter2 >= 2.5):
                # 短边的焊接点改为现场焊接
                if float(quantity1) < float(quantity2):
                    df.at[search_result.index[0], '焊点类型'] = 'F'
                    df.at[search_result.index[0], '规则'] = 'S-F017'
                else:
                    df.at[search_result.index[1], '焊点类型'] = 'F'
                    df.at[search_result.index[1], '规则'] = 'S-F017'

# 计算规则'S-F017'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F017') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F017') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F017改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F017')


rule_state_before = snapshot_rule_state(df)

# 规则S-F019
# 所有管帽焊口都是预制口，不考虑接管长度和管径
filtered_df = df.copy()

# 在filtered_df中，描述1或描述2包含'CAP'或'管帽'的行
mask = (
    dataframe_side_material_matches(filtered_df, 1, 'cap') |
    dataframe_side_material_matches(filtered_df, 2, 'cap')
)

# 修改符合条件的行
filtered_df.loc[mask, '焊点类型'] = 'S'
filtered_df.loc[mask, '规则'] = 'S-F019'

# 将修改反映到原始df
df.update(filtered_df)

# 计算规则'S-F019'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F019') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F019') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F019改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F019')


rule_state_before = snapshot_rule_state(df)

# 规则S-F020
# 法兰与管子的焊口，若管子另一头无焊口，则此口预制；管线末端焊口若与管子焊接，且管子另一端无焊口，且管长不超过12米，则此口预制
# S-F010设备口保持现场口，不参与本规则判断
condition = (
    ((dataframe_side_material_matches(df, 1, 'pipe') & dataframe_side_material_matches(df, 2, 'flange')) |
    (dataframe_side_material_matches(df, 1, 'flange') & dataframe_side_material_matches(df, 2, 'pipe'))) &
    (df['公称直径'] > 40) &
    (df['规则'] != 'S-F010')
)
filtered_rows = df[condition]

# 处理每一行
for index, row in filtered_rows.iterrows():
    if row_material_matches(row, 2, 'pipe'):
        material_code = row['材料唯一码2']
    elif row_material_matches(row, 1, 'pipe'):
        material_code = row['材料唯一码1']
    else:
        continue  # 如果不符合预期的描述组合，跳过此行

    # 在相同单元号内查找 material_code
    unit_no = row['单元号']
    same_unit_df = df[df['单元号'] == unit_no]
    matched_rows = same_unit_df[(same_unit_df['材料唯一码1'] == material_code) | (same_unit_df['材料唯一码2'] == material_code)]
    
    # 如果找到的结果只有一行
    if len(matched_rows) == 1:
        # 更新 '焊点类型' 和 '规则'
        df.at[index, '焊点类型'] = 'S'
        df.at[index, '规则'] = 'S-F020'

# 筛选描述包含"法兰"和"管子"的行，且公称直径在范围内
condition = (
    ((dataframe_side_material_matches(df, 1, 'pipe') & dataframe_side_material_matches(df, 2, 'flange')) |
    (dataframe_side_material_matches(df, 1, 'flange') & dataframe_side_material_matches(df, 2, 'pipe'))) &
    (df['公称直径'] > 40) &
    (df['规则'] != 'S-F010')
)
filtered_rows = df[condition]

# 处理每一行
for index, row in filtered_rows.iterrows():
    if row_material_matches(row, 2, 'pipe'):
        material_code = row['材料唯一码2']
    elif row_material_matches(row, 1, 'pipe'):
        material_code = row['材料唯一码1']
    else:
        continue  # 如果不符合预期的描述组合，跳过此行

    # 在相同单元号内查找 material_code
    unit_no = row['单元号']
    same_unit_df = df[df['单元号'] == unit_no]
    matched_rows = same_unit_df[(same_unit_df['材料唯一码1'] == material_code) | (same_unit_df['材料唯一码2'] == material_code)]
    
    # 如果找到的结果只有一行
    if len(matched_rows) == 1:
        # 更新 '焊点类型' 和 '规则'
        df.at[index, '焊点类型'] = 'S'
        df.at[index, '规则'] = 'S-F020'

# 再次筛选公称直径在范围内且规则不为S-F010的行
diameter_filtered_df = df[(df['公称直径'] > 40) & (df['规则'] != 'S-F010')].copy()

# 遍历每个唯一的'管线号'
for pipeline_number in diameter_filtered_df['管线号'].unique():
    # 获取当前'管线号'下的所有行
    pipeline_rows = diameter_filtered_df[diameter_filtered_df['管线号'] == pipeline_number]
    
    # 创建一个新列，用于存储可转换为数值的焊点编号，无法转换的设为NaN
    pipeline_rows = pipeline_rows.copy()  # 避免SettingWithCopyWarning
    pipeline_rows['焊点编号_数值'] = None
    
    # 尝试将焊点编号转换为数值
    for idx, row in pipeline_rows.iterrows():
        try:
            # 尝试将焊点编号转换为整数
            if pd.notna(row['焊点编号']):
                # 将可能是数值的转换为字符串后提取数字部分
                weld_id_str = str(row['焊点编号'])
                match = re.match(r'^(\d+)', weld_id_str)
                if match:
                    pipeline_rows.at[idx, '焊点编号_数值'] = int(match.group(1))
        except:
            # 转换失败时，保持None值
            pass
    
    # 去除无法转换为数值的行
    pipeline_rows = pipeline_rows.dropna(subset=['焊点编号_数值'])
    
    # 如果没有可用的行，跳过当前管线号
    if pipeline_rows.empty:
        continue
    
    # 按'焊点编号_数值'排序，找出最大的行
    pipeline_rows_sorted = pipeline_rows.sort_values(by='焊点编号_数值', ascending=False)
    
    # 如果没有有效行，跳过
    if len(pipeline_rows_sorted) == 0:
        continue
        
    max_row = pipeline_rows_sorted.iloc[0]  # 焊点编号最大的行
    
    # 找出'焊点编号_数值'第二大的行
    if len(pipeline_rows_sorted) > 1:
        second_max_row = pipeline_rows_sorted.iloc[1]
    else:
        second_max_row = None  # 如果只有一行，则没有第二大的行

    # 检查'描述1'和'描述2'，并尝试读取材料唯一码
    if row_material_matches(max_row, 1, 'pipe'):
        material_code = max_row['材料唯一码1']
        matched_rows = diameter_filtered_df[
            (diameter_filtered_df['材料唯一码1'] == material_code) |
            (diameter_filtered_df['材料唯一码2'] == material_code)
        ]
        if len(matched_rows) == 1 and matched_rows.iloc[0]['数量1'] < 12:
            # 仅当第二大的行存在且'焊点类型'为'F'时，更新最大行
            if second_max_row is not None and second_max_row['焊点类型'] == 'F':
                max_index = max_row.name
                df.at[max_index, '焊点类型'] = 'S'
                df.at[max_index, '规则'] = 'S-F020'
    elif row_material_matches(max_row, 2, 'pipe'):
        material_code = max_row['材料唯一码2']
        matched_rows = diameter_filtered_df[
            (diameter_filtered_df['材料唯一码1'] == material_code) |
            (diameter_filtered_df['材料唯一码2'] == material_code)
        ]
        if len(matched_rows) == 1 and matched_rows.iloc[0]['数量2'] < 12:
            # 仅当第二大的行存在且'焊点类型'为'F'时，更新最大行
            if second_max_row is not None and second_max_row['焊点类型'] == 'F':
                max_index = max_row.name
                df.at[max_index, '焊点类型'] = 'S'
                df.at[max_index, '规则'] = 'S-F020'

# 计算规则'S-F020'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F020') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F020') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F020改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F020')


# ============================================
# 规则S-F029
# 描述：当弯头两侧管段为非整管段（其中一段长度小于2.2米），
# 管件两侧都可设为预制口。判断弯头两侧焊口，仅连接管子的情况，
# 管子非整管（非6米或12米），若一侧管子长度小于2.2米，
# 那么弯头两侧的焊口可设置为预制口S
# ============================================

'''
# 筛选公称直径大于40的行
diameter_mask = (df['公称直径'] > 40)

# 初始化存储弯头材料唯一码的集合
unique_elbow_codes = set()

# 遍历描述1列，收集弯头材料唯一码
for index, row in df.iterrows():
    if diameter_mask.iloc[index] and row_material_matches(row, 1, 'elbow'):
        unique_elbow_codes.add(str(row['材料唯一码1']))

# 遍历描述2列，收集弯头材料唯一码
for index, row in df.iterrows():
    if diameter_mask.iloc[index] and row_material_matches(row, 2, 'elbow'):
        unique_elbow_codes.add(str(row['材料唯一码2']))

# 过滤掉空值
unique_elbow_codes = {code for code in unique_elbow_codes if pd.notna(code)}

# 遍历每个弯头材料唯一码
for elbow_code in unique_elbow_codes:
    # 先找到包含该弯头的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == elbow_code) | (df['材料唯一码2'] == elbow_code)]
    if code_rows.empty:
        continue
    
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找与该弯头相关的所有焊缝（应为2个）
    same_unit_df = df[df['单元号'] == unit_no]
    elbow_welds = same_unit_df[(same_unit_df['材料唯一码1'] == elbow_code) | (same_unit_df['材料唯一码2'] == elbow_code)]
    
    # 弯头应该有两道焊缝
    if len(elbow_welds) != 2:
        continue
    
    weld1_idx, weld2_idx = elbow_welds.index[0], elbow_welds.index[1]
    weld1, weld2 = df.loc[weld1_idx], df.loc[weld2_idx]
    
    # 检查弯头两侧连接的元件是否都是管子
    # 获取weld1另一端的材料信息
    if weld1['材料唯一码1'] == elbow_code:
        other_desc1 = str(weld1['描述2'])
        other_qty1 = weld1['数量2']
        other_code1 = weld1['材料唯一码2']
    else:
        other_desc1 = str(weld1['描述1'])
        other_qty1 = weld1['数量1']
        other_code1 = weld1['材料唯一码1']
    
    # 获取weld2另一端的材料信息
    if weld2['材料唯一码1'] == elbow_code:
        other_desc2 = str(weld2['描述2'])
        other_qty2 = weld2['数量2']
        other_code2 = weld2['材料唯一码2']
    else:
        other_desc2 = str(weld2['描述1'])
        other_qty2 = weld2['数量1']
        other_code2 = weld2['材料唯一码1']
    
    # 判断两侧是否都是管子
    is_pipe1 = material_matches(other_desc1, other_code1, 'pipe')
    is_pipe2 = material_matches(other_desc2, other_code2, 'pipe')
    
    # 如果两侧不都是管子，跳过
    if not (is_pipe1 and is_pipe2):
        continue
    
    # 检查管子是否为非整管（不是6米或12米）
    try:
        qty1 = float(other_qty1) if pd.notna(other_qty1) else 0
        qty2 = float(other_qty2) if pd.notna(other_qty2) else 0
    except (ValueError, TypeError):
        continue
    
    is_whole_pipe1 = (abs(qty1 - 6) < 0.01) or (abs(qty2 - 12) < 0.01)
    is_whole_pipe2 = (abs(qty2 - 6) < 0.01) or (abs(qty2 - 12) < 0.01)
    
    # 如果两侧都是整管，跳过
    if is_whole_pipe1 and is_whole_pipe2:
        continue
    
    # 检查是否有任意一侧管子长度小于2.2米
    pipe_less_than_2_2 = (qty1 < 2.2) or (qty2 < 2.2)
    
    if pipe_less_than_2_2:
        # 将弯头两侧的焊口都设置为预制口S
        df.loc[weld1_idx, '焊点类型'] = 'S'
        df.loc[weld1_idx, '规则'] = 'S-F029'
        df.loc[weld2_idx, '焊点类型'] = 'S'
        df.loc[weld2_idx, '规则'] = 'S-F029'

# 计算规则'S-F029'改为预制焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F029') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F029') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_S - total_english_units_F

print(f"规则S-F029改为预制焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
'''

rule_state_before = snapshot_rule_state(df)

# 规则S-F021
# 遍历DN40以上的所有弯头，如果一个弯头的两道焊缝均是现场口，则修改其中标高低的一道口为预制口
# 1. 筛选 df 中 '公称直径' 大于等于 50 的行
filtered_df = df[(df['公称直径'] > 40)].copy()

# 2. 提取包含 '弯头' 的材料唯一码
# 使用 pandas 的 vectorized 操作来代替 iterrows()，提升性能
mask_弯头1 = dataframe_side_material_matches(filtered_df, 1, 'elbow')
mask_弯头2 = dataframe_side_material_matches(filtered_df, 2, 'elbow')

# 获取所有相关的材料唯一码
materials = set(filtered_df.loc[mask_弯头1, '材料唯一码1']).union(filtered_df.loc[mask_弯头2, '材料唯一码2'])

# 定义需要排除的规则列表
exclude_rules = ['S-F006', 'S-F007', 'S-F008', 'S-F009', 'S-F010', 'S-F010-1', 'S-F004', 'S-F004-1']

# 3. 遍历材料唯一码，查找并修改符合条件的行
for material in materials:
    # 先找到包含该弯头的任一行，获取其单元号
    code_rows = filtered_df[(filtered_df['材料唯一码1'] == material) | (filtered_df['材料唯一码2'] == material)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找对应行
    same_unit_df = filtered_df[filtered_df['单元号'] == unit_no]
    mask_material = (same_unit_df['材料唯一码1'] == material) | (same_unit_df['材料唯一码2'] == material)
    matching_rows = same_unit_df[mask_material]
    
    # 只处理匹配到两行且焊点类型都为 'F' 的情况
    if len(matching_rows) == 2 and all(matching_rows['焊点类型'] == 'F'):
        # 检查两行规则是否属于 exclude_rules
        row_1, row_2 = matching_rows.iloc[0], matching_rows.iloc[1]
        rule_1_in_exclude = row_1['规则'] in exclude_rules
        rule_2_in_exclude = row_2['规则'] in exclude_rules
        
        if not rule_1_in_exclude and not rule_2_in_exclude:
            # 如果两行规则都不在 exclude_rules 中，修改 z 较小的行
            if pd.isna(row_1['z']) or pd.isna(row_2['z']):
                continue
            row_to_update = row_1 if row_1['z'] < row_2['z'] else row_2
        elif rule_1_in_exclude and not rule_2_in_exclude:
            # 如果第一行规则在 exclude_rules 中，修改第二行
            row_to_update = row_2
        elif not rule_1_in_exclude and rule_2_in_exclude:
            # 如果第二行规则在 exclude_rules 中，修改第一行
            row_to_update = row_1
        else:
            # 如果两行规则都在 exclude_rules 中，不进行修改
            continue

        # 修改对应行的 '焊点类型' 和 '规则'
        df.loc[row_to_update.name, '焊点类型'] = 'S'
        df.loc[row_to_update.name, '规则'] = 'S-F021'

# 计算规则'S-F021'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F021') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F021') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F021改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F021')


rule_state_before = snapshot_rule_state(df)

# 规则S-F021-1
# 遍历所有设备口相邻的焊口，如果不是被特定规格修改为现场口，则改为预制口
s_f010_df = df[(df['规则'] == 'S-F010')].copy()
exclude_rules = ['S-F006', 'S-F007', 'S-F008', 'S-F009', 'S-F010', 'S-F010-1', 'S-F004', 'S-F004-1']

for index, row in s_f010_df.iterrows():
    adjacent_component_code = None
    # 确定与设备法兰相邻的元件的唯一码
    if row_material_matches(row, 1, 'flange') or pd.isna(row['描述1']):
        adjacent_component_code = row['材料唯一码2']
    elif row_material_matches(row, 2, 'flange') or pd.isna(row['描述2']):
        adjacent_component_code = row['材料唯一码1']

    if pd.notna(adjacent_component_code):
        # 在相同单元号内查找相邻元件连接的所有焊缝
        unit_no = row['单元号']
        same_unit_df = df[df['单元号'] == unit_no]
        matched_rows = same_unit_df[(same_unit_df['材料唯一码1'] == adjacent_component_code) | (same_unit_df['材料唯一码2'] == adjacent_component_code)]
        
        # 相邻元件应该有两道焊缝
        if len(matched_rows) == 2:
            # 找到另一道焊缝
            other_row_index = matched_rows.index[matched_rows.index != index][0]
            other_row = df.loc[other_row_index]
            
            # 如果另一道焊缝是现场口，且不是因为特定排除规则
            if other_row['焊点类型'] == 'F' and other_row['规则'] not in exclude_rules:
                # 将其改为预制口
                df.loc[other_row_index, '焊点类型'] = 'S'
                df.loc[other_row_index, '规则'] = 'S-F021-1'

# 计算规则'S-F021-1'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F021-1') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F021-1') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_S - total_english_units_F

print(f"规则S-F021-1改为预制焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F021-1')


rule_state_before = snapshot_rule_state(df)

# 规则S-F022
# 所有支管台相关焊缝均参与判断；
# 将其与主管焊缝改为预制口、与支管相连的另一道口改为现场口；
# 如果支管台连接的主管是两端均不预制的管子，则支管台与该主管的焊缝为现场口。
# 小管径预制开关关闭时，本规则开始前先将DN40及以下焊口置为现场口，
# 使小主管上的支管台也能被“主管不预制”条件覆盖。
if not ENABLE_SMALL_DIAMETER_PREFAB:
    small_diameter_condition = df['公称直径'] <= 40
    df.loc[small_diameter_condition, '焊点类型'] = 'F'

# 筛选全部焊缝，不再使用从管线号提取的管径过滤
filtered_df = df.copy()

# 查找包含'管台'、'支管座'、'管箍'或'HALF COUPLING'或'WELDOLET'的行，在filtered_df中
condition = (
    dataframe_side_material_matches(filtered_df, 1, 'branch_outlet') |
    dataframe_side_material_matches(filtered_df, 2, 'branch_outlet')
)
active_condition = condition

# 将筛选条件应用于原始df
# 注意：这里使用filtered_df的索引来定位df中的相应行
df.loc[filtered_df[active_condition].index, '焊点类型'] = 'F'
df.loc[filtered_df[active_condition].index, '规则'] = 'S-F022'

# 筛选 '焊接类型' 为 'boss' 或 'olet'，且符合上述条件的行，在df中进行更新
additional_condition = (filtered_df.loc[active_condition, '焊接类型'] == 'boss') | (filtered_df.loc[active_condition, '焊接类型'] == 'olet')
df.loc[filtered_df[active_condition].loc[additional_condition].index, '焊点类型'] = 'S'

# 在filtered_df中进一步筛选'焊接类型'为'boss'或'olet'
filtered_df = filtered_df[active_condition & ((filtered_df['焊接类型'] == 'boss') | (filtered_df['焊接类型'] == 'olet'))]

# 遍历筛选后的filtered_df
for index, row in filtered_df.iterrows():
    material_code = None

    # 判断 '描述1' 和 '描述2' 是否包含 '管子'
    if row_material_matches(row, 1, 'pipe'):
        material_code = row['材料唯一码1']
    elif row_material_matches(row, 2, 'pipe'):
        material_code = row['材料唯一码2']

    if material_code:
        # 在相同单元号内查找材料唯一码
        unit_no = row['单元号']
        same_unit_df = df[df['单元号'] == unit_no]
        matched_df = same_unit_df[(same_unit_df['材料唯一码1'] == material_code) | (same_unit_df['材料唯一码2'] == material_code)]

        # 找出查找到的所有行中 '公称直径' 的最大值
        max_diameter = matched_df['公称直径'].max()

        # 找出 '公称直径' 等于最大值的所有行
        max_diameter_rows = matched_df[matched_df['公称直径'] == max_diameter]

        # 判断是否刚好有两行的 '公称直径' 等于这个最大值
        if len(max_diameter_rows) == 2:
            # 判断这两行的 '焊点类型' 是否均为 'F'
            if all(max_diameter_rows['焊点类型'] == 'F'):
                # 在原始df中更新'焊点类型'，确保使用原始df的索引
                df.loc[index, '焊点类型'] = 'F'

# 计算规则'S-F022'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F022') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F022') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F022改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F022')


rule_state_before = snapshot_rule_state(df)

# 规则S-F023
# 管径为DN40及以下的，管道也考虑预制；连接设备的法兰口、随设备和仪表元件的法兰、阀门、大小头设置为现场口；螺纹口设置为现场口；NPT和PVC材料焊口设置为现场口；管子与管子连接的焊口设置为现场口
# 初始筛选：基于公称直径和规则条件
filtered_df_initial = df[(df['公称直径'] <= 40) & (df['规则'] != 'S-F022')].copy()

# 初始设置 '焊点类型' 和 '规则'
filtered_df_initial.loc[:, ['焊点类型', '规则']] = ['S', 'S-F023s']

# 更新回原始 df
df.loc[filtered_df_initial.index, ['焊点类型', '规则']] = filtered_df_initial[['焊点类型', '规则']]

# 接下来仅基于 df['公称直径'] <= 40 且规则不为 S-F022 的行进行处理
filtered_df = df[(df['公称直径'] <= 40) & (df['规则'] != 'S-F022')].copy()

# 螺纹口和材料焊口设置为现场口
condition = ((filtered_df['焊接类型'] == 'scw') | (filtered_df['焊接类型'] == 'seton')) & (filtered_df['焊点类型'] == 'S')
filtered_df.loc[condition, ['焊点类型', '规则']] = ['F', 'S-F009']

# 检查 '描述1' 和 '描述2' 是否包含 'NPT' 或 'PVC'
mask = (
    description_series_matches(filtered_df['描述1'], NPT_OR_PVC_DESC_PATTERN) |
    description_series_matches(filtered_df['描述2'], NPT_OR_PVC_DESC_PATTERN)
)
filtered_df.loc[mask, ['焊点类型', '规则']] = ['F', 'S-F009']

# 空白焊接材料处理
condition = pd.isna(filtered_df['焊接材料1']) | pd.isna(filtered_df['焊接材料2'])
filtered_df.loc[condition, ['焊点类型', '规则']] = ['F', 'S-F005']

# 处理包含 '管子' 的行
mask = (
    dataframe_side_material_matches(filtered_df, 1, 'pipe') &
    dataframe_side_material_matches(filtered_df, 2, 'pipe')
)
filtered_df.loc[mask, ['焊点类型', '规则']] = ['F', 'S-F004']

# 阀门焊口设置为现场口
valve_mask = (
    dataframe_side_material_matches(filtered_df, 1, 'valve') |
    dataframe_side_material_matches(filtered_df, 2, 'valve')
)
filtered_df.loc[valve_mask, ['焊点类型', '规则']] = ['F', 'S-F007']

# 设备口保护：S-F023内部规则处理完成后，重新按设备法兰口坐标判定
# 命中的焊口恢复为现场口，并将规则写回S-F010
device_flange_coordinate_set = set(rounded_法兰_coordinates)
device_port_mask = filtered_df['焊点位置'].apply(
    lambda coord: parse_and_round_coordinates(coord) in device_flange_coordinate_set
)
filtered_df.loc[device_port_mask, ['焊点类型', '规则']] = ['F', 'S-F010']

# 遍历处理连续两行及以上 '焊点类型' 为 'S' 的情况，并添加对 '规则' 的判断
current_pipeline = None
consecutive_s_count = 0

for index, row in filtered_df.iterrows():
    if row['焊点类型'] == 'S':
        if row['规则'] == 'S-F022':
            # 如果规则为 'S-F022'，重置计数
            current_pipeline = None
            consecutive_s_count = 0
            continue  # 跳过后续计数逻辑
        if current_pipeline != row['管线号']:  # 如果遇到新管线号，重置计数器
            current_pipeline = row['管线号']
            consecutive_s_count = 1
        else:
            consecutive_s_count += 1
        # 若计数为偶数，更新为 'F'
        if consecutive_s_count % 2 == 0:
            filtered_df.at[index, '焊点类型'] = 'F'
            filtered_df.at[index, '规则'] = 'S-F023f'
    else:
        # 非 'S' 类型时重置计数
        current_pipeline = None
        consecutive_s_count = 0
        
# 将修改后的部分更新回原始 df
df.loc[filtered_df.index, filtered_df.columns] = filtered_df

# 计算规则'S-F023'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F023f') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F023s') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F023改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F023')


rule_state_before = snapshot_rule_state(df)

# 规则S-F024
# 夹套内管的S-F口与其对应的夹套外管的S-F口一致
# 新逻辑:
# 1. 以内管为基准，将其S/F状态同步给外管。
# 2. 焊口对应关系通过三维坐标距离查找，距离阈值为100。
# 3. 如果存在夹套管，则生成一个额外的对应关系报告Excel。

# 用于存储夹套管对应关系的列表
jacket_report_data = []

# 1. 识别所有夹套管对 (内管, 外管) - 更稳健的逻辑
# 确保'管线号'是字符串类型并去除首尾空格
df['管线号'] = df['管线号'].astype(str).str.strip()
all_pipe_nos = df['管线号'].unique()

pipe_map = defaultdict(lambda: {'inner': None, 'outer': None})
for pipe_no in all_pipe_nos:
    # 通过移除(OUT)和周围空格来标准化，获取基础名称
    # 这样可以正确处理像'PIPE-1'和'PIPE-1 (OUT)'这样的名称
    # 之前的正则表达式有问题，因为它假设(OUT)总是在末尾
    # 简单的replace方法在这种情况下更可靠
    base_name = pipe_no.replace('(OUT)', '').strip()
    
    if '(OUT)' in pipe_no:
        pipe_map[base_name]['outer'] = pipe_no
    else:
        pipe_map[base_name]['inner'] = pipe_no

# 筛选出同时具有内管和外管的实际配对
jacket_pairs = []
for base_name, names in pipe_map.items():
    if names['inner'] and names['outer']:
        jacket_pairs.append({'inner': names['inner'], 'outer': names['outer']})


# 2. 遍历每个夹套管对
if jacket_pairs:
    for pair in jacket_pairs:
        inner_pipe_no = pair['inner']
        outer_pipe_no = pair['outer']
        
        inner_welds = df[df['管线号'] == inner_pipe_no]
        outer_welds = df[df['管线号'] == outer_pipe_no]

        if inner_welds.empty or outer_welds.empty:
            continue

        outer_welds = outer_welds.dropna(subset=['x', 'y', 'z'])
        inner_welds = inner_welds.dropna(subset=['x', 'y', 'z'])
        if inner_welds.empty or outer_welds.empty:
            continue

        # 将外管焊口坐标转换为numpy数组以便快速计算
        outer_weld_coords = outer_welds[['x', 'y', 'z']].to_numpy(dtype=np.float64)
        outer_weld_indices = outer_welds.index.to_numpy()

        # 3. 遍历内管的每个焊口
        for inner_idx, inner_weld in inner_welds.iterrows():
            inner_weld_pos = inner_weld[['x', 'y', 'z']].to_numpy(dtype=np.float64)
            
            # 计算与所有外管焊口的距离
            distances = np.linalg.norm(outer_weld_coords - inner_weld_pos, axis=1)
            
            if len(distances) == 0:
                continue

            # 找到最近的外管焊口
            min_dist_idx = np.argmin(distances)
            min_dist = distances[min_dist_idx]
            
            # 4. 检查距离是否在阈值内
            if min_dist <= 130:
                closest_outer_weld_idx = outer_weld_indices[min_dist_idx]
                
                # 5. 同步S/F状态和规则
                inner_weld_type = df.loc[inner_idx, '焊点类型']
                df.loc[closest_outer_weld_idx, '焊点类型'] = inner_weld_type
                df.loc[closest_outer_weld_idx, '规则'] = 'S-F024'
                
                # 6. 记录到报告列表
                jacket_report_data.append({
                    '夹套内管': inner_pipe_no,
                    '对应的夹套外管': outer_pipe_no,
                    '内管焊口号': df.loc[inner_idx, '焊点编号'],
                    '对应的外管焊口号': df.loc[closest_outer_weld_idx, '焊点编号'],
                    '焊点类型': inner_weld_type
                })

# 7. 如果有夹套管数据，生成报告
if jacket_report_data:
    report_df = pd.DataFrame(jacket_report_data)
    report_df.rename(columns={
        '夹套内管': '夹套内管',
        '对应的夹套外管': '对应的夹套外管',
        '内管焊口号': '内管焊口号',
        '对应的外管焊口号': '对应的外管焊口号',
        '焊点类型': '焊点类型'
    }, inplace=True)
    # report_file_path = f'{file_name}-夹套内外管对照表.xlsx'
    # report_df.to_excel(report_file_path, index=False)

# 计算规则'S-F024'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F024') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F024') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F024改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F024')


# ============================================
rule_state_before = snapshot_rule_state(df)
# 规则S-F025（修改版）
# 原描述：带角度安装管子的两侧弯头，竖直方向上的两道口分别设置成F口，水平方向上的两道口分别设置成S口
# 修改后：在判断出带角度安装管子的两侧弯头的基础上，若2个连续的弯头都可以作为偏转弯头，
#         优先选择弯头两端都接直管的作为偏转弯头；将该偏转弯头两道焊口全部改为现场口，
#         另一个弯头两道焊口不做修改
# ============================================

# 筛选出所有管子的材料唯一码
df_pipes = df[df['公称直径'] > 40].copy()
pipe_mask1 = dataframe_side_material_matches(df_pipes, 1, 'pipe')
pipe_mask2 = dataframe_side_material_matches(df_pipes, 2, 'pipe')
pipe_codes1 = df_pipes.loc[pipe_mask1, '材料唯一码1']
pipe_codes2 = df_pipes.loc[pipe_mask2, '材料唯一码2']
all_pipe_codes_025 = pd.unique(pd.concat([pipe_codes1, pipe_codes2]).dropna())

# 遍历每一个管子
for pipe_code in all_pipe_codes_025:
    # 在该单元内查找管子唯一码
    code_rows = df[(df['材料唯一码1'] == pipe_code) | (df['材料唯一码2'] == pipe_code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']
    
    # 在相同单元号内查找管子焊缝
    same_unit_df = df[df['单元号'] == unit_no]
    pipe_welds = same_unit_df[(same_unit_df['材料唯一码1'] == pipe_code) | (same_unit_df['材料唯一码2'] == pipe_code)]
    
    # 如果找到两行，则定位到了管子的两端坐标
    if len(pipe_welds) != 2:
        continue
    
    weld1_idx, weld2_idx = pipe_welds.index[0], pipe_welds.index[1]
    weld1, weld2 = df.loc[weld1_idx], df.loc[weld2_idx]
    
    # 获取两端坐标
    z1, z2 = weld1['z'], weld2['z']
    x1, x2 = weld1['x'], weld2['x']
    y1, y2 = weld1['y'], weld2['y']
    if any(pd.isna(value) for value in (x1, y1, z1, x2, y2, z2)):
        continue
    
    # 判断是否是带角度安装的管子：Z值相同，x和y均不相同，且变化的差值不同
    # Z值相同（允许50mm误差）
    if abs(z1 - z2) > 50:
        continue
    
    # x和y均不相同（差值大于50mm）
    dx = abs(x1 - x2)
    dy = abs(y1 - y2)
    if dx <= 50 or dy <= 50:
        continue
    
    # 变化的差值不同：既不是正xy方向上的，也不是xy之间呈45度角的
    # 如果dx和dy相差在50mm以内，认为是45度角
    if abs(dx - dy) <= 50:
        continue
    
    # 至此，认定这是一段带角度安装的管子
    # 判断这两行的另一端材料是否均是弯头
    # 获取weld1另一端的材料
    if weld1['材料唯一码1'] == pipe_code:
        other_desc1 = weld1['描述2']
        other_code1 = weld1['材料唯一码2']
        other_unit1 = weld1['单位2']
    else:
        other_desc1 = weld1['描述1']
        other_code1 = weld1['材料唯一码1']
        other_unit1 = weld1['单位1']
    
    # 获取weld2另一端的材料
    if weld2['材料唯一码1'] == pipe_code:
        other_desc2 = weld2['描述2']
        other_code2 = weld2['材料唯一码2']
        other_unit2 = weld2['单位2']
    else:
        other_desc2 = weld2['描述1']
        other_code2 = weld2['材料唯一码1']
        other_unit2 = weld2['单位1']
    
    # 判断是否都是弯头
    is_elbow1 = material_matches(other_desc1, other_code1, 'elbow')
    is_elbow2 = material_matches(other_desc2, other_code2, 'elbow')
    
    if not (is_elbow1 and is_elbow2):
        continue
    
    # 符合修改条件，查找两个弯头的材料唯一码
    elbow1_code = other_code1
    elbow2_code = other_code2
    
    # 在该单元内查找弯头1的焊缝
    elbow1_welds = same_unit_df[(same_unit_df['材料唯一码1'] == elbow1_code) | (same_unit_df['材料唯一码2'] == elbow1_code)]
    # 在该单元内查找弯头2的焊缝
    elbow2_welds = same_unit_df[(same_unit_df['材料唯一码1'] == elbow2_code) | (same_unit_df['材料唯一码2'] == elbow2_code)]
    
    # 弯头应该有两道焊缝
    if len(elbow1_welds) != 2 or len(elbow2_welds) != 2:
        continue
    
    # ========== 新增逻辑：判断哪个弯头更适合作为偏转弯头 ==========
    
    # 判断弯头1是否两端都接直管
    def check_elbow_connected_to_pipes(elbow_code, elbow_welds_df, current_weld_idx, unit_df):
        """判断弯头是否两端都接直管"""
        # 获取弯头的两道焊缝
        other_weld_idx = elbow_welds_df.index[elbow_welds_df.index != current_weld_idx][0]
        other_weld = unit_df.loc[other_weld_idx]
        
        # 获取弯头另一端的材料信息
        if other_weld['材料唯一码1'] == elbow_code:
            other_end_desc = other_weld['描述2']
            other_end_code = other_weld['材料唯一码2']
        else:
            other_end_desc = other_weld['描述1']
            other_end_code = other_weld['材料唯一码1']
        
        # 判断是否为直管
        is_pipe = material_matches(other_end_desc, other_end_code, 'pipe')
        return is_pipe
    
    # 检查弯头1是否两端都接直管
    elbow1_both_pipes = check_elbow_connected_to_pipes(elbow1_code, elbow1_welds, weld1_idx, same_unit_df)
    
    # 检查弯头2是否两端都接直管
    elbow2_both_pipes = check_elbow_connected_to_pipes(elbow2_code, elbow2_welds, weld2_idx, same_unit_df)
    
    # 确定要修改为F口的弯头
    elbow_to_modify = None
    
    if elbow1_both_pipes and elbow2_both_pipes:
        # 如果两个弯头都两端接直管，优先选择标高较低的一个（保证确定性）
        # 获取弯头1的标高（使用其任一端焊缝的z值）
        elbow1_z = df.loc[elbow1_welds.index[0], 'z']
        elbow2_z = df.loc[elbow2_welds.index[0], 'z']
        if pd.isna(elbow1_z) or pd.isna(elbow2_z):
            continue
        elbow_to_modify = elbow1_code if elbow1_z <= elbow2_z else elbow2_code
    elif elbow1_both_pipes and not elbow2_both_pipes:
        # 只有弯头1两端接直管
        elbow_to_modify = elbow1_code
    elif not elbow1_both_pipes and elbow2_both_pipes:
        # 只有弯头2两端接直管
        elbow_to_modify = elbow2_code
    else:
        # 两个弯头都不满足两端接直管，不做修改
        continue
    
    # ========== 根据选择的弯头进行修改 ==========
    
    if elbow_to_modify == elbow1_code:
        # 修改弯头1的两道焊口为F口
        for elbow_idx in elbow1_welds.index:
            df.loc[elbow_idx, '焊点类型'] = 'F'
            df.loc[elbow_idx, '规则'] = 'S-F025'
        # 弯头2不做修改（保持原有状态）
        
    else:  # elbow_to_modify == elbow2_code
        # 修改弯头2的两道焊口为F口
        for elbow_idx in elbow2_welds.index:
            df.loc[elbow_idx, '焊点类型'] = 'F'
            df.loc[elbow_idx, '规则'] = 'S-F025'
        # 弯头1不做修改（保持原有状态）

# 计算规则'S-F025'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F025') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F025') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F025改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F025')


# ============================================
rule_state_before = snapshot_rule_state(df)

# 规则S-F033（修正版）
# 描述：管线号与另一管线号连接处的焊口，焊接类型是BW的，
# 焊口两侧材料是管子的情况，与连接处焊口相邻的焊口设置成现场口
# ============================================

# 筛选公称直径大于40的行
diameter_mask = (df['公称直径'] > 40)

# 筛选焊接类型为BW（对焊）的焊口
bw_mask = df['焊接类型'].str.upper() == 'BW'

# 筛选两侧材料都是管子的焊口
pipe_mask = dataframe_side_material_matches(df, 1, 'pipe') & \
            dataframe_side_material_matches(df, 2, 'pipe')

# ===== 新增：判断管线号连接处的逻辑 =====
# 需要判断焊口两侧的材料唯一码对应的管线号不同
# 由于数据中没有直接存储"每个材料唯一码所属的管线号"，
# 我们需要通过查找每个材料唯一码关联的其他焊口来推断

# 先筛选出初步符合条件的焊口
pre_mask = diameter_mask & bw_mask & pipe_mask
candidate_indices = df[pre_mask].index

# 用于存储真正的连接处焊口
connection_weld_indices = []

for idx in candidate_indices:
    row = df.loc[idx]
    
    # 获取焊口两侧的材料唯一码
    code1 = row['材料唯一码1']
    code2 = row['材料唯一码2']
    
    # 获取单元号
    unit_no = row['单元号']
    
    # 查找与code1关联的其他焊口，获取其管线号
    code1_other_welds = df[(df['单元号'] == unit_no) & 
                           ((df['材料唯一码1'] == code1) | (df['材料唯一码2'] == code1)) &
                           (df.index != idx)]
    
    # 查找与code2关联的其他焊口，获取其管线号
    code2_other_welds = df[(df['单元号'] == unit_no) & 
                           ((df['材料唯一码1'] == code2) | (df['材料唯一码2'] == code2)) &
                           (df.index != idx)]
    
    # 获取管线号（如果有其他焊口）
    pipe_no1 = None
    pipe_no2 = None
    
    if not code1_other_welds.empty:
        pipe_no1 = code1_other_welds.iloc[0]['管线号']
    
    if not code2_other_welds.empty:
        pipe_no2 = code2_other_welds.iloc[0]['管线号']
    
    # 判断是否为不同管线号的连接处
    # 条件：两个材料唯一码都有其他焊口，且所属管线号不同
    if (pipe_no1 is not None and pipe_no2 is not None) and (pipe_no1 != pipe_no2):
        connection_weld_indices.append(idx)

# 用于记录已处理的焊口，避免重复
processed_indices = set()

# 遍历每一个连接处焊口
for conn_idx in connection_weld_indices:
    if conn_idx in processed_indices:
        continue
        
    conn_row = df.loc[conn_idx]
    
    # 获取该焊口两侧的管子材料唯一码
    pipe_code1 = conn_row['材料唯一码1']
    pipe_code2 = conn_row['材料唯一码2']
    
    # 获取单元号
    unit_no = conn_row['单元号']
    same_unit_df = df[df['单元号'] == unit_no]
    
    # ===== 处理管子1的相邻焊口 =====
    # 查找管子1的所有焊缝
    pipe1_welds = same_unit_df[(same_unit_df['材料唯一码1'] == pipe_code1) | 
                                (same_unit_df['材料唯一码2'] == pipe_code1)]
    
    # 管子应该有两道焊缝（连接处焊口和另一端焊口）
    if len(pipe1_welds) == 2:
        # 找到另一端焊口（不是当前连接处焊口）
        other_pipe1_idx = pipe1_welds.index[pipe1_welds.index != conn_idx]
        if len(other_pipe1_idx) == 1:
            other_idx = other_pipe1_idx[0]
            # 将该焊口改为现场口F
            if other_idx not in processed_indices:
                df.loc[other_idx, '焊点类型'] = 'F'
                df.loc[other_idx, '规则'] = 'S-F033'
                processed_indices.add(other_idx)
    
    # ===== 处理管子2的相邻焊口 =====
    # 查找管子2的所有焊缝
    pipe2_welds = same_unit_df[(same_unit_df['材料唯一码1'] == pipe_code2) | 
                                (same_unit_df['材料唯一码2'] == pipe_code2)]
    
    # 管子应该有两道焊缝（连接处焊口和另一端焊口）
    if len(pipe2_welds) == 2:
        # 找到另一端焊口（不是当前连接处焊口）
        other_pipe2_idx = pipe2_welds.index[pipe2_welds.index != conn_idx]
        if len(other_pipe2_idx) == 1:
            other_idx = other_pipe2_idx[0]
            # 将该焊口改为现场口F
            if other_idx not in processed_indices:
                df.loc[other_idx, '焊点类型'] = 'F'
                df.loc[other_idx, '规则'] = 'S-F033'
                processed_indices.add(other_idx)
    
    # 标记当前连接处焊口为已处理
    processed_indices.add(conn_idx)

# 计算规则'S-F033'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F033') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F033') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F033改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F033')


rule_state_before = snapshot_rule_state(df)

# 规则S-F030
# 主管为竖直方向的带角度安装的三通，两道主管口均设置为现场口。
# 判定逻辑：
# 1) 用距离法识别三通的支管口与两道主管口；
# 2) 若两道主管口坐标的x,y均不变且z变化，则为竖直主管三通；
# 3) 若支管口到主管竖直连线的垂足落在线段内，且该垂线方向不是正x或正y（即dx、dy均有显著变化，dz近似为0），则认为带角度安装；
# 满足2+3，则两道主管口均设为F，规则写入S-F030。

# 仅处理DN40以上的三通候选焊口
df_tees_026 = df[(df['公称直径'] > 40)].copy()
mask1_026 = dataframe_side_material_matches(df_tees_026, 1, 'tee')
mask2_026 = dataframe_side_material_matches(df_tees_026, 2, 'tee')
codes1_026 = df_tees_026.loc[mask1_026, '材料唯一码1']
codes2_026 = df_tees_026.loc[mask2_026, '材料唯一码2']
unique_tee_codes_026 = pd.unique(pd.concat([codes1_026, codes2_026]).dropna())

TOL = 50.0  # 坐标容差（mm），与脚本其他走向/角度判断保持一致

for tee_code in unique_tee_codes_026:
    # 先找到包含该三通的任一行，获取其单元号
    code_rows = df[(df['材料唯一码1'] == tee_code) | (df['材料唯一码2'] == tee_code)]
    if code_rows.empty:
        continue
    unit_no = code_rows.iloc[0]['单元号']

    # 在相同单元号内查找与该三通相关的所有焊缝（应为3个）
    same_unit_df = df[df['单元号'] == unit_no]
    indices = same_unit_df.index[(same_unit_df['材料唯一码1'] == tee_code) | (same_unit_df['材料唯一码2'] == tee_code)].tolist()
    if len(indices) != 3:
        continue

    idx1, idx2, idx3 = indices[0], indices[1], indices[2]

    # 读取三点坐标（使用已拆分的x,y,z列，避免重复解析字符串）
    try:
        p1 = df.loc[idx1, ['x', 'y', 'z']].to_numpy(dtype=np.float64)
        p2 = df.loc[idx2, ['x', 'y', 'z']].to_numpy(dtype=np.float64)
        p3 = df.loc[idx3, ['x', 'y', 'z']].to_numpy(dtype=np.float64)
        if not (
            p1.shape == (3,) and p2.shape == (3,) and p3.shape == (3,) and
            np.isfinite(p1).all() and np.isfinite(p2).all() and np.isfinite(p3).all()
        ):
            continue
    except Exception:
        continue

    # 防止重复点导致的异常判断
    if np.allclose(p1, p2) or np.allclose(p1, p3) or np.allclose(p2, p3):
        continue

    # 距离法：主管两点距离最大，剩余点为支管
    dist12_sq = np.sum((p1 - p2) ** 2)
    dist13_sq = np.sum((p1 - p3) ** 2)
    dist23_sq = np.sum((p2 - p3) ** 2)

    distances = {dist12_sq: idx3, dist13_sq: idx2, dist23_sq: idx1}
    max_dist_sq = max(distances.keys())
    # 若出现多个最大距离（极少见且难判），跳过
    if list(distances.keys()).count(max_dist_sq) > 1:
        continue

    branch_index = distances[max_dist_sq]
    main_indices = [i for i in indices if i != branch_index]
    if len(main_indices) != 2:
        continue

    p_main1 = df.loc[main_indices[0], ['x', 'y', 'z']].to_numpy(dtype=np.float64)
    p_main2 = df.loc[main_indices[1], ['x', 'y', 'z']].to_numpy(dtype=np.float64)
    p_branch = df.loc[branch_index, ['x', 'y', 'z']].to_numpy(dtype=np.float64)

    # 条件1：竖直主管（x,y基本不变，z有变化）
    if (abs(p_main1[0] - p_main2[0]) > TOL) or (abs(p_main1[1] - p_main2[1]) > TOL):
        continue
    if abs(p_main1[2] - p_main2[2]) <= TOL:
        continue

    # 条件2：支管在主管竖直线段的垂足范围内，且垂线方向不是正x或正y（即dx、dy均显著，dz≈0）
    zmin = min(p_main1[2], p_main2[2])
    zmax = max(p_main1[2], p_main2[2])

    # 垂足在竖直线段内：支管的z落在线段范围内（允许容差）
    if not (zmin - TOL <= p_branch[2] <= zmax + TOL):
        continue

    # 垂足点（竖直线：x,y固定，z取支管的z）
    x0 = (p_main1[0] + p_main2[0]) / 2.0
    y0 = (p_main1[1] + p_main2[1]) / 2.0
    foot = np.array([x0, y0, p_branch[2]], dtype=np.float64)
    v = p_branch - foot
    dx, dy, dz = abs(v[0]), abs(v[1]), abs(v[2])

    # 垂线应近似水平（dz≈0），且方向不是纯x/纯y（即dx、dy都显著）
    if dz > TOL:
        continue
    if not (dx > TOL and dy > TOL):
        continue

    # 满足条件：两道主管口均设为现场口
    df.loc[main_indices, '焊点类型'] = 'F'
    df.loc[main_indices, '规则'] = 'S-F030'

# 计算规则'S-F030'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F030') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F030') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F030改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F030')


# ============================================
rule_state_before = snapshot_rule_state(df)

# 规则S-F031（修改版）
# 描述：一条管线与同一台设备有多个设备口时，每一段管子至少有一端是现场口（打断S-S）
# 新增限制：同一管线接在同一设备的两个设备口上，与两个设备口连线平行且介于两个设备口之间的这段直管
#          （即与2个设备口连线可以形成矩形面），至少留一道现场口
# 判断方法：
# 1) 在'设备连接信息'sheet中，若同一(管线号, 设备位号)出现2行及以上，则认为该管线在该设备上有多个设备口
# 2) 获取这些设备口的坐标，计算两个设备口之间的连线向量
# 3) 对该管线内每一根管子（描述含管子|钢管|PIPE）的材料唯一码：
#    - 判断管子是否与设备口连线平行且介于两个设备口之间（可形成矩形面）
#    - 若符合条件且管子两端焊点类型均为S，则打断其中一道为F
#    - 其他管子按原规则处理
# ============================================

def extract_weld_id_num_for_sf031(weld_id):
    """稳健提取焊点编号中的数字，用于确定性选择打断口。"""
    try:
        if pd.notna(weld_id):
            match = re.search(r'[0-9]+', str(weld_id))
            if match:
                return int(match.group(0))
    except (ValueError, TypeError):
        pass
    return np.nan

def is_pipe_parallel_and_between(pipe_coords, device_coords1, device_coords2, tolerance=50):
    """
    判断管子是否与两个设备口连线平行且介于它们之间
    
    参数:
    pipe_coords: 管子两端坐标列表 [(x1,y1,z1), (x2,y2,z2)]
    device_coords1: 设备口1坐标 (x,y,z)
    device_coords2: 设备口2坐标 (x,y,z)
    tolerance: 容差(mm)
    
    返回:
    (是否平行, 是否介于之间)
    """
    # 解析管子两端坐标
    p1 = np.array(pipe_coords[0])
    p2 = np.array(pipe_coords[1])
    
    # 设备口坐标
    d1 = np.array(device_coords1)
    d2 = np.array(device_coords2)
    
    # 计算向量
    pipe_vector = p2 - p1
    device_vector = d2 - d1
    
    # 检查是否平行（向量夹角接近0度或180度）
    # 计算两个向量的夹角余弦值
    if np.linalg.norm(pipe_vector) == 0 or np.linalg.norm(device_vector) == 0:
        return False, False
    
    cos_angle = np.abs(np.dot(pipe_vector, device_vector) / 
                       (np.linalg.norm(pipe_vector) * np.linalg.norm(device_vector)))
    
    # 余弦值接近1表示平行（允许一定误差）
    is_parallel = cos_angle > 0.95  # 夹角小于18度认为平行
    
    if not is_parallel:
        return False, False
    
    # 检查管子是否介于两个设备口之间
    # 方法：计算管子两端到设备口连线的投影距离
    
    # 设备口连线的单位向量
    device_unit = device_vector / np.linalg.norm(device_vector)
    
    # 计算管子两端到设备口1的向量
    v1 = p1 - d1
    v2 = p2 - d1
    
    # 计算投影长度
    proj1 = np.dot(v1, device_unit)
    proj2 = np.dot(v2, device_unit)
    
    # 设备口连线的总长度
    device_length = np.linalg.norm(device_vector)
    
    # 判断管子是否介于两个设备口之间
    # 条件：管子两端的投影都在0到device_length之间（允许一定容差）
    min_proj = min(proj1, proj2)
    max_proj = max(proj1, proj2)
    
    is_between = (min_proj >= -tolerance) and (max_proj <= device_length + tolerance)
    
    # 判断管子是否与设备口连线形成矩形面
    # 条件：管子平行于设备口连线，且管子两端到设备口连线的垂直距离相等（形成矩形）
    # 计算垂直距离
    perp_dist1 = np.linalg.norm(v1 - proj1 * device_unit)
    perp_dist2 = np.linalg.norm(v2 - proj2 * device_unit)
    
    # 垂直距离相近且管子介于之间，则认为可形成矩形面
    is_rectangular = is_between and (abs(perp_dist1 - perp_dist2) <= tolerance)
    
    return is_parallel, is_rectangular

if df_device_conn is not None and isinstance(df_device_conn, pd.DataFrame) and \
   ('管线号' in df_device_conn.columns) and ('设备位号' in df_device_conn.columns):
    
    # 获取设备连接信息，同时获取坐标信息（如果存在）
    conn_df = df_device_conn[['管线号', '设备位号']].dropna(subset=['管线号', '设备位号']).copy()
    
    # 如果有坐标列，一并获取
    has_coords = False
    if '设备法兰口坐标' in df_device_conn.columns:
        conn_df['设备法兰口坐标'] = df_device_conn.loc[conn_df.index, '设备法兰口坐标']
        has_coords = True
    
    conn_df['管线号'] = conn_df['管线号'].astype(str).str.strip()
    conn_df['设备位号'] = conn_df['设备位号'].astype(str).str.strip()

    # 找出有多个设备口的(管线号, 设备位号)组合
    multi_pairs = conn_df.groupby(['管线号', '设备位号']).size()
    target_pairs = multi_pairs[multi_pairs >= 2]
    
    # 存储需要特殊处理的管线及其对应的设备口坐标
    special_pipes = {}  # {pipe_no: [(设备位号, 坐标1, 坐标2), ...]}

    if has_coords:
        for (pipe_no, device_no), cnt in target_pairs.items():
            # 获取该组合的所有行
            device_rows = conn_df[(conn_df['管线号'] == pipe_no) & (conn_df['设备位号'] == device_no)]
            
            # 提取坐标
            coords = []
            for _, row in device_rows.iterrows():
                coord_str = row.get('设备法兰口坐标')
                if pd.notna(coord_str):
                    try:
                        # 解析坐标 (x,y,z)
                        coord = [float(c.strip()) for c in coord_str.strip('()').split(',')]
                        if len(coord) == 3 and np.isfinite(coord).all():
                            coords.append(tuple(coord))
                    except:
                        continue
            
            # 如果有两个坐标，则记录下来
            if len(coords) >= 2:
                if pipe_no not in special_pipes:
                    special_pipes[pipe_no] = []
                special_pipes[pipe_no].append((device_no, coords[0], coords[1]))

    target_pipe_nos = set(target_pairs.index.get_level_values(0).unique())

    if target_pipe_nos:
        df_pipe_no_norm = df['管线号'].astype(str).str.strip()

        for pipe_no in target_pipe_nos:
            pipe_mask = (df_pipe_no_norm == pipe_no)
            if not pipe_mask.any():
                continue

            pipeline_df = df.loc[pipe_mask].copy()

            # 提取该管线内所有管子的材料唯一码
            pipe_desc_mask1 = dataframe_side_material_matches(pipeline_df, 1, 'pipe')
            pipe_desc_mask2 = dataframe_side_material_matches(pipeline_df, 2, 'pipe')
            pipe_codes = set(pipeline_df.loc[pipe_desc_mask1, '材料唯一码1'].dropna()).union(
                set(pipeline_df.loc[pipe_desc_mask2, '材料唯一码2'].dropna())
            )

            if not pipe_codes:
                continue

            # 获取该管线对应的特殊设备口信息
            pipe_special_info = special_pipes.get(pipe_no, [])

            for pipe_code in pipe_codes:
                matched = pipeline_df[(pipeline_df['材料唯一码1'] == pipe_code) | (pipeline_df['材料唯一码2'] == pipe_code)]
                if matched.empty:
                    continue

                # 获取管子两端坐标
                if len(matched) == 2 and 'x' in matched.columns and 'y' in matched.columns and 'z' in matched.columns:
                    idx1, idx2 = matched.index[0], matched.index[1]
                    pipe_coords_array = matched.loc[[idx1, idx2], ['x', 'y', 'z']].to_numpy(dtype=np.float64)
                    if np.isfinite(pipe_coords_array).all():
                        pipe_coords = [tuple(pipe_coords_array[0]), tuple(pipe_coords_array[1])]
                    else:
                        pipe_coords = None
                else:
                    pipe_coords = None

                def _choose_idx_to_break(df_two_rows):
                    """从两行中确定性选择一行打断为F。"""
                    nums = df_two_rows['焊点编号'].apply(extract_weld_id_num_for_sf031)
                    if nums.notna().any():
                        return nums.idxmin()
                    # 次优先：按标高更低的一端打断
                    if 'z' in df_two_rows.columns and df_two_rows['z'].notna().any():
                        return df_two_rows['z'].idxmin()
                    return df_two_rows.index[0]

                # ===== 新增：针对特殊设备口的处理 =====
                special_handled = False
                
                if pipe_special_info and pipe_coords:
                    for device_no, d1, d2 in pipe_special_info:
                        is_parallel, is_rectangular = is_pipe_parallel_and_between(pipe_coords, d1, d2)
                        
                        if is_rectangular:  # 可以形成矩形面
                            special_handled = True
                            # 对于符合条件的管子，如果两端都是S，则打断一道为F
                            if len(matched) == 2 and matched['焊点类型'].eq('S').all():
                                idx_to_F = _choose_idx_to_break(matched)
                                df.loc[idx_to_F, '焊点类型'] = 'F'
                                df.loc[idx_to_F, '规则'] = 'S-F031'
                            break  # 找到一个符合条件的设备对即可
                
                # ===== 原规则处理（仅当没有被特殊处理时） =====
                if not special_handled:
                    if len(matched) == 2:
                        if matched['焊点类型'].eq('S').all():
                            idx_to_F = _choose_idx_to_break(matched)
                            df.loc[idx_to_F, '焊点类型'] = 'F'
                            df.loc[idx_to_F, '规则'] = 'S-F031'
                    elif len(matched) > 2:
                        max_dn = matched['公称直径'].max()
                        max_rows = matched[matched['公称直径'] == max_dn]
                        # 仅当最大公称直径对应的行恰好有2行时才处理
                        if len(max_rows) == 2 and max_rows['焊点类型'].eq('S').all():
                            idx_to_F = _choose_idx_to_break(max_rows)
                            df.loc[idx_to_F, '焊点类型'] = 'F'
                            df.loc[idx_to_F, '规则'] = 'S-F031'
else:
    print("未读取到有效的'设备连接信息'sheet或缺少列（管线号/设备位号），跳过规则S-F031。")

# 计算规则'S-F031'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F031') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F031') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F031改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
record_rule_triggers(df, rule_state_before, 'S-F031')



# ============================================
# 规则S-F034 - 已注释，不运行
# 描述：根据提供的12个平面信息，判断竖直管子的焊口类型
# 情况一：管子仅穿过一个平面，根据X,Y坐标在平面内，Z坐标在该平面的上、下方，
#        优先设置楼层上方且该焊口距离楼层小于1.5米的为现场口
# 情况二：管子穿过两个及以上平面，上、下两个焊口都设置成现场口
# ============================================

'''
# 读取楼层平面信息
try:
    floor_file_path = '各单元楼层标高及范围.xlsx'
    floor_df = pd.read_excel(floor_file_path, sheet_name='Sheet1')
    
    # 只保留需要的列：楼层标高(Z坐标)、最小X坐标、最大X坐标、最小Y坐标、最大Y坐标
    floor_df = floor_df.iloc[:, 2:7]
    floor_df.columns = ['Z坐标', '最小X', '最大X', '最小Y', '最大Y']
    
    # 将坐标值转换为数值类型
    floor_df['Z坐标'] = pd.to_numeric(floor_df['Z坐标'], errors='coerce')
    floor_df['最小X'] = pd.to_numeric(floor_df['最小X'], errors='coerce')
    floor_df['最大X'] = pd.to_numeric(floor_df['最大X'], errors='coerce')
    floor_df['最小Y'] = pd.to_numeric(floor_df['最小Y'], errors='coerce')
    floor_df['最大Y'] = pd.to_numeric(floor_df['最大Y'], errors='coerce')
    
    # 按Z坐标排序
    floor_df = floor_df.sort_values('Z坐标').reset_index(drop=True)
    
    print(f"成功读取楼层平面信息，共{len(floor_df)}个平面")
    
except Exception as e:
    print(f"读取楼层平面文件失败: {e}")
    floor_df = None

# 规则S-F034
if floor_df is not None and not floor_df.empty:
    
    # 筛选公称直径大于40的行
    diameter_mask = (df['公称直径'] > 40)
    
    # 识别所有管子
    pipe_mask1 = dataframe_side_material_matches(df, 1, 'pipe')
    pipe_mask2 = dataframe_side_material_matches(df, 2, 'pipe')
    
    # 获取所有管子的材料唯一码
    pipe_codes1 = set(df.loc[pipe_mask1 & diameter_mask, '材料唯一码1'].dropna())
    pipe_codes2 = set(df.loc[pipe_mask2 & diameter_mask, '材料唯一码2'].dropna())
    all_pipe_codes = pipe_codes1.union(pipe_codes2)
    
    # 用于记录已处理的焊口
    processed_indices_sf034 = set()
    
    # 遍历每一根管子
    for pipe_code in all_pipe_codes:
        # 找到包含该管子的所有焊口
        code_rows = df[(df['材料唯一码1'] == pipe_code) | (df['材料唯一码2'] == pipe_code)]
        if code_rows.empty:
            continue
        
        # 获取单元号
        unit_no = code_rows.iloc[0]['单元号']
        
        # 在相同单元号内查找管子的所有焊缝
        same_unit_df = df[df['单元号'] == unit_no]
        pipe_welds = same_unit_df[(same_unit_df['材料唯一码1'] == pipe_code) | 
                                   (same_unit_df['材料唯一码2'] == pipe_code)]
        
        # 管子应该有两道焊缝
        if len(pipe_welds) != 2:
            continue
        
        weld1_idx, weld2_idx = pipe_welds.index[0], pipe_welds.index[1]
        
        # 检查是否已处理过
        if weld1_idx in processed_indices_sf034 or weld2_idx in processed_indices_sf034:
            continue
        
        # 获取焊点坐标
        x1, y1, z1 = df.loc[weld1_idx, 'x'], df.loc[weld1_idx, 'y'], df.loc[weld1_idx, 'z']
        x2, y2, z2 = df.loc[weld2_idx, 'x'], df.loc[weld2_idx, 'y'], df.loc[weld2_idx, 'z']
        
        # 判断是否为竖直管子
        if abs(x1 - x2) > 50 or abs(y1 - y2) > 50 or abs(z1 - z2) <= 50:
            continue
        
        # 确定上下端
        if z1 < z2:
            lower_idx, upper_idx = weld1_idx, weld2_idx
            lower_z, upper_z = z1, z2
        else:
            lower_idx, upper_idx = weld2_idx, weld1_idx
            lower_z, upper_z = z2, z1
        
        # 获取管子中心点的x,y坐标
        center_x = (x1 + x2) / 2
        center_y = (y1 + y2) / 2
        
        # 找出管子穿过的平面
        crossed_planes = []
        
        for _, plane in floor_df.iterrows():
            plane_z = plane['Z坐标']
            min_x, max_x = plane['最小X'], plane['最大X']
            min_y, max_y = plane['最小Y'], plane['最大Y']
            
            # 判断管子中心点是否在平面范围内
            if min_x <= center_x <= max_x and min_y <= center_y <= max_y:
                # 判断平面标高是否在管子两端之间
                if lower_z + 50 < plane_z < upper_z - 50:
                    crossed_planes.append(plane_z)
        
        # 去重排序
        crossed_planes = sorted(list(set(crossed_planes)))
        
        # 根据穿过平面的数量进行处理
        if len(crossed_planes) == 1:
            # 情况一：仅穿过一个平面
            plane_z = crossed_planes[0]
            
            # 检查上方焊口距离楼层是否小于1.5米（1500mm）
            distance_to_plane = upper_z - plane_z
            
            if distance_to_plane < 1500:  # 上方焊口距离楼层小于1.5米
                # 优先设置这个焊口为现场口
                df.loc[upper_idx, '焊点类型'] = 'F'
                df.loc[upper_idx, '规则'] = 'S-F034'
                processed_indices_sf034.add(upper_idx)
            else:
                # 如果上方焊口距离大于等于1.5米，则设置上方焊口为现场口（原逻辑）
                df.loc[upper_idx, '焊点类型'] = 'F'
                df.loc[upper_idx, '规则'] = 'S-F034'
                processed_indices_sf034.add(upper_idx)
            
        elif len(crossed_planes) >= 2:
            # 情况二：穿过两个及以上平面
            # 上下两端焊口都设为现场口
            df.loc[upper_idx, '焊点类型'] = 'F'
            df.loc[upper_idx, '规则'] = 'S-F034'
            df.loc[lower_idx, '焊点类型'] = 'F'
            df.loc[lower_idx, '规则'] = 'S-F034'
            processed_indices_sf034.add(upper_idx)
            processed_indices_sf034.add(lower_idx)

    # 计算规则'S-F034'改为安装焊口的寸径
    total_english_units_F = df.loc[(df['规则'] == 'S-F034') & (df['焊点类型'] == 'F'), '英制'].sum()
    total_english_units_S = df.loc[(df['规则'] == 'S-F034') & (df['焊点类型'] == 'S'), '英制'].sum()
    installation_weld_diameter = total_english_units_F - total_english_units_S

    print(f"规则S-F034改为安装焊口的寸径: {installation_weld_diameter}")

    # 计算'焊点类型'为'S'的行中'英制'列的总和
    sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

    # 计算占比
    percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")  
'''
# ============================================

'''
# 规则S-F035 - 已注释，不运行
# 描述：阀门通过法兰连接管子/管件时：
#       1. 法兰的唯一一道焊口设为现场口F
#       2. 与法兰连接的管子/管件的另一道焊口（如果有）设为预制口S
# 前提：两个焊口都没有涉及其他的特定规则，且不是设备口法兰
# ============================================

# 筛选公称直径大于40的行
diameter_mask = (df['公称直径'] > 40)

# 步骤1：识别所有法兰
flange_mask1 = dataframe_side_material_matches(df, 1, 'flange')
flange_mask2 = dataframe_side_material_matches(df, 2, 'flange')

# 获取所有法兰的材料唯一码
flange_codes1 = set(df.loc[flange_mask1 & diameter_mask, '材料唯一码1'].dropna())
flange_codes2 = set(df.loc[flange_mask2 & diameter_mask, '材料唯一码2'].dropna())
all_flange_codes = flange_codes1.union(flange_codes2)

print(f"规则S-F035找到 {len(all_flange_codes)} 个法兰元件")

# 获取设备法兰口坐标（用于排除设备口法兰）
device_flange_coords = set()
try:
    if 'df2' in locals() and df2 is not None:
        if '设备法兰口坐标' in df2.columns:
            for coord in df2['设备法兰口坐标'].dropna():
                rounded_coord = parse_and_round_coordinates(str(coord))
                if rounded_coord:
                    device_flange_coords.add(rounded_coord)
except Exception as e:
    print(f"规则S-F035读取设备法兰口坐标出错: {e}")

# 用于记录已处理的焊口
processed_indices = set()

# 初始化计数器
flange_f_count = 0
flange_already_f_count = 0
flange_excluded_count = 0
flange_device_count = 0
other_side_s_count = 0
other_side_excluded_count = 0
other_side_already_s_count = 0
no_other_weld_count = 0

# 遍历每个法兰
for flange_code in all_flange_codes:
    # 找到包含该法兰的所有焊口
    flange_welds = df[(df['材料唯一码1'] == flange_code) | 
                      (df['材料唯一码2'] == flange_code)]
    
    if flange_welds.empty:
        continue
    
    # 法兰应该只有一道焊口
    if len(flange_welds) != 1:
        continue
    
    flange_weld_idx = flange_welds.index[0]
    
    # 检查是否已被本规则处理过
    if flange_weld_idx in processed_indices:
        continue
    
    flange_weld = df.loc[flange_weld_idx]
    
    # 确定法兰连接的另一端
    if flange_weld['材料唯一码1'] == flange_code:
        other_code = flange_weld['材料唯一码2']
        other_desc = flange_weld['描述2']
    else:
        other_code = flange_weld['材料唯一码1']
        other_desc = flange_weld['描述1']
    
    # 检查是否是设备口法兰
    weld_pos = df.loc[flange_weld_idx, '焊点位置']
    rounded_pos = parse_and_round_coordinates(str(weld_pos))
    if rounded_pos and rounded_pos in device_flange_coords:
        flange_device_count += 1
        continue
    
    # ===== 第一部分：检查法兰焊口 =====
    
    # 检查法兰焊口是否已经是现场口
    if df.loc[flange_weld_idx, '焊点类型'] == 'F':
        flange_already_f_count += 1
        continue
    
    # 检查法兰焊口是否被其他规则排除
    if df.loc[flange_weld_idx, '规则'] in exclude_rules:
        flange_excluded_count += 1
        continue
    
    # ===== 第二部分：查找并检查另一侧焊口 =====
    
    other_weld_idx = None
    other_weld_exists = False
    other_weld_can_modify = False
    
    if pd.notna(other_code):
        unit_no = flange_weld['单元号']
        same_unit_df = df[df['单元号'] == unit_no]
        other_welds = same_unit_df[(same_unit_df['材料唯一码1'] == other_code) | 
                                    (same_unit_df['材料唯一码2'] == other_code)]
        
        if len(other_welds) == 2:
            other_weld_idx = other_welds.index[other_welds.index != flange_weld_idx][0]
            other_weld_exists = True
            
            # 检查另一侧焊口是否已被本规则处理
            if other_weld_idx in processed_indices:
                other_weld_exists = False
            
            # 检查另一侧焊口是否已经是S
            elif df.loc[other_weld_idx, '焊点类型'] == 'S':
                other_side_already_s_count += 1
            
            # 检查另一侧焊口是否被其他规则排除
            elif df.loc[other_weld_idx, '规则'] in exclude_rules:
                other_side_excluded_count += 1
            
            else:
                other_weld_can_modify = True
        
        elif len(other_welds) == 1:
            no_other_weld_count += 1
    
    # ===== 第三部分：执行修改 =====
    
    # 修改法兰焊口为现场口F
    df.loc[flange_weld_idx, '焊点类型'] = 'F'
    df.loc[flange_weld_idx, '规则'] = 'S-F035'
    processed_indices.add(flange_weld_idx)
    flange_f_count += 1
    
    # 修改另一侧焊口为预制口S（如果存在且可修改）
    if other_weld_exists and other_weld_can_modify:
        df.loc[other_weld_idx, '焊点类型'] = 'S'
        df.loc[other_weld_idx, '规则'] = 'S-F035'
        processed_indices.add(other_weld_idx)
        other_side_s_count += 1

# 计算规则'S-F035'改为安装焊口的寸径
total_english_units_F = df.loc[(df['规则'] == 'S-F035') & (df['焊点类型'] == 'F'), '英制'].sum()
total_english_units_S = df.loc[(df['规则'] == 'S-F035') & (df['焊点类型'] == 'S'), '英制'].sum()
installation_weld_diameter = total_english_units_F - total_english_units_S

print(f"规则S-F035改为安装焊口的寸径: {installation_weld_diameter}")

# 计算'焊点类型'为'S'的行中'英制'列的总和
sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

# 计算占比
percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
'''

rule_state_before = snapshot_rule_state(df)


# 规则S-F036
# 所有DN40及以下且规则不为S-F022的焊口不预制，改为现场口F
if not ENABLE_SMALL_DIAMETER_PREFAB:
    condition = (df['公称直径'] <= 40) & (df['规则'] != 'S-F022')

    df.loc[condition, '焊点类型'] = 'F'
    df.loc[condition, '规则'] = 'S-F036'

    # 计算规则'S-F036'改为安装焊口的寸径
    total_english_units_F = df.loc[(df['规则'] == 'S-F036') & (df['焊点类型'] == 'F'), '英制'].sum()
    total_english_units_S = df.loc[(df['规则'] == 'S-F036') & (df['焊点类型'] == 'S'), '英制'].sum()
    installation_weld_diameter = total_english_units_F - total_english_units_S

    print(f"规则S-F036改为安装焊口的寸径: {installation_weld_diameter}")

    # 计算'焊点类型'为'S'的行中'英制'列的总和
    sum_metric_s = df[df['焊点类型'] == 'S']['英制'].sum()

    # 计算占比
    percentage_s_metric = (sum_metric_s / total_sum_metric) * 100

    print(f"运行规则后的预制深度: {percentage_s_metric:.2f}%\n")
    record_rule_triggers(df, rule_state_before, 'S-F036')
else:
    print("规则S-F036未运行：小管径预制规则开关已打开。\n")


# 删除用于计算的列
df = df.drop(columns=['x', 'y', 'z', '走向'])

# 规则运行完毕，进行管段编号生成
def generate_segment_labels():
    """ 生成连续段标记，如A、B、C、...、Z、AA、AB、... """
    for label_number in count(1):
        result = ""
        while label_number:
            label_number, remainder = divmod(label_number - 1, 26)
            result = ascii_uppercase[remainder] + result
        yield result

def find_connected_segments_advanced(group):
    """
    在给定的数据组中找到所有连续的段，并为每个新的'管线号'从'F001'开始分配标记。
    """
    segment_label_gen = generate_segment_labels()
    group = group.reset_index(drop=True)
    connected_segments = {}
    visited = set()

    def dfs(row_index, label):
        """ 深度优先搜索来找到所有连续的行 """
        if row_index in visited:
            return
        visited.add(row_index)
        connected_segments[row_index] = label
        row = group.loc[row_index]
        connected_rows = group[(group['材料唯一码1'] == row['材料唯一码1']) | 
                               (group['材料唯一码1'] == row['材料唯一码2']) |
                               (group['材料唯一码2'] == row['材料唯一码1']) | 
                               (group['材料唯一码2'] == row['材料唯一码2'])].index
        for idx in connected_rows:
            dfs(idx, label)

    for index, _ in group.iterrows():
        if index not in visited:
            label = next(segment_label_gen)
            dfs(index, label)

    return connected_segments

def find_and_label_connected_segments(df):
    """
    对'焊点类型'为'S'的数据进行处理，按'管线号'分组，并为每组相连的行分配管段编号。
    """
    df_filtered = df[df['焊点类型'] == 'S'].copy()  # 筛选'焊点类型'为'S'的数据
    df_filtered['管段编号'] = None  # 初始化'管段编号'列

    for pipeline, group in df_filtered.groupby('管线号'):
        connected_segments = find_connected_segments_advanced(group)
        for index, label in connected_segments.items():
            df_filtered.at[group.index[index], '管段编号'] = label

    return df_filtered

# 应用函数并显示结果
df_labeled = find_and_label_connected_segments(df)
df_labeled  # 显示结果，用于验证逻辑是否正确

# 将新的管段编号合并回原始的df DataFrame
df['管段编号'] = df_labeled['管段编号']

# 保存修改后的DataFrame到新的Excel文件（使用相对路径）
new_excel_file_path = f'{file_name}-运行后.xlsx'
df.to_excel(new_excel_file_path, index=False)

trajectory_df = pd.DataFrame(
    rule_trigger_records,
    columns=[
        '单元号', '管线号', '焊点编号', '步骤序号', '来源规则', '规则',
        '来源焊点类型', '目标焊点类型', '是否改规则', '是否改SF'
    ]
)
if not trajectory_df.empty:
    trajectory_df['规则分类'] = trajectory_df['规则'].map(rule_category_map).fillna('')
    trajectory_df = trajectory_df.sort_values(
        by=['单元号', '管线号', '焊点编号', '步骤序号'],
        kind='stable'
    ).reset_index(drop=True)
    weld_trajectory_summary_df = (
        trajectory_df.groupby(['单元号', '管线号', '焊点编号'])
        .agg(
            影响规则数=('规则', 'nunique'),
            影响总次数=('规则', 'size'),
            SF变更次数=('是否改SF', 'sum'),
            最后一次规则=('规则', 'last')
        )
        .reset_index()
    )
    rule_chain_df = (
        trajectory_df.groupby(['单元号', '管线号', '焊点编号'])['规则']
        .apply(lambda s: ' -> '.join(s.astype(str)))
        .reset_index(name='规则覆盖顺序')
    )
    weld_trajectory_summary_df = weld_trajectory_summary_df.merge(
        rule_chain_df,
        on=['单元号', '管线号', '焊点编号'],
        how='left'
    )
else:
    trajectory_df = pd.DataFrame(columns=[
        '单元号', '管线号', '焊点编号', '步骤序号', '来源规则', '规则',
        '来源焊点类型', '目标焊点类型', '是否改规则', '是否改SF', '规则分类'
    ])
    weld_trajectory_summary_df = pd.DataFrame(columns=[
        '单元号', '管线号', '焊点编号', '影响规则数', '影响总次数', 'SF变更次数',
        '最后一次规则', '规则覆盖顺序'
    ])

trajectory_file_path = f'{file_name}-规则轨迹.xlsx'
if OUTPUT_RULE_ANALYSIS_FILES:
    with pd.ExcelWriter(trajectory_file_path) as writer:
        trajectory_df.to_excel(writer, sheet_name='规则轨迹明细', index=False)
        weld_trajectory_summary_df.to_excel(writer, sheet_name='焊缝轨迹汇总', index=False)
else:
    print("规则轨迹表未输出：规则分析文件输出开关已关闭。")

# 输出规则生效统计表：按单元号+管线号统计各规则影响的焊口数
if not rule_order:
    rule_order = sorted(df['规则'].dropna().astype(str).unique().tolist())

rule_stats_df = (
    df.groupby(['单元号', '管线号', '规则'])
      .size()
      .unstack(fill_value=0)
)

rule_stats_df = rule_stats_df.reindex(columns=rule_order, fill_value=0)
rule_stats_df = rule_stats_df.reset_index()
rule_stats_df = rule_stats_df.sort_values(by=['单元号', '管线号'], kind='stable').reset_index(drop=True)

category_columns = [category for category in rule_category_order if category in set(rule_category_map.values())]
if not category_columns:
    category_columns = rule_category_order.copy()

category_summary_df = rule_stats_df[['单元号', '管线号']].copy()
for category in category_columns:
    category_rules = [rule for rule in rule_order if rule_category_map.get(rule) == category]
    if category_rules:
        category_summary_df[category] = rule_stats_df[category_rules].sum(axis=1)
    else:
        category_summary_df[category] = 0
category_total = category_summary_df[category_columns].sum(axis=1)
for category in category_columns:
    category_summary_df[f'{category}占比'] = np.where(category_total > 0, category_summary_df[category] / category_total, 0)

line_summary_df = (
    df.groupby(['单元号', '管线号'])
      .agg(
          焊口总数=('焊点编号', 'size'),
          预制口数=('焊点类型', lambda s: (s == 'S').sum()),
          安装口数=('焊点类型', lambda s: (s == 'F').sum())
      )
      .reset_index()
)
line_summary_df = line_summary_df.sort_values(by=['单元号', '管线号'], kind='stable').reset_index(drop=True)
line_summary_df['预制率'] = np.where(line_summary_df['焊口总数'] > 0, line_summary_df['预制口数'] / line_summary_df['焊口总数'], 0)

rule_counts_only = rule_stats_df[rule_order]
line_summary_df['最终主导规则'] = rule_counts_only.idxmax(axis=1)
line_summary_df['最终主导规则影响数'] = rule_counts_only.max(axis=1)
line_summary_df.loc[line_summary_df['最终主导规则影响数'] == 0, '最终主导规则'] = ''

category_counts_only = category_summary_df[category_columns]
line_summary_df['最终主导分类'] = category_counts_only.idxmax(axis=1)
line_summary_df['最终主导分类影响数'] = category_counts_only.max(axis=1)
line_summary_df.loc[line_summary_df['最终主导分类影响数'] == 0, '最终主导分类'] = ''

trigger_df = pd.DataFrame(rule_trigger_records, columns=['单元号', '管线号', '规则'])
if trigger_df.empty:
    trigger_rule_stats_df = rule_stats_df[['单元号', '管线号']].copy()
    for rule in rule_order:
        trigger_rule_stats_df[rule] = 0
else:
    trigger_rule_stats_df = (
        trigger_df.groupby(['单元号', '管线号', '规则'])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=rule_order, fill_value=0)
        .reset_index()
        .sort_values(by=['单元号', '管线号'], kind='stable')
        .reset_index(drop=True)
    )

trigger_detail_df = pd.DataFrame(
    rule_trigger_records,
    columns=['单元号', '管线号', '来源规则', '规则', '来源焊点类型', '目标焊点类型']
)
if trigger_detail_df.empty:
    coverage_analysis_df = rule_stats_df[['单元号', '管线号']].copy()
    coverage_analysis_df['覆盖次数'] = 0
    coverage_analysis_df['覆盖链路数'] = 0
    coverage_analysis_df['主覆盖来源规则'] = ''
    coverage_analysis_df['主覆盖目标规则'] = ''
    coverage_analysis_df['主覆盖次数'] = 0
    coverage_analysis_df['类型变更次数'] = 0
    coverage_analysis_df['规则覆盖未改类型次数'] = 0
else:
    coverage_only_df = trigger_detail_df[
        (trigger_detail_df['来源规则'] != '') &
        (trigger_detail_df['来源规则'] != trigger_detail_df['规则'])
    ].copy()
    coverage_only_df['覆盖链路'] = coverage_only_df['来源规则'] + ' -> ' + coverage_only_df['规则']
    coverage_only_df['类型是否变更'] = coverage_only_df['来源焊点类型'] != coverage_only_df['目标焊点类型']

    if coverage_only_df.empty:
        coverage_analysis_df = rule_stats_df[['单元号', '管线号']].copy()
        coverage_analysis_df['覆盖次数'] = 0
        coverage_analysis_df['覆盖链路数'] = 0
        coverage_analysis_df['主覆盖来源规则'] = ''
        coverage_analysis_df['主覆盖目标规则'] = ''
        coverage_analysis_df['主覆盖次数'] = 0
        coverage_analysis_df['类型变更次数'] = 0
        coverage_analysis_df['规则覆盖未改类型次数'] = 0
    else:
        coverage_pair_counts = (
            coverage_only_df.groupby(['单元号', '管线号', '来源规则', '规则'])
            .size()
            .reset_index(name='次数')
            .sort_values(by=['单元号', '管线号', '次数', '来源规则', '规则'], ascending=[True, True, False, True, True], kind='stable')
        )
        dominant_coverage_df = (
            coverage_pair_counts.groupby(['单元号', '管线号'], as_index=False)
            .first()[['单元号', '管线号', '来源规则', '规则', '次数']]
            .rename(columns={
                '来源规则': '主覆盖来源规则',
                '规则': '主覆盖目标规则',
                '次数': '主覆盖次数'
            })
        )
        coverage_summary_base = (
            coverage_only_df.groupby(['单元号', '管线号'])
            .agg(
                覆盖次数=('覆盖链路', 'size'),
                覆盖链路数=('覆盖链路', 'nunique'),
                类型变更次数=('类型是否变更', 'sum')
            )
            .reset_index()
        )
        coverage_summary_base['规则覆盖未改类型次数'] = coverage_summary_base['覆盖次数'] - coverage_summary_base['类型变更次数']
        coverage_analysis_df = coverage_summary_base.merge(dominant_coverage_df, on=['单元号', '管线号'], how='left')
        coverage_analysis_df = coverage_analysis_df.sort_values(by=['单元号', '管线号'], kind='stable').reset_index(drop=True)

trigger_category_summary_df = trigger_rule_stats_df[['单元号', '管线号']].copy()
for category in category_columns:
    category_rules = [rule for rule in rule_order if rule_category_map.get(rule) == category]
    if category_rules:
        trigger_category_summary_df[category] = trigger_rule_stats_df[category_rules].sum(axis=1)
    else:
        trigger_category_summary_df[category] = 0

trigger_rule_counts_only = trigger_rule_stats_df[rule_order]
line_summary_df['主导触发规则'] = trigger_rule_counts_only.idxmax(axis=1)
line_summary_df['主导触发规则次数'] = trigger_rule_counts_only.max(axis=1)
line_summary_df.loc[line_summary_df['主导触发规则次数'] == 0, '主导触发规则'] = ''

trigger_category_counts_only = trigger_category_summary_df[category_columns]
line_summary_df['主导触发分类'] = trigger_category_counts_only.idxmax(axis=1)
line_summary_df['主导触发分类次数'] = trigger_category_counts_only.max(axis=1)
line_summary_df.loc[line_summary_df['主导触发分类次数'] == 0, '主导触发分类'] = ''

if rule_metadata_df.empty:
    rule_metadata_df = pd.DataFrame({
        '序号': list(range(1, len(rule_order) + 1)),
        '规则代号': rule_order,
        '分类': [rule_category_map.get(rule, '') for rule in rule_order],
        '是否运行': ['' for _ in rule_order],
        '说明': ['' for _ in rule_order],
    })
else:
    rule_metadata_df = rule_metadata_df.sort_values(by=['序号', '规则代号'], kind='stable').reset_index(drop=True)

rule_stats_file_path = '规则生效统计表.xlsx'
if OUTPUT_RULE_ANALYSIS_FILES:
    with pd.ExcelWriter(rule_stats_file_path) as writer:
        rule_stats_df.to_excel(writer, sheet_name='按规则统计', index=False)
        category_summary_df.to_excel(writer, sheet_name='按分类汇总', index=False)
        trigger_rule_stats_df.to_excel(writer, sheet_name='按规则触发统计', index=False)
        trigger_category_summary_df.to_excel(writer, sheet_name='按分类触发汇总', index=False)
        coverage_analysis_df.to_excel(writer, sheet_name='规则覆盖分析', index=False)
        line_summary_df.to_excel(writer, sheet_name='管线汇总', index=False)
        rule_metadata_df.to_excel(writer, sheet_name='规则说明', index=False)
else:
    print("规则生效统计表未输出：规则分析文件输出开关已关闭。")

# 首先，对'管线号'进行分组，并计算每个管线号的管段数量
segment_counts_per_line = df.groupby('管线号')['管段编号'].nunique()

# 然后，计算所有管线号的管段数量之和
total_segment_count = segment_counts_per_line.sum()

# 打印结果
print(f"管段数量共计：{total_segment_count}个")

print('运行结束')
