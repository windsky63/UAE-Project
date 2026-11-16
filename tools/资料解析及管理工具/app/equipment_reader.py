# Adapted from root PCF焊缝信息读取v5.py: callable inputs/outputs only.
import os
import re
import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
from openpyxl.utils import get_column_letter
from collections import defaultdict

unique_id = 0


def extract_equipment_tag(nozzle_str):
    """
    按IDF脚本规则从设备管嘴号提取设备位号。
    """
    s = str(nozzle_str)

    if s.startswith('Copy-of-'):
        s = s[len('Copy-of-'):]

    if s.startswith('='):
        return f"'{s}"

    if s.startswith('/') or s.startswith('\\'):
        s = s[1:]

    if '/' in s:
        return s.split('/', 1)[0]
    elif '-' in s:
        return s.rsplit('-', 1)[0]

    return s


def read_pcf_file(file_path):
    """
    读取PCF文件并处理不同编码情况

    Args:
        file_path: PCF文件路径

    Returns:
        文件内容行列表
    """
    # 尝试不同的编码方式
    encodings = ['utf-8', 'gbk', 'gb2312', 'iso-8859-1', 'utf-16', 'big5', 'cp936', 'utf-8-sig']
    lines = None

    # 尝试读取文件
    for encoding in encodings:
        try:
            with open(file_path, 'rb') as f:
                content = f.read()
            if content.startswith(b'\xef\xbb\xbf'):
                encoding = 'utf-8-sig'
            elif content.startswith(b'\xff\xfe') or content.startswith(b'\xfe\xff'):
                encoding = 'utf-16'

            lines = content.decode(encoding).splitlines()
            print(f"成功使用 {encoding} 编码读取文件 {file_path}")
            break
        except UnicodeDecodeError:
            continue
        except Exception as e:
            print(f"读取文件 {file_path} 时出错 (编码: {encoding}): {str(e)}")
            continue

    if lines is None:
        print(f"无法读取文件 {file_path}，已尝试以下编码: {', '.join(encodings)}")
        return []

    return lines


def extract_component_coordinates(file_path):
    """
    提取PCF文件中的管线号和组件坐标信息

    Args:
        file_path: PCF文件路径

    Returns:
        包含管线号和组件坐标的字典列表
    """

    lines = read_pcf_file(file_path)

    if not lines:
        return []

    # 获取管线号
    pipeline_ref = None
    for line in lines:
        if line.strip().startswith('PIPELINE-REFERENCE'):
            parts = line.strip().split(None, 1)
            if len(parts) > 1:
                pipeline_ref = parts[1].strip()
                break

    if not pipeline_ref:
        # 如果没有找到管线号，使用文件名
        pipeline_ref = os.path.splitext(os.path.basename(file_path))[0]

    components = []
    current_component = None
    component_id = None
    component_type = None
    component_seq = 0

    i = 0
    while i < len(lines):
        line = lines[i].strip()

        # 检查是否是新组件的开始(无缩进行)
        if line and not lines[i].startswith(' '):
            # 保存前一个组件信息(如果已有坐标)
            if current_component and current_component.get('坐标'):
                components.append(current_component)

            # 设置当前组件类型
            component_type = line

            # 跳过FLOW-ARROW类型的组件
            if component_type == "FLOW-ARROW":
                current_component = None
            else:
                component_seq += 1
                current_component = {
                    '组件内部ID': component_seq,
                    '管线号': pipeline_ref,
                    '组件类型': component_type,
                    '组件ID': None,
                    'SKEY': '',  # 新增SKEY字段
                    'material_exclude': False,  # 默认为False
                    '设备位号': '',  # 新增设备位号字段
                    '坐标': []
                }

        # 提取组件ID
        elif current_component and line.startswith('COMPONENT-IDENTIFIER'):
            parts = line.split()
            if len(parts) > 1:
                component_id = parts[1]
                current_component['组件ID'] = component_id

        # 提取SKEY值
        elif current_component and line.startswith('SKEY'):
            parts = line.split()
            if len(parts) > 1:
                skey_value = parts[1]
                current_component['SKEY'] = skey_value

        # 提取MATERIAL-LIST值
        elif current_component and line.startswith('MATERIAL-LIST'):
            parts = line.split()
            if len(parts) > 1 and parts[1] == 'EXCLUDE':
                current_component['material_exclude'] = True
                
        # 提取设备位号 - 仅对END-CONNECTION-EQUIPMENT类型组件
        elif current_component and current_component['组件类型'] == 'END-CONNECTION-EQUIPMENT' and line.startswith('CONNECTION-REFERENCE'):
            parts = line.split(None, 1)
            if len(parts) > 1:
                current_component['设备位号'] = parts[1].strip()

        # 获取坐标点 - 处理各种可能的坐标行
        elif current_component and (line.startswith('END-POINT') or
                                    line.startswith('CENTRE-POINT') or
                                    line.startswith('BRANCH1-POINT') or
                                    line.startswith('CO-ORDS')):
            parts = line.split()
            coord_type = parts[0]

            if len(parts) >= 4:  # 确保有X,Y,Z坐标
                x, y, z = parts[1], parts[2], parts[3]

                # 获取管径信息(如果有)
                diameter = parts[4] if len(parts) > 4 else ""

                current_component['坐标'].append({
                    '坐标类型': coord_type,
                    'X': x,
                    'Y': y,
                    'Z': z,
                    '管径': diameter
                })

        i += 1

    # 添加最后一个组件
    if current_component and current_component.get('坐标'):
        components.append(current_component)

    return components


def format_excel(excel_file):
    """格式化Excel文件"""
    wb = load_workbook(excel_file)

    # 定义样式
    header_fill = PatternFill(start_color="E6FFE6", end_color="E6FFE6", fill_type="solid")
    font = Font(name="微软雅黑", size=9)
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )
    alignment = Alignment(horizontal='left', vertical='center', wrap_text=True)

    # 应用样式到每个工作表
    for sheet_name in wb.sheetnames:
        sheet = wb[sheet_name]

        # 获取工作表尺寸
        row_count = sheet.max_row
        column_count = sheet.max_column

        # 应用样式到每个单元格
        for row in range(1, row_count + 1):
            for col in range(1, column_count + 1):
                cell = sheet.cell(row=row, column=col)

                # 应用字体和边框到所有单元格
                cell.font = font
                cell.border = thin_border
                cell.alignment = alignment

                # 为表头行应用填充
                if row == 1:
                    cell.fill = header_fill

        # 自动调整列宽
        for col in range(1, column_count + 1):
            max_length = 0
            column = get_column_letter(col)

            for row in range(1, row_count + 1):
                cell_value = str(sheet.cell(row=row, column=col).value or "")
                max_length = max(max_length, len(cell_value))

            # 调整列宽(添加一些额外空间)
            adjusted_width = max_length + 4
            sheet.column_dimensions[column].width = adjusted_width

    wb.save(excel_file)
    print(f"Excel文件已格式化: {excel_file}")


def group_by_pipeline(all_components):
    # 按单元和管线号分组，避免递归读取多个文件夹时相同管线号混在一起
    grouped_data = defaultdict(list)
    for item in all_components:
        grouped_data[(item.get('单元', ''), item['管线号'])].append(item)
    return dict(grouped_data)


def build_lookup_tables(components):
    """
    为组件列表构建快速查找表

    Args:
        components: 单个管线的所有组件列表

    Returns:
        一个包含坐标查找表和组件内部端点查找表的元组
    """
    coord_map = defaultdict(list)
    component_endpoint_map = defaultdict(list)
    for comp in components:
        # 建立坐标查找表
        coord_key = (comp['X'], comp['Y'], comp['Z'])
        coord_map[coord_key].append(comp)

        # 为同一材料块内的END-POINT建立查找表，不依赖COMPONENT-IDENTIFIER
        if comp.get('组件内部ID') and comp['坐标类型'] == 'END-POINT':
            component_endpoint_map[comp['组件内部ID']].append(comp)
            
    return coord_map, component_endpoint_map


def get_other_end_coordinate(component, component_endpoint_map):
    """
    获取同一材料块内的另一端坐标，不依赖COMPONENT-IDENTIFIER

    Args:
        component: 当前组件信息
        component_endpoint_map: 组件内部端点查找表

    Returns:
        另一端坐标组件信息或None
    """
    # 仅支持END-POINT坐标类型
    if component['坐标类型'] != 'END-POINT':
        return None

    component_internal_id = component.get('组件内部ID')
    if not component_internal_id:
        return None

    endpoints = component_endpoint_map.get(component_internal_id, [])

    # 返回同一材料块内不是当前端点的另一端
    for endpoint in endpoints:
        if endpoint['unique_id'] != component['unique_id']:
            return endpoint
            
    return None


def find_components_at_coordinate(x, y, z, coord_map, exclude_ids=None):
    """
    查找指定XYZ坐标值的组件 (优化版)

    Args:
        x: X坐标值
        y: Y坐标值
        z: Z坐标值
        coord_map: 坐标查找表
        exclude_ids: 要排除的unique_id列表

    Returns:
        找到的组件列表
    """
    if exclude_ids is None:
        exclude_ids = []
    
    # 从查找表直接获取组件，而不是遍历
    components_at_coord = coord_map.get((x, y, z), [])
    
    # 过滤掉排除的ID和非END-POINT类型
    return list(filter(lambda data:
                       data['坐标类型'] == 'END-POINT' and
                       data['unique_id'] not in exclude_ids,
                       components_at_coord))


def trace_connection_recursive(component, all_components, coord_map, comp_id_map, visited_ids=None):
    """
    递归跟踪组件连接，找到符合条件的法兰端点

    Args:
        component: 当前组件信息
        all_components: 当前管号线所有组件列表
        coord_map: 坐标查找表
        comp_id_map: 组件ID查找表
        visited_ids: 已访问的unique_id列表

    Returns:
        符合条件的法兰端点信息或None
    """
    if visited_ids is None:
        visited_ids = []

    # 避免重复访问
    if component['unique_id'] in visited_ids:
        return None

    # 添加到已访问列表
    visited_ids.append(component['unique_id'])

    # 如果当前组件是焊缝且不是从法兰递归过来的，则返回None
    if component['组件类型'] == 'WELD':
        return None

    # 获取组件的坐标
    x, y, z = component['X'], component['Y'], component['Z']

    # 如果当前组件是法兰，检查它是否满足条件
    if component['组件类型'] == 'FLANGE':
        # 获取法兰的另一端
        other_end = get_other_end_coordinate(component, comp_id_map)

        if other_end and other_end['unique_id'] not in visited_ids:
            # 检查法兰另一端是否有连接组件
            other_x, other_y, other_z = other_end['X'], other_end['Y'], other_end['Z']
            other_comps = find_components_at_coordinate(other_x, other_y, other_z, coord_map,
                                                        visited_ids + [other_end['unique_id']])

            # 情况1: 法兰另一端没有连接 - 返回另一端坐标
            if not other_comps:
                return {
                    '坐标': other_end['X'] + ',' + other_end['Y'] + ',' + other_end['Z'],
                    '单元': component.get('单元', ''),
                    '管线号': component['管线号'],
                    '规格': component['管径']
                }

            # 情况2: 法兰另一端直接连接焊点 - 返回另一端坐标
            else:
                # 检查是否直接连接焊点
                welds = list(filter(lambda data: data['组件类型'] == 'WELD', other_comps))
                if welds:
                    return {
                        '坐标': other_end['X'] + ',' + other_end['Y'] + ',' + other_end['Z'],
                        '单元': component.get('单元', ''),
                        '管线号': component['管线号'],
                        '规格': component['管径']
                    }
                # 情况3: 法兰另一端连接了非焊缝组件 - 继续递归处理这些组件
                else:
                    # 将other_end标记为已访问
                    temp_visited = visited_ids.copy()
                    temp_visited.append(other_end['unique_id'])

                    # 递归处理所有连接的非焊缝组件
                    for other_comp in other_comps:
                        result = trace_connection_recursive(other_comp, all_components, coord_map, comp_id_map, temp_visited.copy())
                        if result:
                            return result

    # 搜索与当前组件坐标相同的其他组件（排除已访问过的，避免死循环）
    same_coord_components = find_components_at_coordinate(x, y, z, coord_map, visited_ids)

    # 递归检查与当前组件坐标相同的其他组件
    for same_coord_comp in same_coord_components:
        result = trace_connection_recursive(same_coord_comp, all_components, coord_map, comp_id_map, visited_ids.copy())
        if result:
            return result

    # 如果当前组件没有同坐标的连接组件，检查组件的另一端
    if component['坐标类型'] == 'END-POINT':
        other_end = get_other_end_coordinate(component, comp_id_map)

        if other_end and other_end['unique_id'] not in visited_ids:
            # 递归检查组件的另一端
            visited_ids.append(other_end['unique_id'])

            # 检查与另一端坐标相同的组件
            other_x, other_y, other_z = other_end['X'], other_end['Y'], other_end['Z']
            other_comps = find_components_at_coordinate(other_x, other_y, other_z, coord_map, visited_ids)

            for other_comp in other_comps:
                result = trace_connection_recursive(other_comp, all_components, coord_map, comp_id_map, visited_ids.copy())
                if result:
                    return result

    # 如果没有找到符合条件的法兰，返回None
    return None


def find_component_type_at_coordinate(component_type, x, y, z, coord_map, exclude_ids=None):
    components_at_coord = find_components_at_coordinate(x, y, z, coord_map, exclude_ids)
    return [item for item in components_at_coord if item['组件类型'] == component_type]


def build_flange_result(flange_endpoint, component_endpoint_map):
    flange_other_end = get_other_end_coordinate(flange_endpoint, component_endpoint_map)
    if not flange_other_end:
        return None

    return {
        '坐标': flange_other_end['X'] + ',' + flange_other_end['Y'] + ',' + flange_other_end['Z'],
        '单元': flange_endpoint.get('单元', ''),
        '管线号': flange_endpoint['管线号'],
        '规格': flange_other_end['管径'] or flange_endpoint['管径']
    }


def trace_gasket_to_flange(gasket_endpoint, coord_map, component_endpoint_map, visited_ids=None):
    """
    按 GASKET -> FLANGE 或 GASKET -> VALVE -> GASKET -> ... -> FLANGE 的顺序追踪。
    组件连接只通过坐标匹配，同一材料块另一端通过内部组件编号获取。
    """
    if visited_ids is None:
        visited_ids = set()

    if gasket_endpoint['unique_id'] in visited_ids:
        return None
    visited_ids.add(gasket_endpoint['unique_id'])

    gasket_other_end = get_other_end_coordinate(gasket_endpoint, component_endpoint_map)
    if not gasket_other_end or gasket_other_end['unique_id'] in visited_ids:
        return None
    visited_ids.add(gasket_other_end['unique_id'])

    x, y, z = gasket_other_end['X'], gasket_other_end['Y'], gasket_other_end['Z']
    connected_components = find_components_at_coordinate(x, y, z, coord_map, visited_ids)

    # END-CONNECTION-EQUIPMENT -> GASKET -> FLANGE
    for component in connected_components:
        if component['组件类型'] == 'FLANGE':
            return build_flange_result(component, component_endpoint_map)

    # END-CONNECTION-EQUIPMENT -> GASKET -> (VALVE -> GASKET)n -> FLANGE
    for valve_endpoint in connected_components:
        if valve_endpoint['组件类型'] != 'VALVE':
            continue

        if valve_endpoint['unique_id'] in visited_ids:
            continue

        next_visited = visited_ids.copy()
        next_visited.add(valve_endpoint['unique_id'])

        valve_other_end = get_other_end_coordinate(valve_endpoint, component_endpoint_map)
        if not valve_other_end or valve_other_end['unique_id'] in next_visited:
            continue

        next_visited.add(valve_other_end['unique_id'])
        next_x, next_y, next_z = valve_other_end['X'], valve_other_end['Y'], valve_other_end['Z']
        next_gaskets = find_component_type_at_coordinate('GASKET', next_x, next_y, next_z, coord_map, next_visited)

        for next_gasket in next_gaskets:
            result = trace_gasket_to_flange(next_gasket, coord_map, component_endpoint_map, next_visited.copy())
            if result:
                return result

    return None


def process_equipment_connections(components, coord_map, component_endpoint_map):
    """
    处理外部设备连接的法兰

    Args:
        components: 当前管线的组件列表
        coord_map: 坐标查找表
        component_endpoint_map: 组件内部端点查找表

    Returns:
        连接点信息列表
    """
    result = []

    # 筛选出外部设备连接点
    external_connections = list(
        filter(lambda item: item['组件类型'] == 'END-CONNECTION-EQUIPMENT', components))

    for item in external_connections:
        x, y, z = item['X'], item['Y'], item['Z']
        gaskets = find_component_type_at_coordinate('GASKET', x, y, z, coord_map, [item['unique_id']])

        for gasket in gaskets:
            connection_info = trace_gasket_to_flange(
                gasket,
                coord_map,
                component_endpoint_map,
                {item['unique_id']}
            )
            if connection_info:
                connection_info['设备位号'] = item['设备位号']
                result.append(connection_info)
                break

    return result


def process_excluded_flanges(components, coord_map, component_endpoint_map):
    """
    处理标记为材料清单排除的法兰。
    规则：查找MATERIAL-LIST EXCLUDE的FLANGE，取不连接GASKET的一端作为虚线法兰坐标。

    Args:
        components: 当前管线的组件列表
        coord_map: 坐标查找表
        component_endpoint_map: 组件内部端点查找表

    Returns:
        连接点信息列表
    """
    result = []

    exclude_flange_ids = {
        item['组件内部ID']
        for item in components
        if item['组件类型'] == 'FLANGE'
        and item.get('material_exclude', False)
        and item.get('组件内部ID')
    }

    for component_internal_id in exclude_flange_ids:
        endpoints = component_endpoint_map.get(component_internal_id, [])
        if not endpoints:
            continue

        endpoint_ids = [endpoint['unique_id'] for endpoint in endpoints]
        for endpoint in endpoints:
            x, y, z = endpoint['X'], endpoint['Y'], endpoint['Z']
            connected_gaskets = find_component_type_at_coordinate('GASKET', x, y, z, coord_map, endpoint_ids)
            if connected_gaskets:
                continue

            result.append({
                '坐标': endpoint['X'] + ',' + endpoint['Y'] + ',' + endpoint['Z'],
                '单元': endpoint.get('单元', ''),
                '管线号': endpoint['管线号'],
                '规格': endpoint['管径'],
                '备注': '虚线法兰'
            })
            break

    return result


def process_excluded_reducers(components, component_endpoint_map):
    """
    处理MATERIAL-LIST EXCLUDE的REDUCER*元件，读取两端坐标作为虚线异径管坐标。
    """
    result = []
    reducer_ids = {
        item['组件内部ID']
        for item in components
        if item['组件类型'].startswith('REDUCER')
        and item.get('material_exclude', False)
        and item.get('组件内部ID')
    }

    for component_internal_id in reducer_ids:
        endpoints = component_endpoint_map.get(component_internal_id, [])
        for endpoint in endpoints:
            result.append({
                '坐标': endpoint['X'] + ',' + endpoint['Y'] + ',' + endpoint['Z'],
                '单元': endpoint.get('单元', ''),
                '管线号': endpoint['管线号'],
                '规格': endpoint['管径'],
                '备注': '虚线异径管'
            })

    return result


def analyze_files(pcf_files, unit_name):
    # 提取所有组件坐标
    all_components = []

    for file_path in pcf_files:
        pcf_file = os.path.basename(file_path)
        print(f"正在处理文件: {pcf_file}")

        components = extract_component_coordinates(file_path)
        print(f"  - 提取到 {len(components)} 个组件")

        # 将坐标信息转换为平面格式以便于输出到Excel
        for component in components:
            coords = component.pop('坐标')
            for i, coord in enumerate(coords):
                flat_component = component.copy()
                flat_component['单元'] = unit_name
                flat_component['坐标类型'] = coord['坐标类型']
                flat_component['X'] = coord['X']
                flat_component['Y'] = coord['Y']
                flat_component['Z'] = coord['Z']
                flat_component['管径'] = coord['管径']
                flat_component['坐标序号'] = i + 1

                # 唯一键
                global unique_id
                unique_id += 1
                flat_component['unique_id'] = unique_id

                all_components.append(flat_component)

    # 根据管线号分组
    group_components = group_by_pipeline(all_components)

    equipment_results = []
    extra_install_results = []
    # 处理每个管线的组件
    for key in group_components:
        value = group_components[key]

        # 为当前管线组件建立查找表以提高效率
        coord_map, component_endpoint_map = build_lookup_tables(value)

        # 只处理外部设备连接的法兰
        equipment_connections = process_equipment_connections(value, coord_map, component_endpoint_map)
        equipment_results.extend(equipment_connections)

        excluded_flange_connections = process_excluded_flanges(value, coord_map, component_endpoint_map)
        extra_install_results.extend(excluded_flange_connections)

        excluded_reducer_connections = process_excluded_reducers(value, component_endpoint_map)
        extra_install_results.extend(excluded_reducer_connections)

    # 将结果输出到Excel文件；没有命中内容时也生成与IDF一致的空sheet和表头
    device_columns = ['单元名称', '管线号', '设备管嘴号', '设备位号', '设备法兰口坐标', '规格']
    install_columns = ['单元名称', '管线号', '设备法兰口坐标', '规格', '备注']
    output_file = "PCF设备连接信息.xlsx"

    if equipment_results:
        # 对result进行去重，并保存重复数据
        seen = set()
        duplicates = set()
        unique_result = []

        for x in equipment_results:
            key = (x.get('单元', ''), x['坐标'], x['管线号'])
            if key in seen:
                duplicates.add(key)
            else:
                seen.add(key)
                unique_result.append(x)

        # 使用去重后的结果
        equipment_results = unique_result

        df = pd.DataFrame(equipment_results)
        # 将'坐标'列重命名为'设备法兰口坐标'
        df = df.rename(columns={'坐标': '设备法兰口坐标'})
        df = df.rename(columns={'单元': '单元名称'})
        df['设备管嘴号'] = df['设备位号'].astype(str)
        df['设备位号'] = df['设备管嘴号'].astype(str).apply(extract_equipment_tag)
        df['设备管嘴号'] = df['设备管嘴号'].apply(lambda x: f"'{x}" if x.startswith('=') else x)
        df['规格'] = pd.to_numeric(df['规格'], errors='coerce')
        df['备注'] = '设备法兰'
        df_device = df.reindex(columns=device_columns)
        df_equipment_install = df.reindex(columns=install_columns)
    else:
        df_device = pd.DataFrame(columns=device_columns)
        df_equipment_install = pd.DataFrame(columns=install_columns)
        print("未命中设备法兰内容。")

    if extra_install_results:
        df_extra_install = pd.DataFrame(extra_install_results)
        df_extra_install = df_extra_install.rename(columns={'坐标': '设备法兰口坐标'})
        df_extra_install = df_extra_install.rename(columns={'单元': '单元名称'})
        df_extra_install['规格'] = pd.to_numeric(df_extra_install['规格'], errors='coerce')
        df_extra_install = df_extra_install.reindex(columns=install_columns)
    else:
        df_extra_install = pd.DataFrame(columns=install_columns)

    df_install = pd.concat([df_equipment_install, df_extra_install], ignore_index=True)

    if not df_install.empty:
        # 与IDF输出保持一致：同一坐标既是设备法兰又是虚线法兰时标记为设备法兰（虚线）
        coord_has_both_equip_and_dashed = df_install.groupby('设备法兰口坐标')['备注'].transform(
            lambda s: ('设备法兰' in set(s)) and ('虚线法兰' in set(s))
        )
        df_install.loc[
            coord_has_both_equip_and_dashed & (df_install['备注'] == '设备法兰'),
            '备注'
        ] = '设备法兰（虚线）'

        category_order = ['设备法兰（虚线）', '设备法兰', '设备焊缝', '虚线法兰', '虚线异径管']
        df_install['备注'] = pd.Categorical(df_install['备注'], categories=category_order, ordered=True)
        df_install.sort_values(by='备注', inplace=True)
        df_install.drop_duplicates(subset=['设备法兰口坐标'], keep='first', inplace=True)
        df_install['备注'] = df_install['备注'].astype(str)
        df_install = df_install.reindex(columns=install_columns)
    else:
        print("未命中安装口坐标内容，生成带表头的空sheet。")

    return df_device, df_install
