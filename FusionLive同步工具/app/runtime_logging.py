"""Bounded UTF-8 operational logs; detailed audit data stays in the ledger."""
from collections import OrderedDict
import os
import re
from pathlib import Path
import threading
import time


MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 4
_locks = {}
_locks_guard = threading.Lock()


class BoundedLog:
    """Open per write so Windows can rotate files without an open own handle."""
    def __init__(self, path, max_bytes=MAX_BYTES, backups=BACKUPS):
        self.path = Path(path)
        self.max_bytes, self.backups = max_bytes, backups
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.path.resolve()), threading.RLock())

    def archive(self, index):
        # Keep .log as the final suffix for the management centre's log browser.
        return self.path.with_name(f'{self.path.stem}.{index}{self.path.suffix}')

    def write(self, text):
        if not text:
            return 0
        raw = text.encode('utf-8', errors='replace')
        if len(raw) > self.max_bytes:
            raw = (raw[:max(0, self.max_bytes - 64)].decode('utf-8', errors='ignore')
                   + '\n[单条日志过长，已截断]\n').encode('utf-8')
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size + len(raw) > self.max_bytes:
                self.archive(self.backups).unlink(missing_ok=True)
                for index in range(self.backups - 1, 0, -1):
                    old = self.archive(index)
                    if old.exists():
                        old.replace(self.archive(index + 1))
                self.path.replace(self.archive(1))
            with self.path.open('ab') as out:
                out.write(raw)
        return len(text)

    def flush(self):
        pass  # Each write closes the file.


IMPORTANT = ('失败', '异常', '错误', '中断', '停止', '退出', '注销', '未确认', 'Traceback',
             'Error', 'ERROR', 'WARNING', '警告', '冲突', '待核对')
SAMPLED = ('目录已保存', '清单已保存', '原件/附件核对', '下载完成',
           '开始文件', '文件大小', '保存解析队列与清单', '文件结束', '文件结果',
           '校验文件内容', '开始解压', '解压完成', '包内进度', '解析 PCF', '解析 IDF',
           '管线元件解析完成', '条目结束', '开始生成焊口', '生成/读取焊口',
           '分类小计', '保存解析结果', '扫描进度', '仍在处理', '空闲等待')


class EventGate:
    """Sample each busy category, with exact suppression totals and bounded state."""
    def __init__(self, interval=30, clock=time.monotonic, detailed=None):
        self.interval, self.clock = interval, clock
        self.detailed = os.environ.get('FUSIONLIVE_LOG_DETAIL') == '1' if detailed is None else detailed
        self.entries = OrderedDict()

    def summary(self, key, count):
        return f'日志合并：{key}，另有 {count} 条同类记录；逐文件信息见台账/解析结果。'

    def accept(self, message, *, important=False, key=None, interval=None):
        evidence = re.sub(r'失败\s*\d+', '', message)
        evidence = evidence.replace('含待核对冲突', '').replace('核对重复和冲突', '')
        if important or self.detailed or any(word in evidence for word in IMPORTANT):
            return [message]
        key = key or next((p for p in SAMPLED if message.startswith(p)), message)
        period = interval if interval is not None else (300 if key == '空闲等待' else self.interval)
        at = self.clock()
        if key not in self.entries:
            result = []
            if len(self.entries) >= 128:
                old, (_, count) = self.entries.popitem(last=False)
                if count:
                    result.append(self.summary(old, count))
            self.entries[key] = (at, 0)
            return result + [message]
        last, count = self.entries[key]
        if at - last < period:
            self.entries[key] = (last, count + 1)
            return []
        self.entries[key] = (at, 0)
        return ([self.summary(key, count)] if count else []) + [message]

    def drain(self):
        result = [self.summary(key, count) for key, (_, count) in self.entries.items() if count]
        self.entries.clear()
        return result
