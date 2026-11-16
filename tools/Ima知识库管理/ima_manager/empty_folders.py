"""Report migration source folders; ima rejects automatic folder moves."""
from .config import atomic_json


def report_empty_folders(engine, rows, kb, folder, stop, notify):
    with engine.store.connect() as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='layout_moves'").fetchone():
            return 0
        sources = list(db.execute("SELECT DISTINCT old_folder FROM layout_moves WHERE account=? AND kb=? AND phase IN ('done','done_duplicate') AND old_folder!=new_folder", (engine.account, kb)))
    if not sources:
        return 0
    active = {row.get('target_folder_id') for row in rows}
    candidates = []
    for (identity,) in sources:
        if identity in active or not identity:
            continue
        entries = engine.client.cached_listing(kb, identity)
        if entries is not None and entries:
            continue
        candidates.append(dict(folder_id=identity, state='empty' if entries == [] else 'needs_check'))
    path = engine.store.path.parent / 'empty-folder-cleanup.json'
    atomic_json(path, dict(knowledge_base_id=kb, state='manual_cleanup_required', reason='ima移动接口不支持文件夹；未发现可用的目录删除接口。请在ima客户端清理，程序未移动或删除目录。', folders=candidates, network_requests=0))
    notify(f'旧目录待清理：{len(candidates)} 个候选；ima接口不支持文件夹移动，未自动清理；清单={path}')
    return 0
