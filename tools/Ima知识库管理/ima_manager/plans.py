"""Persist a reviewed manual plan separately from its execution results."""
import hashlib
import json
from pathlib import Path
import uuid

from .api import ImaError
from .config import atomic_json
from .layout import safe, manifest_path

VERSION = 1


def stamp(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def context(settings, source, kb, folder, recursive):
    return dict(source=str(Path(source).resolve()), kb=kb, folder='' if folder == kb else folder,
                recursive=recursive, collection={key: settings.get(key) for key in
                ('collect_enabled', 'database_path', 'downloads_path', 'ledger_path', '_legacy_ledger_mode')})


def receipt_revision(engine, kb):
    with engine.store.connect() as db:
        values = []
        for table in ('receipts', 'update_jobs', 'layout_moves'):
            if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
                rows = db.execute(f'SELECT * FROM {table} WHERE account=? AND kb=?', (engine.account, kb)).fetchall()
                values.append((table, sorted(rows)))
    return digest(values)


def layout_revision(source):
    path = manifest_path(source)
    return hashlib.sha256(path.read_bytes() if path.exists() else b'').hexdigest()


def invalidate(runtime):
    atomic_json(Path(runtime) / 'preview-state.json', {'version': VERSION, 'state': 'building'})


def save(runtime, engine, settings, source, kb, folder, recursive, report, before):
    if receipt_revision(engine, kb) != before[0] or layout_revision(source) != before[1]:
        raise ImaError('生成预览期间上传回执或迁移记录发生变化，请重新预览。')
    validate_files(report['files'], source)
    identity = uuid.uuid4().hex
    artifact = dict(version=VERSION, state='ready', plan_id=identity, account=engine.account,
                    context=context(settings, source, kb, folder, recursive), receipts=before[0],
                    layout=before[1], files=report['files'],
                    directory_snapshot=vars(engine).get('directory_snapshot', {}))
    artifact['checksum'] = digest(artifact)
    atomic_json(Path(runtime) / 'preview-plan.json', artifact)
    finish(runtime, artifact, 'ready')
    report['plan_id'] = identity
    return identity


def validate_files(rows, source):
    for row in rows:
        if 'sha256' not in row:
            continue
        path = safe(source, Path(row['path']), purpose='预览计划')
        try:
            current = stamp(path.stat())
        except OSError:
            raise ImaError('预览中的本地文件已不存在，请重新预览：' + str(path)) from None
        if current != row.get('file_stamp'):
            raise ImaError('预览中的本地文件已变化，请重新预览：' + str(path))
        if row.get('action') == 'delete' and Path(row['original_path']).exists():
            raise ImaError('预览中的逻辑删除文件已恢复，请重新预览。')


def load(runtime, engine, settings, source, kb, folder, recursive, *, expected_id=None):
    path = Path(runtime) / 'preview-plan.json'
    try:
        artifact = json.loads(path.read_text(encoding='utf-8'))
        state = json.loads((Path(runtime) / 'preview-state.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ImaError('没有可执行的预览计划，请先点击“预览上传计划”。') from None
    if (not isinstance(artifact, dict) or artifact.get('version') != VERSION
            or not isinstance(state, dict) or state.get('state') != 'ready'
            or state.get('plan_id') != artifact.get('plan_id')):
        raise ImaError('预览计划不存在、已执行或未成功生成，请重新预览。')
    if expected_id is not None and artifact.get('plan_id') != expected_id:
        raise ImaError('这份预览已被新计划替换，请刷新并核对后再执行。')
    checksum = artifact.get('checksum')
    if checksum != digest({key: value for key, value in artifact.items() if key != 'checksum'}):
        raise ImaError('预览计划校验失败，请重新预览。')
    if artifact.get('account') != engine.account or artifact.get('context') != context(settings, source, kb, folder, recursive):
        raise ImaError('账号或同步配置与预览不同，请重新预览。')
    if artifact.get('receipts') != receipt_revision(engine, kb) or artifact.get('layout') != layout_revision(source):
        raise ImaError('上传回执或目录迁移记录已变化，请重新预览。')
    validate_files(artifact['files'], source)
    engine.directory_snapshot = artifact.get('directory_snapshot', {})
    return artifact


def consume(runtime, artifact):
    finish(runtime, artifact, 'executing')


def finish(runtime, artifact, state):
    # The immutable manifest may contain thousands of files. State transitions
    # only replace a tiny sidecar, never rewrite the whole reviewed plan.
    atomic_json(Path(runtime) / 'preview-state.json',
                {'version': VERSION, 'plan_id': artifact['plan_id'], 'state': state})
