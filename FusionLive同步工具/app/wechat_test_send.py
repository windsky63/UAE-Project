"""Manual send verification using the latest local artifacts; no platform access."""

from paths import HOME, CODE
from datetime import datetime
import hashlib
import json
from pathlib import Path

from analysis_worker import worker_lock
from briefing import LOCAL, atomic_json
from wechat_push.errors import DeliveryNotSent


def digest(data):
    return hashlib.sha256(data).hexdigest()


def latest_artifacts(base):
    """Prefer other valid publications; use fixed samples only when missing."""
    base = Path(base).resolve()
    logs = base / 'logs'
    fixed = logs / 'test_push'
    texts, reports = [], []
    fallback_texts, fallback_reports = [], []
    for name, found in (('推送状态.json', texts), ('简报推送状态.json', reports)):
        for marker in logs.rglob(name):
            try:
                if not marker.resolve().is_relative_to(logs):
                    continue
                fallback = marker.resolve().is_relative_to(fixed)
                selected = (fallback_texts if name == '推送状态.json' else fallback_reports) if fallback else found
                state = json.loads(marker.read_text(encoding='utf-8'))
                generated = datetime.fromisoformat(state['generated_at'])
                if (generated.tzinfo is None or generated > datetime.now(LOCAL)
                        or generated.astimezone(LOCAL).date().isoformat() != state['date']):
                    continue
                if name == '推送状态.json':
                    path = marker.parent / '微信推送文.txt'
                    if state.get('text_file') != path.name or not path.resolve().is_relative_to(logs) or path.stat().st_size > 64000:
                        continue
                    content = path.read_bytes()
                    if digest(content) != state.get('text_sha256'):
                        continue
                    text = content.decode('utf-8-sig').strip()
                    if not text or len(text) > 14000:
                        continue
                    selected.append((generated, str(marker), {'text': text, 'date': state['date'], 'path': str(path), 'fallback': fallback}))
                else:
                    images = state.get('images')
                    if not isinstance(images, list) or not images:
                        continue
                    checked = []
                    for entry in images:
                        path = Path(entry['path'])
                        if not path.is_absolute():
                            path = (base if path.parts and path.parts[0] == 'logs' else marker.parent) / path
                        path = path.resolve()
                        if not path.is_relative_to(marker.parent.resolve()) or not path.is_relative_to(logs) or path.suffix.lower() != '.png':
                            raise ValueError('图片路径不属于简报目录')
                        if digest(path.read_bytes()) != entry['sha256']:
                            raise ValueError('简报图片校验失败')
                        checked.append({'path': str(path), 'sha256': entry['sha256']})
                    selected.append((generated, str(marker), {'images': checked, 'date': state['date'], 'marker': str(marker), 'demo': bool(state.get('demo')), 'fallback': fallback}))
            except (OSError, ValueError, KeyError, TypeError):
                continue
    texts, reports = texts or fallback_texts, reports or fallback_reports
    if not texts or not reports:
        missing = '微信推送文' if not texts else '简报图片'
        raise DeliveryNotSent(f'未找到可校验的{missing}，logs/test_push固定测试文件也不可用。请补齐更新包内的测试文件。')
    text, report = max(texts, key=lambda item:item[:2])[2], max(reports, key=lambda item:item[:2])[2]
    return text, report


def send_latest_test(base, recipient, profile, sender_factory=None):
    """Explicit button action; independent from schedules and production journal."""
    base = Path(base)
    if not isinstance(recipient, str) or not recipient.strip() or len(recipient) > 200 or any(c in recipient for c in '\r\n\t'):
        raise DeliveryNotSent('请填写能在微信中唯一匹配的测试聊天对象完整名称')
    recipient = recipient.strip()
    text, report = latest_artifacts(base)
    message = (f'【同步工具测试发送】\n推送文日期：{text["date"]}；简报日期：{report["date"]}'
               + ('；简报为模拟预览' if report['demo'] else '')
               + ('\n推送文使用固定测试文件。' if text['fallback'] else '')
               + ('\n简报使用固定测试文件。' if report['fallback'] else '') + '\n\n' + text['text'])
    status = dict(recipient=recipient, text_source=text['path'], report_source=report['marker'],
                  submitted_text=False, submitted_images=0, image_count=len(report['images']), state='preparing')
    folder = base / 'runtime/wechat_test'
    folder.mkdir(parents=True, exist_ok=True)
    record = folder / 'latest_result.json'
    with worker_lock(base / 'runtime/wechat_delivery') as acquired:
        if not acquired:
            raise DeliveryNotSent('已有微信发送任务，请等待完成后再测试')
        sender = None
        try:
            # Validate every image before submitting any message.
            for entry in report['images']:
                if digest(Path(entry['path']).read_bytes()) != entry['sha256']:
                    raise DeliveryNotSent('测试图片在查找后发生变化，请重新生成')
            status.update(state='sending', started_at=datetime.now(LOCAL).isoformat())
            atomic_json(record, status)
            if sender_factory is None:
                from wechat_push.uia import WindowsUIASender
                sender = WindowsUIASender(profile)
            else:
                sender = sender_factory()
            sender.send(recipient, message)
            status['submitted_text'] = True
            atomic_json(record, status)
            for entry in report['images']:
                if digest(Path(entry['path']).read_bytes()) != entry['sha256']:
                    raise DeliveryNotSent('测试图片在发送前发生变化，后续图片未发送')
                sender.send_image(recipient, entry['path'])
                status['submitted_images'] += 1
                atomic_json(record, status)
            status['state'] = 'submitted'
        except Exception as exc:
            status.update(state='not_sent' if isinstance(exc, DeliveryNotSent) else 'uncertain', error=str(exc))
            raise
        finally:
            status['finished_at'] = datetime.now(LOCAL).isoformat()
            try:
                atomic_json(record, status)
            finally:
                if sender is not None:
                    try:
                        sender.close()
                    except Exception:
                        pass
    return status
