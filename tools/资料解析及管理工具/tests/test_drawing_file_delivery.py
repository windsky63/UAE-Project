from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from unittest.mock import Mock
import pytest
from drawing_file_delivery import run_once,DEFAULT,LOCAL,candidates,journal,validate
from wechat_push.errors import DeliveryNotSent

@pytest.fixture
def setup(tmp_path):
    runtime=tmp_path/'runtime';runtime.mkdir()
    with sqlite3.connect(runtime/'ledger.sqlite3') as db:
        db.executescript("""CREATE TABLE documents(project,id,folder_id,first_id,reference,revision,filename,metadata,active);
        CREATE TABLE files(project,id,filename,path,bytes,sha256,revision,relation,status,active);
        CREATE TABLE document_files(project,document_id,folder_id,file_id,active);""")
    settings={**DEFAULT,'enabled':True,'recipients':['chat'],'initial_policy':'baseline'}
    return tmp_path,settings,datetime(2026,11,15,11,30,tzinfo=LOCAL)

def add(base,version='v1',revision='A',data=b'pdf',name='drawing.pdf'):
    folder=base/'downloads';folder.mkdir(exist_ok=True);file=folder/name;file.write_bytes(data)
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute('UPDATE documents SET active=0')
        db.execute('UPDATE files SET active=0')
        db.execute('UPDATE document_files SET active=0')
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,1)',('p',version,'folder','family','30201_50150_LT_HE-H4-027-13-00-001',revision,name,json.dumps(dict(documentType='DWG',isLatest=True))))
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,1)',('p',version+'-content',name,'downloads/'+name,len(data),hashlib.sha256(data).hexdigest(),revision,'original','done'))
        db.execute('INSERT INTO document_files VALUES(?,?,?,?,1)',('p',version,'folder',version+'-content'))
    return file

def run(setup,sender,**kwargs):
    base,settings,at=setup
    return run_once(base,settings=settings,at=at,sender_factory=lambda:sender,**kwargs)

def test_first_baseline_then_only_updated_files_with_platform_versions(setup):
    base,settings,at=setup;add(base);sender=Mock()
    assert run(setup,sender)['baseline']==1
    sender.send_file.assert_not_called()
    assert run(setup,sender)['submitted']==0
    add(base,'v2','B',b'new')
    setup=(base,settings,at+timedelta(days=1))
    assert run(setup,sender)['submitted']==1
    sender.send_file.assert_called_once()
    assert '修订号：B' in sender.send.call_args.args[1]
    assert '版本ID：v2' in sender.send.call_args.args[1]
    assert run(setup,sender)['submitted']==0

def test_baseline_and_independent_chats(setup):
    base,settings,_=setup;add(base);settings.update(recipients=['one','two']);sender=Mock()
    assert run(setup,sender)['baseline']==2
    add(base,'v2','B',b'updated')
    assert run(setup,sender)['submitted']==2
    assert [c.args[0] for c in sender.send_file.call_args_list]==['one','two']

def test_schedule_outside_window_never_sends(setup):
    base,settings,at=setup;add(base);sender=Mock()
    result=run_once(base,settings=settings,at=at-timedelta(minutes=1),sender_factory=lambda:sender)
    assert result['submitted']==0;sender.send_file.assert_not_called()

def test_uncertain_metadata_does_not_resend_file(setup):
    base,settings,_=setup;add(base);sender=Mock();run(setup,sender);add(base,'v2','B',b'new');sender.send.side_effect=RuntimeError('failed after file')
    assert run(setup,sender)['uncertain']==1
    assert run(setup,sender,scheduled=False)['submitted']==0
    sender.send_file.assert_called_once()
    with closing(journal(base)) as db:assert db.execute("SELECT COUNT(*) FROM versions WHERE state='uncertain'").fetchone()[0]==1

def test_revised_platform_version_sends_even_with_same_content(setup):
    base,settings,_=setup;add(base);sender=Mock();run(setup,sender)
    add(base,'v2','B')
    assert run(setup,sender)['submitted']==1

def test_changed_file_during_staging_prevents_send(setup):
    base,settings,_=setup;add(base);sender=Mock();run(setup,sender);file=add(base,'v2','B');file.write_bytes(b'BAD')
    assert run(setup,sender)['failed']==1
    sender.send_file.assert_not_called()

def test_missing_file_does_not_create_false_delivery(setup):
    base,settings,_=setup;file=add(base);file.unlink()
    assert candidates(base)=={}


def add_same_name_attachment(base, content_id='another-content', data=b'another pdf'):
    path=base/'downloads/drawing-2.pdf'
    path.write_bytes(data)
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,1)',
            ('p',content_id,'drawing.pdf','downloads/drawing-2.pdf',len(data),
             hashlib.sha256(data).hexdigest(),'A','original','done'))
        db.execute('INSERT INTO document_files VALUES(?,?,?,?,1)',('p','v1','folder',content_id))


def test_same_version_same_name_pdfs_are_independent_attachments(setup):
    base,settings,_=setup
    add(base)
    add_same_name_attachment(base)
    rows=candidates(base)
    assert len(rows)==2
    assert {row['content_id'] for row in rows.values()}=={'v1-content','another-content'}
    assert len({row['source_path'] for row in rows.values()})==2
    sender=Mock()
    assert run(setup,sender)['baseline']==2
    sender.send_file.assert_not_called()


def test_attachment_slot_changes_do_not_resend_existing_or_uncertain_contents(setup):
    base,settings,_=setup
    add(base)
    sender=Mock()
    run(setup,sender)
    with closing(journal(base)) as db,db:
        db.execute("UPDATE versions SET state='uncertain'")
    add_same_name_attachment(base)
    assert run(setup,sender,scheduled=False)['submitted']==1
    sender.send_file.assert_called_once()
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute("UPDATE files SET active=0 WHERE id='v1-content'")
    assert run(setup,sender,scheduled=False)['submitted']==0
    sender.send_file.assert_called_once()


def test_conflicting_active_document_versions_are_still_rejected(setup):
    base,_,_=setup
    add(base)
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute("INSERT INTO documents SELECT project,'v2',folder_id,first_id,reference,'B',filename,metadata,active FROM documents")
        db.execute("INSERT INTO document_files VALUES('p','v2','folder','v1-content',1)")
    with pytest.raises(ValueError,match='图纸当前版本存在歧义'):
        candidates(base)

def test_cancelled_task_never_initializes_sender(setup):
    base,settings,_=setup;add(base);sender=Mock()
    run(setup,sender,cancelled=lambda:True)
    sender.send_file.assert_not_called()

def test_invalid_schedule_rejected():
    with pytest.raises(ValueError):validate(dict(send_time_beijing='25:00'))


def test_new_chat_gets_baseline_before_scheduled_time(setup):
    base,settings,at=setup;add(base);sender=Mock()
    earlier=(base,settings,at-timedelta(hours=1))
    assert run(earlier,sender)['baseline']==1
    add(base,'v2','B',b'new')
    assert run(setup,sender)['submitted']==1

def test_outside_window_with_existing_chat_does_not_scan_all_files(setup):
    from unittest.mock import patch
    base,settings,at=setup;add(base);sender=Mock();run(setup,sender)
    with patch('drawing_file_delivery.candidates',side_effect=AssertionError('unnecessary full scan')):
        result=run_once(base,settings=settings,at=at+timedelta(hours=1),sender_factory=lambda:sender)
    assert result['state']=='outside_window'

def test_interrupted_submission_marked_uncertain_and_not_repeated(setup):
    base,settings,_=setup;add(base);sender=Mock();run(setup,sender)
    with closing(journal(base)) as db,db:db.execute("UPDATE versions SET state='sending_file'")
    run(setup,sender,scheduled=False)
    sender.send_file.assert_not_called()
    with closing(journal(base)) as db:assert db.execute("SELECT COUNT(*) FROM versions WHERE state='uncertain'").fetchone()[0]==1


def test_proven_not_sent_can_retry_next_day(setup):
    base,settings,at=setup;add(base);sender=Mock();run(setup,sender)
    add(base,'v2','B',b'updated')
    sender.send_file.side_effect=DeliveryNotSent('desktop busy before submission')
    assert run(setup,sender)['failed']==1
    sender.send_file.side_effect=None
    assert run((base,settings,at+timedelta(days=1)),sender)['submitted']==1


def test_managed_desktop_worker_accepts_file_operation_without_real_wechat(monkeypatch,tmp_path):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import io
    import wechat_push.desktop_guard as guard
    sender=Mock()
    factory=Mock(return_value=sender)
    module=SimpleNamespace(WindowsUIASender=factory)
    records=[]
    monkeypatch.setenv('PROJECT_MANAGER_HOME',str(tmp_path))
    monkeypatch.setattr(guard,'DesktopMutex',lambda:nullcontext())
    monkeypatch.setattr(guard,'InputProtection',lambda *args:nullcontext(SimpleNamespace(mode='fixture')))
    monkeypatch.setattr(guard,'require_unpaused',lambda:None)
    monkeypatch.setattr(guard.importlib,'import_module',lambda name:module)
    monkeypatch.setattr(guard,'emit',lambda state,**kwargs:records.append(state))
    request=dict(module='shared_services.publication_sender',operation='send_file',profile='profile.json',name='fixture-chat',value='fixture.pdf',expected_sha='fixture-sha')
    monkeypatch.setattr(guard.sys,'stdin',io.StringIO(json.dumps(request)))
    guard.worker()
    sender.send_file.assert_called_once_with('fixture-chat','fixture.pdf',expected_sha='fixture-sha')
    assert 'success' in records


def test_sending_window_crosses_midnight_without_duplicate_delivery(setup):
    base,settings,at=setup;add(base);sender=Mock()
    settings['send_time_beijing']='23:55'
    run_once(base,settings=settings,at=at,sender_factory=lambda:sender)
    add(base,'v2','B',b'updated')
    midnight=(at+timedelta(days=1)).replace(hour=0,minute=2)
    result=run_once(base,settings=settings,at=midnight,sender_factory=lambda:sender)
    assert result['submitted']==1
    assert run_once(base,settings=settings,at=midnight+timedelta(minutes=1),sender_factory=lambda:sender)['state']=='already_checked'
    sender.send_file.assert_called_once()

def test_interrupted_send_is_recovered_even_outside_sending_window(setup):
    base,settings,at=setup;add(base);sender=Mock();run(setup,sender)
    with closing(journal(base)) as db,db:
        db.execute("UPDATE versions SET state='file_submitted'")
    run_once(base,settings=settings,at=at+timedelta(hours=2),sender_factory=lambda:sender)
    with closing(journal(base)) as db:
        assert db.execute('SELECT state FROM versions').fetchone()[0]=='uncertain'
    sender.send_file.assert_not_called()


def test_first_enable_cannot_request_bulk_delivery():
    with pytest.raises(ValueError):
        validate(dict(initial_policy='send_existing'))


def test_responsibility_scope_is_loaded_dynamically(setup, monkeypatch):
    base,_,_=setup;add(base)
    scope={'disciplines':['管道'],'units':['027']}
    monkeypatch.setattr('drawing_briefing.ima_scope',lambda:[scope])
    assert len(candidates(base))==1
    scope['units']=['213']
    assert candidates(base)=={}
    scope['units']=['027'];scope['disciplines']=['钢结构']
    assert candidates(base)=={}


def test_custom_scope_and_empty_types_do_not_widen_global_scope(setup):
    base,settings,_=setup;add(base)
    settings.update(scope_mode='custom',scope={'disciplines':['管道'],'units':['27']})
    assert len(candidates(base,settings=settings))==1
    settings['drawing_types']=[]
    assert candidates(base,settings=settings)=={}
    settings['recipient_types']={'chat':['管道标准图']}
    assert candidates(base,settings=settings)=={}
    settings['drawing_types']=None
    settings['scope']['units']=[]
    assert candidates(base,settings=settings)=={}


def test_per_chat_types_filter_baseline_and_updated_deliveries(setup):
    base,settings,_=setup;add(base)
    other=base/'downloads/general.pdf';other.write_bytes(b'general drawing')
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,1)',
            ('p','general','folder','general-family','30201_50150_LT_HE-H4-027-13-12-001','A','general.pdf','{}'))
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,1)',
            ('p','general-content','general.pdf','downloads/general.pdf',other.stat().st_size,
             hashlib.sha256(other.read_bytes()).hexdigest(),'A','original','done'))
        db.execute('INSERT INTO document_files VALUES(?,?,?,?,1)',('p','general','folder','general-content'))
    settings.update(recipients=['标准图群','通用图群','不发送群'],
                    recipient_types={'标准图群':['管道标准图'],'通用图群':['管道通用图'],'不发送群':[]})
    sender=Mock()
    assert run(setup,sender)['baseline']==2
    with closing(journal(base)) as db:
        rows=db.execute('SELECT chat,payload FROM versions').fetchall()
        assert {(r['chat'],json.loads(r['payload'])['drawing_type']) for r in rows}=={
            ('标准图群','管道标准图'),('通用图群','管道通用图')}
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute("UPDATE documents SET revision='B'")
    assert run(setup,sender,scheduled=False)['submitted']==2
    assert [c.args[0] for c in sender.send_file.call_args_list]==['标准图群','通用图群']


def test_scope_expansion_baselines_existing_files_then_delivers_future_updates(setup):
    base,settings,_=setup;add(base)
    settings['recipient_types']={'chat':[]}
    sender=Mock()
    assert run(setup,sender)['baseline']==0
    settings['recipient_types']={}
    assert run(setup,sender,scheduled=False)['baseline']==1
    sender.send_file.assert_not_called()
    add(base,'v2','B',b'new')
    assert run(setup,sender,scheduled=False)['submitted']==1


def test_group_excluded_types_do_not_block_delivery_with_irrelevant_conflicts(setup):
    base,settings,_=setup;add(base)
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute("INSERT INTO documents SELECT project,'v2',folder_id,first_id,reference,'B',filename,metadata,active FROM documents")
        db.execute("INSERT INTO document_files VALUES('p','v2','folder','v1-content',1)")
    settings['recipient_types']={'chat':['管道通用图']}
    assert candidates(base,settings=settings)=={}


def test_per_chat_disciplines_and_types_must_both_match(setup):
    base,settings,_=setup;add(base)
    settings.update(recipients=['管道群','钢结构群','空专业群'],
        recipient_types={name:['管道标准图'] for name in ['管道群','钢结构群','空专业群']},
        recipient_disciplines={'管道群':['管道'],'钢结构群':['钢结构'],'空专业群':[]})
    sender=Mock()
    assert run(setup,sender)['baseline']==1
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute("UPDATE documents SET revision='B'")
    assert run(setup,sender,scheduled=False)['submitted']==1
    assert sender.send_file.call_args.args[0]=='管道群'
    settings['scope_mode']='custom'
    settings['scope']={'disciplines':['钢结构'],'units':['027']}
    assert candidates(base,settings=settings)=={}


def test_legacy_type_restrictions_keep_inherited_disciplines():
    old=validate({'recipients':['chat'],'recipient_types':{'chat':['管道标准图']}})
    assert old['recipient_disciplines']=={}
    assert old['recipient_types']=={'chat':['管道标准图']}


def test_type_catalog_preserves_discipline_associations():
    from drawing_delivery_settings import drawing_type_catalog
    catalog=drawing_type_catalog()
    assert '管道标准图' in catalog['管道']
    assert '管道标准图' not in catalog['钢结构']
    assert '地下非承压土建管线单线图' in catalog['土建']


def test_shared_type_switch_applies_to_delivery_and_briefing(setup,monkeypatch):
    from drawing_briefing import bucket
    base,settings,_=setup;add(base)
    scope={'disciplines':['管道'],'units':['027'],'drawing_types':['管道通用图']}
    monkeypatch.setattr('drawing_briefing.ima_scope',lambda:[scope])
    assert candidates(base,settings=settings)=={}
    assert bucket('管道','H4-027',[scope],'管道标准图')=='其他图纸'
    scope['drawing_types']=['管道标准图']
    assert len(candidates(base,settings=settings))==1
    assert bucket('管道','H4-027',[scope],'管道标准图')=='负责范围'
    settings.update(scope_mode='custom',scope={'disciplines':['管道'],'units':['027']})
    scope['drawing_types']=[]
    assert candidates(base,settings=settings)=={}


def test_duplicate_delivery_global_types_migrate_to_chat_limits():
    data=validate({'recipients':['one','two'],'drawing_types':['管道标准图'],
                   'recipient_types':{'two':['管道通用图']}})
    assert data['drawing_types'] is None
    assert data['recipient_types']=={'one':['管道标准图'],'two':[]}


def test_pdf_default_and_group_unit_format_intersection(setup,monkeypatch):
    base,settings,_=setup;add(base)
    cad=base/'downloads/drawing.dwg';cad.write_bytes(b'cad')
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,1)',('p','cad-content','drawing.dwg','downloads/drawing.dwg',3,hashlib.sha256(b'cad').hexdigest(),'A','original','done'))
        db.execute('INSERT INTO document_files VALUES(?,?,?,?,1)',('p','v1','folder','cad-content'))
    scope={'disciplines':['管道'],'units':['027']}
    monkeypatch.setattr('drawing_briefing.ima_scope',lambda:[scope])
    assert {r['file_format'] for r in candidates(base,settings=settings).values()}=={'PDF'}
    scope['file_formats']=['PDF','DWG']
    assert len(candidates(base,settings=settings))==2
    settings['recipient_formats']={'chat':['DWG']}
    settings['recipient_units']={'chat':['27']}
    assert {r['file_format'] for r in candidates(base,settings=settings).values()}=={'DWG'}
    sender=Mock();assert run(setup,sender)['baseline']==1
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db,db:db.execute("UPDATE documents SET revision='B'")
    assert run(setup,sender,scheduled=False)['submitted']==1
    assert sender.send_file.call_args.args[1].suffix=='.dwg'
    settings['recipient_units']={'chat':['213']}
    assert candidates(base,settings=settings)=={}
    settings['recipient_units']={'chat':['027']}
    scope['file_formats']=['PDF']
    assert candidates(base,settings=settings)=={}


@pytest.mark.parametrize('extra',[
    {'scope_mode':'all'}, {'scope':{'disciplines':['管道'],'units':['all']}},
    {'drawing_types':'管道标准图'}, {'recipient_types':{'unknown-chat':['管道标准图']}},
    {'recipient_types':{'chat':'管道标准图'}},
    {'recipient_disciplines':{'unknown-chat':['管道']}},
    {'recipient_disciplines':{'chat':'管道'}},
    {'recipient_units':{'chat':['all']}}, {'recipient_formats':{'chat':['EXE']}},
])
def test_invalid_scope_configuration_is_rejected(extra):
    with pytest.raises(ValueError):validate({'recipients':['chat'],**extra})
