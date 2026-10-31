"""Bounded, read-only inspection. Always logs out through FusionClient."""
import os
import json
import xml.etree.ElementTree as ET
from fusion_client import FusionClient, RUNTIME


def main():
    RUNTIME.mkdir(exist_ok=True)
    with FusionClient(os.environ['FUSIONLIVE_USERNAME'], os.environ['FUSIONLIVE_PASSWORD']) as client:
        queue = [client.cabinet_id]
        seen = set()
        for _ in range(5):
            if not queue:
                break
            folder = queue.pop(0)
            if folder in seen:
                continue
            seen.add(folder)
            r = client.request('GET', '/pol/viewDocumentManagerTreeFSAction', params={'folderId': folder})
            (RUNTIME / f'folder_{folder}.xml').write_bytes(r.content)
            tree = ET.fromstring(r.content)
            children = [e.attrib for e in tree if e.tag == 'tree']
            print('Folder', folder, 'children', json.dumps(children, ensure_ascii=False)[:2200], flush=True)
            queue.extend(e['id'] for e in children if e.get('id') not in seen)
            r = client.request('GET', '/pol/viewFolderDocumentListFSAction', params={
                'folderId': folder, 'start': 0, 'limit': 5})
            (RUNTIME / f'list_{folder}.xml').write_bytes(r.content)
            try:
                data = ET.fromstring(r.content)
                print('List root/children', data.tag, [(e.tag, e.attrib) for e in list(data)[:8]], flush=True)
            except ET.ParseError:
                print('List response format:', r.headers.get('Content-Type'), 'bytes', len(r.content), flush=True)


if __name__ == '__main__':
    main()
