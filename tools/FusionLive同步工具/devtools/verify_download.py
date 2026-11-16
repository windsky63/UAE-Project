import os
import json
from fusion_client import FusionClient, RUNTIME
from paths import relative_path


def main():
    with FusionClient(os.environ['FUSIONLIVE_USERNAME'], os.environ['FUSIONLIVE_PASSWORD']) as client:
        items = client.related('25461372')
        results = []
        for item in items:
            result = client.download(item, RUNTIME / 'download_check' / item['filename'])
            result['path'] = relative_path(result['path'])
            result['content_id'] = item['id']
            results.append(result)
            print('已下载校验:', item['filename'], result['bytes'], result['sha256'])
        (RUNTIME / 'download_check.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
