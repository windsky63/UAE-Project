"""Offline real download/analysis -> chat bridge -> simulated Codex -> reply preview."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import sqlite3
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT.parent
SYNC = next(p for p in PROJECT.iterdir() if (p / 'app/analysis_worker.py').is_file())


def sync_stage(target):
    sys.path.insert(0, str(SYNC / 'app'))
    from fusion_client import FusionClient
    from ledger import Ledger
    from sync import download
    from analysis_worker import AnalysisQueue
    from briefing import generate

    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    (target / 'app').mkdir(exist_ok=True)
    for name in ('app/archive_index.py', 'app/business_type.py', 'document_coding_rules.json'):
        shutil.copyfile(SYNC / name, target / name)
    lines = ['UNITS-BORE MM', 'UNITS-CO-ORDS MM', 'PIPELINE-REFERENCE H4-262-SIM-L001']
    for index in range(4):
        lines += ['VALVE' if index == 3 else 'PIPE', f'    END-POINT {index * 1000} 0 0 50',
                  f'    END-POINT {(index + 1) * 1000} 0 0 50',
                  f'    ITEM-CODE {"V" if index == 3 else "P"}{index}',
                  '    DESCRIPTION VALVE BW' if index == 3 else '    DESCRIPTION PIPE BW']
        if index < 3:
            lines += ['WELD', f'    CO-ORDS {(index + 1) * 1000} 0 0 50',
                      '    SKEY WELD', '    WELD-ATTRIBUTE3 ' + ('Shop weld', 'frefab WELD', 'FIELD weld')[index]]
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('H4-262-demo.pcf', '\n'.join(lines) + '\n')
    payloads = {'zip': stream.getvalue(), 'notes': b'SIMULATED_PROJECT_ONLY: pipe class CC1L6D-FA.\n'}
    client = FusionClient('simulation', '')
    client.project_id, client.cabinet_id, client.project_name = 'simulation', 'root', 'SIMULATED_PROJECT'
    client.rendition_security = False
    ledger = Ledger(target / 'runtime/ledger.sqlite3', base_dir=target)
    try:
        ledger.root(client)
        ledger.save_page(client.project_id, 'root', 1, [dict(id='doc', hasContent='true',
                         fileName='H4-262-demo')], 1, True)
        document = ledger.one('SELECT * FROM documents')
        items = [dict(id=key, filename=filename, readAccessStatus='Available',
                      contentFormatCode=fmt, contentSize=str(len(payloads[key])))
                 for key, filename, fmt in (('zip', 'H4-262-pipelines.zip', 'zip'),
                                            ('notes', 'H4-262-notes.txt', 'txt'))]
        ledger.save_related(document, items)

        def response(*args, **kwargs):
            data = payloads[kwargs['params']['rObjectId']]
            result = Mock(headers={'Content-Type': 'application/octet-stream', 'Content-Length': str(len(data))})
            result.__enter__ = Mock(return_value=result)
            result.__exit__ = Mock(return_value=False)
            result.iter_content = lambda size: iter([data[:20], data[20:]])
            return result

        with patch('fusion_client.RUNTIME', target / 'runtime'), \
             patch.object(client, 'request', side_effect=response), patch.object(client, 'check_access_window'), \
             patch('sync.check_window'), patch('analysis_worker.ensure_background_worker'), \
             patch.object(client, 'login', side_effect=AssertionError('No real login')), \
             patch('socket.socket', side_effect=AssertionError('Network forbidden')):
            download(client, ledger)
        files = [dict(r) for r in ledger.all("SELECT id,status,path,sha256 FROM files WHERE active=1")]
        assert len(files) == 2 and all(r['status'] == 'done' for r in files)
        assert not list((target / 'downloads').rglob('*.part'))
        queue = AnalysisQueue(target / 'runtime/ledger.sqlite3', base_dir=target)
        try:
            queue.discover()
            while queue.process_next():
                pass
            assert queue.db.execute("SELECT count(*) FROM jobs WHERE active=1 AND state!='done'").fetchone()[0] == 0
        finally:
            queue.close()
        state = generate(target / 'runtime/ledger.sqlite3', target / 'briefing', use_cache=True)
        return dict(files=files, briefing=state['workbook'], downloaded=2, real_platform_login=False)
    finally:
        ledger.close()
        client.http.close()


FAKE_CODEX = r'''import json,subprocess,sys
from pathlib import Path
prompt=sys.stdin.read()
assert 'Use $wechat-chat' in prompt
assert 'default_permissions=\"project_tools_read\"' in sys.argv
helper=Path('.agents/skills/wechat-chat/sync_files.py')
def tool(*args):
 result=subprocess.run([sys.executable,str(helper),*args],capture_output=True,text=True,encoding='utf-8',check=True)
 return json.loads(result.stdout)
catalog=tool('search','--query','H4-262')
assert catalog['total']==3
member=next(r for r in catalog['items'] if r['kind']=='extracted')
assert member['filename']=='H4-262-demo.pcf'
assert 'Shop weld' in tool('read','--id',member['id'])['text']
identity=next(r['id'] for r in catalog['items'] if r['filename'].endswith('.zip'))
listing=tool('members','--id',identity)
assert listing['items'][0]['member']=='H4-262-demo.pcf'
raw=tool('read','--id',identity,'--member','H4-262-demo.pcf')
assert 'Shop weld' in raw['text'] and 'FIELD weld' in raw['text']
parsed=tool('analysis','--id',identity)
g=parsed['items'][0]
assert g['管线数']==1 and g['焊缝数']==3
assert g['预制焊缝数']==2 and g['安装焊缝数']==1
note=next(r['id'] for r in catalog['items'] if r['filename'].endswith('.txt'))
assert 'CC1L6D-FA' in tool('read','--id',note)['text']
reply=f"【本地模拟】H4-262-SIM-L001：管线{g['管线数']}条，焊口{g['焊缝数']}个，其中预制{g['预制焊缝数']}个、安装{g['安装焊缝数']}个。依据：H4-262-pipelines.zip / H4-262-demo.pcf；材料等级CC1L6D-FA来自H4-262-notes.txt。"
Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(reply,encoding='utf-8')
Path('simulation_tools.json').write_text(json.dumps({'catalog':catalog,'archive':listing,'parsed':parsed},ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps({'type':'turn.completed','usage':{'simulated':True}}))
'''


def simulate(output, sync_python):
    sys.path.insert(0, str(ROOT))
    from wechat_local.config import LocalConfig, WechatChat, SyncSource
    from wechat_local.codex_runner import CodexRunner
    from wechat_local.listener import Listener
    from wechat_local.spool import DesktopSpool
    from wechat_local.state import State

    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source = output / 'sync_tool'
    process = subprocess.run([str(sync_python), str(Path(__file__).resolve()), '--sync-stage', str(source)],
                             capture_output=True, text=True, encoding='utf-8', timeout=120)
    (output / 'sync_stage.log').write_text(process.stdout + process.stderr, encoding='utf-8')
    if process.returncode:
        raise RuntimeError('Simulated download/analysis failed; see sync_stage.log')
    stage = json.loads((source / 'stage_result.json').read_text(encoding='utf-8'))
    before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source.rglob('*')
              if p.is_file() and p.suffix != '.log'}
    fake = output / 'fake_codex.py'
    fake.write_text(FAKE_CODEX, encoding='utf-8')
    chat = WechatChat(id='simulation', name='模拟群', kind='group', reply=True, sync_sources=['fusion'])
    config = LocalConfig(chats=[chat], codex_enabled=True, allow_send=False,
                         codex_auto_access_dirs=False, codex_access_dirs=[str(source)],
                         codex_command=[sys.executable, str(fake)], codex_backend='exec', workspaces=str(output / 'chats'),
                         sync_sources=[SyncSource(id='fusion', root=str(source), projects=['simulation'])])

    class Driver:
        account_id, account_name = 'simulation-account', '模拟助手'
        def __init__(self):
            self.events = []
        def capture(self, chat_id, spool):
            spool.capture_database('simulation', self.events, {'simulation': True})
        def send(self, *args):
            raise AssertionError('Simulation must not send to WeChat')

    driver = Driver()
    spool = DesktopSpool(str(output / 'spool.sqlite3'))
    state = State(str(output / 'listener.sqlite3'))
    listener = Listener(config, driver, spool, state, CodexRunner(config))
    try:
        listener.tick()
        driver.events = [dict(id='simulation-1', chat_id=chat.id, sender='tester', direction='incoming',
            content_type='text', text='@模拟助手 查询H4-262管线的预制和安装焊口，以及材料等级。',
            historical=False, observed_at=datetime.now(timezone.utc).isoformat())]
        listener.tick()
        assert listener.active is not None
        listener.active[1].result(timeout=30)
        listener.tick()
        for _ in range(3):
            listener.tick()
        jobs = [dict(r) for r in state.db.execute('SELECT id,status,reply,usage,error FROM jobs')]
        assert len(jobs) == 1 and jobs[0]['status'] == 'preview' and jobs[0]['error'] is None
        assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in before.items())
        result = dict(simulated=True, real_codex_called=False, wechat_sent=False, network_accessed=False,
                      source_unchanged=True, duplicate_events_ignored=True, downloads=stage, jobs=jobs)
        (output / 'simulation_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        (output / 'reply_preview.txt').write_text(jobs[0]['reply'], encoding='utf-8')
        print(json.dumps(dict(result=str(output / 'simulation_result.json'), status='passed'), ensure_ascii=False))
    finally:
        listener.close()
        spool.db.close()
        state.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--sync-stage')
    parser.add_argument('--output', default=str(ROOT / 'data/simulations' / datetime.now().strftime('%Y%m%d-%H%M%S')))
    parser.add_argument('--sync-python', default=str(PROJECT / '.venv-analysis/Scripts/python.exe'))
    args = parser.parse_args()
    if args.sync_stage:
        result = sync_stage(args.sync_stage)
        (Path(args.sync_stage) / 'stage_result.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    else:
        simulate(args.output, args.sync_python)
