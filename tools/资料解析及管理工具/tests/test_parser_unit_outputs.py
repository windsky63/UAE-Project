"""Real format parsers retain their exported layout and tables after sharing output."""
import csv
import json
from pathlib import Path
import tempfile
import unittest

import parse_idf_to_json as idf
import parse_pcf_to_json as pcf
from tests.test_local_analysis import IDF, PCF


class ParserUnitOutputTests(unittest.TestCase):
    def write_inputs(self, root, kind):
        source = root / 'inputs'
        for unit in ('H4-262', 'H2-233'):
            folder = source / unit
            folder.mkdir(parents=True)
            for index in (1, 2):
                text = (PCF if kind == 'pcf' else IDF).replace('L001', f'L00{index}')
                (folder / f'line-{index}.{kind}').write_text(text, encoding='utf-8')
        return source

    def test_real_parsers_preserve_manifest_models_and_format_specific_tables(self):
        for kind, parser in (('pcf', pcf), ('idf', idf)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = self.write_inputs(root, kind)
                output = root / 'public' / f'{kind}-units'
                output.mkdir(parents=True)
                (output / 'stale.json').write_text('old generation', encoding='utf-8')
                manifest_path, welds, materials = (root / name for name in
                                                  ('manifest.json', 'welds.csv', 'materials.csv'))
                manifest = parser.write_unit_outputs(source, output, manifest_path, 'Project', welds, materials)
                self.assertEqual(json.loads(manifest_path.read_text(encoding='utf-8')), manifest)
                self.assertEqual(manifest['sourceType'], kind + '-unit-directory')
                self.assertEqual((manifest['unitCount'], manifest['pipelineCount']), (2, 4))
                self.assertEqual([u['unitName'] for u in manifest['units']], ['H2-233', 'H4-262'])
                self.assertFalse((output / 'stale.json').exists())
                for unit in manifest['units']:
                    self.assertEqual((unit['componentCount'], unit['weldCount']),
                                     (8, 4) if kind == 'pcf' else (2, 0))
                    paths = [unit['modelUrl'], *(p['modelUrl'] for p in unit['pipelines'])]
                    for url in paths:
                        model = json.loads((root / 'public' / url.lstrip('/')).read_text(encoding='utf-8'))
                        if kind == 'pcf':
                            self.assertEqual(model['sourceType'], 'pcf')
                        else:
                            self.assertNotEqual(model.get('sourceType'), 'pcf')
                # Keep PCF's own derived-table filtering and project-wide counts.
                with welds.open(encoding='utf-8-sig', newline='') as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), 5 if kind == 'pcf' else 0)
                with materials.open(encoding='utf-8-sig', newline='') as stream:
                    self.assertEqual(len(list(csv.DictReader(stream))), 2 if kind == 'pcf' else 0)

    def test_both_formats_refuse_to_clear_an_unapproved_output_directory(self):
        for kind, parser in (('pcf', pcf), ('idf', idf)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = self.write_inputs(root, kind)
                output = root / 'keep-this-directory'
                output.mkdir()
                preserved = output / 'keep.txt'
                preserved.write_bytes(b'preserve existing files')
                with self.assertRaisesRegex(ValueError, 'Refuse to clear unsafe'):
                    parser.write_unit_outputs(source, output, root / 'manifest.json', 'Project', None, None)
                self.assertEqual(preserved.read_bytes(), b'preserve existing files')

    def test_empty_input_clears_previous_generation_without_touching_siblings(self):
        for kind, parser in (('pcf', pcf), ('idf', idf)):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                source = root / 'inputs'; source.mkdir()
                output = root / f'{kind}-units'
                stale = output / 'old-unit' / 'unit.json'
                stale.parent.mkdir(parents=True); stale.write_text('{}', encoding='utf-8')
                sibling = root / 'keep.json'; sibling.write_bytes(b'unchanged')
                manifest = parser.write_unit_outputs(source, output, root / 'manifest.json', 'Project', None, None)
                self.assertEqual((manifest['unitCount'], manifest['pipelineCount'], manifest['units']), (0, 0, []))
                self.assertEqual(list(output.iterdir()), [])
                self.assertEqual(sibling.read_bytes(), b'unchanged')
