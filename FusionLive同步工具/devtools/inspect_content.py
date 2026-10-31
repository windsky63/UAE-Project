import os
import json
from fusion_client import FusionClient, RUNTIME


def main():
    with FusionClient(os.environ['FUSIONLIVE_USERNAME'], os.environ['FUSIONLIVE_PASSWORD']) as client:
        total, records = client.document_page('20462597', limit=5)
        document = min((r for r in records if r.get('hasContent') == 'true'), key=lambda r: int(r['contentSize']))
        print('Selected:', document['id'], document['fileName'], document['contentSize'])
        items = client.related(document['id'])
        (RUNTIME / 'content_sample.json').write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Security flags:', client.rendition_security, client.native_user)
        print('Content items:', json.dumps(items, ensure_ascii=False)[:6500])


if __name__ == '__main__':
    main()
