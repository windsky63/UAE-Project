import json
import re
from pathlib import Path

DEFAULT=dict(enabled=False,send_time_beijing='11:30',send_window_minutes=10,
             recipients=[],initial_policy='baseline',uia_profile='wechat_push/uia_profile.json',
             scope_mode='responsibility', scope=dict(disciplines=[],units=[]),
             drawing_types=None, recipient_types={}, recipient_disciplines={}, recipient_units={}, recipient_formats={})


def drawing_type_catalog():
    path=Path(__file__).resolve().parent.parent/'document_coding_rules.json'
    if not path.is_file():return {}
    rules=json.loads(path.read_text(encoding='utf-8'))
    common={v for v in rules['prefixes'].values() if '图' in v}
    result={name:set(common) for name in rules['disciplines'].values()}
    for item in rules['document_types']:
        if '图' in item['label']:
            result[rules['disciplines'][item['discipline']]].add(item['label'])
    for code,label in rules['construction_isometrics'].items():
        result['土建' if code=='45' else '管道'].add(label)
    return {name:sorted(labels) for name,labels in result.items() if labels}


def drawing_type_options():
    path=Path(__file__).resolve().parent.parent/'document_coding_rules.json'
    if not path.is_file():return []
    rules=json.loads(path.read_text(encoding='utf-8'))
    labels={item['label'] for item in rules['document_types']}
    labels.update(rules['prefixes'].values())
    labels.update(rules['construction_isometrics'].values())
    return sorted(label for label in labels if '图' in label)


def type_list(value,label='图纸类型'):
    if value is None:return None  # Inherit all globally allowed types.
    if not isinstance(value,list) or any(not isinstance(v,str) or not v.strip() for v in value):
        raise ValueError(label+'须为名称列表；空列表表示不发送')
    return list(dict.fromkeys(v.strip() for v in value))


def validate(data):
    if not isinstance(data,dict) or set(data)-set(DEFAULT):
        raise ValueError('图纸发送配置字段无效')
    data={**DEFAULT,**data}
    if type(data['enabled']) is not bool:
        raise ValueError('启用状态须为布尔值')
    if not isinstance(data['send_time_beijing'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',data['send_time_beijing']):
        raise ValueError('发送时间须为北京时间 HH:MM')
    if type(data['send_window_minutes']) is not int or not 1<=data['send_window_minutes']<=120:
        raise ValueError('发送窗口须为1至120分钟')
    if not isinstance(data['recipients'],list) or any(not isinstance(v,str) or not v.strip() or '\n' in v for v in data['recipients']):
        raise ValueError('接收聊天须为完整名称列表')
    data['recipients']=list(dict.fromkeys(v.strip() for v in data['recipients']))
    if data['scope_mode'] not in ('responsibility','custom'):
        raise ValueError('图纸全局范围模式无效')
    scope=data['scope']
    if not isinstance(scope,dict) or set(scope)!={'disciplines','units'}:
        raise ValueError('图纸全局范围须包含专业和单元')
    if any(not isinstance(scope[k],list) or any(not isinstance(v,str) or not v.strip() for v in scope[k])
           for k in ('disciplines','units')):
        raise ValueError('全局专业和单元须为名称列表')
    if any(not re.fullmatch(r'\d{2,3}',v.strip()) for v in scope['units']):
        raise ValueError('全局单元须为两位或三位数字')
    data['scope']=dict(disciplines=list(dict.fromkeys(v.strip() for v in scope['disciplines'])),
                       units=sorted({v.strip().zfill(3) for v in scope['units']}))
    data['drawing_types']=type_list(data['drawing_types'])
    if not isinstance(data['recipient_types'],dict) or any(k not in data['recipients'] for k in data['recipient_types']):
        raise ValueError('群聊图纸类型须对应图纸接收聊天')
    data['recipient_types']={k:type_list(v) for k,v in data['recipient_types'].items()}
    if not isinstance(data['recipient_disciplines'],dict) or any(k not in data['recipients'] for k in data['recipient_disciplines']):
        raise ValueError('群聊专业须对应图纸接收聊天')
    data['recipient_disciplines']={k:type_list(v,'群聊专业') for k,v in data['recipient_disciplines'].items()}
    for key,label in [('recipient_units','群聊单元'),('recipient_formats','群聊文件格式')]:
        if not isinstance(data[key],dict) or any(k not in data['recipients'] for k in data[key]):
            raise ValueError(label+'须对应图纸接收聊天')
        data[key]={k:type_list(v,label) for k,v in data[key].items()}
    for chat,values in data['recipient_units'].items():
        if values is None:continue
        if any(not re.fullmatch(r'\d{2,3}',v) for v in values):raise ValueError('群聊单元须为两位或三位数字')
        data['recipient_units'][chat]=sorted({v.zfill(3) for v in values})
    for chat,values in data['recipient_formats'].items():
        if values is None:continue
        if any(v.upper() not in {'PDF','DWG','DXF'} for v in values):raise ValueError('群聊文件格式须为 PDF、DWG、DXF')
        data['recipient_formats'][chat]=list(dict.fromkeys(v.upper() for v in values))
    # Old releases exposed a second global type list. Retain its restrictions
    # per chat so there is no invisible, duplicate global setting after upgrade.
    if data['drawing_types'] is not None:
        for chat in data['recipients']:
            previous=data['recipient_types'].get(chat)
            data['recipient_types'][chat]=list(data['drawing_types']) if previous is None else [v for v in previous if v in data['drawing_types']]
        data['drawing_types']=None
    if data['initial_policy'] != 'baseline':
        raise ValueError('首次启用仅建立基线，不支持发送现有全部图纸')
    if not isinstance(data['uia_profile'],str) or not data['uia_profile'].strip():
        raise ValueError('微信UIA配置路径不能为空')
    return data


def load(base):
    path=Path(base)/'runtime/drawing_file_delivery.json'
    return validate(json.loads(path.read_text(encoding='utf-8')) if path.exists() else {})

