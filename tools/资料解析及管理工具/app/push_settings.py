"""Persistent send-only settings; schedules are always interpreted in Beijing time."""

from paths import HOME, CODE
import json
import os
from pathlib import Path
from analysis_settings import validate as validate_analysis

DEFAULT = {'enabled': True, 'recipients': [], 'prepare_time_beijing': '10:40',
           'send_time_beijing': '11:00', 'text_send_time_beijing': None, 'briefing_send_time_beijing': None,
           'send_window_minutes': 5, 'uia_profile': 'wechat_push/uia_profile.json',
           'briefing_title': '管道资料同步简报', 'publish_text': True, 'publish_briefing': True,
           'test_recipient': '文件传输助手'}
DEFAULT.update(publication_start_time_beijing=None, publication_end_time_beijing=None)
DEFAULT['publish_drawings'] = True
DEFAULT['unit_text_send_time_beijing'] = None

def unit_text_flag(data,recipient):
    return data.get('recipient_publications',{}).get(recipient,{}).get('publish_unit_text',False)


def publication_window(data):
    """Legacy schedules derive their protected window from actual publication times."""
    start = data.get('publication_start_time_beijing') or min(data['prepare_time_beijing'], data['text_send_time_beijing'], data['briefing_send_time_beijing'])
    latest = max(data['text_send_time_beijing'], data['briefing_send_time_beijing'])
    if any(unit_text_flag(data,n) for n in data.get('recipients',[])):
        start=min(start,data.get('unit_text_send_time_beijing') or data['send_time_beijing'])
        latest=max(latest,data.get('unit_text_send_time_beijing') or data['send_time_beijing'])
    minute = int(latest[:2]) * 60 + int(latest[3:]) + data['send_window_minutes'] + 1
    end = data.get('publication_end_time_beijing') or f'{min(minute,1439)//60:02d}:{min(minute,1439)%60:02d}'
    return start, end


def in_publication_window(data, at):
    start, end = publication_window(data)
    current = at.strftime('%H:%M')
    return start <= current < end


def publication_flags(data):
    flags = tuple(data.get(name, data.get('enabled', True)) for name in ('publish_text', 'publish_briefing'))
    if any(not isinstance(flag, bool) for flag in flags):
        raise ValueError('定时发布开关必须为布尔值')
    return flags


def recipient_flags(data, recipient):
    """Global switches gate the independent choices for each chat."""
    overrides = data.get('recipient_publications', {})
    if not isinstance(overrides, dict):
        raise ValueError('会话发布配置必须为对象')
    for name, flags in overrides.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(flags, dict):
            raise ValueError('会话发布配置格式无效')
        if any(key not in ('publish_text', 'publish_briefing', 'publish_drawings','publish_unit_text') or not isinstance(value, bool)
               for key, value in flags.items()):
            raise ValueError('会话发布开关必须为布尔值')
    local = overrides.get(recipient, {})
    return tuple(global_flag and local.get(key, True) for key, global_flag in
                 zip(('publish_text', 'publish_briefing'), publication_flags(data)))


def active_publication_flags(data):
    flags = [recipient_flags(data, name) for name in data.get('recipients', [])]
    text, pipe = tuple(any(pair[i] for pair in flags) for i in range(2)) if flags else publication_flags(data)
    return text, pipe or any(drawing_flag(data, name) for name in data.get('recipients', []))


def drawing_flag(data, recipient):
    master = data.get('publish_drawings', True)
    if not isinstance(master, bool):
        raise ValueError('图纸简报开关必须为布尔值')
    return master and data.get('recipient_publications', {}).get(recipient, {}).get('publish_drawings', False)


def clean_title(value):
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100 or any(c in value for c in '\r\n\t'):
        raise ValueError('简报标题须为1至100字的单行文字')
    return value.strip()


def clean_test_recipient(value):
    if not isinstance(value, str) or not value.strip() or len(value) > 200 or any(c in value for c in '\r\n\t'):
        raise ValueError('测试聊天对象须为非空的完整名称')
    return value.strip()


def validate_times(data):
    for name in ('text_send_time_beijing','briefing_send_time_beijing','unit_text_send_time_beijing'):
        if data[name] is None:
            data[name] = data['send_time_beijing']
    for name in ('prepare_time_beijing','send_time_beijing','text_send_time_beijing','briefing_send_time_beijing','unit_text_send_time_beijing'):
        text = data[name]
        if not isinstance(text,str) or len(text)!=5 or text[2]!=':':
            raise ValueError('定时时间格式应为HH:MM')
        hour,minute = (int(v) for v in text.split(':'))
        if not (0<=hour<24 and 0<=minute<60):
            raise ValueError('定时时间无效')
    if data['prepare_time_beijing']>=data['briefing_send_time_beijing']:
        raise ValueError('简报生成时间必须早于管道简报发送时间')
    if type(data['send_window_minutes']) is not int or not 1<=data['send_window_minutes']<=10:
        raise ValueError('发送窗口须在1至10分钟之间')
    start, end = publication_window(data)
    for text in (start,end):
        if not isinstance(text,str) or len(text)!=5 or text[2]!=':' or not text[:2].isdigit() or not text[3:].isdigit() or not (0<=int(text[:2])<24 and 0<=int(text[3:])<60):
            raise ValueError('发布保护时段格式应为HH:MM')
    if start >= end:
        raise ValueError('发布保护结束时间必须晚于开始时间')
    protected_keys=['prepare_time_beijing','text_send_time_beijing','briefing_send_time_beijing']
    if any(unit_text_flag(data,n) for n in data.get('recipients',[])):protected_keys.append('unit_text_send_time_beijing')
    if any(not start <= data[key] < end for key in protected_keys):
        raise ValueError('生成和发送时间必须位于发布保护时段内')
    if data.get('publication_end_time_beijing'):
        last_send = max(data['text_send_time_beijing'], data['briefing_send_time_beijing'])
        if int(end[:2])*60+int(end[3:]) < int(last_send[:2])*60+int(last_send[3:])+data['send_window_minutes']:
            raise ValueError('发布保护时段必须覆盖完整发送窗口')


def load_settings(base=HOME):
    path = Path(base) / 'runtime/wechat_push.json'
    saved = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
    text, briefing = publication_flags(saved)
    data = {**DEFAULT, **saved, 'publish_text': text, 'publish_briefing': briefing, 'enabled': text or briefing}
    for name in ('text_send_time_beijing','briefing_send_time_beijing','unit_text_send_time_beijing'):
        if data[name] is None:
            data[name] = data['send_time_beijing']
    data.update(validate_analysis(data))
    data['briefing_title'] = clean_title(data['briefing_title'])
    data['test_recipient'] = clean_test_recipient(data['test_recipient'])
    if not isinstance(data['enabled'], bool) or not isinstance(data['recipients'], list):
        raise ValueError('微信推送配置格式无效')
    names = data['recipients']
    if any(not isinstance(n, str) or not n.strip() or len(n) > 200 for n in names):
        raise ValueError('接收会话必须为非空的完整名称')
    data['recipients'] = list(dict.fromkeys(n.strip() for n in names))
    recipient_flags(data, '')  # Validate even when no chats are selected.
    drawing_flag(data, '')
    data['enabled'] = any(active_publication_flags(data)) or any(unit_text_flag(data,n) for n in data.get('recipients',[]))
    known = data.get('known_chats', [])
    if not isinstance(known, list) or any(not isinstance(n, str) or not n.strip() or len(n) > 200 for n in known):
        raise ValueError('已保存会话列表格式无效')
    validate_times(data)
    return data


def save_settings(data, base=HOME):
    data = {**data, **validate_analysis(data)}
    text, briefing = publication_flags(data)
    data = {**DEFAULT, **data, 'publish_text': text, 'publish_briefing': briefing, 'enabled': text or briefing}
    validate_times(data)
    data['briefing_title'] = clean_title(data['briefing_title'])
    data['test_recipient'] = clean_test_recipient(data['test_recipient'])
    recipient_flags(data, '')
    drawing_flag(data, '')
    data['enabled'] = any(active_publication_flags(data)) or any(unit_text_flag(data,n) for n in data.get('recipients',[]))
    path = Path(base) / 'runtime/wechat_push.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)

