"""Capture runs on the main thread while one bounded Codex task runs in a worker."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import queue
import logging
import time
import sqlite3

from .codex_settings import CodexConfigTimeoutError, CodexGenerationTimeoutError, CodexIdleTimeoutError
from .codex_settings import CodexConnectionTimeoutError, CodexServiceError
from .app_server import CodexBackendTimeoutError, CodexBackendError
from .desktop_guard import desktop_paused
from . import file_transfer, file_cleanup


class Listener:
    def __init__(self, config, driver, spool, state, runner):
        self.config, self.driver, self.spool, self.state, self.runner = config, driver, spool, state, runner
        self.state.bind_account(config.bot_id, driver.account_id)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='codex')
        self.active = None
        self._next_cleanup_check = 0
        self._log_started=time.monotonic()
        self._log_at=float('-inf')
        self._intake_since_log=0

    def age(self, event, started_at=None):
        timestamp = datetime.fromisoformat((started_at or event['observed_at']).replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('Message timestamps must contain timezone')
        return (datetime.now(timezone.utc) - timestamp).total_seconds()

    def eligible(self, event, job=None):
        chat = next((c for c in self.config.chats if c.id == event['chat_id']), None)
        if not chat or not chat.reply or not self.config.codex_enabled:
            return False
        if event.get('historical') or event['direction'] != 'incoming' or event['content_type'] != 'text':
            return False
        # Freshness is checked on intake. Accepted pending jobs may wait without
        # consuming their execution/delivery lifetime, including across restart.
        if job is None or job['status'] != 'pending':
            timestamp = job['started_at'] if job is not None else None
            if job is not None and job['started_at'] and job['finished_at']:
                timestamp = job['finished_at']
            age = self.age(event, timestamp)
            if age < -30 or age > self.config.reply_max_age_seconds:
                return False
        text = event['text'].strip()
        if not text:
            return False
        if chat.kind == 'group':
            mention = '@' + self.driver.account_name
            if not re.search(re.escape(mention) + r'(?=\s|$)', text):
                return False
            text = re.sub(re.escape(mention) + r'(?=\s|$)', '', text).strip()
            quote = event.get('quoted_message')
            has_quote = isinstance(quote, dict) and bool(str(quote.get('text') or '').strip())
            if (not text and not has_quote) or text.startswith('/'):
                return False
        return True

    def tick(self):
        for chat in self.config.chats:
            try:
                self.driver.capture(chat.id, self.spool)
            except Exception:
                # One unavailable chat/database must not starve completed replies
                # or prevent the other chats from being captured this cycle.
                logging.getLogger(__name__).exception('Capture failed: chat_id=%s; retry next cycle', chat.id)
        for event in self.spool.pending():
            self.state.archive(event)
            self._intake_since_log += 1
            if self.eligible(event):
                self.state.enqueue(event)
                job = self.state.job(event['id'])
                if job['status'] == 'pending' and (self.active is not None or self.state.next_job()['id'] != job['id']):
                    self.queue_notice(event, job)
            self.spool.uploaded([event])  # Local durable hand-off; no HTTP upload.
        self.report_progress()
        self.finish_active()
        self.retry_notices()
        self.deliver()
        if self.active is None and self.config.codex_enabled:
            self.start_next()
        if time.monotonic() >= self._next_cleanup_check:
            self._next_cleanup_check = time.monotonic() + 60
            try:
                file_cleanup.run_if_due(self.config, self.state, self.spool)
            except (OSError, ValueError, sqlite3.Error) as exc:
                logging.getLogger(__name__).exception('Outgoing cleanup failed: %s', exc)
        at=time.monotonic()
        if at-self._log_at>=30:
            distribution={row[0]:row[1] for row in self.state.db.execute('SELECT status,count(*) FROM jobs GROUP BY status')}
            logging.getLogger(__name__).info('微信监听心跳：会话数=%s；本间隔接收=%s；任务台账分布=%s；当前任务=%s；本轮存活=%.1f秒；存活不代表任务已完成',
                len(self.config.chats),self._intake_since_log,json.dumps(distribution,ensure_ascii=False),
                self.active[0] if self.active else '无',at-self._log_started)
            self._intake_since_log=0
            self._log_at=at

    def finish_active(self):
        if self.active and self.active[1].done():
            identifier, future = self.active
            if self.state.job(identifier)['status'] == 'cancelled':
                self.active = None
                return
            try:
                result = future.result()
                reply, usage = result
                action = getattr(result, 'file_result', None) or {}
                if action.get('kind') == 'cancel':
                    event = json.loads(self.state.job(identifier)['event'])
                    self.state.save_file_selection(event['chat_id'], file_transfer.sender_key(event), {})
                    for old in self.state.generated():
                        prior = json.loads(old['event'])
                        payload = json.loads(old['file_result']) if old['file_result'] else {}
                        if old['chat_id'] == event['chat_id'] and file_transfer.sender_key(prior) == file_transfer.sender_key(event) and payload.get('kind') == 'send' and old['status'] != 'uncertain':
                            self.state.set_job(old['id'], 'cancelled')
                self.state.set_job(identifier, 'generated', reply=reply, usage=usage,
                                   memory_note=getattr(result, 'memory_note', None),
                                   file_result=getattr(result, 'file_result', None))
            except Exception as exc:
                logging.getLogger(__name__).exception('Generation failed: job_id=%s', identifier)
                self.state.set_job(identifier, 'failed', error=self.generation_error(exc))
                self.failure_notice(self.state.job(identifier), exc)
            self.active = None

    def workspace_for(self, chat_id):
        identity = hashlib.sha256(json.dumps([self.config.bot_id, self.driver.account_id, chat_id]).encode()).hexdigest()[:24]
        return Path(self.config.workspaces) / identity

    def export_memory(self, chat_id):
        if self.config.chat_memory_enabled:
            try:
                self.state.export_memory(chat_id, self.workspace_for(chat_id))
            except (OSError, ValueError) as exc:
                # Sending was already confirmed; a regenerable MD failure must not undo it.
                logging.getLogger(__name__).exception('Chat memory export failed: chat_id=%s; %s', chat_id, exc)

    def generation_error(self, exc):
        if isinstance(exc, CodexConnectionTimeoutError):
            return f'TimeoutError: Codex 网络连接连续失败超过 {self.config.codex_connect_timeout_seconds} 秒，请检查网络或账号'
        if isinstance(exc, CodexBackendError):
            return str(exc)
        if isinstance(exc, CodexBackendTimeoutError):
            return 'TimeoutError: ' + str(exc) + '，请检查本机 Codex 后端状态'
        if isinstance(exc, CodexConfigTimeoutError):
            return 'TimeoutError: ' + str(exc) + '，尚未启动模型生成；请检查本机 Codex 启动和配置读取'
        if isinstance(exc, CodexGenerationTimeoutError):
            return f'TimeoutError: Codex 生成超过 {self.config.codex_timeout_seconds} 秒，尚未进入微信发送阶段'
        if isinstance(exc, CodexIdleTimeoutError):
            return f'TimeoutError: Codex 连续 {self.config.codex_idle_timeout_seconds} 秒未报告进度，且没有正在执行的查询工具'
        if isinstance(exc, TimeoutError):
            return 'TimeoutError: 任务执行超时，具体阶段未确定'
        return f'{type(exc).__name__}: {exc}'

    def failure_notice(self, job, exc):
        if isinstance(exc, (CodexConnectionTimeoutError, CodexServiceError)):
            message = '连接 Codex 失败，请检查网络或账号。'
        else:
            return
        if not self.config.allow_send or not self.config.codex_enabled:
            return
        event = json.loads(job['event'])
        if not self.eligible(event, job):
            return
        command = dict(id='failed:' + job['id'], chat_id=event['chat_id'],
                       text=self.task_header(job, event, finished=True) + '回答如下：\n' + message)
        chat = next(c for c in self.config.chats if c.id == event['chat_id'])
        if self.config.mention_reply_sender and chat.kind == 'group':
            command['mention_user'] = event.get('sender_id') or event['sender']
        self.send_notice(job, 'failed', command)

    def report_progress(self):
        progress = getattr(self.runner, 'progress', None)
        if progress is None:
            return
        while True:
            try:
                identifier, stage = progress.get_nowait()
            except queue.Empty:
                return
            if not self.active or self.active[0] != identifier or self.active[1].done():
                continue
            if stage != 'querying' or not self.config.query_progress_enabled or not self.config.allow_send:
                continue
            row = self.state.job(identifier)
            if row is None:
                continue
            event = json.loads(row['event'])
            if not self.eligible(event, row):
                continue
            identity, text = 'querying:' + identifier, f'任务编号：{self.state.task_number(identifier)}\n正在查询……'
            self.send_notice(row, 'querying', dict(id=identity, chat_id=event['chat_id'], text=text))

    def send_notice(self, job, stage, command):
        command = self.spool.remember_notice(job['id'], stage, command)
        if desktop_paused():
            return
        result = self.spool.execute(command, self.driver,
                                    self.config.send_max_attempts, self.config.send_retry_seconds)
        if result['status'] == 'confirmed':
            self.state.archive(dict(id=command['id'], chat_id=command['chat_id'], sender=self.driver.account_id,
                                    direction='outgoing', content_type='text', text=command['text'],
                                    observed_at=datetime.now(timezone.utc).isoformat()))
            self.spool.finish_notice(command['id'])

    def retry_notices(self):
        if not self.config.allow_send or not self.config.codex_enabled or desktop_paused():
            return
        for identifier, stage, command in self.spool.pending_notices():
            job = self.state.job(identifier)
            # Do not send obsolete "queued" after starting, or "thinking"
            # after generation has finished. A failed notice never holds a reply.
            expected_status = 'pending' if stage == 'queued' else 'failed' if stage == 'failed' else 'running'
            if job is None or job['status'] != expected_status:
                self.spool.finish_notice(command['id'])
                continue
            if stage == 'querying' and not self.config.query_progress_enabled:
                continue
            if stage not in ('queued', 'failed') and self.active and self.active[0] == identifier and self.active[1].done():
                self.spool.finish_notice(command['id'])
                continue
            event = json.loads(job['event'])
            if self.eligible(event, job):
                self.send_notice(job, stage, command)

    def task_identity(self, job, event):
        sender = ' '.join(str(event['sender']).split()).lstrip('@')
        return f'任务编号：{self.state.task_number(job["id"])}\n回复对象：@{sender}\n'

    def task_header(self, job, event, finished=False):
        timestamp = job['finished_at'] if finished else job['started_at']
        at = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).astimezone(timezone(timedelta(hours=8)))
        label = '任务结束时间' if finished else '任务开始时间'
        return (self.task_identity(job, event) +
                f'{label}：{at:%Y-%m-%d %H:%M:%S}（北京时间）\n')

    def queue_notice(self, event, job):
        chat = next(c for c in self.config.chats if c.id == event['chat_id'])
        if not self.config.allow_send or chat.kind != 'group':
            return
        position = self.state.queue_position(job['id'])
        text = self.task_identity(job, event) + f'任务排序：第{position}位\n正在排队中……'
        command = dict(id='queued:' + job['id'], chat_id=chat.id, text=text)
        if self.config.mention_reply_sender:
            command['mention_user'] = event.get('sender_id') or event['sender']
        self.send_notice(job, 'queued', command)

    def acknowledge(self, event, job):
        chat = next(c for c in self.config.chats if c.id == event['chat_id'])
        if not self.config.allow_send or chat.kind != 'group':
            return
        # A separate durable action ID prevents duplicate acknowledgements on replay.
        identity = 'thinking:' + event['id']
        text = self.task_header(job, event) + '正在思考中……'
        command = dict(id=identity, chat_id=chat.id, text=text)
        if self.config.mention_reply_sender:
            command['mention_user'] = event.get('sender_id') or event['sender']
        self.send_notice(job, 'thinking', command)

    def start_next(self):
        job = self.state.next_job()
        if job is None:
            return
        event = json.loads(job['event'])
        if not self.eligible(event, job):
            self.state.set_job(job['id'], 'expired')
            return
        chat = next(c for c in self.config.chats if c.id == job['chat_id'])
        self.state.set_job(job['id'], 'running')
        job = self.state.job(job['id'])
        # Stable hash allows arbitrary display names without allowing path traversal.
        workspace = self.workspace_for(chat.id)
        try:
            self.export_memory(chat.id)
            self.state.export_chat(chat.id, workspace / 'memory.sqlite3', include_memory=self.config.chat_memory_enabled)
            recent = self.state.recent(chat.id, self.config.recent_messages, event['id'])
            context = self.state.file_selection(chat.id, file_transfer.sender_key(event))
            if context and (datetime.now(timezone.utc).timestamp() - context.get('created', 0)) < 900:
                event = dict(event, file_selection=context)
            future = self.executor.submit(self.runner.generate, event, chat, recent, workspace)
        except Exception as exc:
            logging.getLogger(__name__).exception('Job startup failed: job_id=%s', job['id'])
            self.state.set_job(job['id'], 'failed', error=self.generation_error(exc))
            self.failure_notice(self.state.job(job['id']), exc)
            return
        self.active = (job['id'], future)
        # A slow or failed UI acknowledgement must not delay model startup.
        self.acknowledge(event, job)

    def deliver(self):
        if self.config.allow_send and desktop_paused():
            return
        for job in self.state.generated():
            event = json.loads(job['event'])
            if not self.config.allow_send:
                self.state.set_job(job['id'], 'preview')
                continue
            if not self.eligible(event, job):
                self.state.set_job(job['id'], 'expired')
                continue
            # Use persisted timestamps so retries/restarts never change the payload.
            # Old already-generated jobs retain their original plain send payload.
            text = (self.task_header(job, event, finished=True) + '回答如下：\n' + job['reply']
                    if job['started_at'] and job['finished_at'] else job['reply'])
            command = {'id': 'reply:' + job['id'], 'chat_id': job['chat_id'], 'text': text}
            chat = next(c for c in self.config.chats if c.id == job['chat_id'])
            payload = json.loads(job['file_result']) if job['file_result'] else {}
            if payload.get('kind') == 'send':
                try:
                    file_transfer.validate(self.config, chat, event, self.workspace_for(chat.id), payload, self.driver.account_name)
                except file_transfer.UploadSizeLimit as exc:
                    reply = file_transfer.blocked_reply(self.config, chat, self.workspace_for(chat.id),
                        file_transfer.payload_ids(payload), str(exc))
                    self.state.set_job(job['id'], 'generated', reply=reply, file_result={})
                    continue
                except (ValueError, OSError):
                    # Do not claim delivery or let model output substitute a path.
                    reply = file_transfer.blocked_reply(self.config, chat, self.workspace_for(chat.id),
                        file_transfer.payload_ids(payload), '文件版本、授权或路径已变化，本次未继续发送。请重新查找后再选择。')
                    self.state.set_job(job['id'], 'generated', reply=reply, file_result={})
                    continue
                entries = payload.get('files') or [payload]
                commands = [dict(id='file:' + job['id'] + (f':{index}' if payload.get('files') else ''),
                                 chat_id=chat.id, kind='file', path=entry['path'], sha256=entry['sha256'],
                                 filename=entry['filename'], bytes=entry['bytes'])
                            for index, entry in enumerate(entries)]
                command = commands[0]
            if self.config.mention_reply_sender and chat.kind == 'group':
                if command.get('kind') != 'file':
                    command['mention_user'] = event.get('sender_id') or event['sender']
            for outgoing in commands if payload.get('kind') == 'send' else [command]:
                result = self.spool.execute(outgoing,
                                            self.driver, self.config.send_max_attempts, self.config.send_retry_seconds)
                if result['status'] != 'confirmed':
                    break
            if command.get('kind') == 'file' and result['status'] == 'confirmed' and payload.get('remaining'):
                self.state.save_file_selection(chat.id, file_transfer.sender_key(event),
                    dict(kind='pending_send', created=datetime.now(timezone.utc).timestamp(), candidates=payload['remaining'], staging_event_id=event['id']))
                notice = (f'本批已提交 {len(entries)} 个附件，还剩 {payload["remaining_archives"]} 个压缩包'
                          f'（{len(payload["remaining"])} 份文件）。每次最多发送 3 个附件；'
                          '请明确回复“继续发送”后再发送下一批。源文件保持原位。')
                notice_command = dict(id='files-notice:' + job['id'], chat_id=chat.id, text=notice)
                if self.config.mention_reply_sender and chat.kind == 'group':
                    notice_command['mention_user'] = event.get('sender_id') or event['sender']
                result = self.spool.execute(notice_command, self.driver,
                    self.config.send_max_attempts, self.config.send_retry_seconds)
                if result['status'] == 'confirmed':
                    self.state.archive(dict(id=notice_command['id'], chat_id=chat.id,
                        sender=self.driver.account_id, direction='outgoing', content_type='text',
                        text=notice, observed_at=datetime.now(timezone.utc).isoformat()))
            self.state.set_job(job['id'], result['status'], error=result['detail'])
            if command.get('kind') == 'file' and result['status'] == 'not_sent':
                row = self.spool.db.execute('SELECT attempts FROM ui_actions WHERE id=?', (outgoing['id'],)).fetchone()
                if row and row[0] >= self.config.send_max_attempts:
                    reply = file_transfer.blocked_reply(self.config, chat, self.workspace_for(chat.id),
                        file_transfer.payload_ids(payload), '微信未能提交以下文件，已停止重试；如有前面的文件已发送，请勿重复获取。')
                    self.state.set_job(job['id'], 'generated', reply=reply, file_result={})
            if result['status'] == 'confirmed':
                if command.get('kind') == 'file' and not payload.get('remaining'):
                    self.state.save_file_selection(chat.id, file_transfer.sender_key(event), {})
                if payload.get('kind') in ('search', 'cancel'):
                    self.state.save_file_selection(chat.id, file_transfer.sender_key(event), payload if payload['kind'] == 'search' else {})
                self.state.archive({'id': 'reply:' + job['id'], 'chat_id': job['chat_id'], 'sender': self.driver.account_id,
                                    'direction': 'outgoing', 'content_type': 'file' if command.get('kind') == 'file' else 'text',
                                    'text': '\n'.join(entry['filename'] for entry in entries) if command.get('kind') == 'file' else text,
                                    'observed_at': datetime.now(timezone.utc).isoformat()})
                self.export_memory(job['chat_id'])

    def close(self):
        request_stop = getattr(self.runner, 'request_stop', None)
        if request_stop:
            request_stop()
        self.executor.shutdown(wait=True, cancel_futures=True)
        close = getattr(self.runner, 'close', None)
        if close:
            close()
