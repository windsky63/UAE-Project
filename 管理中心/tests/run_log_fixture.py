"""Isolated log cleanup API/UI fixture; no real project logs are touched."""
from http.server import ThreadingHTTPServer
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from manager import BEIJING, Manager, TOOLS
from server import handler

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    manager = Manager(root, root / 'admin/runtime')
    for key in TOOLS:
        (manager.folder(key) / 'runtime').mkdir(parents=True)
    base = manager.folder('sync')
    output = base / 'logs/test'
    output.mkdir(parents=True)
    old = output / 'old-scheduler.log'
    old.write_text('OLD_LOG_FIXTURE\n', encoding='utf-8')
    stamp = (datetime.now(BEIJING).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=1)).timestamp()
    os.utime(old, (stamp, stamp))
    (output / 'today.log').write_text('TODAY_LOG_FIXTURE\n', encoding='utf-8')
    (output / 'progress.log').write_text(''.join(
        f'[2026-10-30 10:00:{i:02d}] 阶段：文件下载；累计完成 {i}/100 个\n' for i in range(30))+
        '[2026-10-30 10:01:00] 下载失败：保留错误证据.pdf\n',encoding='utf-8')
    state = base / 'runtime/briefing_schedule.json'
    state.write_text('{"date":"2026-10-27","phase":"skipped"}', encoding='utf-8')
    token = secrets.token_urlsafe(32)
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler(manager, token))
    def stop():
        sys.stdin.readline()
        server.shutdown()
    threading.Thread(target=stop, daemon=True).start()
    print(json.dumps({'url': f'http://127.0.0.1:{server.server_port}/api/session?token={token}'}), flush=True)
    try:
        server.serve_forever(poll_interval=.1)
    finally:
        manager.codex_chat.close()
        server.server_close()
        assert state.exists(), 'Cleanup deleted business state'
