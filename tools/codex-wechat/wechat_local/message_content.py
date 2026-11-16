"""Normalize quoted replies without treating quoted text as a new instruction."""
import re
import xml.etree.ElementTree as ET


MAX_XML_CHARS = 128 * 1024
MAX_TEXT_CHARS = 16000
MAX_QUOTE_CHARS = 8000
KINDS = {1: 'text', 3: 'image', 34: 'voice', 49: 'file',
         '文本': 'text', '图片': 'image', '语音': 'voice', '文件/链接/卡片': 'file'}


def _appmsg(text):
    if len(text) > MAX_XML_CHARS or re.search(r'<!\s*(?:DOCTYPE|ENTITY)\b', text, re.I):
        return None
    # Some group database variants prepend the sender before the XML envelope.
    text = re.sub(r'^[^\s<>:]{1,128}:\r?\n', '', text.lstrip('\ufeff').strip(), count=1)
    try:
        root = ET.fromstring(text)
    except (ET.ParseError, ValueError):
        return None
    if root.tag == 'appmsg':
        return root
    return root.find('appmsg') if root.tag == 'msg' else None


def _text(element, name, limit):
    child = element.find(name)
    return (''.join(child.itertext()) if child is not None else '')[:limit]


def normalize(kind, content):
    """Return (kind, current author's text, optional referenced message)."""
    if isinstance(kind, int) and kind > 0xFFFF:
        kind &= 0xFFFFFFFF
    category = KINDS.get(kind, 'other')
    text = str(content or '')
    if category != 'file':
        return category, text[:MAX_TEXT_CHARS], None
    app = _appmsg(text)
    if app is None or _text(app, 'type', 16).strip() != '57':
        return category, text[:MAX_TEXT_CHARS], None
    reference = app.find('refermsg')
    if reference is None:
        return category, text[:MAX_TEXT_CHARS], None
    body = _text(app, 'title', MAX_TEXT_CHARS)
    quoted_type = _text(reference, 'type', 32).strip()
    quoted_text = _text(reference, 'content', MAX_XML_CHARS)
    # Nested app messages are context, never recursively promoted to the current
    # message. Present their title rather than passing an XML envelope to Codex.
    if quoted_type == '49':
        nested = _appmsg(quoted_text)
        quoted_text = (_text(nested, 'title', MAX_QUOTE_CHARS) if nested is not None else '') or '[文件/链接/卡片]'
    elif not quoted_text.strip():
        quoted_text = {'3': '[图片]', '34': '[语音]', '43': '[视频]'}.get(quoted_type, '[引用内容不可用]')
    quote = dict(message_id=_text(reference, 'svrid', 128),
                 sender_id=_text(reference, 'chatusr', 256) or _text(reference, 'fromusr', 256),
                 sender=_text(reference, 'displayname', 200), type=quoted_type,
                 text=quoted_text[:MAX_QUOTE_CHARS])
    return 'text', body, quote
