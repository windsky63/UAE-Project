"""Prepare before 11:00 Beijing and send PNGs through the send-only adapter."""

from app_paths import HOME, CODE
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback

from analysis_worker import ensure_background_worker, in_download_window
from push_settings import load_settings, active_publication_flags
from wechat_delivery import deliver, snapshot_push_text
from wechat_push.errors import DeliveryNotSent
from wechat_push.desktop_guard import desktop_paused

LOCAL = timezone(timedelta(hours=8))


class DailyBriefing:
    def __init__(self, base, log=print, *, process_factory=None, delivery=None, analysis_start=None, cancelled=None,
                 async_jobs=False, job_factory=None, recovery_factory=None):
        self.base = Path(base)
        self.log = log
        self.factory = process_factory or subprocess.Popen
        self.delivery = delivery or deliver
        self.analysis_start = analysis_start or ensure_background_worker
        self.cancelled = cancelled or (lambda: False)
        from publication_worker import PublicationJob
        self.async_jobs = async_jobs
        self.job_factory = job_factory or PublicationJob
        self.recovery_factory = recovery_factory or PublicationJob
        self.recovery_job = None
        self.next_recovery = 0
        self.notice_job = self.delivery_job = None
        self.process = self.stream = None
        self.next_prepare = self.next_analysis = self.next_send = self.next_text = 0
        self.next_notice = 0
        self.process_started = None
        self.schedule_seen = None
        self.layout_pending_seen = False
        self.state_path = self.base / 'runtime/briefing_schedule.json'
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {}

    def event(self, message):
        self.log(message)
        folder = self.marker(self.state['date']).parent
        folder.mkdir(parents=True, exist_ok=True)
        with (folder / '定时生成.log').open('a', encoding='utf-8') as stream:
            stream.write(f'[{datetime.now(LOCAL).isoformat(timespec="seconds")}] {message}\n')

    def elapsed(self):
        return f'{time.monotonic() - self.process_started:.3f}秒' if self.process_started is not None else '未知'

    def save(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, self.state_path)
        day = self.state.get('date')
        if day:
            path = self.base / 'logs' / day / '管道简报/定时推送状态.json'
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix('.json.tmp')
            temp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temp, path)

    def marker(self, day):
        return self.base / 'logs' / day / '管道简报/简报推送状态.json'

    def ready(self, day, deadline, marker=None, require_images=True):
        try:
            state = json.loads((marker or self.marker(day)).read_text(encoding='utf-8'))
            generated = datetime.fromisoformat(state['generated_at'])
            return (state.get('date') == day and state.get('ready') and (not require_images or bool(state.get('images')))
                    and not state.get('demo') and generated.tzinfo is not None
                    and generated.astimezone(LOCAL).date().isoformat() == day and generated.astimezone(LOCAL) < deadline)
        except (OSError, ValueError, KeyError):
            return False

    def freeze(self, day, deadline):
        frozen = self.marker(day).with_name('定时推送快照.json')
        _, publish_briefing = active_publication_flags(load_settings(self.base))
        if not self.state.get('snapshot_frozen'):
            candidate = self.marker(day)
            if not self.ready(day, deadline):
                candidate = self.marker(day).with_name('微信推送文快照.json')
                if publish_briefing or not self.ready(day, deadline, candidate, require_images=False):
                    return None
            data = json.loads(candidate.read_text(encoding='utf-8'))
            if not data.get('wechat_push_text') or data.get('wechat_text_format') != 'daily-only-v1':
                data['wechat_push_text'] = snapshot_push_text(self.base, data, deadline)
            data['wechat_text_format'] = 'daily-only-v1'
            temporary = frozen.with_suffix('.json.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
            os.replace(temporary, frozen)
            self.state.update(snapshot_frozen=True, phase='prepared')
            self.save()
        return frozen if self.ready(day, deadline, frozen, require_images=publish_briefing) else None

    def prepare_text_snapshot(self, day, at, deadline):
        path = self.marker(day).with_name('微信推送文快照.json')
        path.parent.mkdir(parents=True, exist_ok=True)
        data = dict(date=day, ready=True, demo=False, generated_at=at.isoformat(),
                    analysis_complete=False, images=[])
        data['wechat_push_text'] = snapshot_push_text(self.base, data, deadline)
        data['wechat_text_format'] = 'daily-only-v1'
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temporary, path)

    def tick(self, at):
        if at.tzinfo is None:
            raise ValueError('定时任务必须使用带时区的时间')
        at = at.astimezone(LOCAL)
        day = at.date().isoformat()
        if self.state.get('date') != day:
            self.close()
            self.state = {'date': day, 'phase': 'waiting', 'send_attempts': 0}
            self.next_prepare = self.next_analysis = self.next_send = self.next_text = 0
            self.next_notice = 0
        self.recover_migration(at)
        # Migration changes original-file paths, not the saved daily text or
        # publication images. Keep publication running; only pause new parsing.
        layout_pending = (self.base / 'runtime/layout.pending').exists()
        if layout_pending != self.layout_pending_seen:
            self.event('文件路径整理未完成：暂停启动原始文件解析；推送文和缓存简报继续定时生成、发送。'
                       if layout_pending else '文件路径整理标记已解除，恢复后台解析调度。')
            self.layout_pending_seen = layout_pending
        if self.notice_job:
            result = self.notice_job.poll()
            if result is not None:
                self.event('待分析通知生成结束：' + ('成功' if 'result' in result else '失败；' + result['error']))
                self.notice_job = None
        if self.delivery_job:
            result = self.delivery_job.poll()
            if result is not None:
                self.delivery_job = None
                self.complete_delivery(result, at)
            return
        settings = load_settings(self.base)
        send_hour, send_minute = (int(v) for v in settings['send_time_beijing'].split(':'))
        prep_hour, prep_minute = (int(v) for v in settings['prepare_time_beijing'].split(':'))
        deadline = at.replace(hour=send_hour, minute=send_minute, second=0, microsecond=0)
        prepare = at.replace(hour=prep_hour, minute=prep_minute, second=0, microsecond=0)
        schedule = (day, prepare.isoformat(), deadline.isoformat(), self.state.get('phase') in
                    ('submitted', 'uncertain', 'skipped', 'disabled'))
        if schedule != self.schedule_seen:
            self.event(f'管道简报调度检查：当前={at.isoformat()}；准备={prepare.isoformat()}；'
                       f'推送={deadline.isoformat()}；状态={self.state.get("phase")}；原因={self.state.get("reason", "无")}')
            self.schedule_seen = schedule
            self.save()
        # Offline work has no time window, even after today's publication ends.
        if not layout_pending and self.recovery_job is None and at.timestamp() >= self.next_analysis:
            database = self.base / 'runtime/ledger.sqlite3'
            if database.exists():
                try:
                    self.analysis_start(self.base, database)
                except Exception as exc:
                    self.event(f'后台分析启动失败：{type(exc).__name__}: {exc}\n{traceback.format_exc()}')
            self.next_analysis = at.timestamp() + 60
        if self.process and self.process.poll() is not None:
            code = self.process.returncode
            self.event(f'管道简报生成进程结束：退出码={code}；耗时={self.elapsed()}；有效快照={bool(self.ready(day, deadline))}')
            self.process_started = None
            self.process = None
            self.stream.close()
            self.stream = None
            if code == 0 and self.ready(day, deadline):
                self.state.update(phase='prepared', prepared_at=at.isoformat())
                self.log('管道简报快照已生成（允许部分结果），北京时间11:00先发推送文再发图片。')
            else:
                self.state.update(phase='waiting_analysis', reason='本次生成失败，保留上一份可用快照，截止前重试')
            self.save()
        if self.state.get('phase') in ('submitted', 'uncertain', 'skipped', 'disabled'):
            return
        if at < deadline:
            timestamp = at.timestamp()
            if at >= prepare and timestamp >= self.next_text:
                self.prepare_text_snapshot(day, at, deadline)
                self.next_text = timestamp + 60
            if (at >= prepare and timestamp >= self.next_notice and not self.ready(day, deadline) and not self.notice_job
                    and not self.state.get('snapshot_frozen')):
                # This small baseline does not scan files or wait on parsers.
                # A slow/crashed full report can never remove this publication.
                self.next_notice = timestamp + 60
                self.event('待分析通知生成开始')
                if self.async_jobs:
                    self.notice_job = self.job_factory(self.base, 'notice', dict(output=str(self.marker(day).parent),
                                                      day=day, title=settings['briefing_title']))
                else:
                    from briefing import generate_pending_notice
                    try:
                        generate_pending_notice(self.marker(day).parent, day, settings['briefing_title'])
                    except Exception as exc:
                        self.event(f'待分析通知生成结束：失败；继续尝试正式简报；{type(exc).__name__}: {exc}\n{traceback.format_exc()}')
                    else:
                        self.event('待分析通知生成结束：成功')
            if at >= deadline - timedelta(seconds=15):
                self.close()
                self.freeze(day, deadline)
                return
            if (at >= prepare and at < deadline - timedelta(seconds=15) and not self.process
                    and not self.notice_job and not self.state.get('snapshot_frozen') and timestamp >= self.next_prepare):
                folder = self.marker(day).parent
                folder.mkdir(parents=True, exist_ok=True)
                self.next_prepare = timestamp + 60
                self.event('管道简报生成进程开始')
                self.stream = (folder / '定时生成.log').open('a', encoding='utf-8')
                env = {k:v for k,v in os.environ.items() if k not in ('FUSIONLIVE_USERNAME', 'FUSIONLIVE_PASSWORD')}
                env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1')
                try:
                    self.process = self.factory([sys.executable, '-B', str(CODE / 'briefing.py'), '--date', day, '--daily-text',
                                                 '--title', settings['briefing_title']],
                        cwd=self.base, stdin=subprocess.DEVNULL, stdout=self.stream, stderr=subprocess.STDOUT,
                        env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                except Exception as exc:
                    self.stream.close()
                    self.stream = None
                    self.event(f'管道简报生成进程结束：启动失败；{type(exc).__name__}: {exc}\n{traceback.format_exc()}')
                    raise
                self.process_started = time.monotonic()
                self.state.update(phase='preparing', started_at=at.isoformat())
                self.save()
            return
        if not settings['enabled']:
            self.state.update(phase='disabled', reason='两个定时发布开关均已关闭；推送文、Excel和图片仍照常生成')
            self.save()
            return
        frozen = self.freeze(day, deadline)
        if frozen is None:
            self.state.update(phase='skipped', reason='当天简报及待分析通知均未能提前生成，请检查生成故障')
            self.save()
            self.log(self.state['reason'])
            return
        if at >= deadline + timedelta(minutes=settings['send_window_minutes']):
            self.state.update(phase='skipped', reason='错过当天11:00推送窗口，不自动补发过期简报')
            self.save()
            self.event(self.state['reason'])
            return
        if at.timestamp() < self.next_send:
            return
        if not settings['recipients']:
            self.state.update(phase='skipped', reason='尚未选择微信接收会话，未发送')
            self.save()
            self.log(self.state['reason'])
            return
        if desktop_paused():
            if self.state.get('reason') != '微信发送已紧急暂停，等待手动恢复':
                self.state.update(phase='prepared', reason='微信发送已紧急暂停，等待手动恢复')
                self.save()
            return
        self.state['send_attempts'] += 1
        self.state['phase'] = 'sending'
        self.save()
        if self.async_jobs:
            try:
                self.delivery_job = self.job_factory(self.base, 'delivery',
                    dict(marker=str(frozen), settings=settings, at=at.isoformat()))
            except Exception as exc:
                self.complete_delivery(dict(error='投递子进程启动失败：' + type(exc).__name__, not_sent=True), at)
            return
        try:
            result = self.delivery(self.base, frozen, settings, at, cancelled=self.cancelled)
            outcome = dict(result=result)
        except DeliveryNotSent as exc:
            outcome = dict(error=str(exc), not_sent=True)
        except Exception as exc:
            outcome = dict(error='发送结果需人工核对：' + type(exc).__name__, not_sent=False)
        self.complete_delivery(outcome, at)

    def complete_delivery(self, outcome, at):
        if 'result' in outcome:
            result = outcome['result']
            self.state['delivery'] = result
            self.state['phase'] = 'submitted' if result['complete'] else 'uncertain' if result['uncertain'] else 'prepared'
            self.state['reason'] = '已提交微信客户端；仅发送模式不读取接收回执' if result['complete'] else '发送未全部完成，详见投递记录'
        else:
            self.state.update(phase='prepared' if outcome.get('not_sent') else 'uncertain', reason=outcome['error'])
        if self.state['phase'] == 'prepared' and self.state['send_attempts'] >= 3:
            self.state.update(phase='skipped', reason='当天推送已达3次安全尝试上限')
        self.next_send = at.timestamp() + 30
        self.save()
        self.log('微信管道简报：' + self.state['reason'])

    def recover_migration(self, at):
        # Run in a separate process: large path/queue updates cannot delay 11:00.
        if self.recovery_job:
            outcome = self.recovery_job.poll()
            if outcome is None:
                return
            self.recovery_job = None
            result = outcome.get('result') or dict(phase='failed', reason=outcome.get('error', '未知错误'))
            self.state['migration_recovery'] = result
            self.save()
            self.event('自动迁移恢复：' + result.get('phase', 'failed') + '；' + result.get('reason', ''))
            self.next_recovery = 0 if result.get('phase') == 'complete' else at.timestamp() + 60
            if result.get('phase') == 'complete':
                self.next_analysis = 0
        runtime = self.base / 'runtime'
        pending = (runtime / 'layout.pending').exists() or (runtime / 'layout.analysis_pause.json').exists()
        if (not self.async_jobs or not pending or self.cancelled() or in_download_window(at)
                or at.timestamp() < self.next_recovery or not (runtime / 'ledger.sqlite3').is_file()):
            return
        self.next_recovery = at.timestamp() + 60
        self.recovery_job = self.recovery_factory(self.base, 'recovery', {})
        self.state['migration_recovery'] = dict(phase='running', started_at=at.isoformat())
        self.save()
        self.event('自动迁移恢复已启动；进度见 runtime/path_migration.log，状态见 runtime/migration_recovery.json；定时发布继续执行。')

    def close(self, *, include_recovery=False):
        if include_recovery and self.recovery_job:
            self.recovery_job.close(self.log)
            self.recovery_job = None
        for name in ('notice_job', 'delivery_job'):
            job = getattr(self, name)
            if job:
                job.close(self.log)
                if name == 'delivery_job':
                    outcome = job.poll()
                    if outcome is not None:
                        self.complete_delivery(outcome, datetime.now(LOCAL))
                setattr(self, name, None)
        if self.process and self.process.poll() is None:
            # This child only renders local reports, never downloads or logs in.
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            self.event(f'管道简报生成进程结束：调度关闭或到达快照截止时间，已终止；耗时={self.elapsed()}')
        self.process = None
        self.process_started = None
        if self.stream:
            self.stream.close()
            self.stream = None


def run_daily_briefing(base, exiting, log=print):
    service = None
    try:
        while not exiting.is_set():
            try:
                if service is None:
                    service = DailyBriefing(base, log, cancelled=exiting.is_set, async_jobs=True)
                service.tick(datetime.now(LOCAL))
            except Exception as exc:
                message = f'管道简报定时任务异常：{type(exc).__name__}: {exc}\n{traceback.format_exc()}'
                log(message)
                exiting.wait(30)
            exiting.wait(.5)
    finally:
        if service is not None:
            service.close(include_recovery=True)
