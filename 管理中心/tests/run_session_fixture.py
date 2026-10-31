"""Disposable local history fixture. No WeChat UI or model is launched."""
from contextlib import closing
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import sys
import tempfile
import threading

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager,writing
from server import handler

with tempfile.TemporaryDirectory() as temporary:
    root=Path(temporary);manager=Manager(root,root/'admin/runtime')
    base=manager.folder('wechat');(base/'data').mkdir(parents=True)
    (base/'wechat.codex.json').write_text(json.dumps({'chats':[{'id':'demo','name':'测试项目群'}]}),encoding='utf-8')
    with closing(sqlite3.connect(base/'data/listener.sqlite3')) as db,db:
        db.execute('CREATE TABLE jobs(id TEXT PRIMARY KEY,chat_id,event,status,reply,usage,error,created_at,updated_at)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',('q1','demo',json.dumps({'text':'测试资料提问'}),'confirmed','这是微信中的测试答复',None,None,'2026-10-30T09:00:00+00:00','2026-10-30T09:01:00+00:00'))
    with writing(manager.tasks_path) as db:
        db.execute('INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?)',('detached-test','sync','check','待核对测试操作','detached',0,None,'2026-10-30',None,'detached-test.log'))
    (manager.runtime/'detached-test.log').write_text('Preserve detached log',encoding='utf-8')
    token=secrets.token_urlsafe(24)
    server=ThreadingHTTPServer(('127.0.0.1',0),handler(manager,token))
    def stop():
        sys.stdin.readline();server.shutdown()
    threading.Thread(target=stop,daemon=True).start()
    print(json.dumps({'url':f'http://127.0.0.1:{server.server_port}/api/session?token={token}'}),flush=True)
    try:server.serve_forever(poll_interval=.1)
    finally:
        manager.codex_chat.close();server.server_close()
        assert (manager.runtime/'detached-test.log').is_file()
