"""Fixture reports only: no actual DB extraction, network, or WeChat sends."""
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from daily_publications import BEIJING, day_value, prepare_manual, snapshot
from manager import Manager


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = Manager(self.root, self.root / 'admin/runtime')
        self.base = self.manager.folder('sync')
        self.day = day_value()
        self.folder = self.base / 'logs' / self.day
        self.report = self.folder / '管道简报'
        self.report.mkdir(parents=True)
        self.generated = datetime.now(BEIJING).isoformat()
        self.settings = dict(recipients=['text-chat', 'report-chat'], publish_text=True, publish_briefing=True,
                             recipient_publications={'text-chat':dict(publish_briefing=False),
                                                     'report-chat':dict(publish_text=False)})
        self.config = self.base / 'runtime/wechat_push.json'
        self.config.parent.mkdir(parents=True)
        self.config.write_text(json.dumps(self.settings), encoding='utf-8')
        self.text = 'fixture daily text'
        raw = self.text.encode('utf-8-sig')
        (self.folder / '微信推送文.txt').write_bytes(raw)
        self.text_marker = dict(date=self.day, generated_at=self.generated, ready=True,
                                text_file='微信推送文.txt', text_sha256=hashlib.sha256(raw).hexdigest())
        self.image = self.report / 'fixture.png'
        self.image.write_bytes(b'fixture image')
        self.report_marker = dict(date=self.day, ready=True, demo=False, generated_at=self.generated,
                                  images=[dict(path=str(self.image),sha256=hashlib.sha256(self.image.read_bytes()).hexdigest())])
        self.write_markers()

    def write_markers(self):
        (self.folder / '推送状态.json').write_text(json.dumps(self.text_marker), encoding='utf-8')
        (self.report / '简报推送状态.json').write_text(json.dumps(self.report_marker), encoding='utf-8')

    def journal(self, rows):
        path = self.base / 'runtime/wechat_delivery/deliveries.sqlite3'
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as db, db:
            db.execute('CREATE TABLE deliveries(day,recipient,page,sha256,state,attempts,error)')
            db.executemany('INSERT INTO deliveries VALUES(?,?,?,?,?,?,?)', [(self.day,*row) for row in rows])
        return path

    def test_read_only_view_exposes_separate_statuses_and_valid_artifacts(self):
        before = sorted(str(p) for p in self.base.rglob('*'))
        result = self.manager.publications(self.day)
        self.assertEqual(result['text']['text'],self.text)
        self.assertEqual(result['text']['delivery_state'],'pending')
        self.assertEqual(result['briefing']['delivery_state'],'pending')
        self.assertTrue(result['briefing']['ready'])
        self.assertEqual(self.manager.artifact(result['briefing']['images'][0]['id']),self.image)
        self.assertEqual(sorted(str(p) for p in self.base.rglob('*')),before)

    def test_journal_overrides_generated_flags_and_handles_uncertain_images(self):
        self.journal([('text-chat',0,hashlib.sha256(self.text.encode()).hexdigest(),'submitted',1,None),
                      ('report-chat',1,self.report_marker['images'][0]['sha256'],'uncertain',1,'fixture interruption')])
        result = snapshot(self.base,self.day)
        self.assertEqual(result['text']['delivery_state'],'submitted')
        self.assertEqual(result['briefing']['delivery_state'],'uncertain')
        self.assertEqual(len(result['deliveries']),2)

    def test_old_version_is_not_claimed_as_current_version_sent(self):
        self.journal([('text-chat',0,'old-hash','submitted',1,None)])
        self.assertEqual(snapshot(self.base,self.day)['text']['delivery_state'],'previous_or_partial')

    def test_unreadable_journal_is_unknown_not_unsent(self):
        path = self.journal([])
        path.write_bytes(b'invalid sqlite')
        result = snapshot(self.base,self.day)
        self.assertEqual(result['text']['delivery_state'],'unknown')
        self.assertTrue(result['errors'])

    def test_manual_selection_uses_saved_chat_switches_and_needs_only_selected_files(self):
        self.image.unlink()
        marker, settings = prepare_manual(self.base,self.day,'text',self.settings)
        self.assertEqual(marker['wechat_push_text'],self.text)
        self.assertTrue(settings['publish_text'])
        self.assertFalse(settings['publish_briefing'])
        self.assertEqual(marker['images'],[])
        with self.assertRaises(FileNotFoundError):
            prepare_manual(self.base,self.day,'all',self.settings)

    def test_manual_refuses_unready_text_tampering_demo_and_old_date(self):
        self.text_marker['ready']=False
        self.write_markers()
        with self.assertRaisesRegex(ValueError,'尚未就绪'):
            prepare_manual(self.base,self.day,'text',self.settings)
        self.text_marker['ready']=True
        self.write_markers()
        (self.folder / '微信推送文.txt').write_text('tampered',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'校验失败'):
            prepare_manual(self.base,self.day,'text',self.settings)
        self.report_marker['demo']=True
        self.write_markers()
        with self.assertRaisesRegex(ValueError,'模拟'):
            prepare_manual(self.base,self.day,'briefing',self.settings)
        yesterday=(datetime.now(BEIJING).date()-timedelta(days=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'当天'):
            self.manager.push_publications(yesterday,'text')

    def test_no_enabled_recipients_or_invalid_date_never_launches(self):
        with patch('manager.subprocess.Popen') as launch:
            for value in ('../escape','2026-02-30','2026-1-1'):
                with self.assertRaises(ValueError):
                    self.manager.publications(value)
            with self.assertRaises(ValueError):
                prepare_manual(self.base,self.day,'text',{**self.settings,'publish_text':False})
            launch.assert_not_called()

    def test_manual_jobs_are_registered_and_duplicate_push_is_blocked(self):
        process = Mock(pid=123, stdin=None)
        process.poll.return_value = None
        with patch.object(self.manager,'python',return_value=sys.executable), \
             patch('manager.subprocess.Popen',return_value=process) as launch:
            task=self.manager.push_publications(self.day,'text')
            self.assertEqual(task['state'],'running')
            command=launch.call_args.args[0]
            self.assertEqual(command[2],str(Path(__file__).resolve().parents[1]/'publication_runner.py'))
            self.assertEqual(command[-1],'text')
            with self.assertRaisesRegex(ValueError,'正在运行'):
                self.manager.push_publications(self.day,'briefing')
            self.assertEqual(launch.call_count,1)

    def test_worker_records_lifecycle_using_stub_sender_modules(self):
        app=self.base/'app'
        app.mkdir()
        (app/'push_settings.py').write_text('def load_settings(base):\n    return '+repr(self.settings)+'\n',encoding='utf-8')
        (app/'wechat_delivery.py').write_text(
            'import json\n'
            'def deliver(base,marker,settings,at,cancelled=None,manual=False):\n'
            '    assert manual is True\n'
            '    assert settings["publish_text"] and not settings["publish_briefing"]\n'
            '    assert json.loads(marker.read_text(encoding="utf-8"))["wechat_push_text"] == "fixture daily text"\n'
            '    return dict(complete=True,submitted=1,already_submitted=0,failed=0,uncertain=0,errors=[])\n',encoding='utf-8')
        package=app/'wechat_push'
        package.mkdir()
        (package/'__init__.py').touch()
        (package/'errors.py').write_text('class DeliveryNotSent(Exception):\n    pass\n',encoding='utf-8')
        worker=Path(__file__).resolve().parents[1]/'publication_runner.py'
        child=subprocess.run([sys.executable,'-B',str(worker),'--base',str(self.base),
                              '--date',self.day,'--kind','text'],capture_output=True,timeout=15)
        self.assertEqual(child.returncode,0,child.stderr)
        status=json.loads((self.report/'手动推送状态.json').read_text(encoding='utf-8'))
        self.assertEqual(status['phase'],'submitted')
        self.assertIn('started_at',status)
        self.assertIn('ended_at',status)
        self.assertEqual(snapshot(self.base,self.day)['manual']['result']['submitted'],1)


if __name__ == '__main__':
    unittest.main()
