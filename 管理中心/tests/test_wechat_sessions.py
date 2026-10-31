from pathlib import Path
from contextlib import closing
import json
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager
from codex_chat import CodexChat,database


class UnifiedSessionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.manager=Manager(self.root,self.root/'admin/runtime')
        self.base=self.manager.folder('wechat');(self.base/'data').mkdir(parents=True)
        (self.base/'wechat.codex.json').write_text(json.dumps({'chats':[{'id':'group','name':'项目群'}]}),encoding='utf-8')
        self.source=self.base/'data/listener.sqlite3'
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,chat_id,event,status,reply,usage,error,created_at,updated_at)')
        self.chat=CodexChat(self.manager);self.addCleanup(self.chat.close)

    def job(self,identity='job1',reply='答复',status='confirmed',updated='2026-10-30T10:00:00+00:00'):
        with closing(sqlite3.connect(self.source)) as db,db:
            db.execute('INSERT OR REPLACE INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                (identity,'group',json.dumps({'text':'检查资料'}),status,reply,None,None,'2026-10-30T09:00:00+00:00',updated))

    def test_wechat_questions_and_answers_visible_and_idempotent(self):
        self.job();row=self.chat.list()[0]
        self.assertEqual(row['source'],'wechat');self.assertIn('项目群',row['title'])
        snapshot=self.chat.snapshot(row['id']);self.assertEqual(len(snapshot['messages']),2)
        self.assertEqual(snapshot['messages'][0]['text'],'检查资料')
        self.assertIn('已确认发送',snapshot['messages'][1]['text'])
        self.assertEqual(len(self.chat.snapshot(row['id'])['messages']),2)

    def test_archived_chat_gets_new_conversation_for_new_jobs(self):
        self.job();identity=self.chat.list()[0]['id'];self.chat.session_action(identity,'archive')
        self.job('job2');active=self.chat.list()
        self.assertEqual(len(active),1);self.assertNotEqual(active[0]['id'],identity)
        self.assertEqual(self.chat.list('archived')[0]['id'],identity)

    def test_restore_then_purge_removes_content_without_resurrecting_old_jobs(self):
        self.job();identity=self.chat.list()[0]['id']
        self.chat.session_action(identity,'delete');self.chat.session_action(identity,'restore')
        self.assertEqual(self.chat.list()[0]['id'],identity)
        self.chat.session_action(identity,'delete');self.chat.session_action(identity,'purge')
        self.job(reply='后续状态变化',updated='2026-10-30T11:00:00+00:00')
        self.assertEqual(self.chat.list(),[]);self.assertEqual(self.chat.list('trash'),[])
        with database(self.chat.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],0)
        with closing(sqlite3.connect(self.source)) as db:self.assertEqual(db.execute('SELECT count(*) FROM jobs').fetchone()[0],1)
        self.job('new');self.assertEqual(len(self.chat.list()),1)

    def test_purge_requires_trash_and_wechat_is_read_only(self):
        self.job();identity=self.chat.list()[0]['id']
        with self.assertRaisesRegex(ValueError,'回收站'):self.chat.session_action(identity,'purge')
        with self.assertRaisesRegex(ValueError,'微信'):self.chat.send(identity,'发消息','request123')
        with self.assertRaisesRegex(ValueError,'微信'):self.chat.configure(identity)

    def test_web_purge_removes_messages_events_and_action_links(self):
        identity=self.chat.create()['session']['id']
        with database(self.chat.path) as db:
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?)',('m',identity,'user','secret','now'))
        self.chat.event(identity,'notice',{'message':'private'})
        self.chat.session_action(identity,'delete');self.chat.session_action(identity,'purge')
        with self.assertRaises(ValueError):self.chat.snapshot(identity)
        with database(self.chat.path) as db:self.assertEqual(db.execute('SELECT count(*) FROM events WHERE session=?',(identity,)).fetchone()[0],0)

    def test_unreadable_wechat_database_does_not_block_web_conversations(self):
        identity=self.chat.create()['session']['id']
        self.source.write_bytes(b'invalid sqlite fixture')
        self.assertIn(identity,[row['id'] for row in self.chat.list()])
        self.assertTrue(self.chat.import_warning)
        self.assertEqual(self.chat.snapshot(identity)['session']['id'],identity)
