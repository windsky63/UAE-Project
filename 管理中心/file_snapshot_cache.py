"""Bounded, process-local reuse of unchanged files; callers treat values as read-only."""
from collections import OrderedDict
from threading import RLock


class FileSnapshotCache:
    def __init__(self, entries=16, bytes_limit=32 * 1024 * 1024, read_limit=None):
        self.limit, self.bytes_limit, self.read_limit = entries, bytes_limit, read_limit
        self.values = OrderedDict()
        self.bytes = 0
        self.lock = RLock()

    @staticmethod
    def stamp(path):
        info = path.stat()
        return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns

    def get(self, path, load, variant=None):
        key = (str(path.absolute()), variant)
        before = self.stamp(path)
        with self.lock:
            entry = self.values.get(key)
            if entry and entry[0] == before:
                self.values.move_to_end(key)
                return entry[1]
        value = load()
        if self.stamp(path) != before:
            return value  # Atomic replacement during a read is not installed.
        weight = min(before[2], self.read_limit) if self.read_limit is not None else before[2]
        with self.lock:
            previous = self.values.pop(key, None)
            if previous:
                self.bytes -= previous[2]
            if weight <= self.bytes_limit:
                self.values[key] = (before, value, weight)
                self.bytes += weight
            while len(self.values) > self.limit or self.bytes > self.bytes_limit:
                _, old = self.values.popitem(last=False)
                self.bytes -= old[2]
        return value
