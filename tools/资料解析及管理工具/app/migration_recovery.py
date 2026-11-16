"""Offline recovery of a recorded file batch, independent of publication."""
from contextlib import ExitStack, closing
import json
import os
from pathlib import Path
import sqlite3

from analysis_worker import worker_lock, in_download_window
from file_layout import (LayoutError, MigrationProgress, ReadLedger, apply,
                         pause_analysis_for_migration, resume_analysis_after_migration)
from fusion_client import now


def recover(base, cancelled=lambda: False):
    base = Path(base).resolve()
    runtime = base / 'runtime'
    marker = runtime / 'layout.pending'
    state_path = runtime / 'migration_recovery.json'
    state = {}
    try:
        state = json.loads(state_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        pass

    def save(phase, **values):
        state.update(phase=phase, updated_at=now().isoformat(), **values)
        temp = state_path.with_suffix('.json.tmp')
        temp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(temp, state_path)
        return dict(state)

    if cancelled() or in_download_window():
        return dict(phase='deferred', reason='下载时段或已请求退出，稍后恢复')
    with ExitStack() as stack:
        # Non-blocking locks protect original files and their queue. Published
        # images live under logs, so recovery must not take the delivery lock.
        locks = ((runtime, 'sync.lock'), (runtime, 'path_maintenance.lock'),
                 (runtime, 'history_repair.lock'))
        for folder, name in locks:
            if not stack.enter_context(worker_lock(folder, name)):
                return dict(phase='deferred', reason='等待任务释放文件：' + str(folder / name))
        if not marker.exists():
            # A prior process may have cleared the marker just before shutdown.
            resume_analysis_after_migration(base)
            return save('complete', reason='迁移已经完成')
        progress = stack.enter_context(MigrationProgress(base))
        try:
            if cancelled() or in_download_window():
                return save('deferred', reason='已请求退出或进入下载时段')
            database = runtime / 'ledger.sqlite3'
            db = stack.enter_context(closing(sqlite3.connect(database.as_uri() + '?mode=rw', uri=True, timeout=5)))
            db.row_factory = sqlite3.Row
            ledger = ReadLedger(db, base)
            if not ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"):
                raise LayoutError('未找到有效文件台账，保留迁移现场')
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'history_repairs' in tables and ledger.one("SELECT 1 FROM history_repairs WHERE state!='done' LIMIT 1"):
                raise LayoutError('另有未完成的历史整理记录，需使用目录整理工具恢复')
            save('running', reason='自动恢复已登记迁移，不创建新的文件移动计划')
            pause_analysis_for_migration(base)
            if not stack.enter_context(worker_lock(runtime / 'file_analysis')):
                progress.message('已请求解析完成当前文件后暂停；60秒后继续自动恢复。')
                return save('deferred', reason='等待解析完成当前文件并释放锁')
            backup = Path(state.get('backup', ''))
            if not (state.get('backup') and backup.is_file() and backup.parent == runtime / 'backups'):
                backup = runtime / 'backups' / ('before-auto-layout-' + now().strftime('%Y%m%d-%H%M%S-%f') + '.sqlite3')
                backup.parent.mkdir(exist_ok=True)
                progress.phase('备份迁移恢复前台账')
                def backup_progress(status, remaining, total):
                    if cancelled() or in_download_window():
                        raise LayoutError('备份已停止，稍后自动重试')
                    progress.update(done=total-remaining, total=total)
                with closing(sqlite3.connect(backup)) as target:
                    db.backup(target, pages=256, progress=backup_progress)
                save('running', backup=str(backup))
            count = apply(ledger, recover_only=True, progress=progress, log=progress.message,
                          cancelled=lambda: cancelled() or in_download_window())
            resume_analysis_after_migration(base)
            progress.message('自动迁移恢复完成，已解除迁移自身的解析暂停；手动停止请求继续保留。')
            result = save('complete', recovered_moves=count, reason='迁移收尾完成')
            # A subsequent batch must get its own before-recovery backup.
            state.pop('backup', None)
            save('complete')
            return result
        except Exception as exc:
            # Keep both the journal and pause ownership for the next attempt.
            progress.message('自动迁移恢复未完成，保留现场并于60秒后重试：' + str(exc))
            return save('failed', reason=type(exc).__name__ + ': ' + str(exc))
