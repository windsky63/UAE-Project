"""Cloud writes must stay within the local sync tree and uploaded identities."""
import hashlib
import os
from pathlib import Path

from .api import ImaError, folder_id, target


def local_directories(source):
    root = Path(source).resolve()
    if not root.is_dir():
        raise ImaError('同步目录不存在，不能修改云端内容。')
    paths = {(): root}
    for current, directories, _ in os.walk(root, followlinks=False):
        directories[:] = [d for d in directories if not d.startswith('.') and
                          not (Path(current) / d).is_symlink() and
                          (Path(current) / d).resolve().is_relative_to(root)]
        for name in directories:
            path = Path(current) / name
            paths[path.relative_to(root).parts] = path
    return root, paths


def cloud_directories(client, source, kb):
    root, local = local_directories(source)
    # Accept a selected source root's own name as the cloud root wrapper too.
    allowed = dict(local)
    allowed.update({(root.name, *parts): path for parts, path in local.items()})
    result = {'': root, kb: root}
    pending = [('', ())]
    visited = set()
    while pending:
        parent, parts = pending.pop()
        if parent in visited:
            continue
        visited.add(parent)
        for item in client.listing(kb, parent):
            fid = folder_id(item)
            name = item.get('title') or item.get('name') or (item.get('folder_info') or {}).get('name')
            relative = (*parts, name)
            if fid and relative in allowed:
                result[fid] = allowed[relative]
                pending.append((fid, relative))
        if len(visited) > 10000:
            raise ImaError('同步目录数量异常，已停止核对。')
    return result


def require_directory(client, source, kb, folder):
    if not folder or folder == kb:
        return Path(source).resolve()
    path = cloud_directories(client, source, kb).get(folder)
    if path is None:
        raise ImaError('该云端目录在待同步中没有对应目录，不允许修改或同步到此处。')
    return path


def require_file(client, store, source, kb, folder, item):
    require_directory(client, source, kb, folder)
    root = Path(source).resolve()
    title, media = item.get('title'), item.get('media_id')
    if not title or not media or folder_id(item):
        raise ImaError('只允许处理待同步中对应的普通文件。')
    account = hashlib.sha256(client.client_id.encode()).hexdigest()
    with store.connect() as db:
        receipts = db.execute('SELECT path FROM receipts WHERE account=? AND kb=? AND media_id=?',
                              (account, kb, media)).fetchall()
    for (saved_path,) in receipts:
        path = Path(saved_path)
        if (path.name == title and path.is_file() and not path.is_symlink() and
                path.resolve().is_relative_to(root) and
                path.resolve().relative_to(root).parts[0] != '删除'):
            return path
    raise ImaError('该云端文件没有匹配的本地同名文件和上传回执，不允许操作。')


def create_local_folder(client, source, kb, parent, name):
    validate_name(name)
    local_parent = require_directory(client, source, kb, parent)
    path = local_parent / name
    root = Path(source).resolve()
    if not path.is_dir() or path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ImaError('待同步中不存在对应文件夹，不允许在云端创建。')
    return create_folder(client, kb, parent, name)


def rename_local_file(client, store, source, kb, folder, item, name):
    validate_name(name)
    path = require_file(client, store, source, kb, folder, item)
    new_path = path.with_name(name)
    if not new_path.is_file() or new_path.is_symlink():
        raise ImaError('本地没有新名称对应的文件，不允许单独修改云端名称。')
    return rename_file(client, kb, folder, item['media_id'], name, expected_title=item['title'])


def move_local_file(client, store, source, kb, folder, item, destination):
    require_file(client, store, source, kb, folder, item)
    require_directory(client, source, kb, destination)
    return move_file(client, kb, folder, item['media_id'], destination, expected_title=item['title'])


# Low-level operations retain explicit API opt-in and verify the cloud result.
def validate_name(name):
    if not isinstance(name, str) or not name.strip() or name != name.strip() or len(name) > 1024:
        raise ImaError('名称不能为空、包含首尾空格或超过 1024 字符。')
    if any(c in name for c in '/\\\r\n\x00') or name in {'.', '..'}:
        raise ImaError('名称不能包含路径分隔符或控制字符。')
    return name


def live_listing(client, kb, folder):
    """Refresh identities immediately before a move/rename, even in a batch."""
    from .tree import DirectorySession
    if isinstance(client, DirectorySession):
        client.cache.pop((kb, '' if folder == kb else folder), None)
    return client.listing(kb, folder)


def get_file(client, kb, folder, media_id, expected_title=None):
    entries = live_listing(client, kb, folder)
    found = [r for r in entries if r.get('media_id') == media_id]
    if len(found) != 1 or folder_id(found[0]):
        raise ImaError('源目录中未找到该文件；实验管理不操作文件夹。')
    if expected_title is not None and found[0].get('title') != expected_title:
        raise ImaError('云端文件名与本地同步文件已不匹配，不允许操作。')
    return found[0], entries


def create_folder(client, kb, parent, name):
    validate_name(name)
    before = client.listing(kb, parent)
    if any((r.get('title') or r.get('name', '')).casefold() == name.casefold() for r in before):
        raise ImaError('当前目录已有同名条目。')
    data = client.call('create_folder', dict(target(kb, parent), name=name), experimental=True)
    media_id = data.get('media_id')
    after = client.listing(kb, parent)
    if not media_id or not any(folder_id(r) == media_id and
            (r.get('title') or r.get('name')) == name for r in after):
        raise ImaError('建目录结果未能确认，请刷新后核对，不要直接重复操作。')
    return media_id


def rename_file(client, kb, folder, media_id, name, *, expected_title=None):
    validate_name(name)
    own, entries = get_file(client, kb, folder, media_id, expected_title)
    if own.get('title') == name:
        return
    if any(r.get('media_id') != media_id and r.get('title', '').casefold() == name.casefold() for r in entries):
        raise ImaError('当前目录已有同名条目。')
    client.call('rename_knowledge', {'knowledge_base_id': kb, 'media_id': media_id,
                                   'name': name}, experimental=True)
    after = client.listing(kb, folder)
    if not any(r.get('media_id') == media_id and r.get('title') == name for r in after):
        raise ImaError('改名结果未能确认，请刷新后核对，不要直接重复操作。')


def move_file(client, kb, source_folder, media_id, destination_folder, *, expected_title=None):
    if (source_folder or kb) == (destination_folder or kb):
        raise ImaError('源目录与目标目录相同。')
    own, _ = get_file(client, kb, source_folder, media_id, expected_title)
    destination = live_listing(client, kb, destination_folder)
    if any(r.get('title', '').casefold() == own.get('title', '').casefold() for r in destination):
        raise ImaError('目标目录已有同名条目。')
    body = {'src_knowledge_base_id': kb, 'dst_knowledge_base_id': kb,
            'infos': [{'media_id': media_id}]}
    if destination_folder:
        body['dst_folder_id'] = destination_folder
    data = client.call('move_knowledge', body, experimental=True)
    per_item = data.get('move_results', {}).get(media_id, {})
    if per_item.get('ret_code') != 0:
        raise ImaError('服务端未确认该文件移动成功，请刷新目录核对。')
    source_after = client.listing(kb, source_folder)
    destination_after = client.listing(kb, destination_folder)
    if any(r.get('media_id') == media_id for r in source_after) or not any(
            r.get('media_id') == media_id for r in destination_after):
        raise ImaError('移动位置未能确认，请刷新后核对，不要直接重复操作。')
