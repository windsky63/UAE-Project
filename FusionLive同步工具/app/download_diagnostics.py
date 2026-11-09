"""Bounded, redacted evidence for failed platform download requests."""
from html.parser import HTMLParser
import json
import re

import requests


class VisibleText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.hidden = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in ('script', 'style'):
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def redact(value):
    text = str(value)
    text = re.sub(r'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=._-]+', '[redacted authorization]', text)
    # Never retain URL queries or common credential/session values in evidence.
    text = re.sub(r'(https?://[^\s?<>"\']+)\?[^\s<>"\']*', r'\1?[redacted]', text)
    text = re.sub(r'(?i)(\b(?:password|passwd|token|access_token|api_key|authorization|cookie|'
                  r'set-cookie|jsessionid|sessionid|session|guid|username)\b["\']?\s*[:=]\s*["\']?)'
                  r'[^\s<>"\',;]+', r'\1[redacted]', text)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b', '[redacted email]', text)
    return text


def response_evidence(response, *, limit=8192):
    headers = {key: redact(response.headers[key])[:250] for key in (
        'Content-Type', 'Content-Length', 'Server', 'Via', 'Retry-After',
        'X-Request-ID', 'X-Correlation-ID', 'CF-Ray', 'X-Amz-Error-Code') if key in response.headers}
    result = dict(status=response.status_code, headers=headers)
    content_type = response.headers.get('Content-Type', '').lower()
    if not any(marker in content_type for marker in ('text/', 'html', 'json', 'xml')):
        result['body_note'] = '响应未声明文本类型，不采集正文'
        return result
    try:
        raw = bytearray()
        for chunk in response.iter_content(chunk_size=min(limit, 8192)):
            raw.extend(chunk[:limit-len(raw)])
            if len(raw) >= limit:
                break
        text = raw.decode('utf-8', errors='replace')
        parser = VisibleText()
        parser.feed(text)
        if 'html' in content_type:
            text = ' '.join(parser.parts)
        result['body_excerpt'] = redact(' '.join(text.split()))[:1500]
        result['body_limit_reached'] = len(raw) >= limit
    except (requests.RequestException, OSError) as exc:
        result['body_read_error'] = type(exc).__name__
    return result


def download_parameters(client, item):
    virtual = item.get('isVirtualLink') == 'true'
    params = {'zip': 'false', 'saveAs': 'filename', 'appendRevision': 'false',
              'appendStatus': 'false', 'rObjectId': item.get('virtualLinkId') if virtual else item['id'],
              'checkout': 'false', 'objectType': 'VIRTUAL_LINK' if virtual else 'DOCUMENT',
              'clientPId': client.project_id}
    if client.rendition_security:
        params.update(saveRendition='false', isRenditionSecurityEnabled='true')
    return params


def describe_failure(response):
    evidence = response_evidence(response)
    return evidence, f'{response.status_code} 下载请求失败；' + json.dumps(evidence, ensure_ascii=False)
