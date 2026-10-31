"""Serve the real UI/API with a local protocol fixture, never a model or external service."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sys

HOME=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HOME))
from manager import Manager,TOOLS
from codex_chat import CodexChat
from server import handler

runtime=HOME/'runtime'
root=runtime/'chat-browser-workspace'
root.mkdir(parents=True,exist_ok=True)
(root/'fixture.txt').write_text('SIMULATION: shop=2, field=1',encoding='utf-8')
config=root/TOOLS['wechat']['folder']
config.mkdir(exist_ok=True)
(config/'wechat.codex.json').write_text('{"codex_command":["codex"]}',encoding='utf-8')
manager=Manager(root,runtime/'chat-browser-fixture')
manager.codex_chat=CodexChat(manager,[sys.executable,'-u',str(Path(__file__).with_name('fake_codex_server.py'))])
token=secrets.token_urlsafe(32)
server=ThreadingHTTPServer(('127.0.0.1',8766),handler(manager,token))
session={'url':f'http://127.0.0.1:8766/api/session?token={token}'}
(runtime/'codex-fixture-session.json').write_text(json.dumps(session),encoding='utf-8')
print('Local protocol fixture: http://127.0.0.1:8766 (no model calls)',flush=True)
try:
    server.serve_forever(poll_interval=.2)
except KeyboardInterrupt:
    pass
finally:
    manager.codex_chat.close()
    server.server_close()
