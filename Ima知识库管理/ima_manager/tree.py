"""Resolve relative local directories without changing the cloud during preview."""
from pathlib import Path

from .api import ImaError, folder_id

LAYOUT = 'relative-tree-v1'


class DirectorySession:
    """Cache complete directory reads for one plan or execution only."""
    def __init__(self, client):
        self.client = client
        self.cache = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    def listing(self, kb, folder=''):
        folder = '' if folder == kb else folder
        key = (kb, folder)
        if key not in self.cache:
            # Failed/partial pagination is never installed in the cache.
            self.cache[key] = self.client.listing(kb, folder)
        return self.cache[key]

    def call(self, endpoint, body, **kwargs):
        try:
            return self.client.call(endpoint, body, **kwargs)
        finally:
            # Also invalidate after a lost response: the write may have committed.
            if endpoint in {'create_folder', 'add_knowledge', 'import_urls'}:
                kb = body.get('knowledge_base_id')
                folder = body.get('folder_id', '')
                self.cache.pop((kb, '' if folder == kb else folder), None)
            elif endpoint in {'move_knowledge', 'rename_knowledge'}:
                self.cache.clear()


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
