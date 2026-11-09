import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import venv

from paths import HOME


@unittest.skipUnless(os.name == 'nt','Windows CMD integration')
class MaintenanceLauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='maintenance-cmd-')
        self.addCleanup(self.temp.cleanup)
        self.project=Path(self.temp.name)/'中文 项目'
        self.tool=self.project/HOME.name
        (self.tool/'app').mkdir(parents=True)
        self.cmd=self.tool/'整理文件目录.cmd'
        shutil.copyfile(HOME/self.cmd.name,self.cmd)
        for name in ('path_maintenance_launcher.py','paths.py','runtime_logging.py'):
            shutil.copyfile(HOME/'app'/name,self.tool/'app'/name)
        (self.tool/'main.py').write_text(
            'import json,os,sys\nfrom pathlib import Path\n'
            'Path(__file__).with_name("launched.json").write_text(json.dumps({"args":sys.argv[1:],"python":sys.executable}),encoding="utf-8")\n'
            'print("MAINT_TEST_OUTPUT",flush=True)\n'
            'print("MAINT_TEST_ERROR",file=sys.stderr,flush=True)\n'
            'sys.exit(int(os.environ.get("MAINT_TEST_EXIT","0")))\n',encoding='utf-8')

    def run_cmd(self,*arguments,input='',shared=True,exit_code=0):
        if shared:
            venv.EnvBuilder(with_pip=False).create(self.project/'.venv-analysis')
        command='""'+str(self.cmd)+'" '+subprocess.list2cmdline(list(arguments))+'"'
        interpreter=subprocess.list2cmdline([os.environ.get('COMSPEC','cmd.exe')])
        return subprocess.run(interpreter+' /d /s /c '+command,input=input,capture_output=True,
            text=True,encoding='utf-8',errors='replace',timeout=20,
            env={**os.environ,'FUSION_MAINTENANCE_NO_PAUSE':'1','MAINT_TEST_EXIT':str(exit_code)})

    def record(self):
        return json.loads((self.tool/'launched.json').read_text(encoding='utf-8'))

    def log(self):
        return (self.tool/'runtime/path_maintenance_launcher.log').read_text(encoding='utf-8')

    def test_cmd_is_ascii_crlf_and_retains_pause(self):
        data=self.cmd.read_bytes();data.decode('ascii')
        self.assertNotIn(b'\n',data.replace(b'\r\n',b''))
        self.assertIn(b'\r\npause\r\n',data)
        self.assertNotIn(b'choice /',data)

    def test_preview_menu_uses_shared_interpreter_from_unicode_space_directory(self):
        result=self.run_cmd(input='1\n')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.record()['args'],['path_maintenance'])
        self.assertEqual(Path(self.record()['python']),self.project/'.venv-analysis/Scripts/python.exe')
        self.assertIn('MAINT_TEST_OUTPUT',self.log())

    def test_apply_menu_is_explicit(self):
        result=self.run_cmd(input='2\n')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.record()['args'],['path_maintenance','--apply'])

    def test_exit_menu_never_starts_maintenance(self):
        result=self.run_cmd(input='0\n')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertFalse((self.tool/'launched.json').exists())

    def test_input_eof_is_reported_as_failure_and_logged(self):
        result=self.run_cmd(input='')
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertFalse((self.tool/'launched.json').exists())
        self.assertIn('无法读取菜单选择',self.log())

    def test_arguments_and_nonzero_child_exit_are_preserved(self):
        source=str(self.project/'服务器 数据')
        result=self.run_cmd('--base-dir',source,exit_code=7)
        self.assertEqual(result.returncode,7,result.stdout+result.stderr)
        self.assertEqual(self.record()['args'],['path_maintenance','--base-dir',source])
        self.assertIn('MAINT_TEST_ERROR',self.log())
        self.assertIn('退出码：7',self.log())

    def test_missing_shared_environment_is_logged_without_system_python_fallback(self):
        result=self.run_cmd('--help',shared=False)
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertFalse((self.tool/'launched.json').exists())
        self.assertIn('Shared Python environment was not found',self.log())

    def test_missing_launcher_is_logged(self):
        (self.tool/'app/path_maintenance_launcher.py').unlink()
        result=self.run_cmd('--help')
        self.assertEqual(result.returncode,1,result.stdout+result.stderr)
        self.assertIn('Missing path_maintenance_launcher.py',self.log())
