from pathlib import Path
import shutil
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager


def test_drawing_delivery_settings_persist_with_revision_check(tmp_path):
    manager=Manager(root=tmp_path)
    tool=manager.folder('analysis')/'app'
    tool.mkdir(parents=True)
    source=Path(__file__).resolve().parents[2]/'tools/资料解析及管理工具/app/drawing_delivery_settings.py'
    shutil.copy2(source,tool/source.name)
    shutil.copy2(source.parents[1]/'document_coding_rules.json',tool.parent/'document_coding_rules.json')
    initial=manager.config('drawing_delivery')
    assert initial['data']['initial_policy']=='baseline'
    assert not initial['data']['enabled']
    assert '管道标准图' in initial['drawing_type_catalog']['管道']
    assert '管道标准图' not in initial['drawing_type_catalog']['钢结构']
    saved=manager.save_config('drawing_delivery',{**initial['data'],'enabled':True,'recipients':['fixture-chat'],'send_time_beijing':'13:20'},initial['revision'])
    assert saved['data']['recipients']==['fixture-chat']
    assert saved['data']['send_time_beijing']=='13:20'
    assert manager.config_path('drawing_delivery').parent==manager.folder('sync')/'runtime'
    with pytest.raises(ValueError):
        manager.save_config('drawing_delivery',saved['data'],initial['revision'])
    with pytest.raises(ValueError):
        manager.save_config('drawing_delivery',{**saved['data'],'send_time_beijing':'99:00'},saved['revision'])
    scoped=manager.save_config('drawing_delivery',{**saved['data'],
        'scope_mode':'custom','scope':{'disciplines':['管道'],'units':['27']},
        'drawing_types':['管道标准图','管道通用图'],
        'recipient_types':{'fixture-chat':['管道标准图']},
        'recipient_disciplines':{'fixture-chat':['管道']},
        'recipient_units':{'fixture-chat':['27']},'recipient_formats':{'fixture-chat':['pdf']}},saved['revision'])
    loaded=manager.config('drawing_delivery')['data']
    assert loaded==scoped['data']
    assert loaded['scope']['units']==['027']
    assert loaded['recipient_types']=={'fixture-chat':['管道标准图']}
    assert loaded['drawing_types'] is None
    assert loaded['recipient_disciplines']=={'fixture-chat':['管道']}
    assert loaded['recipient_units']=={'fixture-chat':['027']}
    assert loaded['recipient_formats']=={'fixture-chat':['PDF']}
