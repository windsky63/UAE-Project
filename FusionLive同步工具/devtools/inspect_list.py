import os
import xml.etree.ElementTree as ET
from fusion_client import FusionClient, RUNTIME


def main():
    with FusionClient(os.environ['FUSIONLIVE_USERNAME'], os.environ['FUSIONLIVE_PASSWORD']) as client:
        client.http.headers.update({'X-Requested-With': 'XMLHttpRequest', 'Referer': 'https://us.fusion.live/pol/protocolSwitchFsAction'})
        params = {'folderId': '20462597', 'start': 0, 'limit': 5,
                  'fsSort': 'REFERENCE', 'dir': 'ASC', 'includeLinks': 'true', 'fetchPropDetails': 'false'}
        for method in ('GET', 'POST'):
            r = client.request(method, '/pol/viewFolderDocumentListFSAction', **({'params': params} if method == 'GET' else {'data': params}))
            (RUNTIME / f'list_verified_{method}.xml').write_bytes(r.content)
            tree = ET.fromstring(r.content)
            print(method, 'root', tree.tag, tree.attrib, 'bytes', len(r.content))
            if tree.tag != 'success':
                print(r.text[:4500])
                break


if __name__ == '__main__':
    main()
