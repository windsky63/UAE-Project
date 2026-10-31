"""Authenticated local broker for the WeChat and management Codex adapters.

Only compatible launch settings share a process. Session policy and public
event filtering belong to each adapter; the broker enforces thread ownership.
IPC uses JSON bytes, never deserializes pickle from a connected peer.
"""
from contextlib import contextmanager
import hashlib
import json
from multiprocessing.connection import Client as PipeClient, Listener as PipeListener
import os
from pathlib import Path
import queue
import secrets
import subprocess
import sys
import tempfile
import threading
import time

MAX_ACTIVE_TURNS = 2
MAX_THREADS_PER_CLIENT = 64
IDLE_SECONDS = 120
ORPHAN_GRACE_SECONDS = 10
MAX_FRAME = 4 * 1024 * 1024


class RpcError(RuntimeError):
    pass


class RpcTimeout(TimeoutError):
    pass


def config_signature(cwd):
    home = Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex')
    paths = {home / 'config.toml', home / 'managed_config.toml', home / 'auth.json'}
    current = Path(cwd).resolve()
    for parent in (current, *current.parents):
        paths.add(parent / '.codex/config.toml')
    if os.name == 'nt' and os.environ.get('PROGRAMDATA'):
        paths.add(Path(os.environ['PROGRAMDATA']) / 'OpenAI/Codex/config.toml')
    rows = []
    for path in sorted(paths, key=str):
        try:
            stat = path.stat()
            rows.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        except FileNotFoundError:
            pass
        except OSError:
            rows.append((str(path), 'unreadable'))
    return str(home.resolve()), tuple(rows), os.environ.get('OPENAI_BASE_URL', '')


@contextmanager
def startup_lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0, 2)
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        deadline = time.monotonic() + 25
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise RpcTimeout('等待共享 Codex 后端启动锁超时') from None
                time.sleep(.05)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class RpcProcess:
    """One bounded stdio implementation, exclusively owned by the broker."""
    def __init__(self, command, cwd, notification):
        self.notification = notification
        self.lock = threading.RLock()
        self.pending, self.serial = {}, 0
        self.outbox = queue.Queue(maxsize=64)
        self.closed = False
        self.process = subprocess.Popen(command, cwd=cwd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.reader = threading.Thread(target=self.read, daemon=True)
        self.errors = threading.Thread(target=self.drain, daemon=True)
        self.writer = threading.Thread(target=self.write, daemon=True)
        self.reader.start()
        self.errors.start()
        self.writer.start()
        try:
            self.rpc('initialize', dict(clientInfo=dict(name='project_tools_shared', version='1'),
                capabilities=dict(experimentalApi=True)), 20)
            self.send(dict(method='initialized', params={}))
        except BaseException:
            self.close()
            raise

    def send(self, value):
        if self.closed or self.process.poll() is not None:
            raise RpcError('Codex 连接已断开')
        try:
            self.outbox.put_nowait(value)
        except queue.Full:
            raise RpcError('共享 Codex 请求队列已满') from None

    def write(self):
        try:
            while not self.closed:
                try:
                    value = self.outbox.get(timeout=.1)
                except queue.Empty:
                    continue
                self.process.stdin.write(json.dumps(value, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
        except (OSError, ValueError):
            self.notification(dict(method='connection/closed', params={}))

    def rpc(self, method, params, timeout=20):
        with self.lock:
            self.serial += 1
            identity = self.serial
            target = queue.Queue(maxsize=1)
            self.pending[identity] = target
        try:
            self.send(dict(id=identity, method=method, params=params))
            try:
                result = target.get(timeout=timeout)
            except queue.Empty:
                raise RpcTimeout('Codex 后端请求超时：' + method) from None
            if result.get('error') is not None:
                raise RpcError(str(result['error'].get('message', 'Codex 请求失败'))[:1000])
            return result.get('result') or {}
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def read(self):
        try:
            for line in self.process.stdout:
                if len(line) > MAX_FRAME:
                    raise RpcError('Codex 事件超过大小上限')
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if 'id' in message and 'method' not in message:
                    with self.lock:
                        target = self.pending.get(message['id'])
                    if target:
                        try:
                            target.put_nowait(message)
                        except queue.Full:
                            pass
                elif 'id' in message:
                    if message.get('method') == 'item/tool/call':
                        self.notification(message)
                    else:
                        self.send(dict(id=message['id'], error=dict(code=-32601,
                            message='Project tools do not approve interactive or privileged requests')))
                else:
                    self.notification(message)
        except (OSError, ValueError, RuntimeError):
            pass
        finally:
            with self.lock:
                for target in self.pending.values():
                    try:
                        target.put_nowait(dict(error=dict(message='Codex 连接已断开')))
                    except queue.Full:
                        pass
            if not self.closed:
                self.notification(dict(method='connection/closed', params={}))

    def drain(self):
        while self.process.stderr.read(4096):
            pass  # Never retain raw CLI credentials, diagnostics or tool output.

    def close(self):
        self.closed = True
        if self.process.poll() is None:
            if os.name == 'nt':
                try:
                    subprocess.run(['taskkill', '/PID', str(self.process.pid), '/T', '/F'],
                        capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired):
                    pass
            if self.process.poll() is None:
                self.process.kill()
        self.process.wait(timeout=5)
        for thread in (self.reader, self.errors, self.writer):
            if thread is not threading.current_thread():
                thread.join(timeout=2)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            stream.close()


def send_frame(connection, value):
    data = json.dumps(value, ensure_ascii=False).encode('utf-8')
    if len(data) > MAX_FRAME:
        raise RpcError('共享 Codex 消息超过大小上限')
    connection.send_bytes(data)


def receive_frame(connection):
    value = json.loads(connection.recv_bytes(MAX_FRAME).decode('utf-8'))
    if not isinstance(value, dict):
        raise RpcError('共享 Codex 消息格式错误')
    return value


class Peer:
    def __init__(self, connection):
        self.connection, self.scope = connection, None
        self.outbox = queue.Queue(maxsize=256)
        self.closed = threading.Event()
        self.close_lock = threading.Lock()
        self.writer = threading.Thread(target=self.write, daemon=True)
        self.writer.start()

    def put(self, value, wait=False):
        done = threading.Event() if wait else None
        try:
            self.outbox.put_nowait((value, done))
        except queue.Full:
            self.close()
        if done:
            done.wait(timeout=2)

    def write(self):
        try:
            while not self.closed.is_set():
                try:
                    value, done = self.outbox.get(timeout=.1)
                except queue.Empty:
                    continue
                send_frame(self.connection, value)
                if done:
                    done.set()
        except (OSError, EOFError, ValueError, RuntimeError):
            self.close()

    def close(self):
        with self.close_lock:
            if not self.closed.is_set():
                self.closed.set()
                self.connection.close()


class Broker:
    def __init__(self, command, cwd, idle_seconds=IDLE_SECONDS):
        self.lock = threading.RLock()
        self.peers, self.owners, self.active = set(), {}, {}
        self.tool_requests = {}
        self.closed = threading.Event()
        self.shutdown_lock = threading.Lock()
        self.raw_stopped = False
        self.idle_seconds = idle_seconds
        self.idle_expired = False
        self.failed = False
        self.last_activity = time.monotonic()
        self.raw = RpcProcess(command, cwd, self.notification)

    def stop_raw(self):
        with self.shutdown_lock:
            if not self.raw_stopped:
                self.raw_stopped = True
                self.raw.close()

    def notification(self, message):
        method, params = message.get('method'), message.get('params') or {}
        thread = params.get('threadId')
        with self.lock:
            if method == 'item/tool/call' and 'id' in message:
                owner = self.owners.get(thread)
                if owner is not None and owner.scope == 'web' and not owner.closed.is_set() and thread in self.active:
                    self.tool_requests[message['id']] = (owner, thread)
                    owner.put(dict(event=message))
                else:
                    self.raw.send(dict(id=message['id'], result=dict(success=False,
                        contentItems=[dict(type='inputText', text='当前入口未授权业务工具调用')])) )
                return
            if method == 'turn/completed':
                self.active.pop(thread, None)
                self.last_activity = time.monotonic()
            elif method == 'turn/started' and thread in self.active:
                active = self.active[thread]
                active['turn'] = (params.get('turn') or {}).get('id')
                if active['peer'].closed.is_set() and active['turn']:
                    self.raw.send(dict(id='late-cancel-' + secrets.token_hex(4), method='turn/interrupt',
                                       params=dict(threadId=thread, turnId=active['turn'])))
            if method == 'connection/closed':
                self.failed = True
                self.closed.set()
                targets = list(self.peers)
            else:
                owner = self.owners.get(thread)
                targets = [owner] if owner is not None else []
                if method == 'thread/closed':
                    self.owners.pop(thread, None)
                    self.active.pop(thread, None)
        for peer in targets:
            peer.put(dict(event=message))

    def request(self, peer, method, params, timeout):
        if method in ('initialize', 'initialized'):
            return {}  # Exactly one backend initialize, performed by RpcProcess.
        if method == 'project/tool-response':
            with self.lock:
                pending = self.tool_requests.get(params.get('requestId'))
                if pending is None or pending[0] is not peer or peer.scope != 'web':
                    raise RpcError('不能回答其他入口的工具调用')
                result = params.get('result')
                if not isinstance(result, dict) or not isinstance(result.get('success'), bool):
                    raise RpcError('工具返回格式无效')
                self.tool_requests.pop(params['requestId'])
                self.raw.send(dict(id=params['requestId'], result=result))
            return {}
        if method not in ('config/read', 'thread/start', 'thread/resume', 'thread/unsubscribe', 'turn/start', 'turn/interrupt'):
            raise RpcError('共享 Codex 不支持该协议操作')
        thread = params.get('threadId')
        with self.lock:
            self.last_activity = time.monotonic()
            if method in ('thread/start', 'thread/resume', 'turn/start'):
                allowed = ('project_tools_read',) if peer.scope == 'wechat' else ('project_tools_read', 'project_tools_edit')
                if params.get('permissions') not in allowed or params.get('approvalPolicy') != 'never':
                    raise RpcError('当前入口没有请求该权限的授权')
                if method == 'thread/start' and peer.scope == 'wechat' and params.get('ephemeral') is not True:
                    raise RpcError('微信入口只允许隔离的临时会话')
                if params.get('dynamicTools') and peer.scope != 'web':
                    raise RpcError('当前入口未启用业务操作工具')
            if thread and self.owners.get(thread) is not peer:
                if method == 'thread/resume' and thread not in self.owners:
                    if sum(owner is peer for owner in self.owners.values()) >= MAX_THREADS_PER_CLIENT:
                        raise RpcError('当前入口已达驻留会话上限，请释放旧会话')
                    self.owners[thread] = peer
                else:
                    raise RpcError('不能操作其他入口的 Codex 会话')
            if method == 'thread/start' and sum(owner is peer for owner in self.owners.values()) >= MAX_THREADS_PER_CLIENT:
                raise RpcError('当前入口已达驻留会话上限，请释放旧会话')
            if method == 'turn/start':
                if (thread in self.active or len(self.active) >= MAX_ACTIVE_TURNS
                        or any(item['peer'] is peer for item in self.active.values())):
                    raise RpcError('共享 Codex 当前生成数已达上限，请稍后再试')
                self.active[thread] = dict(peer=peer, turn=None)
        try:
            result = self.raw.rpc(method, params, timeout)
        except BaseException as exc:
            with self.lock:
                if method == 'turn/start':
                    # A timeout may have started generation: do not free the
                    # global slot until the real completion/disconnect arrives.
                    if not isinstance(exc, RpcTimeout) or self.raw.process.poll() is not None:
                        self.active.pop(thread, None)
                if method == 'thread/resume':
                    self.owners.pop(thread, None)
            raise
        with self.lock:
            if method in ('thread/start', 'thread/resume'):
                self.owners[result['thread']['id']] = peer
            elif method == 'thread/unsubscribe':
                self.owners.pop(thread, None)
            elif method == 'turn/start' and thread in self.active:
                self.active[thread]['turn'] = result['turn']['id']
        return result

    def serve_peer(self, connection):
        peer = Peer(connection)
        with self.lock:
            self.peers.add(peer)
            self.last_activity = time.monotonic()
        try:
            while not peer.closed.is_set() and not self.closed.is_set():
                request = receive_frame(connection)
                identity = request.get('id')
                if request.get('op') == 'close':
                    with self.lock:
                        last = len(self.peers) == 1
                        if last:
                            self.closed.set()
                    if last:
                        self.stop_raw()
                    peer.put(dict(id=identity, result=dict(detached=True)), wait=True)
                    break
                try:
                    if request.get('op') == 'hello':
                        if request.get('scope') not in ('wechat', 'web'):
                            raise RpcError('Unknown Codex entry point')
                        peer.scope = request['scope']
                        result = dict(pid=self.raw.process.pid, max_active_turns=MAX_ACTIVE_TURNS,
                                      max_threads_per_client=MAX_THREADS_PER_CLIENT, idle_seconds=self.idle_seconds)
                    elif peer.scope:
                        result = self.request(peer, request['method'], request.get('params') or {},
                                              min(60, max(.05, float(request.get('timeout', 20)))))
                    else:
                        raise RpcError('Codex 连接尚未初始化')
                    peer.put(dict(id=identity, result=result))
                except Exception as exc:
                    peer.put(dict(id=identity, error=dict(message=str(exc)[:1000], timeout=isinstance(exc, TimeoutError))))
        except (OSError, EOFError, ValueError, RuntimeError):
            pass
        finally:
            peer.close()
            with self.lock:
                self.peers.discard(peer)
                for request_id, (owner, _) in list(self.tool_requests.items()):
                    if owner is peer:
                        self.tool_requests.pop(request_id)
                        try:
                            self.raw.send(dict(id=request_id, result=dict(success=False,
                                contentItems=[dict(type='inputText', text='业务入口已断开；不得自动重发操作')])) )
                        except (OSError, RuntimeError):
                            pass
                owned = [thread for thread, owner in self.owners.items() if owner is peer]
                for thread in owned:
                    self.owners.pop(thread, None)
                    active = self.active.get(thread)
                    if active:
                        active['detached_at'] = time.monotonic()
                    try:
                        if active and active.get('turn'):
                            self.raw.send(dict(id='detach-' + secrets.token_hex(4), method='turn/interrupt',
                                               params=dict(threadId=thread, turnId=active['turn'])))
                        self.raw.send(dict(id='detach-' + secrets.token_hex(4), method='thread/unsubscribe', params=dict(threadId=thread)))
                    except (OSError, ValueError, RuntimeError):
                        pass
                if not self.peers:
                    self.closed.set()

    def monitor(self, endpoint, family):
        while not self.closed.wait(.1):
            with self.lock:
                if not self.active and time.monotonic() - self.last_activity >= self.idle_seconds:
                    self.idle_expired = True
                    self.closed.set()
                elif self.orphans_expired(time.monotonic()):
                    # A disconnected entry's interrupt may never complete. Only
                    # recycle once no live entry is generating; never kill a peer.
                    self.failed = True
                    self.closed.set()
        if self.idle_expired or self.failed:
            for peer in list(self.peers):
                peer.put(dict(event=dict(method='connection/closed', params={})), wait=True)
                peer.close()
        deadline = time.monotonic() + 2
        while self.peers and time.monotonic() < deadline:
            time.sleep(.01)
        self.stop_raw()
        if family == 'AF_UNIX':
            Path(endpoint).unlink(missing_ok=True)
        os._exit(0)

    def orphans_expired(self, now):
        return bool(self.active) and all(
            item['peer'].closed.is_set() and now - item.get('detached_at', now) >= ORPHAN_GRACE_SECONDS
            for item in self.active.values())


_launched = []  # Retain Popen handles until brokers exit; they outlive an adapter.


class LeaseProcess:
    """Adapter liveness, not ownership of the shared OS process."""
    def __init__(self, client, pid):
        self.client, self.pid = client, pid

    def poll(self):
        return 0 if self.client.closed.is_set() else None


class SharedConnection:
    def __init__(self, command, cwd, scope, callback, *, idle_seconds=IDLE_SECONDS):
        self.callback, self.scope = callback, scope
        self.closed = threading.Event()
        self.lock = threading.RLock()
        self.rpc_lock = threading.Lock()
        self.close_lock = threading.Lock()
        self.pipe_closed = False
        self.serial, self.pending = 0, {}
        self.events = queue.Queue(maxsize=256)
        self.outbox = queue.Queue(maxsize=32)
        self.root = Path(cwd).resolve()
        identity = json.dumps([2, command, str(self.root), config_signature(cwd), idle_seconds], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(identity.encode()).hexdigest()[:32]
        # Registry belongs to this installation, not an arbitrary task cwd.
        # Creating a new cwd/data folder would also change automatic directory
        # grants and cause needless backend restarts on the next web turn.
        directory = Path(__file__).resolve().parents[1] / 'data' / 'shared-codex' / digest
        with startup_lock(directory / 'startup.lock'):
            key_path = directory / 'auth.key'
            if not key_path.exists():
                with key_path.open('xb') as stream:
                    stream.write(secrets.token_bytes(32))
                if os.name != 'nt':
                    key_path.chmod(0o600)
            key = key_path.read_bytes()
            metadata = directory / 'endpoint.json'
            connection = None
            result = None
            try:
                info = json.loads(metadata.read_text(encoding='utf-8'))
                connection = PipeClient(info['address'], family=info['family'], authkey=key)
                result = self.handshake(connection, scope)
            except (OSError, EOFError, ValueError, KeyError, RuntimeError, TimeoutError):
                if connection is not None:
                    connection.close()
                connection = None
            if connection is None:
                token = digest[:12] + '-' + secrets.token_hex(6)
                family = 'AF_PIPE' if os.name == 'nt' else 'AF_UNIX'
                address = r'\\.\pipe\ProjectToolsCodex-' + token if os.name == 'nt' else str(Path(tempfile.gettempdir()) / ('ptcodex-' + token + '.sock'))
                request = dict(command=command, cwd=str(self.root), family=family, address=address, key=str(key_path), idle_seconds=idle_seconds)
                # stdin carries UTF-8 JSON, including Chinese installation paths.
                # The parent's -X utf8 flag is not inherited by a new interpreter.
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-u', str(Path(__file__).resolve()), '--broker'],
                    stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
                _launched[:] = [item for item in _launched if item.poll() is None]
                _launched.append(process)
                process.stdin.write(json.dumps(request, ensure_ascii=False).encode('utf-8'))
                process.stdin.close()
                deadline = time.monotonic() + 25
                while connection is None:
                    try:
                        connection = PipeClient(address, family=family, authkey=key)
                    except (OSError, EOFError):
                        if process.poll() is not None or time.monotonic() >= deadline:
                            if process.poll() is None:
                                process.kill()
                            raise RpcError('无法启动共享 Codex 后端，请检查 CLI 和登录配置') from None
                        time.sleep(.05)
                metadata.write_text(json.dumps(dict(address=address, family=family)), encoding='utf-8')
                result = self.handshake(connection, scope)
            self.connection = connection
            self.info = result
            self.process = LeaseProcess(self, result['pid'])
            self.reader = threading.Thread(target=self.read, daemon=True, name='shared-codex-reader')
            self.dispatcher = threading.Thread(target=self.dispatch, daemon=True, name='shared-codex-events')
            self.writer = threading.Thread(target=self.write, daemon=True, name='shared-codex-writer')
            self.reader.start()
            self.dispatcher.start()
            self.writer.start()

    @staticmethod
    def handshake(connection, scope):
        send_frame(connection, dict(id=0, op='hello', scope=scope))
        if not connection.poll(20):
            raise RpcTimeout('共享 Codex 连接初始化超时')
        response = receive_frame(connection)
        if response.get('id') != 0 or not (response.get('result') or {}).get('pid'):
            raise RpcError('共享 Codex 连接已关闭')
        return response['result']

    def request(self, value, timeout):
        # One in-flight request per entry keeps cancelled/timed-out requests
        # from sitting behind a slow RPC and starting a hidden turn afterwards.
        if not self.rpc_lock.acquire(timeout=max(.05, timeout)):
            raise RpcTimeout('等待当前入口的 Codex 请求完成超时，未提交新请求')
        try:
            return self._request(value, timeout)
        finally:
            self.rpc_lock.release()

    def _request(self, value, timeout):
        with self.lock:
            if self.closed.is_set():
                raise RpcError('共享 Codex 连接已断开')
            if len(self.pending) >= 64:
                raise RpcError('共享 Codex 待处理请求过多')
            self.serial += 1
            identity = self.serial
            value['id'] = identity
            target = queue.Queue(maxsize=1)
            self.pending[identity] = target
            try:
                self.outbox.put_nowait(value)
            except queue.Full:
                self.pending.pop(identity, None)
                raise RpcError('共享 Codex 请求队列已满') from None
        try:
            try:
                response = target.get(timeout=timeout + .5)
            except queue.Empty:
                self.closed.set()
                self.close_pipe()
                raise RpcTimeout('共享 Codex 请求超时') from None
            if response.get('error'):
                error = response['error']
                raise (RpcTimeout if error.get('timeout') else RpcError)(error.get('message', '共享 Codex 请求失败'))
            return response.get('result') or {}
        finally:
            with self.lock:
                self.pending.pop(identity, None)

    def rpc(self, method, params, timeout=20):
        return self.request(dict(method=method, params=params, timeout=timeout), timeout)

    def write(self):
        try:
            while not self.closed.is_set():
                try:
                    value = self.outbox.get(timeout=.1)
                except queue.Empty:
                    continue
                send_frame(self.connection, value)
        except (OSError, ValueError, RuntimeError):
            self.closed.set()
            self.close_pipe()

    def read(self):
        try:
            while not self.closed.is_set():
                message = receive_frame(self.connection)
                if 'event' in message:
                    self.events.put_nowait(message['event'])
                    if message['event'].get('method') == 'connection/closed':
                        break
                else:
                    with self.lock:
                        target = self.pending.get(message.get('id'))
                    if target:
                        target.put_nowait(message)
        except (OSError, EOFError, ValueError, RuntimeError, queue.Full):
            pass
        finally:
            self.closed.set()
            self.close_pipe()
            with self.lock:
                for target in self.pending.values():
                    try:
                        target.put_nowait(dict(error=dict(message='共享 Codex 连接已断开')))
                    except queue.Full:
                        pass

    def dispatch(self):
        try:
            while not self.closed.is_set() or not self.events.empty():
                try:
                    message = self.events.get(timeout=.1)
                except queue.Empty:
                    continue
                self.callback(message)
        except Exception:
            self.closed.set()
            self.close_pipe()
        finally:
            try:
                self.callback(dict(method='connection/closed', params={}))
            except Exception:
                pass

    def close_pipe(self):
        with self.close_lock:
            if not self.pipe_closed:
                self.pipe_closed = True
                self.connection.close()

    def close(self):
        if not self.closed.is_set():
            try:
                self.request(dict(op='close'), 12)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                pass
        self.closed.set()
        self.close_pipe()
        for thread in (getattr(self, 'reader', None), getattr(self, 'dispatcher', None), getattr(self, 'writer', None)):
            if thread and thread is not threading.current_thread():
                thread.join(timeout=2)


def main():
    request = json.load(sys.stdin)
    broker = Broker(request['command'], request['cwd'], idle_seconds=request.get('idle_seconds', IDLE_SECONDS))
    listener = PipeListener(request['address'], family=request['family'], authkey=Path(request['key']).read_bytes())
    threading.Thread(target=broker.monitor, args=(request['address'], request['family']), daemon=True).start()
    while not broker.closed.is_set():
        try:
            connection = listener.accept()
        except (OSError, EOFError):
            break
        threading.Thread(target=broker.serve_peer, args=(connection,), daemon=True).start()


if __name__ == '__main__' and sys.argv[1:] == ['--broker']:
    main()
