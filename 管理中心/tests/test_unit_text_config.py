from pathlib import Path
import shutil
import sys
import pytest
from manager import Manager

def test_independent_settings_commands_and_revision(tmp_path,monkeypatch):
    manager=Manager(root=tmp_path)
    app=manager.folder('analysis')/'app';app.mkdir(parents=True)
    shutil.copy2(Path(__file__).resolve().parents[2]/'tools/资料解析及管理工具/app/unit_text.py',app/'unit_text.py')
    old=manager.folder('sync')/'runtime/wechat_push.json';old.parent.mkdir(parents=True);old.write_text('{"original":true}')
    initial=manager.config('unit_text')
    assert not initial['data']['enabled'];assert not initial['snapshot']['ready']
    with pytest.raises(ValueError):manager.save_config('unit_text',initial['data'],initial['revision'])
    old.write_text('{"recipients":["unit chat","other"],"unit_text_send_time_beijing":"11:20","recipient_publications":{"unit chat":{"publish_unit_text":true}}}')
    shared=manager.config('unit_text')
    assert shared['data']['recipients']==['unit chat']
    assert shared['data']['send_time_beijing']=='11:20'
    monkeypatch.setattr(manager,'python',lambda tool:sys.executable)
    command,cwd=manager.command('analysis','unit-text')
    assert 'unit_text' in command;assert '--data-home' in command;assert cwd==manager.folder('sync')
    assert '--send' in manager.command('analysis','unit-text-send')[0]
    analysis=next(tool for tool in manager.overview()['tools'] if tool['id']=='analysis')
    assert 'unit-text' in analysis['actions']
