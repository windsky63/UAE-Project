"""Old PCF location flags are audit data; embedded rules determine S/F."""
from pathlib import Path
import tempfile
import unittest

from local_analysis import analyze, classify, normalize_weld_location
from tests.test_local_analysis import PCF


class PCFWeldLocationTests(unittest.TestCase):
    def test_labels_ignore_case_and_optional_weld_word(self):
        for expected, labels in (
            ('shop', ('shop', 'Shop weld', 'SHOP WELD', 'frefab weld', 'FREFAB', 'prefab weld', 'pre-fab weld')),
            ('field', ('field', 'FiElD WeLd', 'site weld', 'SITE', 'install weld', 'INSTALL')),
        ):
            for label in labels:
                with self.subTest(label=label):
                    self.assertEqual(normalize_weld_location('  ' + label + '  '), expected)
                    self.assertEqual(classify({'_location': label}, 'none'), expected)
        for label in (None, '', 'weld', 'workshop weld', 'shopweld', 'unknown', 'shop field weld'):
            with self.subTest(label=label):
                self.assertIsNone(normalize_weld_location(label))

    def test_location_flags_do_not_override_algorithm_totals(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / 'inputs'
            inputs.mkdir()
            for label, expected in (('Shop weld', 'shop'), ('frefab weld', 'shop'),
                                    ('PREFAB WELD', 'shop'), ('field weld', 'shop'),
                                    ('site WELD', 'shop'), ('INSTALL', 'shop')):
                with self.subTest(label=label):
                    text = PCF.replace('CO-ORDS 1000 0 0 50',
                        'END-POINT 1000 0 0 50\n    END-POINT 1000 0 0 50')
                    text = text.replace('WELD-LOCATION SHOP', 'WELD-ATTRIBUTE3 ' + label)
                    (inputs / 'sample.pcf').write_text(text, encoding='utf-8')
                    groups, report, _ = analyze(root=inputs, project='p', output=root / 'out')
                    record = groups['p', 'H4-262', 'PCF']
                    self.assertEqual(record['焊缝数'], 1)
                    self.assertEqual(record['寸口量'], 2)
                    self.assertEqual(record['预制焊缝数'], int(expected == 'shop'))
                    self.assertEqual(record['安装焊缝数'], int(expected == 'field'))
                    self.assertEqual(record['预制寸口量'], 2 if expected == 'shop' else 0)
                    self.assertEqual(record['安装寸口量'], 2 if expected == 'field' else 0)
                    self.assertEqual(report['welds'][0]['分类'], expected)

    def test_attribute3_cannot_override_algorithm(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / 'inputs'
            inputs.mkdir()
            for attribute, expected in (('field weld', 'shop'), ('unknown', 'shop')):
                with self.subTest(attribute=attribute):
                    text = PCF.replace('WELD-LOCATION SHOP',
                        'WELD-LOCATION SHOP\n    WELD-ATTRIBUTE3 ' + attribute)
                    (inputs / 'sample.pcf').write_text(text, encoding='utf-8')
                    _, report, _ = analyze(root=inputs, project='p', output=root / 'out')
                    self.assertEqual(report['welds'][0]['分类'], expected)

    def test_adjacent_component_attribute_does_not_classify_weld(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            inputs = root / 'inputs'
            inputs.mkdir()
            text = PCF.replace('DESCRIPTION PIPE BW', 'DESCRIPTION PIPE BW\n    WELD-ATTRIBUTE3 Shop weld')
            text = text.replace('WELD-LOCATION SHOP', 'WELD-ATTRIBUTE3 unknown')
            (inputs / 'sample.pcf').write_text(text, encoding='utf-8')
            groups, _, _ = analyze(root=inputs, project='p', output=root / 'out')
            self.assertEqual(groups['p', 'H4-262', 'PCF']['焊缝数'], 1)
            self.assertEqual(groups['p', 'H4-262', 'PCF']['预制焊缝数'], 1)
