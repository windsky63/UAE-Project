"""Bounded live probes: only the generated file recorded in this run's manifest.

These endpoints are NOT part of the verified official Skills API allowlist.
Never reuse this helper as a general-purpose destructive API client.
"""
import argparse
import hashlib
import json
from pathlib import Path

import requests

from .api import ImaClient, ImaError, folder_id
from .config import DEFAULT_SOURCE, RUNTIME, atomic_json, load_credentials


def validate_manifest(record, client):
    name, run_id = record.get('name', ''), record.get('run_id', '')
    path = Path(record.get('local_path', '')).resolve()
    if not run_id or name != f'ima工具测试_{run_id}.txt':
        raise ImaError('实验清单的测试文件名不匹配。')
    if path.parent != DEFAULT_SOURCE.resolve() or path.name != name:
        raise ImaError('实验仅允许操作本工具待同步目录中的专用测试文件。')
    if not record.get('upload_verified') or not record.get('media_id'):
        raise ImaError('实验文件尚未确认成功上传。')
    if record.get('account') != hashlib.sha256(client.client_id.encode()).hexdigest():
        raise ImaError('实验账号与上传账号不一致。')
    if hashlib.sha256(path.read_bytes()).hexdigest() != record['upload'][0]['sha256']:
        raise ImaError('实验文件已经改变。')


def probe(client, endpoint, body):
    if endpoint not in {'create_folder', 'move_knowledge', 'rename_knowledge', 'delete_knowledge'}:
        raise ImaError('不在限定实验接口范围内。')
    if client.rate_gate is not None:
        client.rate_gate.wait(endpoint, client.sleep)
    try:
        response = client.session.post('https://ima.qq.com/openapi/wiki/v1/' + endpoint,
            json=body, headers={'ima-openapi-clientid': client.client_id,
                               'ima-openapi-apikey': client.api_key,
                               'ima-openapi-ctx': 'skill_version=1.1.10'},
            timeout=(15, 60), allow_redirects=False)
    except requests.RequestException:
        return {'transport_error': '请求失败或超时，不自动重试'}
    try:
        result = response.json()
    except ValueError:
        return {'http_status': response.status_code, 'error': '响应不是 JSON'}
    # Persist no access keys even if a service unexpectedly echoes request values.
    result = json.loads(client.redact(json.dumps(result, ensure_ascii=False)))
    if (isinstance(result, dict) and result.get('code') in (200001, 110021)) or response.status_code == 429:
        client.increase_interval()
        if client.rate_gate is not None:
            client.rate_gate.defer(client.retry_delay(response, 120))
    return {'http_status': response.status_code, 'response': result}


def run(record, client, save):
    validate_manifest(record, client)
    kb, media_id, run_id = record['kb'], record['media_id'], record['run_id']
    results = record.setdefault('results', {})

    def checkpoint(operation, body):
        if operation in results:
            raise ImaError(f'{operation} 已有实验记录；不自动重复该写入。')
        results[operation] = {'attempted': True, 'request': body}
        save(record)  # Durable journal before each mutation, including uncertain requests.
        result = probe(client, operation, body)
        results[operation].update(result)
        save(record)
        print(operation, json.dumps(result, ensure_ascii=False))
        return result.get('response', {})

    root = client.listing(kb)
    matching = [r for r in root if r.get('media_id') == media_id]
    if len(matching) != 1 or matching[0].get('title') != record['name']:
        raise ImaError('云端根目录中未找到完全匹配的本轮测试文件，停止实验。')

    folder_name = 'ima工具测试目录_' + run_id
    if any((r.get('title') or r.get('name')) == folder_name for r in root):
        raise ImaError('本轮测试目录已存在，不重复创建或操作其他同名目录。')
    created = checkpoint('create_folder', {'knowledge_base_id': kb, 'name': folder_name})
    folders = client.listing(kb)
    own_folders = [r for r in folders if (r.get('title') or r.get('name') or
                    (r.get('folder_info') or {}).get('name')) == folder_name]
    if len(own_folders) != 1:
        results['create_folder']['verified'] = False
        save(record)
        return record
    folder = folder_id(own_folders[0]) or own_folders[0].get('media_id')
    if not folder:
        raise ImaError('测试目录未返回可用标识。')
    record['test_folder'] = folder
    record['test_folder_name'] = folder_name
    results['create_folder']['verified'] = True
    save(record)

    moved = checkpoint('move_knowledge', {'src_knowledge_base_id': kb, 'dst_knowledge_base_id': kb,
                      'dst_folder_id': folder, 'infos': [{'media_id': media_id}]})
    root_after = client.listing(kb)
    destination = client.listing(kb, folder)
    root_present = any(r.get('media_id') == media_id for r in root_after)
    dest_present = any(r.get('media_id') == media_id for r in destination)
    results['move_knowledge'].update(verified=not root_present and dest_present,
                                    root_present=root_present, destination_present=dest_present)
    save(record)
    current_folder = folder if dest_present else ''
    current = destination if dest_present else root_after
    if not any(r.get('media_id') == media_id for r in current):
        raise ImaError('移动后测试条目位置不明确，停止后续写入。')

    renamed = 'ima工具测试_' + run_id + '_已重命名.txt'
    checkpoint('rename_knowledge', {'knowledge_base_id': kb, 'media_id': media_id, 'name': renamed})
    current = client.listing(kb, current_folder)
    own = [r for r in current if r.get('media_id') == media_id]
    if len(own) != 1:
        raise ImaError('改名后未找到测试条目，停止删除实验。')
    results['rename_knowledge'].update(verified=own[0].get('title') == renamed,
                                       actual_title=own[0].get('title'))
    save(record)

    checkpoint('delete_knowledge', {'knowledge_base_id': kb, 'media_ids': [media_id]})
    current = client.listing(kb, current_folder)
    root_final = client.listing(kb)
    still_present = any(r.get('media_id') == media_id for r in current + root_final)
    results['delete_knowledge'].update(verified=not still_present, still_present=still_present)
    record['final_folder'] = current_folder
    record['final_title'] = renamed if results['rename_knowledge']['verified'] else record['name']
    save(record)
    return record


def main():
    parser = argparse.ArgumentParser(description='仅针对本轮生成的测试 TXT 验证未文档化接口。')
    parser.add_argument('--execute', required=True, action='store_true')
    parser.parse_args()
    path = RUNTIME / 'live-test.json'
    client = ImaClient(*load_credentials())
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        result = run(record, client, lambda value: atomic_json(path, value))
        print(json.dumps({'results': result['results'], 'final_folder': result.get('final_folder')}, ensure_ascii=False, indent=2))
    except (ImaError, OSError, ValueError) as exc:
        print(client.redact(str(exc)))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
