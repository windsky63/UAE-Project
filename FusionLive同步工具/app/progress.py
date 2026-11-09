"""Explicit phase and record counters for the resident console."""

from paths import HOME, CODE
import json
import time

PREFIX = '@@PROGRESS '


def emit(phase, **values):
    print(PREFIX + json.dumps(dict(phase=phase, **values), ensure_ascii=False), flush=True)


class DownloadProgress:
    """Sample transferred bytes, while keeping verified completion separate from partials."""
    def __init__(self, ledger, project):
        self.downloads = self.wire_bytes = self.current_bytes = self.current_total = 0
        self.current_file = ''
        self.started = self.sample_time = time.monotonic()
        self.sample_bytes = 0
        self.recent_rate = 0
        self.refresh(ledger, project)

    def refresh(self, ledger, project):
        """Refresh discovered totals between batches without resetting transfers."""
        row = ledger.one('''SELECT COUNT(*) total,COALESCE(SUM(size),0) total_bytes,
          COALESCE(SUM(status='done'),0) done,COALESCE(SUM(CASE WHEN status='done' THEN size ELSE 0 END),0) done_bytes
          FROM files WHERE project=? AND active=1''', (project,))
        self.total, self.total_bytes = row['total'], row['total_bytes']
        self.done, self.done_bytes = row['done'], row['done_bytes']
        self.publish(force=True)

    def begin(self, item):
        self.current_file = item['filename']
        self.current_total = item['size'] or 0
        self.current_bytes = 0
        self.publish(force=True)

    def update(self, received, expected):
        self.wire_bytes += max(0, received - self.current_bytes)
        self.current_bytes, self.current_total = received, expected
        self.publish()

    def resume(self, received, expected):
        """Restored disk bytes are progress, not traffic received in this run."""
        self.current_bytes, self.current_total = received, expected
        self.publish(force=True)

    def complete(self, size):
        self.wire_bytes += max(0, size - self.current_bytes)
        self.downloads += 1
        self.done += 1
        self.done_bytes += size
        self.current_bytes = self.current_total = 0
        self.current_file = ''
        self.publish(force=True)

    def failed(self):
        self.current_bytes = self.current_total = 0
        self.current_file = ''
        self.publish(force=True)

    def publish(self, force=False):
        at = time.monotonic()
        interval = at - self.sample_time
        if not force and interval < 5:
            return
        average = self.wire_bytes/(at-self.started) if at > self.started else 0
        if interval >= 5:
            self.recent_rate = (self.wire_bytes-self.sample_bytes)/interval
            self.sample_time, self.sample_bytes = at, self.wire_bytes
        emit('文件下载', downloads=self.downloads, download_total_files=self.total,
             download_done_files=self.done, download_total_bytes=self.total_bytes,
             download_done_bytes=self.done_bytes, current_file=self.current_file,
             current_bytes=self.current_bytes, current_total=self.current_total,
             transfer_bytes=self.wire_bytes,
             recent_bytes_per_second=self.recent_rate if at-self.started >= 5 else average,
             average_bytes_per_second=average)


class Progress:
    def __init__(self):
        self.values = {'phase': '启动', 'directories': 0, 'pages': 0, 'documents': 0, 'downloads': 0}
        self.last_update = time.monotonic()

    def accept(self, line):
        if not line.startswith(PREFIX):
            return False
        incoming = json.loads(line[len(PREFIX):])
        if not isinstance(incoming,dict) or ('phase' in incoming and not isinstance(incoming['phase'],str)):
            raise ValueError('进度事件必须为对象；phase存在时必须为字符串')
        if 'phase' in incoming and incoming['phase'] != self.values.get('phase'):
            # Counters belong to a phase; stale current filenames and speeds
            # must never appear as evidence that the next stage is progressing.
            for key in list(self.values):
                if (key.startswith(('download_','current_','associated_','recent_','average_'))
                        or key in ('transfer_bytes','processed','elapsed')):
                    self.values.pop(key,None)
        self.values.update(incoming)
        self.last_update = time.monotonic()
        return True

    def message(self):
        v = self.values
        result = f'阶段：{v["phase"]}；目录 {v["directories"]}；清单页 {v["pages"]}；文档记录 {v["documents"]}；下载完成 {v["downloads"]}'
        if v.get('workflow_stage') and v['workflow_stage'] != v['phase']:
            result = v['workflow_stage'] + ' / ' + result
        if v.get('cached_inventory'):
            result += '；沿用已保存清单'
        if v['phase'] == '文件下载' and 'download_total_files' in v:
            total, done = v['download_total_bytes'], v['download_done_bytes']
            percent = 100 * done / total if total else 0
            result += (f'；当前有效版本完成 {v["download_done_files"]}/{v["download_total_files"]} 个'
                       f'；已完成 {done/1e9:.2f}/{total/1e9:.2f} GB（{percent:.1f}%）')
            recent = v.get('recent_bytes_per_second', 0) if time.monotonic()-self.last_update < 15 else 0
            result += f'；近期 {recent/1e6:.2f} MB/s；本次传输均速 {v.get("average_bytes_per_second",0)/1e6:.2f} MB/s'
            if v.get('current_file'):
                name = v['current_file']
                result += f'；当前 {name} {v["current_bytes"]/1e6:.1f}/{v["current_total"]/1e6:.1f} MB'
        if 'associated_total' in v:
            result += f'；关联已核对 {v["associated_done"]}/{v["associated_total"]}'
            if v.get('associated_failed'):
                result += f'；暂存失败 {v["associated_failed"]}（未算完成）'
            processed, elapsed = v.get('processed', 0), v.get('elapsed', 0)
            remaining = v['associated_total'] - v['associated_done']
            if processed >= 5 and elapsed > 0 and remaining > 0 and v['phase'] == '原件/附件核对':
                minutes = remaining * elapsed / processed / 60
                result += f'；按本次速度估计剩余约 {minutes:.0f} 分钟'
        idle = time.monotonic() - self.last_update
        if idle >= 60:
            result += f'；距上次进度更新 {int(idle)} 秒，等待当前操作完成'
        return result
