"""Structured stdin/stdout adapter used by the management center."""
from pathlib import Path

from .api import ImaClient, ImaError
from .config import RUNTIME, load_credentials, load_settings, save_credentials
from .sync import Engine, Store, scan, move_local_to_deleted


def run(request):
    operation = request.get('operation')
    data = request.get('data') or {}
    if operation == 'credentials':
        save_credentials(data.get('client_id', ''), data.get('api_key', ''))
        return {'saved': True}
    settings = load_settings()
    source = Path(settings['source']).resolve()
    kb, folder = data.get('kb', settings['kb']), data.get('folder', settings['folder'])
    store = Store(RUNTIME / 'state.sqlite3')
    if operation == 'scan':
        return {'items': scan(source, settings['recursive'])}
    if operation == 'history':
        return {'items': store.history()}
    if operation == 'move-local':
        supplied = Path(data.get('path', ''))
        path = supplied if supplied.is_absolute() else source / supplied
        if not path.resolve().is_relative_to(source):
            raise ImaError('文件不在当前待同步目录中。')
        result = move_local_to_deleted(source, path)
        return {'path': str(result), 'saved': True}
    client = ImaClient(*load_credentials())
    if operation == 'bases':
        return {'items': client.bases()}
    if operation == 'list':
        return {'items': client.listing(kb, folder)}
    if operation == 'search':
        return {'items': client.search(kb, data.get('query', ''))}
    if operation == 'destinations':
        from .scope import cloud_directories
        return {'items': [{'id': identity, 'name': path.relative_to(source).as_posix() if path != source else '根目录'}
                          for identity, path in cloud_directories(client, source, kb).items() if identity != kb]}
    if operation == 'resolve':
        path = Path(data.get('path', ''))
        if not path.resolve().is_relative_to(source):
            raise ImaError('文件不在待同步目录中。')
        if type(data.get('uploaded')) is not bool:
            raise ImaError('需要明确填写人工核对结果。')
        Engine(client, store).resolve_upload(kb, folder, path, data['uploaded'])
        return {'saved': True}
    if operation in {'create-folder', 'rename', 'move'}:
        from .scope import create_local_folder, rename_local_file, move_local_file
        if operation == 'create-folder':
            return {'id': create_local_folder(client, source, kb, folder, data.get('name', ''))}
        item = {'media_id': data.get('media_id', ''), 'title': data.get('title', '')}
        if operation == 'rename':
            rename_local_file(client, store, source, kb, folder, item, data.get('name', ''))
        else:
            move_local_file(client, store, source, kb, folder, item, data.get('destination', ''))
        from .daily import record
        record(source, '云端改名' if operation == 'rename' else '云端移动', item['title'], '成功',
               target=kb + '/' + (folder or '根目录'),
               detail=('新名=' + data['name']) if operation == 'rename' else '移至=' + (data.get('destination') or '根目录'))
        return {'saved': True}
    raise ImaError('不支持的 ima 操作。')
