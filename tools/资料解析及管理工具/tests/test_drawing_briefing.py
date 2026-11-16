import json
import sqlite3
from drawing_briefing import bucket, collect, ima_scope

def test_drawing_only_chat_configuration_roundtrip(tmp_path):
    from push_settings import DEFAULT, load_settings, save_settings, active_publication_flags
    save_settings({**DEFAULT,'publish_text':False,'publish_briefing':False,'recipients':['图纸群'],
                   'recipient_publications':{'图纸群':{'publish_drawings':True}}},tmp_path)
    settings = load_settings(tmp_path)
    assert settings['enabled']
    assert active_publication_flags(settings) == (False,True)
    assert settings['recipient_publications']['图纸群']['publish_drawings']

def test_ima_responsibility_scope():
    rules = ima_scope()
    assert set(rules[0]['units']) == {'213','027','037','038','039','265','273'}
    for profession in ('管道','钢结构','安全、消防及火气设备'):
        assert bucket(profession,'H4-213',rules) == '负责范围'
        assert bucket(profession,'H2-27',rules) == '负责范围'
        assert bucket(profession,'H4-262',rules) == '其他图纸'
    assert bucket('电气','H4-213',rules) == '其他图纸'


def test_scope_matches_both_dimensions():
    rules = [{'disciplines':['管道'], 'units':['H4-262']}]
    assert bucket('管道','H4-262',rules) == '负责范围'
    assert bucket('电气','H4-262',rules) == '其他图纸'
    assert bucket('管道','H4-263',rules) == '其他图纸'
    assert bucket('管道','未识别单元',rules) == '待识别'
    assert bucket('管道','H4-262',[]) == '范围未配置'

def test_daily_dedup_and_file_events(tmp_path):
    path = tmp_path/'ledger.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE documents(project,id,folder_id,filename,reference,revision,metadata);
        CREATE TABLE document_files(project,file_id,document_id,folder_id);
        CREATE TABLE changes(project,entity,entity_id,folder_id,kind,created,id);''')
        for folder in ('a','b'):
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?)',('p','d',folder,'drawing.pdf','drawing-1','B',json.dumps({'documentType':'ISO'})))
            db.execute('INSERT INTO document_files VALUES(?,?,?,?)',('p','f','d',folder))
        for i,(entity,identifier,kind,at) in enumerate([
            ('document','d','added','2026-11-14T01:00:00+08:00'),
            ('file','f','updated','2026-11-13T17:00:00+00:00'),
            ('document','d','not_visible','2026-11-13T01:00:00+08:00')]):
            db.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?)',('p',entity,identifier,'a',kind,at,i))
    rows = collect(path,'2026-11-14',[])
    assert len(rows) == 1
    assert rows[0]['kinds'] == {'added','updated'}

def test_file_count_includes_unchanged_but_excludes_inactive(tmp_path):
    path = tmp_path/'ledger.sqlite3'
    with sqlite3.connect(path) as db:
        db.executescript('''CREATE TABLE documents(project,id,folder_id,filename,reference,revision,metadata,active);
        CREATE TABLE document_files(project,file_id,document_id,folder_id);
        CREATE TABLE changes(project,entity,entity_id,folder_id,kind,created,id);''')
        for identifier, active in [('stable',1),('gone',0),('old',0)]:
            db.execute('INSERT INTO documents VALUES(?,?,?,?,?,?,?,?)',('p',identifier,'a','drawing.pdf',identifier,'B',json.dumps({'documentType':'ISO'}),active))
        db.execute('INSERT INTO changes VALUES(?,?,?,?,?,?,?)',('p','document','gone','a','not_visible','2026-11-14T01:00:00+08:00',1))
    rows = collect(path,'2026-11-14',[])
    assert len(rows) == 2
    assert sum(r['active'] for r in rows) == 1
    assert sum('not_visible' in r['kinds'] for r in rows) == 1
    assert next(r for r in rows if r['reference']=='stable')['kinds'] == set()
