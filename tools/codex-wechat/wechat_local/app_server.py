"""Resident Codex backend with isolated per-chat sessions and prewarmed rotation."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import queue
import threading
import time

from .codex_settings import (CodexConfigTimeoutError, CodexGenerationTimeoutError,
                             CodexIdleTimeoutError, CodexConnectionWatch, CodexServiceError, service_failure,
                             permission_arguments, transport_overrides)
from .shared_backend import SharedConnection, RpcError, RpcTimeout, config_signature


class CodexBackendTimeoutError(TimeoutError):
    """A protocol request timed out; the model may not have started yet."""


class CodexBackendError(RuntimeError):
    """Sanitized backend diagnostics that are safe to persist in the job record."""


def public_notification(message):
    """Discard reasoning, raw commands, tool output and raw error messages."""
    method, params = message.get('method', ''), message.get('params') or {}
    if not isinstance(params, dict):
        return None
    thread = params.get('threadId')
    if not thread:
        return None
    safe = dict(threadId=thread)
    if params.get('turnId'):
        safe['turnId'] = params['turnId']
    if method in ('item/started', 'item/completed'):
        item = params.get('item') or {}
        detail = {key: item[key] for key in ('id', 'type', 'phase') if key in item}
        if item.get('type') == 'agentMessage' and method == 'item/completed':
            detail['text'] = str(item.get('text', ''))[:65536]
        safe['item'] = detail
    elif method in ('turn/started', 'turn/completed'):
        turn = params.get('turn') or {}
        safe['turn'] = dict(id=turn.get('id'), status=turn.get('status'), failed=bool(turn.get('error')))
        safe['turn']['serviceFailure'] = service_failure(turn.get('error'))
    elif method == 'thread/tokenUsage/updated':
        info = params.get('tokenUsage') or {}
        usage = info.get('last') or {}
        safe['usage'] = {key: value for key, value in usage.items() if isinstance(value, (int, float))}
        safe['modelContextWindow'] = info.get('modelContextWindow')
    elif method == 'thread/closed':
        pass
    elif method == 'error':
        safe['willRetry'] = params.get('willRetry') is True
        safe['serviceFailure'] = service_failure(params.get('error') or params.get('message'))
    elif method.startswith(('item/', 'turn/')):
        safe['modelProgress'] = method in ('item/reasoning/textDelta', 'item/reasoning/summaryTextDelta', 'item/agentMessage/delta')
        method = 'progress/heartbeat'
    else:
        return None
    return dict(method=method, params=safe)


class Client:
    """WeChat event/privacy adapter over the shared local Codex transport."""
    def __init__(self, command, cwd):
        self.events = queue.Queue(maxsize=256)
        self.overflow = threading.Event()
        try:
            self.transport = SharedConnection(command, cwd, 'wechat', self.put_event)
        except (RpcError, RpcTimeout) as exc:
            reason = service_failure(str(exc))
            if reason:
                raise CodexServiceError(reason) from None
            raise CodexBackendError('Codex 连接初始化失败，请检查本机后端与登录状态') from None
        self.process = self.transport.process

    def put_event(self, message):
        event = message if message.get('method') == 'connection/closed' else public_notification(message)
        if event is not None:
            try:
                self.events.put_nowait(event)
            except queue.Full:
                if event.get('method') != 'progress/heartbeat':
                    self.overflow.set()

    def rpc(self, method, params, timeout=20):
        try:
            return self.transport.rpc(method, params, timeout)
        except RpcTimeout:
            self.transport.close()
            if method == 'config/read':
                raise CodexConfigTimeoutError(f'读取 Codex 配置超时：config/read 等待响应超过 {timeout} 秒') from None
            raise CodexBackendTimeoutError(f'Codex 后端 {method} 等待响应超过 {timeout} 秒') from None
        except RpcError as exc:
            reason = service_failure(str(exc))
            if reason:
                raise CodexServiceError(reason) from None
            raise CodexBackendError('Codex 后端请求失败：' + method) from None

    def close(self):
        self.transport.close()


@dataclass
class BackendResult:
    text: str
    usage: dict
    tool_calls: int
    process_reused: bool
    config_cache_hit: bool
    setup_seconds: float
    session_metrics: dict


@dataclass
class Conversation:
    thread: str
    signature: str
    params: dict
    create_seconds: float
    turns: int = 0
    used_tokens: int | None = None
    window_tokens: int | None = None
    rotation_due: bool = False
    seen_ids: deque = field(default_factory=lambda: deque(maxlen=512))


@dataclass
class ChatSession:
    current: Conversation | None = None
    standby: object = None


class ResidentBackend:
    CONFIG_TTL = 60
    MAX_RETIRED_THREADS = 32

    def __init__(self, config, progress):
        self.config, self.progress = config, progress
        self.client, self.key, self.retired = None, None, 0
        self.cache = {}
        self.sessions, self.closed_threads = {}, set()
        self.cleanups = []
        self.session_workers = ThreadPoolExecutor(max_workers=2, thread_name_prefix='codex-session')
        self.lock = threading.Lock()
        self.closed = False
        self.stop_requested = threading.Event()

    def discard(self):
        client, self.client = self.client, None
        self.cache.clear()
        for slot in self.sessions.values():
            if slot.standby:
                slot.standby.cancel()
        for future in self.cleanups:
            future.cancel()
        self.sessions.clear()
        self.closed_threads.clear()
        self.cleanups.clear()
        self.retired = 0
        if client:
            client.close()

    def ensure(self, command, cwd, access_dirs):
        key = (tuple(command), str(Path(cwd).resolve()), tuple(access_dirs), config_signature(cwd))
        cleanup_failed = any(f.done() and (f.cancelled() or not f.result()) for f in self.cleanups)
        self.cleanups = [f for f in self.cleanups if not f.done()]
        if (self.client and self.client.process.poll() is None and self.key == key
                and self.retired < self.MAX_RETIRED_THREADS and not cleanup_failed):
            return self.client, True
        self.discard()
        arguments = command + permission_arguments(access_dirs, workspace_write=False)
        arguments += ['--ask-for-approval', 'never', 'app-server', '--listen', 'stdio://']
        self.client = Client(arguments, cwd)
        self.key = key
        return self.client, False

    def effective_config(self, client, workspace):
        path = str(Path(workspace).resolve())
        signature = config_signature(workspace)
        cached = self.cache.get(path)
        if cached and cached[0] == signature and time.monotonic() - cached[1] < self.CONFIG_TTL:
            return cached[2], True
        effective = client.rpc('config/read', dict(includeLayers=False, cwd=path), 15).get('config') or {}
        if not isinstance(effective, dict):
            raise CodexBackendError('Codex 连接配置格式无效')
        self.cache[path] = (signature, time.monotonic(), effective)
        return effective, False

    @staticmethod
    def _create(client, params, signature):
        started = time.monotonic()
        thread = client.rpc('thread/start', params, 30)['thread']['id']
        return Conversation(thread, signature, params, round(time.monotonic() - started, 3))

    @staticmethod
    def _unsubscribe(client, conversation):
        try:
            client.rpc('thread/unsubscribe', dict(threadId=conversation.thread), 5)
            return True
        except Exception:
            return False

    def _retire(self, client, conversation):
        if conversation is not None:
            self.retired += 1
            self.cleanups.append(self.session_workers.submit(self._unsubscribe, client, conversation))

    def _abandon_standby(self, client, slot):
        future, slot.standby = slot.standby, None
        if future is None or future.cancel():
            return
        self.retired += 1
        def cleanup():
            try:
                return self._unsubscribe(client, future.result())
            except Exception:
                return True  # Empty session creation failed; there is no generation to replay.
        self.cleanups.append(self.session_workers.submit(cleanup))

    def _window(self, conversation):
        return self.config.codex_context_window_tokens or conversation.window_tokens or 0

    def _ratio(self, conversation):
        window = self._window(conversation)
        if window > 0 and conversation.used_tokens is not None and conversation.used_tokens > 0:
            return conversation.used_tokens / window * 100
        return None

    def _rotation_reason(self, conversation):
        ratio = self._ratio(conversation)
        if conversation.rotation_due or (ratio is not None and ratio >= self.config.codex_session_rotate_percent):
            return 'context_threshold'
        if ratio is None and conversation.turns >= self.config.codex_session_unknown_max_turns:
            return 'unknown_usage_turn_limit'
        return None

    def _maybe_prewarm(self, client, slot):
        current = slot.current
        if not self.config.codex_session_prewarm or current is None or slot.standby is not None:
            return
        ratio = self._ratio(current)
        due = (current.rotation_due or (ratio is not None and ratio >= self.config.codex_session_prewarm_percent)
               or (ratio is None and current.turns >= self.config.codex_session_unknown_max_turns - 1))
        if due:
            slot.standby = self.session_workers.submit(self._create, client, current.params, current.signature)

    def _accept_usage(self, client, params):
        for slot in self.sessions.values():
            current = slot.current
            if current is None or current.thread != params.get('threadId'):
                continue
            usage = params.get('usage') or {}
            used = usage.get('totalTokens')
            if used is None and 'inputTokens' in usage:
                used = usage.get('inputTokens', 0) + usage.get('outputTokens', 0)
            if isinstance(used, (int, float)) and used >= 0:
                current.used_tokens = int(used)
            window = params.get('modelContextWindow')
            if isinstance(window, (int, float)) and window > 0:
                current.window_tokens = int(window)
            ratio = self._ratio(current)
            if ratio is not None and ratio >= self.config.codex_session_rotate_percent:
                current.rotation_due = True  # Finish this turn even if Codex compacts it internally.
            self._maybe_prewarm(client, slot)
            break

    def _drain_idle_events(self, client):
        while True:
            try:
                event = client.events.get_nowait()
            except queue.Empty:
                return
            if event['method'] == 'connection/closed':
                raise CodexBackendError('Codex 常驻后端连接已断开')
            if event['method'] == 'thread/tokenUsage/updated':
                self._accept_usage(client, event['params'])
            elif event['method'] == 'thread/closed':
                self.closed_threads.add(event['params']['threadId'])

    def _select_session(self, client, slot, params, signature):
        current = slot.current
        reason = 'initial'
        if current is not None:
            if current.signature != signature:
                reason = 'settings_changed'
                self._abandon_standby(client, slot)
            elif current.thread in self.closed_threads:
                reason = 'thread_closed'
            else:
                reason = self._rotation_reason(current)
                if reason is None:
                    return current, True, False, 'continued'
            self._retire(client, current)
            slot.current = None
        candidate = None
        if slot.standby is not None:
            future, slot.standby = slot.standby, None
            try:
                candidate = future.result(timeout=30)
            except TimeoutError:
                if not future.done():
                    raise CodexBackendTimeoutError('等待预建会话完成超过 30 秒') from None
            except Exception:
                pass
            if candidate is not None and (candidate.signature != signature or candidate.thread in self.closed_threads):
                self._retire(client, candidate)
                candidate = None
        prewarmed = candidate is not None
        slot.current = candidate or self._create(client, params, signature)
        return slot.current, False, prewarmed, reason

    @staticmethod
    def _task_prompt(prompt, conversation, continued):
        _, marker, raw = prompt.partition('Task data follows:\n')
        if not marker:
            return prompt, {}
        payload = json.loads(raw)
        seen = set(conversation.seen_ids)
        if continued:
            payload.pop('handoff_memory', None)
            payload['recent'] = [row for row in payload.get('recent', []) if row.get('id') not in seen]
            payload['related_chat_memory'] = [row for row in payload.get('related_chat_memory', []) if row.get('job_id') not in seen]
        else:
            handoff = {row.get('job_id') for row in payload.get('handoff_memory', [])}
            payload['related_chat_memory'] = [row for row in payload.get('related_chat_memory', []) if row.get('job_id') not in handoff]
        return ('Fresh task data supersedes previous project snapshots. Earlier assistant outputs are drafts; '
                'observed chat and confirmed memories are delivery evidence.\nTask data follows:\n'
                + json.dumps(payload, ensure_ascii=False)), payload

    @staticmethod
    def _remember_payload(conversation, payload):
        identifiers = [payload.get('message_id')]
        identifiers += [row.get('id') for row in payload.get('recent', [])]
        for key in ('related_chat_memory', 'handoff_memory'):
            identifiers += [row.get('job_id') for row in payload.get(key, [])]
        for identity in identifiers:
            if identity and identity not in conversation.seen_ids:
                conversation.seen_ids.append(identity)

    def generate(self, command, cwd, workspace, access_dirs, prompt, event_id, schema=None, session_key=None):
        with self.lock:
            started = time.monotonic()
            if self.closed:
                raise CodexBackendError('Codex 后端已经关闭')
            client, reused = self.ensure(command, cwd, access_dirs)
            try:
                self._drain_idle_events(client)
                params = dict(cwd=str(Path(workspace).resolve()), permissions='project_tools_read',
                              approvalPolicy='never', ephemeral=True)
                prefix, marker, _ = prompt.partition('Task data follows:\n')
                if marker and prefix.strip():
                    params['developerInstructions'] = prefix
                hit = False
                if self.config.codex_transport != 'auto':
                    effective, hit = self.effective_config(client, workspace)
                    params.update(transport_overrides(effective, self.config.codex_transport))
                if self.config.codex_model:
                    params['model'] = self.config.codex_model
                signature = hashlib.sha256((json.dumps(params, sort_keys=True) + repr(config_signature(workspace))).encode()).hexdigest()
                key = (str(Path(workspace).resolve()), str(session_key or workspace))
                slot = self.sessions.setdefault(key, ChatSession())
                acquiring = time.monotonic()
                standby_ready = slot.standby is not None and slot.standby.done()
                conversation, continued, prewarmed, reason = self._select_session(client, slot, params, signature)
                acquire_seconds = round(time.monotonic() - acquiring, 3)
                turn_prompt, payload = self._task_prompt(prompt, conversation, continued)
                turn_params = dict(threadId=conversation.thread, input=[dict(type='text', text=turn_prompt)],
                                   permissions='project_tools_read', approvalPolicy='never', summary='concise')
                if schema is not None:
                    turn_params['outputSchema'] = schema
                setup_seconds = time.monotonic() - started
                turn_started = time.monotonic()
                turn = client.rpc('turn/start', turn_params, min(30, self.config.codex_timeout_seconds or 30))['turn']['id']
                text, usage, tools = self.wait_turn(client, conversation.thread, turn, event_id, turn_started)
                conversation.turns += 1
                self._remember_payload(conversation, payload)
                self._maybe_prewarm(client, slot)
                ratio = self._ratio(conversation)
                metrics = dict(shared_backend_pid=client.process.pid, shared_backend_limits=client.transport.info,
                    session_reused=continued, session_prewarmed=prewarmed, session_transition=reason,
                    session_prewarm_ready=prewarmed and standby_ready, session_acquire_seconds=acquire_seconds,
                    session_create_seconds=conversation.create_seconds if not continued else 0,
                    session_turns=conversation.turns, context_used_tokens=conversation.used_tokens,
                    context_window_tokens=self._window(conversation) or None,
                    context_percent=round(ratio, 2) if ratio is not None else None,
                    context_usage_source=('unknown' if ratio is None else
                                          'manual_window' if self.config.codex_context_window_tokens else 'reported_window'),
                    context_rotate_percent=self.config.codex_session_rotate_percent,
                    next_session_rotation_due=self._rotation_reason(conversation) is not None,
                    turn_input_chars=len(turn_prompt),
                    recent_messages_sent=len(payload.get('recent', [])),
                    related_memory_count_sent=len(payload.get('related_chat_memory', [])),
                    handoff_notes_sent=len(payload.get('handoff_memory', [])),
                    next_session_preparing=slot.standby is not None and not slot.standby.done(),
                    next_session_ready=(slot.standby is not None and slot.standby.done()
                                        and not slot.standby.cancelled() and slot.standby.exception() is None))
                return BackendResult(text, usage, tools, reused, hit, round(setup_seconds, 3), metrics)
            except Exception:
                # Never replay a failed or possibly started generation. Restart for the next task only.
                self.discard()
                raise

    def wait_turn(self, client, thread, turn_id, event_id, started):
        last_progress = time.monotonic()
        connection = CodexConnectionWatch(self.config.codex_connect_timeout_seconds)
        active, answers, usage, tool_count, notified = set(), [], {}, 0, False
        while True:
            if self.stop_requested.is_set():
                raise CodexBackendError('Codex 任务已主动停止')
            if client.overflow.is_set():
                raise CodexBackendError('Codex 后端事件队列溢出，当前任务停止，未自动重试')
            now = time.monotonic()
            if self.config.codex_timeout_seconds and now - started >= self.config.codex_timeout_seconds:
                raise CodexGenerationTimeoutError('Codex generation timed out')
            idle = self.config.codex_idle_timeout_seconds
            if idle and not active and now - last_progress >= idle:
                raise CodexIdleTimeoutError('Codex stopped reporting progress')
            try:
                event = client.events.get(timeout=.1)
            except queue.Empty:
                if client.process.poll() is not None:
                    raise CodexBackendError('Codex 常驻后端在生成期间退出')
                continue
            method, params = event['method'], event['params']
            if method == 'connection/closed':
                raise CodexBackendError('Codex 常驻后端连接已断开')
            if method == 'thread/tokenUsage/updated':
                self._accept_usage(client, params)
            if method == 'thread/closed':
                self.closed_threads.add(params['threadId'])
                if params['threadId'] == thread:
                    raise CodexBackendError('当前 Codex 会话已关闭')
            if params.get('threadId') != thread or (params.get('turnId') and params['turnId'] != turn_id):
                continue
            last_progress = time.monotonic()
            if method == 'progress/heartbeat' and params.get('modelProgress'):
                connection.progress()
            if method in ('item/started', 'item/completed'):
                item = params.get('item') or {}
                if item.get('type') in ('reasoning', 'agentMessage', 'commandExecution', 'mcpToolCall', 'dynamicToolCall', 'webSearch'):
                    connection.progress()
                if item.get('type') in ('commandExecution', 'mcpToolCall', 'dynamicToolCall', 'webSearch'):
                    if method == 'item/started':
                        active.add(item.get('id'))
                        tool_count += 1
                        if not notified:
                            self.progress.put((event_id, 'querying'))
                            notified = True
                    else:
                        active.discard(item.get('id'))
                if method == 'item/completed' and item.get('type') == 'agentMessage' and item.get('phase') in (None, 'final_answer'):
                    answers.append(str(item.get('text') or ''))
            elif method == 'thread/tokenUsage/updated':
                mapping = dict(inputTokens='input_tokens', cachedInputTokens='cached_input_tokens',
                               outputTokens='output_tokens', reasoningOutputTokens='reasoning_output_tokens')
                usage = {mapping[k]: v for k, v in params.get('usage', {}).items() if k in mapping}
            elif method == 'turn/completed':
                turn = params['turn']
                if turn.get('id') != turn_id:
                    continue
                if turn.get('status') != 'completed' or turn.get('failed'):
                    if turn.get('serviceFailure'):
                        raise CodexServiceError(turn['serviceFailure'])
                    raise CodexBackendError('Codex 生成失败或被中断')
                if not answers or not answers[-1].strip():
                    raise CodexBackendError('Codex 未返回已完成的最终回复')
                return answers[-1].strip(), usage, tool_count
            elif method == 'error':
                reason = params.get('serviceFailure')
                if reason == 'account':
                    raise CodexServiceError(reason)
                if not params.get('willRetry'):
                    if reason:
                        raise CodexServiceError(reason)
                    raise CodexBackendError('Codex 生成请求失败，请检查本机连接与登录状态')
                if reason == 'network':
                    connection.retry(time.monotonic())

    def request_stop(self):
        self.stop_requested.set()

    def close(self):
        self.request_stop()
        with self.lock:
            self.closed = True
            self.discard()
        self.session_workers.shutdown(wait=True, cancel_futures=True)
