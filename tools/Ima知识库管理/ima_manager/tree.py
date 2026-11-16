"""Resolve relative local directories without changing the cloud during preview."""
from pathlib import Path

from .api import ImaError, folder_id

LAYOUT = 'relative-tree-v1'


class DirectorySession:
    """Cache complete directory reads for one plan or execution only."""
    def __init__(self, client):
        self.client = client
        self.cache = {}
        self.paths = {}
        self.name_checks = {}

    def export(self):
        return {'directories': [dict(kb=kb, folder=folder, entries=entries)
                                for (kb, folder), entries in self.cache.items()],
                'paths': [dict(source=source, kb=kb, folder=folder, path=str(path))
                          for (source, kb, folder), path in self.paths.items()],
                'name_checks': [dict(kb=kb, folder=folder, name=name, exists=exists)
                                for (kb, folder, name), exists in self.name_checks.items()]}

    def restore(self, snapshot):
        from copy import deepcopy
        self.cache = {(item['kb'], item['folder']): deepcopy(item['entries'])
                      for item in snapshot.get('directories', [])}
        self.paths = {(item['source'], item['kb'], item['folder']): Path(item['path'])
                      for item in snapshot.get('paths', [])}
        self.name_checks = {(item['kb'], item['folder'], item['name']): item['exists']
                            for item in snapshot.get('name_checks', [])}

    def repeated_many(self, kb, folder, params):
        results = self.client.repeated_many(kb, folder, params)
        folder = '' if folder == kb else folder
        self.name_checks.update({(kb, folder, name.casefold()): exists for name, exists in results.items()})
        return results

    def repeated(self, kb, folder, name, media_type):
        folder = '' if folder == kb else folder
        key = (kb, folder, name.casefold())
        if (kb, folder) not in self.cache and key in self.name_checks:
            return self.name_checks[key]
        return any((item.get('title') or item.get('name') or '').casefold() == name.casefold()
                   for item in self.listing(kb, folder))

    def __getattr__(self, name):
        return getattr(self.client, name)

    def listing(self, kb, folder=''):
        folder = '' if folder == kb else folder
        key = (kb, folder)
        if key not in self.cache:
            # Failed/partial pagination is never installed in the cache.
            self.cache[key] = self.client.listing(kb, folder)
        return self.cache[key]

    def cached_listing(self, kb, folder=''):
        return self.cache.get((kb, '' if folder == kb else folder))

    def call(self, endpoint, body, **kwargs):
        if endpoint in {'move_knowledge', 'rename_knowledge', 'create_folder'}:
            # A successful write response is authoritative for this session.
            kb = body.get('src_knowledge_base_id', body.get('knowledge_base_id'))
            media_ids = ({info['media_id'] for info in body['infos']} if endpoint == 'move_knowledge'
                         else {body.get('media_id')})
            affected = {key for key, entries in self.cache.items() if key[0] == kb
                        and any(item.get('media_id') in media_ids for item in entries)}
            if endpoint == 'create_folder':
                parent = body.get('folder_id', '')
                affected.add((kb, '' if parent == kb else parent))
            elif endpoint == 'move_knowledge':
                destination_kb = body['dst_knowledge_base_id']
                destination = body.get('dst_folder_id', '')
                affected.add((destination_kb, '' if destination == destination_kb else destination))
            snapshots = dict(self.cache)
            for key in affected:
                if key in snapshots:
                    snapshots[key] = [dict(item) for item in snapshots[key]]
            try:
                data = self.client.call(endpoint, body, **kwargs)
            except Exception:
                self.paths = {key: path for key, path in self.paths.items() if key[1] != kb}
                media = ({info['media_id'] for info in body['infos']}
                         if endpoint == 'move_knowledge' else {body.get('media_id')})
                for key, entries in snapshots.items():
                    if key[0] == kb and (endpoint == 'create_folder' or
                            any(item.get('media_id') in media for item in entries)):
                        self.cache.pop(key, None)
                if endpoint == 'move_knowledge':
                    self.cache.pop((body['dst_knowledge_base_id'], body.get('dst_folder_id', '')), None)
                raise
            if endpoint == 'create_folder':
                parent = body.get('folder_id', '')
                key = (kb, '' if parent == kb else parent)
                media = data.get('media_id')
                if media:
                    entries = snapshots.get(key)
                    if entries is not None:
                        entries.append(dict(media_id=media, title=body['name'], media_type=99))
                    snapshots[(kb, media)] = []
            elif endpoint == 'rename_knowledge':
                for key in affected:
                    if key in snapshots and key[0] == kb:
                        entries = snapshots[key]
                        for item in entries:
                            if item.get('media_id') == body['media_id']:
                                item['title'] = body['name']
            else:
                results = data.get('move_results') or {}
                moved = {info['media_id'] for info in body['infos']
                         if results.get(info['media_id'], {}).get('ret_code') == 0}
                destination_kb = body['dst_knowledge_base_id']
                folder = body.get('dst_folder_id', '')
                destination = (destination_kb, '' if folder == destination_kb else folder)
                entries_to_move = {}
                for key in affected:
                    if key in snapshots and key[0] == kb and key != destination:
                        entries = snapshots[key]
                        entries_to_move.update({item['media_id']: item for item in entries
                                                if item.get('media_id') in moved})
                        snapshots[key] = [item for item in entries if item.get('media_id') not in moved]
                if destination in snapshots:
                    present = {item.get('media_id') for item in snapshots[destination]}
                    snapshots[destination].extend(item for media, item in entries_to_move.items() if media not in present)
                    if moved - present - entries_to_move.keys():
                        snapshots.pop(destination)
            self.cache = snapshots
            return data
        if endpoint == 'add_knowledge':
            kb, folder = body['knowledge_base_id'], body.get('folder_id', '')
            key = (kb, '' if folder == kb else folder)
            try:
                data = self.client.call(endpoint, body, **kwargs)
            except Exception:
                self.cache.pop(key, None)
                self.paths = {entry: path for entry, path in self.paths.items() if entry[1] != kb}
                raise
            if data.get('media_id') and key in self.cache:
                self.cache[key].append(dict(media_id=data['media_id'], title=body['title'],
                                            media_type=body['media_type']))
            if data.get('media_id'):
                self.name_checks[(*key, body['title'].casefold())] = True
            return data
        try:
            return self.client.call(endpoint, body, **kwargs)
        finally:
            if endpoint == 'import_urls':
                self.cache.pop((body['knowledge_base_id'], body.get('folder_id', '')), None)


class CloudTree:
    def __init__(self, client, kb, folder='', create=None):
        self.client, self.kb = client, kb
        self.folder = '' if folder == kb else folder
        self.create = create
        self.cache = {}

    def entries(self, folder):
        if folder not in self.cache:
            self.cache[folder] = self.client.listing(self.kb, folder)
        return self.cache[folder]

    def parent(self, parts):
        parent = self.folder
        for name in parts:
            if name in ('', '.', '..') or '/' in name or '\\' in name:
                raise ImaError('目标相对目录无效，请重新预览。')
            if parent is None:
                continue
            matches = [item for item in self.entries(parent)
                       if (item.get('title') or item.get('name') or
                           (item.get('folder_info') or {}).get('name')) == name]
            if matches:
                if len(matches) != 1 or not folder_id(matches[0]):
                    raise ImaError('云端路径存在同名条目或同名文件夹不唯一：' + name)
                parent = folder_id(matches[0])
            elif self.create:
                created = self.create(self.kb, parent, name)
                # The creator already verified the new folder ID. Reuse that
                # identity instead of re-reading the parent for the next file.
                self.cache[parent].append({'media_id': created, 'title': name, 'media_type': 99})
                parent = created
            else:
                parent = None
        return parent


def relative_target(row, local_base):
    path = Path(row.get('original_path') or row['path'])
    try:
        return path.relative_to(local_base)
    except ValueError:
        raise ImaError('该本地文件不在所选云端目录对应的本地子树内。') from None
