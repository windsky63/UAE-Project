"""Optional persistent OpenSSH worker: hash NAS bytes without SMB transfer."""
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from collections import deque
import base64
import hashlib
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import tempfile
import threading
import time


class RemoteHashError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteHashSettings:
    host: str
    username: str
    root: str
    port: int = 22
    identity_file: str = ''
    known_hosts_file: str = ''
    python: str = 'python3'
    timeout: int = 3600
    fallback: bool = False

    @classmethod
    def parse(cls, raw, base):
        if not isinstance(raw, dict) or type(raw.get('enabled', False)) is not bool:
            raise ValueError('remote_hash必须是配置对象，enabled必须为布尔值。')
        if not raw.get('enabled', False):
            return None
        host, user, root = (raw.get(k, '') for k in ('host', 'username', 'root_path'))
        if not isinstance(host, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]*', host):
            raise ValueError('remote_hash.host无效。')
        if not isinstance(user, str) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*', user):
            raise ValueError('remote_hash.username无效。')
        if (not isinstance(root, str) or not root.startswith('/') or root == '/'
                or any(c in root for c in '\r\n\0') or '..' in root.split('/')):
            raise ValueError('remote_hash.root_path须为NAS内目标目录的绝对路径。')
        python = raw.get('python_command', 'python3')
        if not isinstance(python, str) or not re.fullmatch(r'[A-Za-z0-9_./-]+', python) or python.startswith('-'):
            raise ValueError('remote_hash.python_command无效。')
        port, timeout = raw.get('port', 22), raw.get('timeout_seconds', 3600)
        if type(port) is not int or not 1 <= port <= 65535 or type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError('remote_hash端口或单文件校验超时无效。')
        fallback = raw.get('fallback_to_smb', False)
        if type(fallback) is not bool:
            raise ValueError('remote_hash.fallback_to_smb须为布尔值。')
        def local(name):
            value = raw.get(name, '')
            if not isinstance(value, str) or any(c in value for c in '\r\n\0'):
                raise ValueError('remote_hash.' + name + '无效。')
            return str((Path(base)/value).resolve()) if value else ''
        return cls(host, user, root.rstrip('/'), port, local('identity_file'),
                   local('known_hosts_file'), python, timeout, False)


# The worker is sent in memory, never installed on the NAS. All filesystem
# operations here are read-only. openat/O_NOFOLLOW rejects links at every level.
WORKER = r'''
import os, sys, json, hashlib, stat
def emit(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)
def stamp(s):
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def open_root(root):
    if not root.startswith('/') or '..' in root.split('/'):
        raise ValueError('InvalidRoot')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in filter(None,root.split('/')):
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise
try:
    config=json.loads(sys.stdin.readline())
    root=open_root(config['root'])
    emit({'ready':1})
    for line in sys.stdin:
        try:
            req=json.loads(line);parts=req['relative'].split('/')
            if any(p in ('','.','..') or '\\' in p or '\x00' in p for p in parts):
                raise ValueError('InvalidRelativePath')
            parent=os.dup(root)
            try:
                for part in parts[:-1]:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                    os.close(parent);parent=child
                fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
                with os.fdopen(fd,'rb') as stream:
                    before=os.fstat(stream.fileno())
                    if not stat.S_ISREG(before.st_mode):
                        raise ValueError('NotRegularFile')
                    digest=hashlib.sha256()
                    while True:
                        block=stream.read(1048576)
                        if not block:break
                        digest.update(block)
                    after=os.fstat(stream.fileno())
                live=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
                if stamp(before)!=stamp(after) or stamp(after)!=stamp(live):
                    raise ValueError('FileChanged')
                emit({'ok':True,'size':before.st_size,'sha256':digest.hexdigest()})
            finally:
                os.close(parent)
        except Exception as exc:
            emit({'ok':False,'error':type(exc).__name__+':'+str(exc)[:80]})
    os.close(root)
except Exception as exc:
    emit({'ok':False,'error':type(exc).__name__})
    sys.exit(1)
'''


class RemoteHasher:
    def __init__(self, settings, target, check=lambda: None):
        self.settings, self.target, self.check = settings, Path(target), check
        self.process = None
        self.messages = queue.Queue(maxsize=8)
        self.errors = deque(maxlen=4)

    def command(self):
        ssh = shutil.which('ssh')
        if not ssh:
            raise RemoteHashError('未找到OpenSSH客户端。')
        cfg = self.settings
        command = [ssh, '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
                   '-o', 'ConnectTimeout=10', '-o', 'ServerAliveInterval=15',
                   '-o', 'ServerAliveCountMax=2', '-o', 'ForwardAgent=no',
                   '-o', 'ClearAllForwardings=yes', '-p', str(cfg.port), '-l', cfg.username]
        if cfg.identity_file:
            command += ['-o', 'IdentitiesOnly=yes', '-i', cfg.identity_file]
        if cfg.known_hosts_file:
            command += ['-o', 'UserKnownHostsFile=' + cfg.known_hosts_file]
        loader = 'import base64;exec(base64.b64decode(' + repr(base64.b64encode(WORKER.encode()).decode()) + '))'
        command += [cfg.host, shlex.quote(cfg.python) + ' -u -c ' + shlex.quote(loader)]
        return command

    def _read(self, stream):
        try:
            while True:
                line = stream.readline(16385)
                if not line:
                    self.messages.put(None)
                    return
                self.messages.put(line)
        except (OSError, ValueError):
            self.messages.put(None)

    def _stderr(self, stream):
        try:
            while line := stream.readline(4096):
                self.errors.append(line.strip())
        except (OSError, ValueError):
            pass

    def _send(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=True) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RemoteHashError('SSH校验会话已中断。') from exc
        deadline = time.monotonic() + self.settings.timeout
        while True:
            self.check()
            if time.monotonic() >= deadline:
                raise RemoteHashError('NAS端单文件校验超时。')
            try:
                line = self.messages.get(timeout=min(.25, max(.001, deadline-time.monotonic())))
            except queue.Empty:
                continue
            if line is None:
                raise RemoteHashError('SSH会话退出；请检查密钥、主机指纹、目录权限及NAS上的Python3。')
            try:
                value = json.loads(line)
            except (ValueError, TypeError) as exc:
                raise RemoteHashError('NAS端返回了无效校验响应。') from exc
            if not isinstance(value, dict) or len(line) > 16384:
                raise RemoteHashError('NAS端校验响应格式无效。')
            if value.get('ok') is False:
                raise RemoteHashError('NAS端拒绝校验：' + str(value.get('error', 'unknown')))
            return value

    def start(self):
        if self.process is not None:
            return
        self.check()
        try:
            self.process = subprocess.Popen(self.command(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            threading.Thread(target=self._read,args=(self.process.stdout,),daemon=True).start()
            threading.Thread(target=self._stderr,args=(self.process.stderr,),daemon=True).start()
            if self._send({'root': self.settings.root}).get('ready') != 1:
                raise RemoteHashError('NAS端校验协议不匹配。')
            # Bind the SSH path to the actual SMB target, rather than trusting a
            # configured Linux path merely because a same-named file exists.
            with tempfile.NamedTemporaryFile(prefix='.fusionlive-hash-probe-',dir=self.target,delete=False) as stream:
                probe = Path(stream.name)
                content = os.urandom(32)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                if self._hash(probe.name, len(content)) != hashlib.sha256(content).hexdigest():
                    raise RemoteHashError('NAS内部路径与SMB目标不一致。')
            finally:
                probe.unlink()
        except BaseException:
            self.close()
            raise

    def _hash(self, relative, size):
        parts = PurePosixPath(relative).parts
        if (not relative or relative.startswith('/') or '..' in parts
                or '\\' in relative or '\0' in relative):
            raise RemoteHashError('NAS端校验相对路径无效。')
        result = self._send({'relative': relative})
        digest = result.get('sha256')
        if (result.get('ok') is not True or type(result.get('size')) is not int
                or result['size'] != size or not isinstance(digest, str)
                or not re.fullmatch('[0-9a-f]{64}', digest)):
            raise RemoteHashError('NAS端大小或SHA256响应无效。')
        return digest

    def hash(self, relative, size):
        self.start()
        return self._hash(relative, size)

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=2)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
        finally:
            for stream in (process.stdout, process.stderr):
                stream.close()
