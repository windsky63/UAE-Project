import hashlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import shutil
import sys
import subprocess
import tempfile
import zipfile
from contextlib import closing

import pytest

from wechat_local.config import LocalConfig, SyncSource, WechatChat, load_config
from wechat_local.sync_files import prepare_catalog, search, read_file, members, analysis, summary


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'sync'
    (root / 'runtime/file_analysis/jobs/job/run').mkdir(parents=True)
    (root / 'downloads').mkdir()
    db = sqlite3.connect(root / 'runtime/ledger.sqlite3')
    db.execute('CREATE TABLE files(project,id,filename,path,bytes,sha256,status,active)')
    for project, identity, state, active in [('p','one','done',1), ('p','pending','pending',1),
                                           ('p','old','done',0), ('other','secret','done',1)]:
        path = root / 'downloads' / (identity + '.txt')
        path.write_text('confirmed source ' + identity, encoding='utf-8')
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)', (project,identity,path.name,
            'downloads/' + path.name, path.stat().st_size,hashlib.sha256(path.read_bytes()).hexdigest(),state,active))
    db.commit()
    db.close()
    config = LocalConfig(sync_sources=[SyncSource(id='fusion',root=str(root),projects=['p'])])
    chat = WechatChat(id='permitted',name='permitted',kind='private',sync_sources=['fusion'])
    workspace = tmp_path / 'chat'
    prepare_catalog(config,chat,workspace)
    return root,config,chat,workspace


def test_catalog_filters_project_status_and_active_and_parameterizes_queries(source):
    _,_,_,workspace = source
    result = search(workspace)
    assert result['total'] == 1
    assert result['items'][0]['filename'] == 'one.txt'
    assert search(workspace, "' OR 1=1 --")['total'] == 0
    assert search(workspace,offset=1)['items'] == []
    assert 'confirmed source one' in read_file(workspace,result['items'][0]['id'])['text']


def test_extracted_files_are_searchable_without_guessing_the_parent_zip():
    # The Windows extraction engine adds long per-job paths to this base.
    with tempfile.TemporaryDirectory(prefix='ai-') as temporary:
        _check_extracted_catalog(Path(temporary))


def _check_extracted_catalog(base):
    root,config,chat,workspace=source.__wrapped__(base)
    actual=Path(__file__).resolve().parents[2]/'FusionLive同步工具'
    (root/'app').mkdir()
    for name in ('app/business_type.py','app/archive_index.py','document_coding_rules.json'):
        shutil.copyfile(actual/name,root/name)
    archive=root/'downloads/iso-package.zip'
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('ISO/1-43-52-1167-FLC-AAE_0.pdf',b'%PDF fixture')
        z.writestr('Office/spec.docx',b'office fixture')
        z.writestr('notes/inside.txt','direct member evidence')
    with closing(sqlite3.connect(root/'runtime/ledger.sqlite3')) as db, db:
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',('p','archive',archive.name,
            'downloads/'+archive.name,archive.stat().st_size,hashlib.sha256(archive.read_bytes()).hexdigest(),'done',1))
    # This catalog test must run outside the production download-window gate.
    launcher = "import sys,runpy; from pathlib import Path; from datetime import datetime,timezone,timedelta; sys.path.insert(0,str(Path(sys.argv[1]).parent/'app')); import fusion_client; fusion_client.now=lambda:datetime(2026,11,2,12,tzinfo=timezone(timedelta(hours=8))); runpy.run_path(sys.argv.pop(1),run_name='__main__')"
    result=subprocess.run([sys.executable,'-c',launcher,str(actual/'main.py'),'analysis_worker','--database',str(root/'runtime/ledger.sqlite3'),
        '--base-dir',str(root)],capture_output=True,text=True,encoding='utf-8',timeout=20)
    assert result.returncode==0,result.stdout+result.stderr
    before=(root/'runtime/ledger.sqlite3').read_bytes()
    prepare_catalog(config,chat,workspace)
    found=search(workspace,'1167-FLC-AAE')['items']
    assert len(found)==1 and found[0]['kind']=='extracted'
    assert found[0]['business']['business_type']=='地上新建管道单线图'
    assert found[0]['archive']=='iso-package.zip'
    assert found[0]['member_path']=='ISO/1-43-52-1167-FLC-AAE_0.pdf'
    assert search(workspace,'spec.docx')['total']==1
    text=search(workspace,'inside.txt')['items'][0]
    assert read_file(workspace,text['id'])['text']=='direct member evidence'
    totals=summary(workspace)
    assert totals['downloaded_records']==2 and totals['extracted_records']==3
    assert (root/'runtime/ledger.sqlite3').read_bytes()==before
    with closing(sqlite3.connect(root/'runtime/ledger.sqlite3')) as db, db:
        db.execute("UPDATE files SET active=0 WHERE id='archive'")
    with pytest.raises(ValueError,match='version changed'):
        read_file(workspace,text['id'])
    prepare_catalog(config,chat,workspace)
    assert search(workspace,'1167-FLC-AAE')['total']==0


def test_shared_context_helper_queries_snapshot_from_different_cwd(source):
    from wechat_local.project_context import prepare_project_context
    root, config, chat, workspace = source
    project = prepare_project_context(config, chat, workspace, [str(root)])
    guide = Path(__file__).resolve().parents[1]/'.agents/skills/wechat-chat/project-qa.md'
    assert guide.read_text(encoding='utf-8') in project['instructions']
    helper = workspace/'.agents/skills/wechat-chat/sync_files.py'
    result = subprocess.run([sys.executable, '-X', 'utf8', str(helper), 'summary',
                             '--workspace', str(workspace)], cwd=root, capture_output=True,
                            text=True, encoding='utf-8', check=True)
    report = json.loads(result.stdout)
    assert report['downloaded_records'] == 1
    assert report['sources'][0]['projects'] == ['p']


def test_runner_supplies_fresh_ledger_aggregates_without_model_tool_round_trip(source, tmp_path):
    from wechat_local.codex_runner import CodexRunner
    root, config, chat, workspace = source
    fake = tmp_path / 'summary_codex.py'
    fake.write_text('''import sys,json,pathlib
payload=json.loads(sys.stdin.read().split('Task data follows:\\n')[1])
current=payload['current_project_summary']
assert current['sources'][0]['projects']==['p']
answer={'reply':str(current['available_files']),'memory_note':'历史统计需按最新台账重新核验'}
pathlib.Path(sys.argv[sys.argv.index('--output-last-message')+1]).write_text(json.dumps(answer),encoding='utf-8')
''', encoding='utf-8')
    config.codex_command = [sys.executable, str(fake)]
    config.codex_backend = 'exec'
    config.codex_auto_access_dirs = False
    config.codex_access_dirs = [str(root)]
    runner = CodexRunner(config)
    event = dict(id='1', text='当前下载文件数量', sender='alice')
    before = (root / 'runtime/ledger.sqlite3').read_bytes()
    assert runner.generate(event, chat, [], workspace).reply == '1'
    assert (root / 'runtime/ledger.sqlite3').read_bytes() == before
    with sqlite3.connect(root / 'runtime/ledger.sqlite3') as db:
        db.execute("UPDATE files SET status='done' WHERE id='pending'")
    assert runner.generate({**event, 'id':'2'}, chat, [], workspace).reply == '2'
    metrics = json.loads((workspace / 'generation-metrics.json').read_text(encoding='utf-8'))
    assert metrics['tool_calls'] == 0


def test_business_summary_uses_all_authorized_ledger_files_and_parent_rules(source):
    root,config,chat,workspace = source
    actual = Path(__file__).resolve().parents[2] / 'FusionLive同步工具'
    (root/'app').mkdir()
    for name in ('app/business_type.py', 'document_coding_rules.json'):
        shutil.copyfile(actual/name, root/name)
    with sqlite3.connect(root/'runtime/ledger.sqlite3') as db:
        db.executescript('''CREATE TABLE documents(project,id,folder_id,reference,active);
            CREATE TABLE document_files(project,document_id,folder_id,file_id,active);
            INSERT INTO documents VALUES('p','layout','folder','30201_50150_LT_HE-H4-262-13-20-001',1);
            INSERT INTO document_files VALUES('p','layout','folder','one',1);
            INSERT INTO document_files VALUES('p','layout','folder','one',1);
        ''')
        for index in range(24):
            filename = f'30201_50150_LT_HE-H1-001-16-60-100__{index:03}.pdf'
            (root/'downloads'/filename).write_bytes(b'x')
            db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?)',
                       ('p', str(index), filename, 'downloads/'+filename, 1,
                        hashlib.sha256(b'x').hexdigest(), 'done', 1))
        db.execute("INSERT INTO files VALUES('p','missing','missing.pdf','downloads/missing.pdf',1,'sha','done',1)")
    before = (root/'runtime/ledger.sqlite3').read_bytes()
    prepare_catalog(config,chat,workspace)
    result = summary(workspace)
    assert result['downloaded_records'] == 26
    assert result['available_files'] == 25 and result['missing_or_invalid'] == 1
    assert {r['business_type']: r['files'] for r in result['items']} == {
        '管道布置平面及立面图': 1, '电缆桥架布置图': 24}
    inherited = next(r for r in result['items'] if r['files']==1)
    assert inherited['classification_status'] == 'inherited'
    assert result['sources'][0]['classification_state'] == 'ready'
    assert summary(workspace, project='other')['downloaded_records'] == 0
    assert search(workspace)['total'] == 26 and len(search(workspace)['items']) == 8
    assert (root/'runtime/ledger.sqlite3').read_bytes() == before
    chat.sync_sources = []
    prepare_catalog(config,chat,workspace)
    assert summary(workspace)['items'] == []


def test_business_summary_reports_missing_rules_and_sources(source):
    _,config,chat,workspace = source
    result = summary(workspace)
    assert result['items'][0]['classification_status'] == 'unavailable'
    assert result['sources'][0]['classification_state'] == 'unavailable'
    config.sync_sources[0].root = str(workspace/'does-not-exist')
    prepare_catalog(config,chat,workspace)
    assert summary(workspace)['sources'][0]['state'] == 'source_unavailable'


def test_revoked_chat_cannot_reuse_previous_file_ids(source):
    _,config,chat,workspace = source
    identity = search(workspace)['items'][0]['id']
    chat.sync_sources = []
    prepare_catalog(config,chat,workspace)
    assert search(workspace)['total'] == 0
    with pytest.raises(ValueError,match='authorized'):
        read_file(workspace,identity)


def test_no_sources_is_empty_and_missing_source_is_reported(tmp_path):
    chat = WechatChat(id='x',name='x',kind='private')
    assert prepare_catalog(LocalConfig(),chat,tmp_path)['files'] == 0
    config = LocalConfig(sync_sources=[SyncSource(id='missing',root=str(tmp_path/'missing'),projects=['p'])])
    chat.sync_sources = ['missing']
    result = prepare_catalog(config,chat,tmp_path)
    assert result['sources'] == [dict(source='missing',state='source_unavailable')]
    assert not (tmp_path/'missing').exists()


def test_version_change_and_same_size_corruption_are_rejected(source):
    root,_,_,workspace = source
    identity = search(workspace)['items'][0]['id']
    path = root/'downloads/one.txt'
    original = path.read_bytes()
    path.write_bytes(b'x'*len(original))
    with pytest.raises(ValueError,match='hash mismatch'):
        read_file(workspace,identity)
    path.write_bytes(original)
    with sqlite3.connect(root/'runtime/ledger.sqlite3') as db:
        db.execute("UPDATE files SET active=0 WHERE id='one'")
    with pytest.raises(ValueError,match='version changed'):
        read_file(workspace,identity)


def test_ledger_paths_cannot_escape_downloads(source):
    root,config,chat,workspace = source
    (root/'secret.txt').write_text('private',encoding='utf-8')
    with sqlite3.connect(root/'runtime/ledger.sqlite3') as db:
        db.execute("UPDATE files SET path='downloads/../secret.txt' WHERE id='one'")
    prepare_catalog(config,chat,workspace)
    result = search(workspace)['items'][0]
    assert result['availability'] == 'invalid_path'
    with pytest.raises(ValueError,match='outside'):
        read_file(workspace,result['id'])


def test_zip_member_read_and_analysis_are_bounded_and_read_only(source):
    root,config,chat,workspace = source
    path = root/'downloads/pipes.zip'
    with zipfile.ZipFile(path,'w') as archive:
        archive.writestr('H4-262.pcf','PIPELINE-REFERENCE H4-262-L001\nWELD-ATTRIBUTE3 Shop weld\n')
        archive.writestr('too_large.txt',b'a'*(2*1024*1024+1))
    sha = hashlib.sha256(path.read_bytes()).hexdigest()
    with sqlite3.connect(root/'runtime/ledger.sqlite3') as db:
        db.execute('UPDATE files SET filename=?,path=?,bytes=?,sha256=? WHERE id=?',
                   ('pipes.zip','downloads/pipes.zip',path.stat().st_size,sha,'one'))
    queue_path = root/'runtime/file_analysis/queue.sqlite3'
    with sqlite3.connect(queue_path) as db:
        db.execute('CREATE TABLE jobs(key,project,file_id,expected_sha,state,result_path,error,active,updated)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                   ('job','p','one',sha,'done','jobs/job/run/analysis.json',None,1,1))
    result_path = root/'runtime/file_analysis/jobs/job/run/analysis.json'
    result_path.write_text(json.dumps(dict(groups=[dict(project='p',unit='H4-262',welds=1),
                                                   dict(project='other',secret='hidden')])),encoding='utf-8')
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    prepare_catalog(config,chat,workspace)
    identity = search(workspace)['items'][0]['id']
    assert members(workspace,identity)['total'] == 2
    assert 'Shop weld' in read_file(workspace,identity,member='H4-262.pcf')['text']
    assert len(read_file(workspace,identity,limit=4,member='H4-262.pcf')['text']) == 4
    with pytest.raises(ValueError,match='2 MiB'):
        read_file(workspace,identity,member='too_large.txt')
    result = analysis(workspace,identity)
    assert result['total'] == 1 and result['items'][0]['project'] == 'p'
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not (workspace/'H4-262.pcf').exists()
    with sqlite3.connect(queue_path) as db:
        db.execute("UPDATE jobs SET result_path='../../outside/analysis.json'")
    with pytest.raises(ValueError,match='outside'):
        analysis(workspace,identity)


def test_configuration_resolves_source_root_and_rejects_unknown_grants(tmp_path):
    path = tmp_path/'config.json'
    path.write_text(json.dumps(dict(sync_sources=[dict(id='fusion',root='../sync',projects=['p'])],
                                   chats=[dict(id='x',name='x',kind='private',sync_sources=['fusion'])])),encoding='utf-8')
    assert load_config(path).sync_sources[0].root == str((tmp_path/'../sync').resolve())
    with pytest.raises(ValueError,match='unknown sync source'):
        LocalConfig(chats=[dict(id='x',name='x',kind='private',sync_sources=['unknown'])])


def test_shared_directories_auto_discovery_manual_override_and_empty(tmp_path):
    base=tmp_path/'codex-wechat'
    base.mkdir()
    sibling=tmp_path/'project data'
    sibling.mkdir()
    path=base/'wechat.codex.json'
    path.write_text('{}',encoding='utf-8')
    assert load_config(path).codex_access_dirs==[str(sibling.resolve())]
    path.write_text(json.dumps(dict(codex_auto_access_dirs=False,codex_access_dirs=['../project data'],
                                   codex_transport='https')),encoding='utf-8')
    config=load_config(path)
    assert config.codex_access_dirs==[str(sibling.resolve())] and config.codex_transport=='https'
    path.write_text('{"codex_auto_access_dirs":false,"codex_access_dirs":[]}',encoding='utf-8')
    assert load_config(path).codex_access_dirs==[]


def test_catalog_auto_discovers_only_granted_directories_without_chat_source_mapping(source):
    root,config,chat,workspace=source
    chat.sync_sources=[]
    result=prepare_catalog(config,chat,workspace,access_dirs=[str(root)])
    assert result['files']==2 and search(workspace)['total']==2
    # The directory grant includes every project in that source, including "other".
    assert {r['project'] for r in search(workspace)['items']}=={'p','other'}
    prepare_catalog(config,chat,workspace,access_dirs=[])
    assert search(workspace)['total']==0
    chat.sync_sources=['fusion']
    prepare_catalog(config,chat,workspace,access_dirs=[])
    assert search(workspace)['total']==0


def test_real_sync_to_simulated_codex_preview(tmp_path):
    root = Path(__file__).resolve().parents[1]
    sync_python = root.parent/'.venv-analysis/Scripts/python.exe'
    if not sync_python.is_file():
        pytest.skip('Companion FusionLive analysis environment is not installed')
    spec = importlib.util.spec_from_file_location('simulate_sync_pipeline',root/'scripts/simulate_sync_pipeline.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Use a short temporary base: the companion parser adds deeply nested job paths.
    with tempfile.TemporaryDirectory(prefix='cw-') as temporary:
        output = Path(temporary)
        module.simulate(output,sync_python)
        result = json.loads((output/'simulation_result.json').read_text(encoding='utf-8'))
    assert result['source_unchanged'] and result['duplicate_events_ignored']
    assert not result['wechat_sent'] and not result['real_codex_called']
    assert result['jobs'][0]['status'] == 'preview'
    assert '预制2个、安装1个' in result['jobs'][0]['reply']
