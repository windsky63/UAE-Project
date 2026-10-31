"""Validated analysis settings stored alongside the existing sync configuration."""
import json
import math
from pathlib import Path

DEFAULTS = {'analysis_poll_seconds': 5, 'analysis_idle_log_seconds': 300,
            'analysis_idle_exit_seconds': 0, 'analysis_auto_idle_exit_seconds': 120}


def validate(data):
    result = {key: data.get(key, default) for key, default in DEFAULTS.items()}
    for key, value in result.items():
        minimum = 0.1 if key in ('analysis_poll_seconds', 'analysis_idle_log_seconds') else 0
        if type(value) not in (int, float) or not math.isfinite(value) or not minimum <= value <= 86400:
            raise ValueError(f'{key} 须为 {minimum}—86400 秒的有限数值')
    return result


def load(base):
    path = Path(base) / 'runtime/wechat_push.json'
    data = json.loads(path.read_text(encoding='utf-8')) if path.is_file() else {}
    return validate(data)
