"""Explicit manual publication worker; launched only after the UI confirmation."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid

from daily_publications import BEIJING, prepare_manual


def run(base, day, kind, *, cancel_file=None, expected_generation=None):
    base = Path(base).resolve()
    if cancel_file is not None:
        cancel_file = Path(cancel_file).absolute()
        if (cancel_file.parent != base / 'runtime/codex_actions' or
                cancel_file.resolve() != cancel_file or cancel_file.suffix != '.cancel'):
            raise ValueError('取消标记路径无效')
    sys.path.insert(0, str(base / 'app'))
    from push_settings import load_settings
    from wechat_delivery import deliver
    from wechat_push.errors import DeliveryNotSent

    started = time.time_ns()
    state = dict(date=day, kind=kind, phase='sending', started_at=datetime.now(BEIJING).isoformat())
    folder = base / 'logs' / day / '管道简报'
    folder.mkdir(parents=True, exist_ok=True)
    status = folder / '手动推送状态.json'
    def save():
        temporary = status.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, status)
    def cancelled():
        return (cancel_file is not None and cancel_file.exists()) or any(path.exists() and path.stat().st_mtime_ns >= started for path in
                   (base / 'runtime/stop.request', base / 'runtime/scheduler.exit.request'))
    save()
    print('手动推送开始：' + state['started_at'] + '；类型=' + kind, flush=True)
    try:
        marker, settings = prepare_manual(base, day, kind, load_settings(base))
        if expected_generation is not None and (marker.get('generated_at') != expected_generation or
                                                marker.get('analysis_complete') is not True):
            raise ValueError('简报已被其他任务更新或分析未完整，未发送；请重新生成核对')
        frozen = folder / ('手动推送快照-' + uuid.uuid4().hex + '.json')
        frozen.write_text(json.dumps(marker, ensure_ascii=False, indent=2), encoding='utf-8')
        result = deliver(base, frozen, settings, datetime.now(BEIJING), cancelled=cancelled, manual=True)
        state.update(result=result, phase='submitted' if result['complete'] else 'uncertain' if result['uncertain'] else 'not_sent')
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return 0 if result['complete'] else 1
    except (ValueError, OSError, DeliveryNotSent) as exc:
        state.update(phase='not_sent', error=str(exc))
        print('手动推送未完成：' + str(exc), flush=True)
        return 1
    except Exception as exc:
        state.update(phase='uncertain', error=str(exc))
        raise
    finally:
        state['ended_at'] = datetime.now(BEIJING).isoformat()
        save()
        print('手动推送结束：' + state['ended_at'] + '；状态=' + state['phase'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--kind', choices=('text', 'briefing', 'all'), required=True)
    parser.add_argument('--cancel-file', type=Path)
    parser.add_argument('--expected-generation')
    args = parser.parse_args()
    # Validate the date before constructing any output path.
    from daily_publications import day_value
    if day_value(args.date) != day_value():
        parser.error('仅支持当天推送')
    raise SystemExit(run(args.base, args.date, args.kind, cancel_file=args.cancel_file,
                         expected_generation=args.expected_generation))
