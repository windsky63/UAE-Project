from datetime import datetime
from pathlib import Path
import hashlib
import json
import sqlite3
from unittest.mock import Mock
import pytest
import unit_text as publication

AT=datetime(2026,11,16,11,0,tzinfo=publication.LOCAL)

@pytest.fixture
def base(tmp_path,monkeypatch):
    (tmp_path/'runtime').mkdir()
    with sqlite3.connect(tmp_path/'runtime/ledger.sqlite3') as db:
        db.execute('CREATE TABLE files(project,id,filename,path,bytes,sha256,status,active,downloaded_at,download_date_source)')
    monkeypatch.setattr('drawing_briefing.ima_scope',lambda:[dict(disciplines=['管道'],units=['213','027'],file_formats=['PDF'])])
    return tmp_path

def add(base,name,received='2026-11-16T02:00:00+00:00',active=1):
    path=base/'downloads'/name;path.parent.mkdir(exist_ok=True);path.write_bytes(name.encode())
    with sqlite3.connect(base/'runtime/ledger.sqlite3') as db:
        db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?)',('p',name,name,'downloads/'+name,path.stat().st_size,hashlib.sha256(path.read_bytes()).hexdigest(),'done',active,received,'下载完成实录'))
    return path

def test_exact_text_current_files_unit_normalization_and_beijing_day(base):
    add(base,'H4-213-old.pcf','2026-11-15T10:00:00+08:00')
    add(base,'H4-213-new.pcf','2026-11-15T18:00:00+00:00')
    add(base,'H4-213-a.pdf');add(base,'H4-213-a.sha')
    add(base,'H2-27-a.pcf');add(base,'H2-27-a.pdf','2026-11-15T10:00:00+08:00')
    add(base,'H4-213-inactive.pcf',active=0);add(base,'H4-262-outside.pcf')
    legacy=base/'logs/2026-11-16/微信推送文.txt';legacy.parent.mkdir(parents=True);legacy.write_text('original daily text')
    result=publication.generate(base,at=AT,members=[])
    assert result['ready']
    assert result['text']==('27单元PCF文件共收到1份，PDF文件共收到1份，SHA文件共收到0份；今日PCF更新数量1份，PDF更新数量0份，SHA更新数量0份。\n'
        '213单元PCF文件共收到2份，PDF文件共收到1份，SHA文件共收到1份；今日PCF更新数量1份，PDF更新数量1份，SHA更新数量1份。')
    assert legacy.read_text()=='original daily text'
    assert publication.snapshot(base,'2026-11-16')['text']==result['text']

def test_archive_members_inherit_receipt_date_and_deduplicate_output_path(base):
    parent=add(base,'H4-213-model.zip')
    cache=base/'runtime/file_analysis';cache.mkdir()
    with sqlite3.connect(cache/'queue.sqlite3') as db:
        db.execute('CREATE TABLE jobs(key,project,file_id,expected_sha,state,active,updated)')
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?)',('job','p',parent.name,hashlib.sha256(parent.read_bytes()).hexdigest(),'done',1,0))
    path=base/'runtime/file_analysis/member.pcf';path.write_text('pcf')
    member=dict(project='p',parent_file_id=parent.name,filename='H4-213-a.pcf',path=str(path),availability='ready',member_path='H4-213-a.pcf',job_key='job',origin='archive!/H4-213-a.pcf')
    result=publication.generate(base,at=AT,members=[member,dict(member)])
    assert result['ready'];assert result['counts']['213']['total']['PCF']==1;assert result['counts']['213']['today']['PCF']==1

def test_unknown_date_and_unregistered_archive_are_not_ready(base):
    add(base,'H4-213-a.pcf',None);add(base,'H4-213-model.zip')
    result=publication.generate(base,at=AT,members=[])
    assert not result['ready'];assert '下载日期' in result['reason'];assert '压缩包' in result['reason']

def test_independent_manual_send_uses_confirmed_snapshot_and_daily_receipt(base):
    add(base,'H4-213-a.pcf');report=publication.generate(base,at=AT,members=[])
    sender=Mock();settings={**publication.DEFAULT,'recipients':['unit chat']}
    assert publication.run_once(base,at=AT,settings=settings,sender_factory=lambda:sender)['state']=='disabled'
    with pytest.raises(ValueError,match='已变化'):
        publication.run_once(base,at=AT,manual=True,expected_sha='bad',settings=settings,sender_factory=lambda:sender)
    assert publication.run_once(base,at=AT,manual=True,expected_sha=report['sha256'],settings=settings,sender_factory=lambda:sender)['submitted']==1
    sender.send.assert_called_once_with('unit chat',report['text'])
    assert publication.run_once(base,at=AT,manual=True,expected_sha=report['sha256'],settings=settings,sender_factory=lambda:sender)['submitted']==0
    assert not (base/'runtime/wechat_delivery/deliveries.sqlite3').exists()

def test_uncertain_send_is_not_repeated(base):
    add(base,'H4-213-a.sha');report=publication.generate(base,at=AT,members=[])
    sender=Mock();sender.send.side_effect=RuntimeError('submission uncertain')
    settings={**publication.DEFAULT,'recipients':['unit chat']}
    result=publication.run_once(base,at=AT,manual=True,expected_sha=report['sha256'],settings=settings,sender_factory=lambda:sender)
    assert result['uncertain']==1
    assert publication.run_once(base,at=AT,manual=True,expected_sha=report['sha256'],settings=settings,sender_factory=lambda:sender)['submitted']==0
    sender.send.assert_called_once()


def test_own_schedule_and_configured_wechat_adapter(base,monkeypatch):
    add(base,'H4-213-a.pcf')
    (base/'runtime/unit_text.json').write_text(json.dumps({**publication.DEFAULT,'enabled':True,'recipients':['unit scheduled chat'],'send_time_beijing':'11:20'}),encoding='utf-8')
    (base/'runtime/wechat_push.json').write_text(json.dumps({'recipients':['unit scheduled chat','original chat'],'unit_text_send_time_beijing':'11:20',
        'recipient_publications':{'unit scheduled chat':{'publish_unit_text':True},'original chat':{'publish_unit_text':False}}}),encoding='utf-8')
    sender=Mock();factory=Mock(return_value=sender)
    monkeypatch.setattr('wechat_push.uia.WindowsUIASender',factory)
    assert publication.run_once(base,at=AT)['state']=='outside_window'
    factory.assert_not_called()
    due=AT.replace(minute=20)
    assert publication.run_once(base,at=due)['submitted']==1
    assert sender.send.call_args.args[0]=='unit scheduled chat'
    assert factory.call_args.args[0]==Path(publication.__file__).resolve().parent.parent/'wechat_push/uia_profile.json'
    assert publication.run_once(base,at=due)['submitted']==0
    sender.send.assert_called_once()


def test_unit_text_runs_in_shared_publication_controller(base):
    from briefing_schedule import DailyBriefing
    from push_settings import load_settings
    settings=dict(recipients=['selected','other'],publish_text=False,publish_briefing=False,publish_drawings=False,
                  unit_text_send_time_beijing='11:20',recipient_publications={'selected':{'publish_unit_text':True}})
    (base/'runtime/wechat_push.json').write_text(json.dumps(settings),encoding='utf-8')
    loaded=load_settings(base)
    assert loaded['enabled']
    assert publication.load(base)['recipients']==['selected']
    factory=Mock(return_value=Mock(poll=Mock(return_value=None)))
    controller=DailyBriefing(base,log=Mock(),async_jobs=True,job_factory=factory)
    controller.tick_unit_text(AT.replace(minute=20),loaded)
    assert factory.call_args.args[1]=='unit_text'
