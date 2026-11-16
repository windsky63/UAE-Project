"""Remote protocol, real subprocess cleanup and optional POSIX worker checks."""
from dataclasses import replace
import hashlib
import json
import os
import shutil
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
import sys,json,hashlib,pathlib,time,os,stat
root=pathlib.Path(json.loads(sys.stdin.readline())['root'])
root.mkdir(exist_ok=True)
def metadata(s):
 return [s.st_dev+101,s.st_ino+202,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns]
print('{"ready":1}',flush=True)
for line in sys.stdin:
 r=json.loads(line)
 if r.get('op')=='ensure_dirs':
  try:
   for relative in r['entries']:
    p=root/relative
    matches=[v for v in p.parent.iterdir() if v.name.casefold()==p.name.casefold()]
    if len(matches)>1:raise ValueError('CaseCollision')
    renamed=False
    if matches:
     if matches[0].is_symlink() or not matches[0].is_dir():raise ValueError('NotDirectory')
     if matches[0].name!=p.name:matches[0].rename(p);renamed=True
    else:p.mkdir()
    print(json.dumps(dict(ensured=relative,renamed=renamed)),flush=True)
   print('{"ok":true,"done":true}',flush=True)
  except Exception as e:print(json.dumps(dict(ok=False,error=type(e).__name__)),flush=True)
  continue
 if r.get('op') in ('rename','archive'):
  try:
   source=root/r['relative']
   if metadata(source.lstat())!=r['stamp']:raise ValueError('EntryChanged')
   destination=(root.with_name(root.name+'.nas-archive') if r['op']=='archive' else root)/r['destination']
   if r['op']=='archive':destination.parent.mkdir(parents=True,exist_ok=True)
   if destination.exists():raise FileExistsError('DestinationExists')
   source.rename(destination)
   print(json.dumps(dict(ok=True,moved=r['relative'],destination=r['destination'])),flush=True)
  except Exception as e:print(json.dumps(dict(ok=False,error=type(e).__name__)),flush=True)
  continue
 if r.get('op')=='rmdir':
  try:
   for entry in r['entries']:
    p=root/entry['relative'];s=p.lstat()
    if not stat.S_ISDIR(s.st_mode) or metadata(s)[:2]!=entry['identity']:raise ValueError('DirectoryChanged')
    p.rmdir()
    print(json.dumps(dict(removed=entry['relative'])),flush=True)
   print('{"ok":true,"done":true}',flush=True)
  except Exception as e:
   print(json.dumps(dict(ok=False,error=type(e).__name__)),flush=True)
  continue
 if r.get('op')=='scan':
  for p in root.rglob('*'):
   s=p.lstat()
   print(json.dumps(dict(entry=p.relative_to(root).as_posix(),stamp=metadata(s))),flush=True)
  print('{"ok":true,"done":true}',flush=True)
  continue
 if r.get('op')=='stat':
  s=(root/r['relative']).lstat()
  print(json.dumps(dict(ok=True,stamp=metadata(s))),flush=True)
  continue
 if r['relative']=='wait.bin':time.sleep(30)
 try:
  data=(root/r['relative']).read_bytes()
  print(json.dumps(dict(ok=True,size=len(data),sha256=hashlib.sha256(data).hexdigest())),flush=True)
 except Exception as e:
  print(json.dumps(dict(ok=False,error=type(e).__name__)),flush=True)
'''


class RemoteHashTests(unittest.TestCase):
    def test_missing_or_blank_ssh_username_defaults_to_windsky(self):
        for username in (None, ''):
            raw = dict(enabled=True, host='nas.local', root_path='/volume1/project')
            if username is not None:
                raw['username'] = username
            self.assertEqual(RemoteHashSettings.parse(raw, self.root).username, 'windsky')

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

    def test_streamed_inventory_repeats_and_reuses_hash_session(self):
        (self.root/'空目录').mkdir()
        p=self.root/"图纸 ' $(example).pdf";p.write_bytes(b'payload')
        first=dict(self.worker.inventory())
        process=self.worker.process
        self.assertEqual(set(first),{'空目录',p.name})
        self.assertEqual(first[p.name][3],7)
        self.assertEqual(self.worker.metadata(p.name),first[p.name])
        self.assertEqual(dict(self.worker.inventory()),first)
        self.assertEqual(self.worker.hash(p.name,7),hashlib.sha256(b'payload').hexdigest())
        self.assertIs(self.worker.process,process)

    def test_inventory_cancel_with_full_queue_closes_readers_and_can_restart(self):
        for n in range(150):
            (self.root/str(n)).touch()
        stream=self.worker.inventory()
        next(stream)
        process=self.worker.process
        readers=list(self.worker.readers)
        # Let the real child fill the bounded response queue.
        deadline=time.monotonic()+3
        while not self.worker.messages.full() and time.monotonic()<deadline:
            time.sleep(.01)
        self.assertTrue(self.worker.messages.full())
        stream.close()
        self.assertIsNotNone(process.poll())
        self.assertTrue(all(not t.is_alive() for t in readers))
        self.assertEqual(len(dict(self.worker.inventory())),150)

    def test_inventory_rejects_incomplete_invalid_or_unsafe_responses(self):
        import stat
        good={'entry':'good.pdf','stamp':[1,2,stat.S_IFREG|0o644,7,4,5]}
        cases=[
            [good,None],
            [good,{'ok':False,'error':'PermissionError'}],
            [{'entry':'../escape','stamp':good['stamp']}],
            [{'entry':'good.pdf','stamp':[1,2,stat.S_IFLNK,0,0,0]}],
            [{'entry':'good.pdf','stamp':[True,2,stat.S_IFREG,0,0,0]}],
            [{'entry':'good.pdf','stamp':[1,2,stat.S_IFREG,-1,0,0]}],
        ]
        for records in cases:
            self.worker.start()
            process=self.worker.process
            messages=[json.dumps(v)+'\n' if v is not None else None for v in records]
            for value in messages:self.worker.messages.put(value)
            with self.subTest(records=records),patch.object(self.worker,'_write'),self.assertRaises(RemoteHashError):
                list(self.worker.inventory())
            self.assertIsNotNone(process.poll())

    def test_scan_timeout_and_stop_close_real_process(self):
        class Stopped(Exception):pass
        for cancel in (False,True):
            self.worker.start()
            process=self.worker.process
            self.worker.settings=replace(self.cfg,timeout=1)
            def check():
                if cancel:raise Stopped()
            self.worker.check=check
            with patch.object(self.worker,'_write'),self.assertRaises(Stopped if cancel else RemoteHashError):
                list(self.worker.inventory())
            self.assertIsNotNone(process.poll())
            self.worker.check=lambda:None

    def test_remote_size_mismatch_is_not_a_valid_proof(self):
        p = self.root/'file.pdf';p.write_bytes(b'payload')
        with self.assertRaises(RemoteHashError):self.worker.hash(p.name,1)

    def test_empty_directory_batches_keep_files_and_expected_directories(self):
        (self.root/'old/nested').mkdir(parents=True)
        (self.root/'keep').mkdir()
        (self.root/'keep/file.pdf').write_bytes(b'keep')
        for n in range(35):(self.root/f'old-{n}').mkdir()
        nodes=dict(self.worker.inventory())
        planned=[(name,stamp) for name,stamp in nodes.items() if name.startswith('old')]
        planned.sort(key=lambda node:len(node[0].split('/')),reverse=True)
        process=self.worker.process
        self.assertEqual(list(self.worker.remove_empty_directories(planned)),[n[0] for n in planned])
        self.assertIs(self.worker.process,process)
        self.assertEqual((self.root/'keep/file.pdf').read_bytes(),b'keep')
        self.assertEqual(set(dict(self.worker.inventory())),{'keep','keep/file.pdf'})

    def test_empty_directory_cleanup_rejects_nonempty_or_replaced_identity(self):
        for changed_identity in (False,True):
            p=self.root/'old';p.mkdir(exist_ok=True)
            nodes=dict(self.worker.inventory())
            stamp=nodes['old']
            if changed_identity:
                stamp=(stamp[0],stamp[1]+1,*stamp[2:])
            else:
                (p/'new.txt').write_bytes(b'external data')
            with self.subTest(changed_identity=changed_identity),self.assertRaises(RemoteHashError):
                list(self.worker.remove_empty_directories([('old',stamp)]))
            self.assertIsNone(self.worker.process)
            self.assertTrue(p.is_dir())

    def test_empty_directory_cleanup_rejects_root_and_traversal_before_sending(self):
        for name in ('','/','../outside','a/../outside'):
            with self.subTest(name=name),self.assertRaises(RemoteHashError):
                list(self.worker.remove_empty_directories([(name,(1,2,0o40755,0,0,0))]))
        self.assertIsNone(self.worker.process)

    def test_directory_mutations_use_persistent_process_and_preserve_archives(self):
        paths=['Parent',*(f'Parent/Child{i}' for i in range(40))]
        self.assertEqual(list(self.worker.ensure_directories(paths)),[(p,False) for p in paths])
        process=self.worker.process
        old=self.root/'Parent/old.pdf';old.write_bytes(b'old bytes')
        stamp=self.worker.metadata('Parent/old.pdf')
        self.worker.move('Parent/old.pdf','Parent/new.pdf',stamp)
        stamp=self.worker.metadata('Parent/new.pdf')
        self.worker.move('Parent/new.pdf','token/Parent/new.pdf',stamp,archive=True)
        archive=self.root.with_name(self.root.name+'.nas-archive')
        self.addCleanup(shutil.rmtree,archive)
        self.assertEqual((archive/'token/Parent/new.pdf').read_bytes(),b'old bytes')
        self.assertFalse((self.root/'Parent/new.pdf').exists())
        self.assertIs(self.worker.process,process)

    def test_directory_mutation_conflicts_do_not_overwrite_or_move_changed_source(self):
        source=self.root/'source';source.write_bytes(b'keep')
        target=self.root/'target';target.write_bytes(b'other')
        stamp=self.worker.metadata('source')
        with self.assertRaises(RemoteHashError):self.worker.move('source','target',stamp)
        self.assertIsNone(self.worker.process)
        self.assertEqual(source.read_bytes(),b'keep');self.assertEqual(target.read_bytes(),b'other')
        source.write_bytes(b'changed bytes')
        with self.assertRaises(RemoteHashError):self.worker.move('source','new',stamp)
        self.assertFalse((self.root/'new').exists())
        for relative in ('../outside','/outside','a/../b'):
            with self.subTest(relative=relative),self.assertRaises(RemoteHashError):
                list(self.worker.ensure_directories([relative]))

    @unittest.skipUnless(os.name=='posix','Production worker uses Linux openat/O_NOFOLLOW')
    def test_production_directory_mutations_preserve_bytes_and_reject_links(self):
        self.launch.return_value=[sys.executable,'-u','-c',WORKER]
        (self.root/'parent').mkdir()
        self.assertEqual(list(self.worker.ensure_directories(['Parent','Parent/Child'])),
                         [('Parent',True),('Parent/Child',False)])
        source=self.root/'Parent/Child/file';source.write_bytes(b'keep')
        stamp=self.worker.metadata('Parent/Child/file')
        self.worker.move('Parent/Child/file','Parent/Child/renamed',stamp)
        stamp=self.worker.metadata('Parent/Child/renamed')
        archive=self.root.with_name(self.root.name+'.nas-archive')
        self.addCleanup(shutil.rmtree,archive)
        self.worker.move('Parent/Child/renamed','token/file',stamp,archive=True)
        self.assertEqual((archive/'token/file').read_bytes(),b'keep')
        source.write_bytes(b'new')
        stamp=self.worker.metadata('Parent/Child/file')
        with self.assertRaises(RemoteHashError):
            self.worker.move('Parent/Child/file','token/file',stamp,archive=True)
        self.assertEqual(source.read_bytes(),b'new')
        self.assertEqual((archive/'token/file').read_bytes(),b'keep')
        (self.root/'link').symlink_to(self.root/'Parent',target_is_directory=True)
        with self.assertRaises(RemoteHashError):list(self.worker.ensure_directories(['link/escape']))
        self.assertFalse((self.root/'Parent/escape').exists())

    @unittest.skipUnless(os.name=='posix','Production worker uses Linux openat/O_NOFOLLOW')
    def test_production_empty_directory_cleanup_rejects_links_and_nonempty_nodes(self):
        self.launch.return_value=[sys.executable,'-u','-c',WORKER]
        (self.root/'old/nested').mkdir(parents=True)
        nodes=dict(self.worker.inventory())
        self.assertEqual(list(self.worker.remove_empty_directories([
            ('old/nested',nodes['old/nested']),('old',nodes['old'])])),['old/nested','old'])
        (self.root/'keep').mkdir()
        stamp=self.worker.metadata('keep')
        (self.root/'keep/file').write_bytes(b'keep')
        with self.assertRaises(RemoteHashError):list(self.worker.remove_empty_directories([('keep',stamp)]))
        (self.root/'link').symlink_to(self.root/'keep',target_is_directory=True)
        with self.assertRaises(RemoteHashError):list(self.worker.remove_empty_directories([('link',stamp)]))
        self.assertEqual((self.root/'keep/file').read_bytes(),b'keep')

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

    @unittest.skipUnless(os.name=='posix','Production worker uses Linux openat/O_NOFOLLOW')
    def test_production_inventory_repeats_and_rejects_links(self):
        self.launch.return_value=[sys.executable,'-u','-c',WORKER]
        (self.root/'nested/empty').mkdir(parents=True)
        p=self.root/'nested/图纸.pdf';p.write_bytes(b'payload')
        first=dict(self.worker.inventory())
        self.assertEqual(set(first),{'nested','nested/empty','nested/图纸.pdf'})
        self.assertEqual(dict(self.worker.inventory()),first)
        self.assertEqual(self.worker.metadata('nested/图纸.pdf'),first['nested/图纸.pdf'])
        (self.root/'link').symlink_to(self.root/'nested',target_is_directory=True)
        with self.assertRaises(RemoteHashError):list(self.worker.inventory())
        self.assertIsNone(self.worker.process)

    @unittest.skipUnless(os.name=='posix','Production worker uses Linux openat/O_NOFOLLOW')
    def test_production_inventory_rejects_special_nodes(self):
        self.launch.return_value=[sys.executable,'-u','-c',WORKER]
        os.mkfifo(self.root/'pipe')
        with self.assertRaises(RemoteHashError):list(self.worker.inventory())


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
