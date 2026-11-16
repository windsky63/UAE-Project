import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from paths import HOME,CODE
from preflight import REQUIRED_FILES,RELEASE_ID


class LayoutTests(unittest.TestCase):
    def test_root_launcher_dispatches_business_rules_without_network(self):
        result=subprocess.run([sys.executable,str(HOME/'main.py'),'business_type','--filename',
            '30201_50150_LT_HE-H4-262-13-20-001.pdf'],capture_output=True,text=True,encoding='utf-8',
            env={**__import__('os').environ,'PYTHONUTF8':'1'},timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['business_type'],'管道布置平面及立面图')

    def fixture(self,root):
        files={}
        for name in REQUIRED_FILES:
            target=root/name
            target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(HOME/name,target)
            files[name]={'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'bytes':target.stat().st_size}
        (root/'release_manifest.json').write_text(json.dumps(dict(format=1,release=RELEASE_ID,files=files)),encoding='utf-8')




if __name__=='__main__':unittest.main()
