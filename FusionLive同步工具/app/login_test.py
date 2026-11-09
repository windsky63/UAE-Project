"""One-file connection test with a persisted Beijing-time access window."""

from paths import HOME, CODE
import json
import re
from pathlib import Path

from fusion_client import FusionClient, RUNTIME, WindowClosed, now
from ledger import safe_name


def validate_window(start, end):
    def minute(value):
        if not isinstance(value, str) or not re.fullmatch(r'\d{2}:\d{2}', value):
            raise ValueError('时间请使用 HH:MM 格式。')
        hour, part = map(int, value.split(':'))
        if hour > 23 or part > 59:
            raise ValueError('时间无效。')
        return hour * 60 + part
    first, last = minute(start), minute(end)
    if not 60 <= first < last <= 630:
        raise ValueError('测试时段必须在北京时间 01:00—10:30 内，且结束时间晚于开始时间。')
    return first, last


def load_window(runtime=None):
    path = Path(runtime or RUNTIME) / 'login_test_settings.json'
    if not path.exists():
        return '01:00', '10:30'
    data = json.loads(path.read_text(encoding='utf-8'))
    validate_window(data['start'], data['end'])
    return data['start'], data['end']


def save_window(start, end, runtime=None):
    validate_window(start, end)
    path = Path(runtime or RUNTIME) / 'login_test_settings.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'start': start, 'end': end}, ensure_ascii=False), encoding='utf-8')
    temporary.replace(path)


def check_test_window(at=None, runtime=None, *, manual_override=False, check_stop=True):
    runtime = Path(runtime or RUNTIME)
    if (runtime / 'login.paused').exists():
        raise WindowClosed('账号登录已暂停；测试按钮同样受登录暂停限制。')
    start, end = load_window(runtime)
    first, last = validate_window(start, end)
    at = at or now()
    if not manual_override and not first <= at.hour * 60 + at.minute < last:
        raise WindowClosed(f'登录测试仅允许北京时间 {start}—{end}，当前不在允许时段。')
    if check_stop and (runtime / 'stop.request').exists():
        raise WindowClosed('收到停止请求。')


class TestClient(FusionClient):
    def __init__(self, username, password, *, manual_override=False):
        super().__init__(username, password)
        self.manual_override = manual_override

    def check_access_window(self):
        check_test_window(manual_override=self.manual_override)


def download_one(client, ledger):
    item = ledger.one("""SELECT metadata FROM files WHERE project=? AND active=1
        AND json_extract(metadata,'$.readAccessStatus')='Available'
        ORDER BY COALESCE(size,0),id LIMIT 1""", (client.project_id,))
    if item is None:
        raise RuntimeError('本地台账没有当前项目可读取的文件，请先完成文件盘点。')
    metadata = json.loads(item['metadata'])
    target = RUNTIME / 'login_test' / safe_name(metadata['id']) / safe_name(metadata['filename'], 160)
    result = client.download(metadata, target)
    result['path'] = str(Path(result['path']).relative_to(ledger.base_dir))
    result['content_id'] = metadata['id']
    result['time_beijing'] = now().isoformat()
    (RUNTIME / 'login_test_result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f"登录测试已下载一个文件：{metadata['filename']}；{result['bytes']} 字节；SHA256 {result['sha256']}", flush=True)
    return 'done', 0
