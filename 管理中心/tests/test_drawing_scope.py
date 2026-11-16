import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import Manager
from drawing_scope import validate

class DrawingScopeTests(unittest.TestCase):
    def test_save_and_revision_conflict(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            path = root/'管理中心/config/drawing_scope.json'
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({'disciplines':['管道'],'units':['213']}), encoding='utf-8')
            manager = Manager(root=root)
            before = manager.config('drawing_scope')
            saved = manager.save_config('drawing_scope', {'disciplines':['钢结构'], 'units':['27','027','213']}, before['revision'])
            self.assertEqual(saved['data'], {'disciplines':['钢结构'], 'units':['027','213'],'drawing_types':None,'file_formats':['PDF']})
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), saved['data'])
            with self.assertRaisesRegex(ValueError, '其他窗口'):
                manager.save_config('drawing_scope', before['data'], before['revision'])
            with self.assertRaises(ValueError):
                manager.save_config('drawing_scope', {'disciplines':['管道'],'units':['H4-213']}, saved['revision'])
            self.assertEqual(manager.config('drawing_scope'), saved)

    def test_invalid_and_empty_scope(self):
        for data in ({'units':[True],'disciplines':[]}, {'units':['xx'],'disciplines':[]}, {'units':[],'disciplines':'管道'}):
            with self.assertRaises(ValueError):
                validate(data)
        self.assertEqual(validate({'units':[],'disciplines':[]}), {'units':[],'disciplines':[],'drawing_types':None,'file_formats':['PDF']})

    def test_type_switches_and_old_scope_compatibility(self):
        self.assertIsNone(validate({'units':['27'],'disciplines':['管道']})['drawing_types'])
        self.assertEqual(validate({'units':['27'],'disciplines':['管道'],'drawing_types':[]})['drawing_types'],[])
        with self.assertRaises(ValueError):validate({'units':['27'],'disciplines':['管道'],'drawing_types':'all'})
