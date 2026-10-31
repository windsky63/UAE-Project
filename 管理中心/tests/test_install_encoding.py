"""Regression: isolated pip must not decode Chinese requirement paths as GBK."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
spec=importlib.util.spec_from_file_location('project_install',ROOT/'setup_python312.py')
installer=importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallEncodingTests(unittest.TestCase):
    def test_isolated_installer_reads_utf8_even_when_environment_disables_it(self):
        code=('import json,sys; from pathlib import Path; '
              'print(json.dumps(dict(utf8=sys.flags.utf8_mode, '
              'content=Path(sys.argv[1]).read_text()),ensure_ascii=True))')
        result=subprocess.run(installer.isolated_python(sys.executable,'-c',code,str(ROOT/'requirements.txt')),
            env={**os.environ,'PYTHONUTF8':'0'},capture_output=True,text=True,encoding='utf-8',check=True)
        payload=json.loads(result.stdout)
        self.assertEqual(payload['utf8'],1)
        self.assertEqual(payload['content'],(ROOT/'requirements.txt').read_text(encoding='utf-8'))
