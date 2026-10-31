"""Algorithm integration: equipment topology, rule provenance and original S/F flags."""
from pathlib import Path
import ast
import csv
import subprocess
import sys
import tempfile
import unittest

from local_analysis import analyze
from weld_classification import apply_rules, rule_fields

PCF = '''UNITS-BORE MM
UNITS-CO-ORDS MM
PIPELINE-REFERENCE H4-262-TEST
PIPE
    END-POINT 0 0 0 25
    END-POINT 1000 0 0 25
    ITEM-CODE P1
    DESCRIPTION PIPE BW
WELD
    CO-ORDS 1000 0 0 25
    SKEY WELD
    WELD-LOCATION SHOP
FLANGE
    END-POINT 1000 0 0 25
    END-POINT 1100 0 0 25
    ITEM-CODE F1
    DESCRIPTION FLANGE WN
    SKEY FLWN
GASKET
    END-POINT 1100 0 0 25
    END-POINT 1102 0 0 25
    ITEM-CODE G1
END-CONNECTION-EQUIPMENT
    CO-ORDS 1102 0 0 25
    CONNECTION-REFERENCE /E-101/N1
'''


class EmbeddedPrefabTests(unittest.TestCase):
    def test_embedded_rules_match_original_workbook_execution(self):
        import pandas as pd
        rows = []
        for index, (dn, left, right) in enumerate(((25, 'PIPE BW', 'PIPE BW'), (50, 'PIPE BW', 'CAP'),
                                                 (50, 'PIPE BW', 'VALVE')), 1):
            rows.append({'管线号': f'H4-262-L{index}', '单元号': 'H4-262', '焊点编号': f'W{index}',
                         '公称直径': dn, '英制': 1 if dn == 25 else 2, '吋径': 1 if dn == 25 else 2,
                         '焊点位置': f'{index*1000},0,0', '焊接类型': 'butt', '焊点类型': 'F', '管段编号': '',
                         '描述1': left, '描述2': right, '焊接材料1': f'P{index}', '焊接材料2': f'X{index}',
                         '材料唯一码1': f'P-{index}', '材料唯一码2': f'X-{index}',
                         '数量1': 1000, '数量2': 1000, '单位1': 'mm', '单位2': 'mm'})
        install = pd.DataFrame(columns=['单元名称','管线号','设备法兰口坐标','规格','备注'])
        device = pd.DataFrame(columns=['单元名称','管线号','设备管嘴号','设备位号','设备法兰口坐标','规格'])
        actual, _, _ = apply_rules(rows, install, device)
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            pd.DataFrame(rows).reindex(columns=rule_fields()).to_excel(base/'welds.xlsx', index=False)
            with pd.ExcelWriter(base/'equipment.xlsx') as writer:
                install.to_excel(writer, sheet_name='安装口坐标', index=False)
                device.to_excel(writer, sheet_name='设备连接信息', index=False)
            original = Path(__file__).resolve().parents[1]/'app/prefab_rules_260818.py'
            tree = ast.parse(original.read_text(encoding='utf-8-sig'))
            for node in tree.body:
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'file_name' for t in node.targets):
                    node.value = ast.Constant('welds')
                if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'file_path2' for t in node.targets):
                    node.value = ast.Constant(str(base/'equipment.xlsx'))
            (base/'original.py').write_text(ast.unparse(tree), encoding='utf-8')
            result = subprocess.run([sys.executable, str(base/'original.py')], cwd=base, capture_output=True,
                                    text=True, encoding='utf-8', env={**__import__('os').environ,'PYTHONIOENCODING':'utf-8'}, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            expected = pd.read_excel(base/'welds-运行后.xlsx')
        self.assertEqual(actual['焊点类型'].tolist(), expected['焊点类型'].tolist())
        self.assertEqual(actual['规则'].tolist(), expected['规则'].tolist())

    def analyze_text(self, text):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / 'source'
            source.mkdir()
            path = source / 'test.pcf'
            path.write_text(text, encoding='utf-8')
            before = path.read_bytes()
            _, report, _ = analyze(root=source, project='p', output=base / 'output')
            with (base / 'output/exports/焊口表.csv').open(encoding='utf-8-sig', newline='') as stream:
                exported = list(csv.DictReader(stream))
            self.assertEqual(len(exported), len(report['welds']))
            if exported:
                self.assertEqual(exported[0]['分类规则'], report['welds'][0]['_classification_rule'])
            self.assertEqual(path.read_bytes(), before)
            self.assertFalse(any(item.get('state') == '分析失败' for item in report['files']), report['files'])
            return report

    def test_equipment_coordinate_algorithm_overrides_old_shop_flag(self):
        report = self.analyze_text(PCF)
        self.assertEqual(len(report['welds']), 1)
        weld = report['welds'][0]
        self.assertEqual(weld['分类'], 'field')
        self.assertEqual(weld['_classification_rule'], 'S-F010')
        self.assertEqual(weld['_location_original'], 'shop')
        self.assertTrue(weld['_classification_trace'])
        self.assertEqual(weld['单元号'], 'H4-262')
        self.assertTrue(weld['材料唯一码1'])
        self.assertTrue(weld['材料唯一码2'])

    def test_inclined_pipe_uses_shared_instance_code_and_field_rule(self):
        text = '''UNITS-BORE MM
UNITS-CO-ORDS MM
PIPELINE-REFERENCE H4-262-SLOPE
PIPE
    END-POINT 0 0 0 100
    END-POINT 1000 1000 1000 100
    ITEM-CODE P1
    DESCRIPTION PIPE BW
WELD
    CO-ORDS 0 0 0 100
    SKEY WELD
WELD
    CO-ORDS 1000 1000 1000 100
    SKEY WELD
ELBOW
    END-POINT -100 0 -100 100
    CENTRE-POINT 0 0 -100 100
    END-POINT 0 0 0 100
    ITEM-CODE E1
    DESCRIPTION ELBOW BW
ELBOW
    END-POINT 1000 1000 1000 100
    CENTRE-POINT 1000 1000 1100 100
    END-POINT 1100 1000 1100 100
    ITEM-CODE E1
    DESCRIPTION ELBOW BW
'''
        report = self.analyze_text(text)
        rows = report['welds']
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['分类'] for row in rows}, {'field'})
        self.assertEqual({row['_classification_rule'] for row in rows}, {'S-F006'})
        self.assertTrue(all(row['单元号'] == 'H4-262' for row in rows))
        pipe_codes, elbow_codes = set(), set()
        for row in rows:
            for side in (1, 2):
                self.assertTrue(row[f'材料唯一码{side}'])
                if row[f'材料代码{side}'] == 'P1':
                    pipe_codes.add(row[f'材料唯一码{side}'])
                elif row[f'材料代码{side}'] == 'E1':
                    elbow_codes.add(row[f'材料唯一码{side}'])
        self.assertEqual(len(pipe_codes), 1)
        self.assertEqual(len(elbow_codes), 2)  # Same catalogue code, distinct instances.

    def test_archive_unit_context_is_used_when_pipeline_has_no_unit(self):
        from local_analysis import parse_file, model_weld_rows
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'sample.pcf'
            path.write_text(PCF.replace('H4-262-TEST', 'L-TEST'), encoding='utf-8')
            source, model = parse_file(path, Path(directory) / 'split')[0]
            rows = model_weld_rows(model, source, 'H1-000')
            self.assertTrue(rows)
            self.assertTrue(all(row['单元号'] == 'H1-000' for row in rows))

    def test_classifier_rejects_missing_instance_identity_before_default_shop(self):
        from weld_classification import classify_pcf
        row = {'单元号':'H4-262', '焊口号':'1', '材料代码1':'P1', '材料唯一码1':''}
        with self.assertRaisesRegex(ValueError, '材料唯一码1未生成'):
            classify_pcf({}, 'unused.pcf', [row])

    def test_no_equipment_data_does_not_leak_between_runs(self):
        self.analyze_text(PCF)
        report = self.analyze_text(PCF[:PCF.index('GASKET')])
        self.assertEqual(report['welds'][0]['分类'], 'shop')
        self.assertNotEqual(report['welds'][0]['_classification_rule'], 'S-F010')

    def test_inch_coordinates_match_equipment_points_in_mm(self):
        text = PCF.replace('UNITS-CO-ORDS MM', 'UNITS-CO-ORDS INCH')
        report = self.analyze_text(text)
        self.assertEqual(report['welds'][0]['分类'], 'field')
        self.assertEqual(report['welds'][0]['_classification_rule'], 'S-F010')
