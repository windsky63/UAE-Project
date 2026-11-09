"""Read-only daily publications view, with strict local artifact validation."""
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import importlib.util

BEIJING = timezone(timedelta(hours=8))

_source = Path(__file__).resolve().parents[1]/'FusionLive同步工具/app/file_inventory.py'
_spec = importlib.util.spec_from_file_location('shared_project_file_inventory',_source)
_inventory = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_inventory)


def version_progress(database):
    """Read the latest generated snapshot; never scan directories or open SQL."""
    database = Path(database)
    try:
        base = database.parent.parent if database.parent.name=='runtime' else database.parent
        snapshot=_inventory.read_daily_snapshot(base)
        return {**snapshot['file_inventory'],'snapshot_date':snapshot['date'],
                'generated_at':snapshot['generated_at'],'snapshot_ready':snapshot['ready']}
    except (OSError,ValueError,KeyError,TypeError):
        return None


def live_text(base, *, require_ready=False):
    day=datetime.now(BEIJING).date().isoformat()
    data=_inventory.read_daily_snapshot(base,day,require_ready=require_ready)
    return data['text'],data


def day_value(value=''):
    day = value or datetime.now(BEIJING).date().isoformat()
    if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
        raise ValueError('日期格式须为 YYYY-MM-DD')
    return day


def safe_file(path, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.exists() and not path.is_symlink():
        raise FileNotFoundError('本机尚无该日文件')
    if not path.is_relative_to(root) or path.resolve() != path or not path.is_file():
        raise ValueError('文件不存在或路径不属于当天输出目录')
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or candidate.is_junction():
            raise ValueError('不允许使用链接目录中的文件')
    return path


def read_marker(path, folder):
    path = safe_file(path, folder)
    if path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('状态文件过大')
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('状态文件格式错误')
    return value


def validate_marker(marker, day):
    generated = datetime.fromisoformat(marker['generated_at'])
    if marker.get('date') != day or generated.tzinfo is None or generated.astimezone(BEIJING).date().isoformat() != day:
        raise ValueError('生成日期与所选日期不一致')
    if generated > datetime.now(BEIJING):
        raise ValueError('生成时间晚于当前时间')
    if marker.get('demo'):
        raise ValueError('模拟简报不能用于正式推送')


def load_text(base, day):
    if day==day_value():
        text,marker=live_text(base)
        return marker,None,text
    folder = Path(base) / 'logs' / day
    path=folder/'管道简报/文字发送备份.json'
    marker=read_marker(path,folder)
    validate_marker(marker,day)
    text=marker['text']
    if hashlib.sha256(text.encode('utf-8')).hexdigest()!=marker.get('sha256'):
        raise ValueError('文字发送备份校验失败')
    if not text or len(text) > 14000:
        raise ValueError('推送文为空或超过单条消息长度')
    return marker, path, text


def load_report(base, day):
    folder = Path(base) / 'logs' / day / '管道简报'
    marker = read_marker(folder / '简报推送状态.json', folder)
    validate_marker(marker, day)
    if not marker.get('ready') or not isinstance(marker.get('images'), list) or not marker['images']:
        raise ValueError('管道简报尚未生成完成')
    for image in marker['images']:
        path = safe_file(image['path'], folder)
        if path.suffix.lower() != '.png' or hashlib.sha256(path.read_bytes()).hexdigest() != image['sha256']:
            raise ValueError('简报图片校验失败')
    return marker


def selected_settings(settings, kind):
    if kind not in ('text', 'briefing', 'all'):
        raise ValueError('推送类型无效')
    # Keep saved global and per-recipient switches; manual only relaxes the clock window.
    data = dict(settings)
    for key, wanted in (('publish_text', kind in ('text', 'all')),
                        ('publish_briefing', kind in ('briefing', 'all'))):
        data[key] = bool(settings.get(key, settings.get('enabled', True))) and wanted
    return data


def targets(settings, kind):
    data = selected_settings(settings, kind)
    local = data.get('recipient_publications', {})
    result = []
    for name in data.get('recipients', []):
        flags = local.get(name, {})
        text = data['publish_text'] and flags.get('publish_text', True)
        report = data['publish_briefing'] and flags.get('publish_briefing', True)
        if text or report:
            result.append(dict(name=name, text=bool(text), briefing=bool(report)))
    return result


def snapshot(base, day='', settings=None):
    base, day = Path(base), day_value(day)
    folder = base / 'logs' / day
    errors = []
    result = dict(date=day, today=day_value(), text=dict(ready=False, text='', images=[]),
                  briefing=dict(ready=False, images=[]), schedule={}, deliveries=[], errors=errors)
    for kind in ('text', 'briefing'):
        try:
            if kind == 'text':
                marker, path, text = load_text(base, day)
                result[kind].update(ready=bool(marker.get('ready')), text=text, path=str(path),
                                    hashes=[hashlib.sha256(text.encode('utf-8')).hexdigest()])
                if not marker.get('ready'):
                    result[kind]['reason'] = '下载日报仍未完成盘点，当前为阶段性内容，暂不能手动推送'
            else:
                marker = load_report(base, day)
                result[kind].update(ready=True, images=marker['images'],
                                    workbook=marker.get('workbook'), hashes=[i['sha256'] for i in marker['images']],
                                    fallback_notice=bool(marker.get('fallback_notice')))
            result[kind]['generated_at'] = marker.get('generated_at')
        except FileNotFoundError:
            result[kind]['reason'] = '本机尚无该日文件'
        except (OSError, ValueError, KeyError, TypeError) as exc:
            result[kind]['reason'] = str(exc)
    schedule = folder / '管道简报' / '定时推送状态.json'
    if schedule.exists():
        try:
            result['schedule'] = read_marker(schedule, folder)
        except (OSError, ValueError) as exc:
            errors.append('定时状态读取失败：' + str(exc))
    manual = folder / '管道简报' / '手动推送状态.json'
    if manual.exists():
        try:
            result['manual'] = read_marker(manual, folder)
        except (OSError, ValueError) as exc:
            errors.append('手动推送状态读取失败：' + str(exc))
    journal = base / 'runtime/wechat_delivery/deliveries.sqlite3'
    journal_error = False
    if journal.exists():
        try:
            safe_file(journal, base / 'runtime/wechat_delivery')
            with closing(sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
                db.row_factory = sqlite3.Row
                result['deliveries'] = [dict(row) for row in db.execute(
                    'SELECT recipient,page,sha256,state,attempts,error FROM deliveries WHERE day=? ORDER BY recipient,page', (day,))]
        except (OSError, ValueError, sqlite3.Error) as exc:
            journal_error = True
            errors.append('投递台账读取失败：' + str(exc))
    try:
        if settings is None:
            config = base / 'runtime/wechat_push.json'
            settings = read_marker(config, base / 'runtime') if config.exists() else {}
        result['targets'] = targets(settings, 'all')
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result['targets'] = []
        errors.append('发布配置读取失败：' + str(exc))
    for kind, is_text in (('text', True), ('briefing', False)):
        content = result[kind]
        records = [row for row in result['deliveries'] if (row['page'] == 0) == is_text]
        expected = [(target['name'], page, sha) for target in result['targets'] if target['text' if is_text else 'briefing']
                    for page, sha in enumerate(content.get('hashes', []), 0 if is_text else 1)]
        lookup = {(row['recipient'], row['page']): row for row in records}
        matched = [lookup.get((name, page), {}) for name, page, sha in expected]
        if journal_error:
            state = 'unknown'
        elif any(row['state'] in ('uncertain', 'sending') for row in records):
            state = 'uncertain'
        elif any(row['state'] == 'not_sent' for row in records):
            state = 'not_sent'
        elif records and expected and all(row.get('state') == 'submitted' and row.get('sha256') == sha
                 for row, (_, _, sha) in zip(matched, expected)):
            state = 'submitted'
        elif any(row['state'] == 'submitted' for row in records):
            state = 'previous_or_partial'
        else:
            state = 'pending'
        content.update(delivery_state=state, submitted=sum(row['state'] == 'submitted' for row in records),
                       expected=len(expected))
    return result


def prepare_manual(base, day, kind, settings):
    day = day_value(day)
    if day != day_value():
        raise ValueError('手动推送仅支持北京时间当天内容，不补发历史日报')
    active = targets(settings, kind)
    if not active:
        raise ValueError('没有启用该类型推送的接收会话，请先在同步配置中设置')
    needs_text = any(t['text'] for t in active)
    needs_report = any(t['briefing'] for t in active)
    marker = load_report(base, day) if needs_report else dict(date=day, ready=True, demo=False,
                generated_at=datetime.now(BEIJING).isoformat(), images=[])
    marker = dict(marker)
    if needs_text:
        text,text_marker=live_text(base,require_ready=True)
        if not text_marker.get('ready'):
            raise ValueError('下载日报尚未就绪，未推送')
        marker.update(live_text=True)
    return marker, selected_settings(settings, kind)
