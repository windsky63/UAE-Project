"""Finite, explicitly requested local analysis -> briefing -> optional delivery."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import uuid

from daily_publications import BEIJING, day_value, load_report


def run(base, job_id, send=False, *, execute=None, report_loader=load_report):
    base = Path(base).resolve()
    if not re.fullmatch(r'[a-f0-9]{32}', job_id):
        raise ValueError('任务ID无效')
    day = day_value()
    folder = base / 'runtime/codex_actions'
    folder.mkdir(parents=True, exist_ok=True)
    path, cancel = folder / (job_id + '.json'), folder / (job_id + '.cancel')
    started = datetime.now(BEIJING)
    state = dict(job_id=job_id, date=day, phase='starting', send_requested=send,
                 started_at=started.isoformat(), sent=False)

    def save():
        temp = path.with_suffix('.' + uuid.uuid4().hex + '.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, path)

    def stopped():
        return cancel.exists() or any((base / 'runtime' / name).exists()
                                     for name in ('stop.request', 'scheduler.exit.request'))

    def command(args):
        if stopped():
            raise RuntimeError('任务已取消或同步工具处于安全停止状态；未发送')
        result = (execute or subprocess.run)(args, cwd=base, stdin=subprocess.DEVNULL, check=False)
        if result.returncode:
            raise RuntimeError(f'{state["phase"]}失败，退出码 {result.returncode}')

    save()
    try:
        if not (base / 'runtime/ledger.sqlite3').is_file():
            raise ValueError('本机没有下载台账，无法生成最新正式简报')
        state['phase'] = 'analyzing'; save()
        command([sys.executable, '-u', str(base / 'main.py'), 'analysis_worker'])
        state['phase'] = 'generating'; save()
        command([sys.executable, '-u', str(base / 'main.py'), 'briefing', '--date', day])
        marker = report_loader(base, day)
        generated = datetime.fromisoformat(marker['generated_at'])
        if generated < started or marker.get('fallback_notice') or marker.get('demo'):
            raise ValueError('本次未生成新的正式简报，不能使用旧文件或示例代替')
        state.update(workbook=marker.get('workbook'), images=marker['images'],
                     analysis_complete=marker.get('analysis_complete') is True)
        if send:
            if not state['analysis_complete']:
                raise ValueError('已生成阶段性简报，但当前分析未完整，未发送；请先处理待解析或失败记录')
            state['phase'] = 'sending'; save()
            command([sys.executable, '-u', str(Path(__file__).with_name('publication_runner.py')),
                     '--base', str(base), '--date', day, '--kind', 'briefing', '--cancel-file', str(cancel),
                     '--expected-generation', marker['generated_at']])
            state.update(phase='submitted', sent=True)
        else:
            state['phase'] = 'generated' if state['analysis_complete'] else 'generated_partial'
        return 0
    except Exception as exc:
        # Sending may have begun; never turn an ambiguous outcome into a retry.
        state.update(phase='uncertain' if state['phase'] == 'sending' else 'not_sent', error=str(exc))
        print(str(exc), flush=True)
        return 1
    finally:
        state['ended_at'] = datetime.now(BEIJING).isoformat()
        save()
        print(json.dumps(state, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base', type=Path, required=True)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--send', action='store_true')
    args = parser.parse_args()
    raise SystemExit(run(args.base, args.job_id, args.send))
