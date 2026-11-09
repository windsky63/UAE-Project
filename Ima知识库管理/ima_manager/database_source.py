"""Read-only projection of the downloader's live ledger into collection records."""
from contextlib import closing
from functools import lru_cache
import importlib.util
from pathlib import Path
import sqlite3

from .api import ImaError
from .download_paths import existing_download_path


@lru_cache(maxsize=2)
def sync_module(name):
    # Load only the existing pure classification/index helpers, never sync.py/Ledger.
    path = Path(__file__).resolve().parents[2] / 'FusionLive同步工具/app' / (name + '.py')
    if not path.is_file():
        raise ImaError('同步工具资料读取模块缺失：' + str(path))
    spec = importlib.util.spec_from_file_location('_ima_source_' + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def read_database(database, selection, *, downloads=None):
    path = Path(database).resolve()
    downloads = Path(downloads).resolve() if downloads is not None else path.parent.parent / 'downloads'
    if not path.is_file():
        raise ImaError('下载数据库不存在，请设置同步工具的 runtime/ledger.sqlite3 路径。')
    business = sync_module('business_type')
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=3)) as db:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            columns = {row[1] for row in db.execute('PRAGMA table_info(files)')}
            if not {'project', 'id', 'filename', 'path', 'active'}.issubset(columns):
                raise ImaError('下载数据库缺少 files 文件身份或路径字段，请确认选择的是同步工具下载数据库。')
            contexts = business.ledger_file_contexts(db, include_inactive=True)
            mappings = {(str(row['project']), str(row['file_id'])): row['local_path']
                        for row in db.execute('SELECT project,file_id,local_path FROM local_paths')} if 'local_paths' in tables else {}
            files = [dict(row) for row in db.execute('SELECT * FROM files ORDER BY project,id')]
            records = []
            parents_by_id = {}
            for item in files:
                key = str(item['project']), str(item['id'])
                parents = contexts.get((item['project'], item['id']), [])
                registered = list(dict.fromkeys(value for value in
                    [mappings.get(key), item.get('path'), item.get('target_path')]
                    if existing_download_path(downloads, value) is not None))
                # The sync tool owns publication/archival. Collect what is on disk,
                # including retained old versions, rather than the remote latest set.
                if not registered:
                    continue
                parents_by_id[key] = (item, parents, registered)
                result = business.file_business(item, parents)
                row = {'工作区': key[0], '内容ID': key[1], '文件名': item['filename'],
                       '业务类型': result['business_type'], '业务专业': result['discipline'],
                       '文档编号': result.get('matched_number') or '；'.join(dict.fromkeys(parent.get('reference') or '' for parent in parents)),
                       '目录': '；'.join(dict.fromkeys(parent.get('folder_path') or '' for parent in parents)),
                       '本地路径': item.get('path'), '下载状态': item.get('status'),
                       '_registered_paths': registered, '_record_source': 'database'}
                if selection(row):
                    records.append(row)
        # Use the same committed, version-checked archive index as the exporter.
        archive = sync_module('archive_index')
        for member in archive.current_members(path.parent.parent, ledger_path=path,
                                             include_inactive=True, downloads=downloads):
            key = str(member['project']), str(member['parent_file_id'])
            parent = parents_by_id.get(key)
            if not parent or parent[0].get('sha256') != member['parent_sha256']:
                continue
            result = business.classify(filename=member['filename'])
            row = {'工作区': key[0], '内容ID': member['id'], '文件名': member['filename'],
                   '业务类型': result['business_type'], '业务专业': result['discipline'],
                   '目录': '；'.join(dict.fromkeys(item.get('folder_path') or '' for item in parent[1])),
                   '所属压缩包内容ID': key[1], '所属压缩包文件名': member['parent_filename'],
                   '包内完整路径': member['member_path'], '_registered_paths': parent[2],
                   '_record_source': 'database'}
            if selection(row):
                records.append(row)
        return list(enumerate(records, 1))
    except sqlite3.Error as exc:
        raise ImaError(f'下载数据库暂不可读：{exc}') from None
