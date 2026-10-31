"""Real files API/UI with an isolated 32-row, multi-parent ledger fixture."""
from http.server import ThreadingHTTPServer
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
import secrets
import sys

HOME=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HOME))
from test_file_catalog import FileCatalogTests
from server import handler

fixture=FileCatalogTests()
fixture.setUp()
if os.environ.get('FILE_FIXTURE_EXTRA')=='1':
    with closing(sqlite3.connect(fixture.ledger)) as db,db:
        for index in range(120):
            db.execute('''INSERT INTO files(project,id,filename,path,bytes,status,active)
                VALUES('p',?,?,?,0,'pending',1)''',('page-'+str(index),f'pagination-{index:03d}.pcf','downloads/missing.pcf'))
token=secrets.token_urlsafe(32)
server=ThreadingHTTPServer(('127.0.0.1',8767),handler(fixture.manager,token))
session={'url':f'http://127.0.0.1:8767/api/session?token={token}'}
(HOME/'runtime/file-fixture-session.json').write_text(json.dumps(session),encoding='utf-8')
print('Isolated file ledger fixture: http://127.0.0.1:8767',flush=True)
try:
    server.serve_forever(poll_interval=.2)
except KeyboardInterrupt:
    pass
finally:
    fixture.manager.codex_chat.close()
    server.server_close()
    fixture.doCleanups()
