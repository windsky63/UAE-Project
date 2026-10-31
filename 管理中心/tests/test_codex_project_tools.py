from contextlib import closing, redirect_stdout
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import Manager
from codex_project_tools import ProjectTools, authorization
from briefing_runner import run
from daily_publications import BEIJING


class ProjectToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root, self.root/'admin/runtime')
        self.base = self.manager.folder('sync')
        (self.base/'runtime').mkdir(parents=True)
        self.config = {'wechat':{'codex_access_dirs':[str(self.base)]},
                       'sync':{'enabled':True, 'recipients':['configured-chat'], 'publish_briefing':True}}
        self.configuration = patch.object(self.manager, 'config', side_effect=lambda tool: {'data':self.config[tool]})
        self.configuration.start(); self.addCleanup(self.configuration.stop)
        self.tools = ProjectTools(self.manager, self.root/'admin/runtime/tools.sqlite3')
        self.actions = []

    def launch(self, tool, action):
        task = uuid.uuid4().hex
        self.actions.append((tool, action, task))
        with closing(sqlite3.connect(self.manager.tasks_path)) as db, db:
            db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',
                       (task, tool, action, 'fixture', 'succeeded', 0, 0, 'now', 'now', 'fixture.log'))
        return {'id':task}

    def test_current_request_only_and_negative_or_hypothetical_requests(self):
        for text in ('如何生成管道简报并发送到微信', '比如生成管道简报再发送到微信',
                     '不要生成管道简报，也不要发送到微信', '查询单线图数量'):
            with self.subTest(text=text), patch.object(self.manager,'action') as launch:
                with self.assertRaises(ValueError):
                    self.tools.execute('s','r',text,{'action':'generate_and_send'})
                launch.assert_not_called()
        self.assertTrue(authorization('请你生成最新的管道简报，然后发送到微信')['send'])
        self.assertFalse(authorization('生成管道简报，不要发微信')['send'])

    def test_authorized_job_is_idempotent_and_generation_cannot_escalate_to_send(self):
        with patch.object(self.manager,'action',side_effect=self.launch):
            first = self.tools.execute('s','r','生成最新管道简报',{'action':'generate'})
            second = self.tools.execute('s','r','生成最新管道简报',{'action':'generate'})
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(len(self.actions),1)
            with self.assertRaises(ValueError):
                self.tools.execute('s','r','生成最新管道简报',{'action':'generate_and_send'})
            with self.assertRaises(ValueError):
                self.tools.execute('other','x','',{'action':'status','task_id':first['id']})

    def test_send_uses_configured_targets_and_requires_granted_source(self):
        text='生成管道简报并发送到微信'
        with patch.object(self.manager,'action',side_effect=self.launch):
            self.tools.execute('s','r',text,{'action':'generate_and_send'})
            self.assertEqual(self.actions[0][1],'briefing-send')
            self.config['sync']['recipients']=[]
            with self.assertRaises(ValueError):
                self.tools.execute('s','r2',text,{'action':'generate_and_send'})
            self.config['wechat']['codex_access_dirs']=[]
            with self.assertRaises(ValueError):
                self.tools.execute('s','r3','生成管道简报',{'action':'generate'})
            self.assertEqual(len(self.actions),1)

    def test_no_shell_arguments_and_cancellation_marks_owned_job(self):
        with patch.object(self.manager,'action',side_effect=self.launch):
            result=self.tools.execute('s','r','生成管道简报',{'action':'generate'})
            self.tools.cancel_session('s')
            self.assertTrue((self.base/'runtime/codex_actions'/(result['id']+'.cancel')).exists())
            with self.assertRaises(ValueError):
                self.tools.execute('s','r2','生成管道简报',{'action':'generate','cmd':'anything.cmd'})

    def test_manager_constructs_fixed_noninteractive_commands(self):
        with patch.object(self.manager,'python',return_value=sys.executable):
            command, cwd=self.manager.command('sync','briefing-send')
        self.assertIn('--send', command)
        self.assertIn('briefing_runner.py',command[2])
        self.assertEqual(cwd,self.base)


class BriefingWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        (self.base/'runtime').mkdir()
        (self.base/'runtime/ledger.sqlite3').touch()
        self.job=uuid.uuid4().hex
        self.calls=[]

    def execute(self,args,**kwargs):
        self.calls.append(args)
        self.assertNotIn('shell',kwargs)
        return SimpleNamespace(returncode=0)

    def marker(self,base,day):
        return dict(generated_at=datetime.now(BEIJING).isoformat(),analysis_complete=True,
                    images=[{'path':'fixture.png','sha256':'fixture'}],workbook='fixture.xlsx')

    def result(self):
        return json.loads((self.base/'runtime/codex_actions'/(self.job+'.json')).read_text(encoding='utf-8'))

    def test_generate_then_send_in_order_with_freshness_pin(self):
        with redirect_stdout(io.StringIO()):
            code=run(self.base,self.job,True,execute=self.execute,report_loader=self.marker)
        self.assertEqual(code,0)
        self.assertEqual(self.calls[0][-1],'analysis_worker')
        self.assertIn('briefing',self.calls[1])
        self.assertIn('--expected-generation',self.calls[2])
        self.assertEqual(self.result()['phase'],'submitted')

    def test_incomplete_or_old_reports_are_never_sent(self):
        for variant in ('partial','old'):
            self.job=uuid.uuid4().hex;self.calls=[]
            def marker(base,day):
                value=self.marker(base,day)
                if variant=='partial':value['analysis_complete']=False
                else:value['generated_at']=(datetime.now(BEIJING)-timedelta(days=1)).isoformat()
                return value
            with redirect_stdout(io.StringIO()):
                self.assertEqual(run(self.base,self.job,True,execute=self.execute,report_loader=marker),1)
            self.assertEqual(len(self.calls),2)
            self.assertEqual(self.result()['phase'],'not_sent')

    def test_stop_prevents_any_command_and_does_not_clear_stop_marker(self):
        (self.base/'runtime/stop.request').touch()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run(self.base,self.job,True,execute=self.execute),1)
        self.assertEqual(self.calls,[])
        self.assertTrue((self.base/'runtime/stop.request').exists())

    def test_partial_generation_is_reported_without_sending(self):
        def marker(base,day):
            return {**self.marker(base,day),'analysis_complete':False}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(run(self.base,self.job,False,execute=self.execute,report_loader=marker),0)
        self.assertEqual(self.result()['phase'],'generated_partial')
        self.assertFalse(self.result()['sent'])

    def test_real_subprocess_chain_uses_fixture_sender_only(self):
        (self.base/'main.py').write_text(
            'from pathlib import Path\nimport sys,json,hashlib\nfrom datetime import datetime,timezone,timedelta\n'
            'base=Path(__file__).parent\n'
            'with (base/"stages.txt").open("a") as out:out.write(sys.argv[1]+"\\n")\n'
            'if sys.argv[1]=="briefing":\n'
            ' day=sys.argv[-1];folder=base/"logs"/day/"管道简报";folder.mkdir(parents=True)\n'
            ' image=folder/"fixture.png";image.write_bytes(b"fixture, not a live image")\n'
            ' state=dict(date=day,ready=True,demo=False,analysis_complete=True,generated_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),images=[dict(path=str(image.resolve()),sha256=hashlib.sha256(image.read_bytes()).hexdigest())])\n'
            ' (folder/"简报推送状态.json").write_text(json.dumps(state),encoding="utf-8")\n',encoding='utf-8')
        app=self.base/'app';app.mkdir()
        (app/'push_settings.py').write_text(
            'def load_settings(base):return dict(enabled=True,recipients=["fixture-chat"],publish_text=False,publish_briefing=True)\n',encoding='utf-8')
        (app/'wechat_delivery.py').write_text(
            'def deliver(base,marker,settings,at,cancelled,manual):\n'
            ' assert manual and not cancelled()\n'
            ' assert settings["recipients"]==["fixture-chat"]\n'
            ' (base/"fixture-sender-called.txt").write_text("fixture only")\n'
            ' return dict(complete=True,uncertain=0,submitted=1)\n',encoding='utf-8')
        package=app/'wechat_push';package.mkdir();(package/'__init__.py').touch()
        (package/'errors.py').write_text('class DeliveryNotSent(Exception):pass\n',encoding='utf-8')
        result=subprocess.run([sys.executable,'-B',str(Path(__file__).resolve().parents[1]/'briefing_runner.py'),
            '--base',str(self.base),'--job-id',self.job,'--send'],capture_output=True,text=True,encoding='utf-8',timeout=15)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual((self.base/'stages.txt').read_text().splitlines(),['analysis_worker','briefing'])
        self.assertTrue((self.base/'fixture-sender-called.txt').is_file())
        self.assertEqual(self.result()['phase'],'submitted')


if __name__=='__main__':unittest.main()
