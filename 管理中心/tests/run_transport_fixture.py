"""Isolated browser fixture for transport selection and retry display."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import shutil
import sys
import tempfile

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME))
from codex_chat import CodexChat
from manager import Manager, TOOLS
from server import handler

with tempfile.TemporaryDirectory(prefix='transport-ui-', dir=HOME/'runtime') as directory:
    root = Path(directory)
    (root/'fixture.txt').write_text('SIMULATION: shop=2, field=1', encoding='utf-8')
    config = root/TOOLS['wechat']['folder']
    config.mkdir()
    package=config/'wechat_local'
    package.mkdir()
    (package/'__init__.py').write_text('',encoding='utf-8')
    for name in ('config.py','codex_settings.py'):
        shutil.copyfile(HOME.parent/'codex-wechat/wechat_local'/name,package/name)
    (root/'shared-documents/subset').mkdir(parents=True)
    (config/'wechat.codex.json').write_text('{"codex_command":["codex"]}', encoding='utf-8')
    manager = Manager(root, root/'admin-runtime')
    manager.python=lambda tool:str(HOME.parent/'.venv-analysis/Scripts/python.exe')
    manager.codex_chat = CodexChat(manager, [sys.executable, '-u', str(Path(__file__).with_name('fake_codex_server.py'))])
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(manager, token))
    (HOME/'runtime/transport-browser-session.json').write_text(json.dumps(
        dict(url=f'http://127.0.0.1:{server.server_port}/api/session?token={token}')), encoding='utf-8')
    print('Transport browser fixture ready (no model calls)', flush=True)
    try:
        server.serve_forever(poll_interval=.2)
    except KeyboardInterrupt:
        pass
    finally:
        manager.codex_chat.close()
        server.server_close()
