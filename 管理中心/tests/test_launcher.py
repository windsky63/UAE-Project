"""Exercise the shipped CMD entry point in a Chinese, spaced project path."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='admin-launch-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'中文项目 带空格'
        self.root.mkdir()
        self.cmd=self.root/'打开管理中心.cmd'
        shutil.copyfile(ROOT/'打开管理中心.cmd',self.cmd)
        shutil.copyfile(ROOT/'admin_launcher.py',self.root/'admin_launcher.py')
        self.app=self.root/'管理中心'
        (self.app/'web/dist').mkdir(parents=True)
        (self.app/'web/dist/index.html').write_text('<html></html>',encoding='utf-8')
        # Prefer the same interpreter as the test runner, without relying on PATH.
        # Use a real venv redirector, not a copied executable alone.
        import venv
        venv.EnvBuilder(with_pip=False).create(self.root/'.venv-analysis')

    def run_cmd(self,*args):
        command=f'""{self.cmd}" '+subprocess.list2cmdline(list(args))+'"'
        # CMD /s /c consumes its own outer quotes. Passing this through
        # list2cmdline again would add literal backslash-escaped quotes.
        interpreter=subprocess.list2cmdline([os.environ.get('COMSPEC','cmd.exe')])
        return subprocess.run(interpreter+' /d /s /c '+command,
            capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=20,
            env={**os.environ,'ADMIN_NO_PAUSE':'1'})

    def test_cmd_uses_ascii_and_windows_line_endings(self):
        source=self.cmd.read_bytes()
        source.decode('ascii')
        self.assertNotIn(b'\n',source.replace(b'\r\n',b''))
        self.assertIn(b'admin_launcher.py',source)

    @unittest.skipUnless(os.name=='nt','CMD integration runs on Windows')
    def test_real_cmd_passes_unicode_paths_arguments_and_utf8_environment(self):
        (self.app/'server.py').write_text(
            'import json,os,sys\nfrom pathlib import Path\n'
            'Path(__file__).with_name("launched.json").write_text(json.dumps({"args":sys.argv[1:],'
            '"utf8":os.environ.get("PYTHONUTF8"),"encoding":os.environ.get("PYTHONIOENCODING")}),encoding="utf-8")\n'
            'print("LAUNCH_OK",flush=True)\n',encoding='utf-8')
        result=self.run_cmd('--no-browser','--port','8769')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertIn('LAUNCH_OK',result.stdout)
        data=json.loads((self.app/'launched.json').read_text(encoding='utf-8'))
        self.assertEqual(data,dict(args=['--no-browser','--port','8769'],utf8='1',encoding='utf-8'))
        self.assertTrue((self.app/'runtime/launcher.log').is_file())

    @unittest.skipUnless(os.name=='nt','CMD integration runs on Windows')
    def test_missing_server_reports_error_and_nonzero_exit(self):
        result=self.run_cmd('--no-browser')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Missing server.py',result.stderr)
        self.assertIn('Missing server.py',(self.app/'runtime/launcher.log').read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main()
