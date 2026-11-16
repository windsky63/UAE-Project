"""Portable operational logging. Managed processes use the centre's implementation."""
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import threading
import time

BEIJING = timezone(timedelta(hours=8))
DEFAULTS = dict(progress_seconds=30, idle_seconds=300, max_bytes=5*1024*1024,
                backups=4, detailed=False)
ENV = {key: 'PROJECT_LOG_'+key.upper() for key in DEFAULTS}


def utf8_streams():
    # CLI entry points opt in; importing a tool as a library does not change its host.
    for stream in (sys.stdout, sys.stderr):
        configure = getattr(stream, 'reconfigure', None)
        if configure:
            configure(encoding='utf-8', errors='replace')


def options():
    result = dict(DEFAULTS)
    for key in DEFAULTS:
        value = os.environ.get(ENV[key])
        if value is None:
            continue
        try:
            if key == 'detailed':
                result[key] = value == '1'
            else:
                number = int(value)
                lower, upper = {'progress_seconds': (5, 3600), 'idle_seconds': (30, 86400),
                                'max_bytes': (1024, 100*1024*1024), 'backups': (1, 20)}[key]
                if lower <= number <= upper:
                    result[key] = number
        except ValueError:
            pass
    return result


def managed_environment(manager, env):
    """The centre overrides inherited debug/noisy settings for every managed tool."""
    manager = Path(manager).resolve()
    config = manager/'config/logging.json'
    if not config.is_file():
        config = manager/'config/logging.example.json'
    values = dict(DEFAULTS)
    if config.is_file():
        raw = json.loads(config.read_text(encoding='utf-8-sig'))
        if not isinstance(raw, dict) or set(raw)-set(DEFAULTS):
            raise ValueError('日志配置字段无效：'+str(config))
        for key, value in raw.items():
            if key == 'detailed':
                valid = type(value) is bool
            else:
                bounds = {'progress_seconds': (5, 3600), 'idle_seconds': (30, 86400),
                          'max_bytes': (1024, 100*1024*1024), 'backups': (1, 20)}[key]
                valid = type(value) is int and bounds[0] <= value <= bounds[1]
            if not valid:
                raise ValueError('日志配置数值无效：'+key)
            values[key] = value
    env.update({ENV[key]: str(int(value)) for key, value in values.items()})
    env.update(PROJECT_MANAGER_HOME=str(manager), PROJECT_TOOLS_LOG_CAPTURED='1',
               FUSIONLIVE_LOG_DETAIL='1' if values['detailed'] else '0', PYTHONUNBUFFERED='1')
    return values


def redact(value):
    text = re.sub(r'(?im)(\b(?:Cookie|Set-Cookie):\s*)[^\r\n]+', r'\1[已隐藏]', str(value))
    pattern = r'''(?i)((?:["']?(?:password|passwd|pwd|token|client[_-]?secret|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|cookie|secret)["']?)\s*[:=]\s*)("[^"\r\n]*"|'[^'\r\n]*'|Bearer\s+[^\s,;}]+|[^\s,;}&]+)'''
    def replace(match):
        hidden = '"[已隐藏]"' if match[2].startswith('"') else "'[已隐藏]'" if match[2].startswith("'") else '[已隐藏]'
        return match[1]+hidden
    text = re.sub(pattern, replace, text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+', 'Bearer [已隐藏]', text)
    return re.sub(r'(https?://)[^/@\s:]+:[^/@\s]+@', r'\1[已隐藏]@', text)


def level(message):
    explicit = re.search(r'\[(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\]', message, re.I)
    if explicit:
        return {'WARN': 'WARNING', 'FATAL': 'CRITICAL'}.get(explicit[1].upper(), explicit[1].upper())
    if message.startswith(('下载完成', '开始文件', '文件大小', '阶段：', 'NAS处理心跳：', 'ima同步心跳：', '微信监听心跳：')):
        return 'INFO'
    message = re.sub(r'''(?i)["']?\b(?:error|errors|failed|conflict|uncertain)["']?\s*[:=]\s*(?:0|null|None|false|\[\]|\{\})(?=[,}\s]|$)''', '', message)
    evidence = re.sub(r'(?:失败|错误|异常|冲突|待核对|待核实)\s*[:：=]?\s*0\b', '', message)
    evidence = re.sub(r'无(?:失败|错误|异常)|(?:error|exception)\s*[:=]\s*(?:null|None)\b', '', evidence, flags=re.I)
    evidence = evidence.replace('含待核对冲突', '').replace('核对重复和冲突', '')
    if re.search(r'失败|异常|Traceback|\b(?:error|failed|exception)\b|\b\w+(?:Error|Exception):', evidence, re.I):
        return 'ERROR'
    if re.search(r'警告|冲突|待核对|待核实|重试|\b(?:warning|uncertain|conflict)\b', evidence, re.I):
        return 'WARNING'
    return 'INFO'


def log_path(base, tool, name, at=None):
    at = at or datetime.now(BEIJING)
    manager = os.environ.get('PROJECT_MANAGER_HOME')
    root = Path(os.environ.get('PROJECT_LOG_ROOT') or Path(manager).resolve().parent/'logs') if manager else Path(base).resolve()/'logs'
    return root/at.astimezone(BEIJING).strftime('%Y-%m-%d')/tool/name


def formatted(message, tool, severity=None):
    text = redact(message).rstrip('\r\n')
    if re.match(r'^\[\d{4}-\d\d-\d\d', text):
        return text
    at = datetime.now(BEIJING).isoformat(timespec='seconds')
    run = os.environ.get('PROJECT_TOOLS_RUN_ID', 'standalone')
    return f'[{at}] [{severity or level(text)}] [{tool} PID={os.getpid()} run={run}] {text}'


_locks = {}
_guard = threading.Lock()


class DailyLog:
    """Resolve the Beijing date at each write; keep no open file across rotations."""
    def __init__(self, base, tool, name, *, captured=False):
        self.base, self.tool, self.name, self.captured = base, tool, name, captured

    def write(self, text):
        if self.captured and os.environ.get('PROJECT_TOOLS_LOG_CAPTURED') == '1':
            return len(text)
        path = log_path(self.base, self.tool, self.name)
        data = redact(text).encode('utf-8', errors='replace')
        config = options()
        maximum, backups = config['max_bytes'], config['backups']
        if len(data) > maximum:
            data = data[:maximum-96].decode('utf-8', errors='ignore').encode('utf-8')+'\n[单条日志过长，已截断]\n'.encode('utf-8')
        with _guard:
            lock = _locks.setdefault(str(Path(self.base).resolve())+':'+self.tool, threading.RLock())
        with lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            def archive(i): return path.with_name(f'{path.stem}.{i}{path.suffix}')
            if path.exists() and path.stat().st_size+len(data) > maximum:
                archive(backups).unlink(missing_ok=True)
                for i in range(backups-1, 0, -1):
                    if archive(i).exists(): archive(i).replace(archive(i+1))
                path.replace(archive(1))
            with path.open('ab') as output:
                output.write(data)
        return len(text)

    def flush(self):
        pass

    def close(self):
        pass


def capture_output(pipe, output, tool):
    """Continuously drain child output; preserve errors and sample protocol progress."""
    if pipe is None or not hasattr(pipe, '__iter__'):
        return None
    def read():
        last, previous_phase, warned = float('-inf'), None, False
        try:
            with pipe:
                for value in pipe:
                    line = value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value
                    if line.startswith('@@PROGRESS '):
                        try:
                            value = json.loads(line[len('@@PROGRESS '):])
                            phase = value.get('phase', previous_phase or '处理')
                            done = value.get('download_done_files', value.get('associated_done'))
                            total = value.get('download_total_files', value.get('associated_total'))
                            at = time.monotonic()
                            if phase == previous_phase and at-last < options()['progress_seconds'] and not (total is not None and done == total) and not (value.get('error') or value.get('errors')):
                                continue
                            last, previous_phase = at, phase
                            line = '阶段：'+str(phase)+(f'；完成 {done}/{total}' if done is not None and total is not None else '')
                            if value.get('current_file'): line += '；当前文件：'+str(value['current_file'])
                            if value.get('error') or value.get('errors'): line = '进度报告异常：'+line+'；原因='+str(value.get('error') or value.get('errors'))
                        except (ValueError, TypeError):
                            line = '进度事件格式无效，保留其他任务输出。'
                    if line.strip():
                        try: output.write(formatted(line, tool)+'\n')
                        except OSError as exc:
                            if not warned:
                                print(f'子进程日志写入失败：{type(exc).__name__}：{redact(exc)}；继续排空输出。', file=sys.stderr, flush=True)
                                warned = True
        except (OSError, ValueError) as exc:
            try: output.write(formatted(f'子进程输出读取异常：{type(exc).__name__}：{exc}', tool, 'ERROR')+'\n')
            except OSError: pass
    thread = threading.Thread(target=read, name=tool+'-log-drain', daemon=True)
    thread.start()
    return thread


class StreamLog:
    """Mirror a CLI stream without changing its protocol or suppressing exceptions."""
    def __init__(self, stream, output, tool):
        self.stream, self.output, self.tool = stream, output, tool
        self.pending, self.warned = '', False

    def write(self, text):
        self.pending += text
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            line = redact(line)
            self.stream.write(line+'\n')
            if line.strip() and not line.startswith(('@@PROGRESS ', '@history-cleanup ')):
                try: self.output.write(formatted(line, self.tool)+'\n')
                except OSError as exc:
                    if not self.warned:
                        self.stream.write(f'日志写入失败：{type(exc).__name__}：{exc}；继续在窗口输出。\n')
                        self.warned = True
        return len(text)

    def flush(self):
        if self.pending:
            self.write('\n')
        self.stream.flush()

    def finish(self):
        if self.pending: self.write('\n')
        self.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


# Every tool ships this file. Only explicit management selects the shared copy.
_manager = os.environ.get('PROJECT_MANAGER_HOME')
if _manager:
    _shared = Path(_manager).resolve()/'shared_services/log_policy.py'
    if _shared != Path(__file__).resolve():
        if not _shared.is_file():
            raise RuntimeError('管理中心日志服务缺失：'+str(_shared))
        _key = '_project_logging_'+str(_shared)
        if _key not in sys.modules:
            _spec = importlib.util.spec_from_file_location(_key, _shared)
            _module = importlib.util.module_from_spec(_spec)
            sys.modules[_key] = _module
            try: _spec.loader.exec_module(_module)
            except BaseException:
                sys.modules.pop(_key, None)
                raise
        _module = sys.modules[_key]
        for _name in ('options', 'managed_environment', 'redact', 'level', 'log_path', 'formatted', 'DailyLog', 'StreamLog', 'capture_output', 'utf8_streams'):
            globals()[_name] = getattr(_module, _name)
