"""Remote protocol, real subprocess cleanup and optional POSIX worker checks."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from nas_remote_hash import RemoteHasher, RemoteHashSettings, RemoteHashError, WORKER
import setup_nas_ssh as setup


# A real child process supplies the same narrow JSON protocol on Windows. The
# production POSIX filesystem worker is also exercised on POSIX hosts below.
FIXTURE = '''
import sys,json,hashlib,pathlib,time
root=pathlib.Path(json.loads(sys.stdin.readline())['root'])
print('{"ready":1}',flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if r['relative']=='wait.bin':time.sleep(30)
 try:
  data=(root/r['relative']).read_bytes()
  print(json.dumps(dict(ok=True,size=len(data),sha256=hashlib.sha256(data).hexdigest())),flush=True)
 except Exception as e:
  print(json.dumps(dict(ok=False,error=type(e).__name__)),flush=True)
'''


class RemoteHashTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.cfg = RemoteHashSettings('nas.local','reader',self.root.as_posix(),timeout=5)
        self.worker = RemoteHasher(self.cfg,self.root)
        self.addCleanup(self.worker.close)
        self.command = patch.object(self.worker,'command',return_value=[sys.executable,'-u','-c',FIXTURE])
        self.launch = self.command.start()
        self.addCleanup(self.command.stop)

    def test_persistent_process_hashes_unicode_names_and_cleans_mapping_probe(self):
        p = self.root/"图纸 ' $(example).pdf"
        p.write_bytes(b'verified payload')
        self.assertEqual(self.worker.hash(p.name,p.stat().st_size),hashlib.sha256(p.read_bytes()).hexdigest())
        process = self.worker.process
        self.assertEqual(self.worker.hash(p.name,p.stat().st_size),hashlib.sha256(p.read_bytes()).hexdigest())
        self.assertIs(self.worker.process,process)
        self.assertEqual(list(self.root.glob('.fusionlive-hash-probe-*')),[])

    def test_wrong_smb_mapping_is_rejected_and_probe_cleaned(self):
        other = self.root/'other'
        other.mkdir()
        self.worker.settings = replace(self.cfg,root=other.as_posix())
        with self.assertRaises(RemoteHashError):self.worker.start()
        self.assertIsNone(self.worker.process)
        self.assertEqual(list(self.root.glob('.fusionlive-hash-probe-*')),[])

    def test_remote_size_mismatch_is_not_a_valid_proof(self):
        p = self.root/'file.pdf';p.write_bytes(b'payload')
        with self.assertRaises(RemoteHashError):self.worker.hash(p.name,1)

    def test_traversal_never_reaches_worker(self):
        for name in ('../outside','/outside','folder/../../outside','folder\\outside'):
            with self.subTest(name=name), self.assertRaises(RemoteHashError):
                self.worker._hash(name,1)

    def test_timeout_closes_real_child(self):
        self.worker.start()
        process=self.worker.process
        self.worker.settings=replace(self.cfg,timeout=1)
        try:
            with self.assertRaises(RemoteHashError):self.worker.hash('wait.bin',1)
        finally:self.worker.close()
        self.assertIsNotNone(process.poll())

    def test_stop_interrupts_wait_and_real_child_can_be_closed(self):
        self.worker.start()
        process=self.worker.process
        stop=threading.Event()
        class Stopped(Exception):pass
        def check():
            if stop.is_set():raise Stopped()
        self.worker.check=check
        timer=threading.Timer(.2,stop.set);timer.start()
        try:
            with self.assertRaises(Stopped):self.worker.hash('wait.bin',1)
        finally:
            timer.join();self.worker.close()
        self.assertIsNotNone(process.poll())

    def test_ssh_exit_is_detected(self):
        self.launch.return_value=[sys.executable,'-c','raise SystemExit(1)']
        with self.assertRaises(RemoteHashError):self.worker.start()
        self.assertIsNone(self.worker.process)

    def test_host_verification_and_noninteractive_key_auth_are_required(self):
        self.command.stop()
        with patch('nas_remote_hash.shutil.which',return_value='ssh'):
            command=self.worker.command()
        self.assertIn('StrictHostKeyChecking=yes',command)
        self.assertIn('BatchMode=yes',command)
        self.assertNotIn('StrictHostKeyChecking=no',command)
        self.assertNotIn(self.cfg.root,command[-1])  # Paths travel over stdin, not shell interpolation.

    def test_disabled_config_preserves_default_smb(self):
        self.assertIsNone(RemoteHashSettings.parse({},self.root))
        self.assertIsNone(RemoteHashSettings.parse({'enabled':False},self.root))

    def test_legacy_fallback_option_does_not_enable_smb_fallback(self):
        cfg=RemoteHashSettings.parse(dict(enabled=True,host='nas',username='windsky',
            root_path='/volume1/share',fallback_to_smb=True),self.root)
        self.assertFalse(cfg.fallback)

    def test_invalid_config_is_rejected(self):
        base=dict(enabled=True,host='nas.local',username='reader',root_path='/volume1/share')
        for change in ({'host':'-oProxyCommand=bad'},{'username':'reader;id'},
                       {'root_path':'/'},{'root_path':'/volume1/../etc'},
                       {'port':True},{'timeout_seconds':0},{'python_command':'python3;id'},
                       {'fallback_to_smb':'false'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                RemoteHashSettings.parse({**base,**change},self.root)

    @unittest.skipUnless(os.name=='posix','Production worker uses Linux openat/O_NOFOLLOW')
    def test_production_worker_hashes_and_rejects_symlinks(self):
        self.launch.return_value=[sys.executable,'-u','-c',WORKER]
        p=self.root/'file.pdf';p.write_bytes(b'payload')
        self.assertEqual(self.worker.hash(p.name,7),hashlib.sha256(b'payload').hexdigest())
        (self.root/'link.pdf').symlink_to(p)
        with self.assertRaises(RemoteHashError):self.worker.hash('link.pdf',7)


class SetupTests(unittest.TestCase):
    def test_unc_mapping_preserves_unicode_share_and_subdirectories(self):
        self.assertEqual(setup.target_parts(r'\\192.168.32.177\工程项目文件\阿联酋项目FusionLive平台资料'),
                         ('192.168.32.177','工程项目文件',['阿联酋项目FusionLive平台资料']))

    def test_setup_saves_only_verified_config_and_preserves_other_fields(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.json'
            original=json.dumps({'target_root':'unchanged','custom':{'value':1}}).encode()
            p.write_bytes(original)
            backup=setup.save_config(p,original,{'enabled':True,'username':'windsky'})
            self.assertEqual(backup.read_bytes(),original)
            raw=json.loads(p.read_text(encoding='utf-8'))
            self.assertEqual(raw['custom'],{'value':1})
            self.assertTrue(raw['remote_hash']['enabled'])
            self.assertNotIn('password',raw['remote_hash'])

    def test_concurrent_config_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'config.json';p.write_bytes(b'{"edited":true}')
            with self.assertRaises(RuntimeError):setup.save_config(p,b'{}',{})
            self.assertEqual(p.read_bytes(),b'{"edited":true}')
            self.assertEqual(list(Path(folder).glob('*.bak')),[])

    def test_first_login_inherits_keyboard_without_password_or_script_on_stdin(self):
        result=subprocess.CompletedProcess([],0,stdout='FUSIONLIVE_SSH_SETUP={"python":"/usr/bin/python3","roots":[]}\n')
        with patch.object(setup.subprocess,'run',return_value=result) as run:
            info=setup.first_login('nas',22,'windsky',Path('key'),Path('known'),'public-script')
        command=run.call_args.args[0]
        self.assertIn('BatchMode=no',command)
        self.assertIn('StrictHostKeyChecking=ask',command)
        self.assertNotIn('input',run.call_args.kwargs)
        self.assertNotIn('stdin',run.call_args.kwargs)
        self.assertEqual(command[-1],'sh -c public-script')
        self.assertNotIn('password',run.call_args.kwargs)
        self.assertEqual(info['python'],'/usr/bin/python3')

    def test_failed_login_does_not_continue_with_missing_result(self):
        result=subprocess.CompletedProcess([],255,stdout='')
        with patch.object(setup.subprocess,'run',return_value=result),self.assertRaises(RuntimeError):
            setup.first_login('nas',22,'windsky',Path('key'),Path('known'),'script')

    def test_public_key_parser_rejects_shell_content(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'key.pub';p.write_text('ssh-ed25519 AAAA;touch-bad comment',encoding='ascii')
            with self.assertRaises(ValueError):setup.public_key(p)

    def test_remote_installer_is_idempotent_and_does_not_overwrite_authorized_keys(self):
        script=setup.install_script('ssh-ed25519 AAAA','shared; $(do-not-execute)',["folder's name"])
        self.assertIn('grep -qF --',script)
        self.assertIn('>> "$HOME/.ssh/authorized_keys"',script)
        self.assertIn('restrict ssh-ed25519 AAAA',script)
        self.assertNotIn('shared; $(do-not-execute)',script)
        self.assertNotIn("folder's name",script)
        self.assertNotIn('sudo',script)

    def test_discovery_code_executes_in_real_child_without_shell_injection(self):
        import base64
        payload=base64.b64encode(json.dumps({'share':'nonexistent; $(echo BAD)','parts':[]}).encode()).decode()
        code=setup.DISCOVER.replace('PAYLOAD',repr(payload))
        result=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        info=json.loads(result.stdout.split('FUSIONLIVE_SSH_SETUP=')[1])
        self.assertEqual(info['roots'],[])
        self.assertTrue(info['python'])


if __name__=='__main__':unittest.main()
