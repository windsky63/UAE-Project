"""Daily text then PNG delivery with persistent per-recipient submission records."""

from app_paths import HOME, CODE
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3

from analysis_worker import worker_lock
from push_settings import active_publication_flags, recipient_flags
from wechat_push.errors import DeliveryNotSent, DeliveryUncertain
from wechat_push.desktop_guard import desktop_paused

LOCAL = timezone(timedelta(hours=8))


def validate_report(base, marker, day, deadline, require_images=True):
    state = json.loads(Path(marker).read_text(encoding='utf-8'))
    if state.get('date') != day or not state.get('ready') or state.get('demo'):
        raise DeliveryNotSent('当天简报文件未就绪或为模拟预览，未推送')
    generated = datetime.fromisoformat(state['generated_at'])
    if generated.tzinfo is None or generated.astimezone(LOCAL).date().isoformat() != day or generated.astimezone(LOCAL) >= deadline:
        raise DeliveryNotSent('简报未在当天发送时间之前生成，未推送')
    folder = (Path(base) / 'logs' / day / '管道简报').resolve()
    images = state.get('images')
    if not require_images:
        return state
    if not isinstance(images, list) or not images:
        raise DeliveryNotSent('简报图片清单为空')
    for entry in images:
        path = Path(entry['path']).resolve()
        if not path.is_relative_to(folder) or path.suffix.lower() != '.png' or not path.is_file():
            raise DeliveryNotSent('图片路径不属于当天简报输出目录')
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry['sha256']:
            raise DeliveryNotSent('图片校验失败，未推送')
    return state


def snapshot_push_text(base, state, deadline):
    """Freeze today's existing download daily text, never a previous day's text."""
    day = state['date']
    folder = Path(base) / 'logs' / day
    daily = '【资料下载日报】\n' + day + '\n当日下载日报暂未就绪，不使用旧日报或虚构下载数量。'
    try:
        marker = json.loads((folder / '推送状态.json').read_text(encoding='utf-8'))
        path = folder / '微信推送文.txt'
        generated = datetime.fromisoformat(marker['generated_at'])
        if (marker['date'] != day or marker.get('text_file') != path.name or generated.tzinfo is None
                or generated.astimezone(LOCAL).date().isoformat() != day or generated >= deadline or path.stat().st_size > 64000):
            raise ValueError('当天日报标记无效')
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != marker.get('text_sha256'):
            raise ValueError('下载日报校验失败')
        text = content.decode('utf-8-sig').strip()
        if not text or len(text) > 14000:
            raise ValueError('下载日报长度不适合单条消息')
        daily = text
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return daily


class DeliveryJournal:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS deliveries(
          day TEXT,recipient TEXT,page INTEGER,sha256 TEXT NOT NULL,
          state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,error TEXT,
          PRIMARY KEY(day,recipient,page))''')
        self.db.commit()

    def close(self):
        self.db.close()

    def claim(self, day, recipient, page, sha):
        self.db.execute('BEGIN IMMEDIATE')
        row = self.db.execute('SELECT * FROM deliveries WHERE day=? AND recipient=? AND page=?', (day, recipient, page)).fetchone()
        if row and row['sha256'] != sha:
            self.db.rollback()
            raise DeliveryUncertain('同一天相同接收方已有不同版本的发送记录，需人工核对')
        if row and row['state'] == 'submitted':
            self.db.commit()
            return 'submitted'
        if row and (row['state'] in ('sending', 'uncertain') or row['attempts'] >= 3):
            self.db.rollback()
            raise DeliveryUncertain('上次发送结果不确定或已达尝试上限，不自动重发')
        self.db.execute('''INSERT INTO deliveries VALUES(?,?,?,?,'sending',1,NULL)
          ON CONFLICT(day,recipient,page) DO UPDATE SET state='sending',attempts=deliveries.attempts+1,error=NULL''',
          (day, recipient, page, sha))
        self.db.commit()
        return 'sending'

    def finish(self, day, recipient, page, state, error=None):
        with self.db:
            self.db.execute('UPDATE deliveries SET state=?,error=? WHERE day=? AND recipient=? AND page=?',
                            (state, error, day, recipient, page))


def deliver(base, marker, settings, at, sender_factory=None, cancelled=None, *, manual=False):
    """Scheduled window by default; explicit manual sends may publish today's report now."""
    base = Path(base)
    if desktop_paused():
        raise DeliveryNotSent('微信发送已紧急暂停，请运行“恢复微信发送.cmd”后再推送')
    if at.tzinfo is None:
        raise ValueError('发送时间必须包含时区')
    at = at.astimezone(LOCAL)
    hour, minute = (int(v) for v in settings['send_time_beijing'].split(':'))
    deadline = at.replace(hour=hour, minute=minute, second=0, microsecond=0)
    publish_text, publish_briefing = active_publication_flags(settings)
    if not (publish_text or publish_briefing) or not settings['recipients']:
        raise DeliveryNotSent('微信推送未启用或尚未选择接收会话')
    if not manual and not deadline <= at < deadline + timedelta(minutes=settings['send_window_minutes']):
        raise DeliveryNotSent('当前不在当天微信推送时间窗口内')
    day = at.date().isoformat()
    state = validate_report(base, marker, day, at + timedelta(microseconds=1) if manual else deadline,
                            require_images=publish_briefing)
    with worker_lock(base / 'runtime/wechat_delivery') as acquired:
        if not acquired:
            raise DeliveryNotSent('已有微信发送任务正在操作桌面')
        journal = DeliveryJournal(base / 'runtime/wechat_delivery/deliveries.sqlite3')
        sender = None
        result = {'submitted': 0, 'already_submitted': 0, 'failed': 0, 'uncertain': 0, 'errors': []}
        try:
            for recipient in settings['recipients']:
                publish_text, publish_briefing = recipient_flags(settings, recipient)
                # Legacy frozen snapshots may contain the removed analysis appendix.
                text = (state.get('wechat_push_text') if state.get('wechat_text_format') == 'daily-only-v1'
                        else None) or snapshot_push_text(base, state, deadline)
                items = [(0, {'text': text, 'sha256': hashlib.sha256(text.encode('utf-8')).hexdigest()})] if publish_text else []
                if publish_briefing:
                    items.extend(enumerate(state['images'], 1))
                for page, image in items:
                    if desktop_paused():
                        result['failed'] += 1
                        result['errors'].append('微信发送已紧急暂停，后续推送未发送')
                        break
                    if cancelled and cancelled():
                        result['failed'] += 1
                        result['errors'].append('已请求安全退出，后续推送未发送')
                        break
                    try:
                        if journal.claim(day, recipient, page, image['sha256']) == 'submitted':
                            result['already_submitted'] += 1
                            continue
                    except DeliveryUncertain as exc:
                        result['uncertain'] += 1
                        result['errors'].append(str(exc))
                        break
                    try:
                        if sender is None:
                            try:
                                if sender_factory is None:
                                    from wechat_push.uia import WindowsUIASender
                                    sender = WindowsUIASender(base / settings['uia_profile'])
                                else:
                                    sender = sender_factory()
                            except Exception as exc:
                                raise DeliveryNotSent('微信发送器未就绪：' + type(exc).__name__) from exc
                        # Verify immutable bytes again immediately before each UI operation.
                        if page == 0:
                            sender.send(recipient, image['text'])
                        else:
                            if hashlib.sha256(Path(image['path']).read_bytes()).hexdigest() != image['sha256']:
                                raise DeliveryNotSent('发送前图片发生变化')
                            sender.send_image(recipient, image['path'])
                    except DeliveryNotSent as exc:
                        journal.finish(day, recipient, page, 'not_sent', str(exc))
                        result['failed'] += 1
                        result['errors'].append(str(exc))
                        break
                    except Exception as exc:
                        journal.finish(day, recipient, page, 'uncertain', str(exc))
                        result['uncertain'] += 1
                        result['errors'].append(str(exc))
                        break
                    else:
                        journal.finish(day, recipient, page, 'submitted')
                        result['submitted'] += 1
        finally:
            journal.close()
            if sender:
                try:
                    sender.close()
                except Exception:
                    pass
        result['complete'] = not (result['failed'] or result['uncertain'])
        result['delivery_verified'] = False  # UI submission is not a receiver receipt.
        return result
