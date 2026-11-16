"""Persistent OpenSSH worker for NAS hashes, streamed inventories and metadata."""
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
import stat
import subprocess
import tempfile
import threading
import time


class RemoteHashError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


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
        host, user, root = raw.get('host', ''), raw.get('username', 'windsky'), raw.get('root_path', '')
        if user is None or user == '': user = 'windsky'
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


# The worker is sent in memory, never installed on the NAS. Reads and explicitly
# planned empty-directory removals use openat/O_NOFOLLOW at every level.
WORKER = r'''
import os, sys, json, hashlib, stat, ctypes
def emit(value):
    print(json.dumps(value, separators=(',', ':')), flush=True)
def stamp(s):
    return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def metadata(s):
    return (s.st_dev,s.st_ino,s.st_mode,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
def scan(fd, prefix=''):
    before=os.fstat(fd)
    with os.scandir(fd) as entries:
        for entry in entries:
            name=entry.name
            if name in ('','.','..') or '\\' in name or '\x00' in name:
                raise ValueError('InvalidRelativePath')
            relative=prefix+name
            info=os.stat(name,dir_fd=fd,follow_symlinks=False)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise ValueError('NotRegularFileOrDirectory:'+relative)
            emit({'entry':relative,'stamp':metadata(info)})
            if stat.S_ISDIR(info.st_mode):
                child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
                try:
                    if metadata(os.fstat(child))!=metadata(info):
                        raise ValueError('DirectoryChanged')
                    scan(child,relative+'/')
                finally:
                    os.close(child)
            if metadata(os.stat(name,dir_fd=fd,follow_symlinks=False))!=metadata(info):
                raise ValueError('EntryChanged')
    if metadata(os.fstat(fd))!=metadata(before):
        raise ValueError('DirectoryChanged')
def open_root(root,create=False):
    if not root.startswith('/') or '..' in root.split('/'):
        raise ValueError('InvalidRoot')
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        parts=list(filter(None,root.split('/')))
        for index,part in enumerate(parts):
            if create and index==len(parts)-1:
                try:os.mkdir(part,dir_fd=fd)
                except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd
    except BaseException:
        os.close(fd);raise

def parts_of(relative):
    if not isinstance(relative,str):
        raise ValueError('InvalidRelativePath')
    parts=relative.split('/')
    if any(p in ('','.','..') or '\\' in p or '\x00' in p for p in parts):
        raise ValueError('InvalidRelativePath')
    return parts

def parent_of(root,relative,create=False):
    parts=parts_of(relative)
    fd=os.dup(root)
    try:
        for part in parts[:-1]:
            if create:
                try:os.mkdir(part,dir_fd=fd)
                except FileExistsError:pass
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd);fd=child
        return fd,parts[-1]
    except BaseException:
        os.close(fd);raise

def move_exclusive(src_fd,src,dst_fd,dst):
    # Linux RENAME_NOREPLACE also prevents replacing an empty directory that
    # appeared after inspection. Unsupported NAS kernels fail closed.
    libc=ctypes.CDLL(None,use_errno=True)
    rename=getattr(libc,'renameat2',None)
    if rename is None:raise RuntimeError('RenameNoReplaceUnavailable')
    rename.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    rename.restype=ctypes.c_int
    if rename(src_fd,os.fsencode(src),dst_fd,os.fsencode(dst),1):
        error=ctypes.get_errno()
        raise OSError(error,os.strerror(error))
try:
    config=json.loads(sys.stdin.readline())
    root=open_root(config['root'],create=True)
    emit({'ready':1})
    for line in sys.stdin:
        try:
            req=json.loads(line)
            if req.get('op') in ('ensure_dirs','rename','archive','rmdir'):
                live_root=open_root(config['root'])
                try:
                    if metadata(os.fstat(live_root))[:2]!=metadata(os.fstat(root))[:2]:
                        raise ValueError('RootChanged')
                finally:os.close(live_root)
            if req.get('op')=='ensure_dirs':
                entries=req['entries']
                if not isinstance(entries,list) or not 1<=len(entries)<=32:
                    raise ValueError('InvalidDirectoryBatch')
                siblings={}
                for relative in entries:
                    parts_of(relative)
                    parent,name=parent_of(root,relative)
                    try:
                        identity=metadata(os.fstat(parent))[:2]
                        if identity not in siblings:
                            names={}
                            for entry in os.listdir(parent):
                                names.setdefault(entry.casefold(),[]).append(entry)
                            siblings[identity]=names
                        names=siblings[identity]
                        matches=names.get(name.casefold(),[])
                        if len(matches)>1:raise ValueError('CaseCollision:'+relative)
                        renamed=False
                        if matches:
                            old=matches[0]
                            child=os.open(old,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                            try:
                                if metadata(os.fstat(child))!=metadata(os.stat(old,dir_fd=parent,follow_symlinks=False)):
                                    raise ValueError('DirectoryChanged')
                                if old!=name:
                                    move_exclusive(parent,old,parent,name)
                                    renamed=True
                            finally:os.close(child)
                        else:
                            os.mkdir(name,dir_fd=parent)
                        names[name.casefold()]=[name]
                        emit({'ensured':relative,'renamed':renamed})
                    finally:os.close(parent)
                emit({'ok':True,'done':True})
                continue
            if req.get('op') in ('rename','archive'):
                relative=req['relative'];destination=req['destination']
                parts_of(relative);parts_of(destination)
                expected=req['stamp']
                if (not isinstance(expected,list) or len(expected)!=6
                        or any(type(v) is not int for v in expected)):
                    raise ValueError('InvalidExpectedMetadata')
                source,name=parent_of(root,relative)
                archive_fd=None;target=None
                try:
                    target_root=root
                    if req['op']=='archive':
                        # Fixed sibling of the authenticated target; never accept
                        # an arbitrary absolute archive destination from a request.
                        parent_path,leaf=config['root'].rstrip('/').rsplit('/',1)
                        archive_parent=open_root(parent_path or '/')
                        try:
                            current=os.stat(leaf,dir_fd=archive_parent,follow_symlinks=False)
                            if metadata(current)[:2]!=metadata(os.fstat(root))[:2] or not stat.S_ISDIR(current.st_mode):
                                raise ValueError('RootChanged')
                            archive_name=leaf+'.nas-archive'
                            try:os.mkdir(archive_name,dir_fd=archive_parent)
                            except FileExistsError:pass
                            archive_fd=os.open(archive_name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=archive_parent)
                            if os.fstat(archive_fd).st_dev!=os.fstat(root).st_dev:
                                raise ValueError('ArchiveNotOnSameFilesystem')
                        finally:os.close(archive_parent)
                        target_root=archive_fd
                    target,dest=parent_of(target_root,destination,create=req['op']=='archive')
                    live=os.stat(name,dir_fd=source,follow_symlinks=False)
                    if list(metadata(live))!=expected or not (stat.S_ISREG(live.st_mode) or stat.S_ISDIR(live.st_mode)):
                        raise ValueError('EntryChanged')
                    move_exclusive(source,name,target,dest)
                    emit({'ok':True,'moved':relative,'destination':destination})
                finally:
                    os.close(source)
                    if target is not None:os.close(target)
                    if archive_fd is not None:os.close(archive_fd)
                continue
            if req.get('op')=='rmdir':
                entries=req['entries']
                if not isinstance(entries,list) or not 1<=len(entries)<=32:
                    raise ValueError('InvalidDirectoryBatch')
                for entry in entries:
                    relative=entry['relative'];parts=relative.split('/')
                    if any(p in ('','.','..') or '\\' in p or '\x00' in p for p in parts):
                        raise ValueError('InvalidRelativePath')
                    expected=entry['identity']
                    if (not isinstance(expected,list) or len(expected)!=2
                            or any(type(v) is not int for v in expected) or expected[1]<=0):
                        raise ValueError('InvalidDirectoryIdentity')
                    parent=os.dup(root)
                    try:
                        for part in parts[:-1]:
                            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                            os.close(parent);parent=child
                        child=os.open(parts[-1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                        try:
                            info=os.fstat(child)
                            live=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
                            if ([info.st_dev,info.st_ino]!=expected or metadata(info)!=metadata(live)):
                                raise ValueError('DirectoryChanged')
                            # rmdir cannot remove files or nonempty directories.
                            os.rmdir(parts[-1],dir_fd=parent)
                        finally:
                            os.close(child)
                    finally:
                        os.close(parent)
                    emit({'removed':relative})
                emit({'ok':True,'done':True})
                continue
            if req.get('op')=='scan':
                # A fresh open file description starts at directory offset 0
                # on every scan; dup(root) would share its directory offset.
                fd=os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
                try:
                    scan(fd)
                finally:
                    os.close(fd)
                emit({'ok':True,'done':True})
                continue
            parts=req['relative'].split('/')
            if any(p in ('','.','..') or '\\' in p or '\x00' in p for p in parts):
                raise ValueError('InvalidRelativePath')
            parent=os.dup(root)
            try:
                for part in parts[:-1]:
                    child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
                    os.close(parent);parent=child
                if req.get('op')=='stat':
                    info=os.stat(parts[-1],dir_fd=parent,follow_symlinks=False)
                    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                        raise ValueError('NotRegularFileOrDirectory')
                    emit({'ok':True,'stamp':metadata(info)})
                    continue
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
        self.reader_stop = threading.Event()
        self.readers = []

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

    def _read(self, stream, messages, stopped):
        def put(value):
            while not stopped.is_set():
                try:
                    messages.put(value, timeout=.1)
                    return
                except queue.Full:
                    continue
        try:
            while not stopped.is_set():
                line = stream.readline(16385)
                if not line:
                    put(None)
                    return
                put(line)
        except (OSError, ValueError):
            put(None)

    def _stderr(self, stream):
        try:
            while line := stream.readline(4096):
                self.errors.append(line.strip())
        except (OSError, ValueError):
            pass

    def _write(self, message):
        try:
            self.process.stdin.write(json.dumps(message, ensure_ascii=True) + '\n')
            self.process.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise RemoteHashError('SSH校验会话已中断。') from exc

    def _send(self, message):
        self._write(message)
        return self._receive()

    def _receive(self):
        deadline = time.monotonic() + self.settings.timeout
        while True:
            self.check()
            if time.monotonic() >= deadline:
                raise RemoteHashError('NAS端操作等待响应超时。')
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
                detail=str(value.get('error', 'unknown'))
                raise RemoteHashError('NAS端拒绝校验：' + detail,
                                      code=value.get('error_type') or detail.split(':',1)[0])
            return value

    @staticmethod
    def _metadata(value):
        stamp = value.get('stamp')
        if (not isinstance(stamp, list) or len(stamp) != 6
                or any(type(v) is not int for v in stamp)
                or stamp[3] < 0
                or not (stat.S_ISREG(stamp[2]) or stat.S_ISDIR(stamp[2]))):
            raise RemoteHashError('NAS端目录属性响应无效。')
        return tuple(stamp)

    def inventory(self):
        """Stream bounded records; an incomplete scan never becomes an inventory."""
        self.start()
        complete = False
        try:
            self._write({'op': 'scan'})
            while True:
                value = self._receive()
                if value.get('done') is True and value.get('ok') is True:
                    complete = True
                    return
                relative = value.get('entry')
                self._relative(relative)
                yield relative, self._metadata(value)
        finally:
            if not complete:
                self.close()  # Discard unread records after cancellation or a bad response.

    def metadata(self, relative):
        self._relative(relative)
        self.start()
        value = self._send({'op': 'stat', 'relative': relative})
        if value.get('ok') is not True:
            raise RemoteHashError('NAS端目录属性响应无效。')
        return self._metadata(value)

    def move(self, relative, destination, stamp, *, archive=False):
        self._relative(relative);self._relative(destination)
        expected=self._metadata({'stamp':list(stamp)})
        self.start()
        try:
            value=self._send({'op':'archive' if archive else 'rename','relative':relative,
                              'destination':destination,'stamp':list(expected)})
            if (value.get('ok') is not True or value.get('moved')!=relative
                    or value.get('destination')!=destination):
                raise RemoteHashError('SSH移动响应与归档或更名计划不一致。')
        except BaseException:
            self.close();raise

    def ensure_directories(self, relatives):
        entries=list(relatives)
        for relative in entries:self._relative(relative)
        for offset in range(0,len(entries),32):
            batch=entries[offset:offset+32]
            self.start()
            complete=False
            try:
                self._write({'op':'ensure_dirs','entries':batch})
                for relative in batch:
                    self.check()
                    value=self._receive()
                    if value.get('ensured')!=relative or type(value.get('renamed')) is not bool:
                        raise RemoteHashError('SSH目录创建响应与计划不一致。')
                    yield relative,value['renamed']
                value=self._receive()
                if value.get('ok') is not True or value.get('done') is not True:
                    raise RemoteHashError('SSH目录创建未确认完成。')
                complete=True
            finally:
                if not complete:self.close()

    def remove_empty_directories(self, nodes):
        """Remove only explicitly planned directory identities, deepest first."""
        batch = []
        for relative, stamp in nodes:
            self._relative(relative)
            stamp = self._metadata({'stamp': list(stamp)})
            if not stat.S_ISDIR(stamp[2]) or stamp[1] <= 0:
                raise RemoteHashError('SSH空目录清理缺少可靠目录身份。')
            batch.append({'relative': relative, 'identity': list(stamp[:2])})
            if len(batch) == 32:
                yield from self._remove_directory_batch(batch)
                batch = []
        if batch:
            yield from self._remove_directory_batch(batch)

    def _remove_directory_batch(self, batch):
        self.start()
        complete = False
        try:
            self._write({'op': 'rmdir', 'entries': batch})
            for entry in batch:
                self.check()
                if self._receive().get('removed') != entry['relative']:
                    raise RemoteHashError('SSH空目录清理响应与计划不一致。')
                yield entry['relative']
            value = self._receive()
            if value.get('done') is not True or value.get('ok') is not True:
                raise RemoteHashError('SSH空目录清理未确认完成。')
            complete = True
        finally:
            if not complete:
                self.close()

    def start(self):
        if self.process is not None:
            return
        self.check()
        try:
            self.messages = queue.Queue(maxsize=8)
            self.reader_stop = threading.Event()
            self.errors.clear()
            self.process = subprocess.Popen(self.command(), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding='utf-8',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
            self.readers = [
                threading.Thread(target=self._read,args=(self.process.stdout,self.messages,self.reader_stop),daemon=True),
                threading.Thread(target=self._stderr,args=(self.process.stderr,),daemon=True)]
            for reader in self.readers:
                reader.start()
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
                probe.unlink(missing_ok=True)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def _relative(relative):
        if not isinstance(relative, str):
            raise RemoteHashError('NAS端相对路径响应无效。')
        parts = PurePosixPath(relative).parts
        if (not relative or relative.startswith('/') or '..' in parts
                or '\\' in relative or '\0' in relative
                or any(p in ('', '.', '..') for p in relative.split('/'))):
            raise RemoteHashError('NAS端校验相对路径无效。')

    def _hash(self, relative, size):
        self._relative(relative)
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
        self.reader_stop.set()
        try:
            process.stdin.close()
            process.wait(timeout=2)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
        finally:
            for stream in (process.stdout, process.stderr):
                stream.close()
            for reader in self.readers:
                reader.join(timeout=1)
            self.readers = []
