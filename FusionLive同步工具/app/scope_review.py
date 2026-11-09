"""Offline project-wide inventory summaries for the operator's scope review."""

from paths import HOME, CODE
import csv
import json
import os
from collections import defaultdict
from fusion_client import now
from reporting import volume
from paths import exports_dir


def export_scope_review(ledger, project):
    folders = {row['id']: dict(row) for row in ledger.all('SELECT * FROM folders WHERE project=? AND active=1', (project,))}
    children, documents, placeholders, files = (defaultdict(set) for _ in range(4))
    for folder in folders.values():
        if folder['parent'] in folders:
            children[folder['parent']].add(folder['id'])
    for doc in ledger.all('SELECT * FROM documents WHERE project=? AND active=1', (project,)):
        key = (doc['id'], doc['folder_id'])
        documents[doc['folder_id']].add(key)
        metadata = json.loads(doc['metadata'])
        if metadata.get('hasContent') == 'false' and metadata.get('hasAttachments') != 'true':
            placeholders[doc['folder_id']].add(key)
    content = {row['id']: dict(row) for row in ledger.all('SELECT id,size,status FROM files WHERE project=? AND active=1', (project,))}
    for row in ledger.all('''SELECT x.folder_id,x.file_id FROM document_files x JOIN documents d
      ON d.project=x.project AND d.id=x.document_id AND d.folder_id=x.folder_id
      WHERE x.project=? AND x.active=1 AND d.active=1''', (project,)):
        if row['file_id'] in content:
            files[row['folder_id']].add(row['file_id'])
    cache, visiting = {}, set()
    def collect(identifier):
        if identifier in cache:
            return cache[identifier]
        if identifier in visiting:
            raise RuntimeError('目录层级存在循环，无法生成范围汇总。')
        visiting.add(identifier)
        docs, missing, items = (set(values[identifier]) for values in (documents, placeholders, files))
        for child in children[identifier]:
            subdocs, submissing, subitems = collect(child)
            docs.update(subdocs)
            missing.update(submissing)
            items.update(subitems)
        visiting.remove(identifier)
        cache[identifier] = docs, missing, items
        return cache[identifier]
    target = exports_dir(ledger.base_dir) / '目录范围汇总.csv'
    temp = target.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['工作区ID', '目录ID', '父目录ID', '平台完整目录', '本目录文档记录数',
                         '含子目录文档记录数', '含子目录未上传占位记录数', '本目录原件附件去重数',
                         '含子目录原件附件去重数', '含子目录文件总字节', '含子目录文件GB', '含子目录受限文件数'])
        for folder in sorted(folders.values(), key=lambda row: row['path']):
            docs, missing, items = collect(folder['id'])
            size = sum(content[i]['size'] or 0 for i in items)
            row = [project, folder['id'], folder['parent'], folder['path'], len(documents[folder['id']]),
                   len(docs), len(missing), len(files[folder['id']]), len(items), size, round(size / 1e9, 6),
                   sum(content[i]['status'] == 'restricted' for i in items)]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
    os.replace(temp, target)
    totals = volume(ledger, project)
    scope = ledger.one('SELECT definition,confirmed_at FROM download_scopes WHERE project=?', (project,))
    approved = bool(scope and scope['confirmed_at'] and json.loads(scope['definition']).get('mode') == 'all')
    message = ['全项目资料盘点 / 人工确认施工范围', '生成时间（北京）：' + now().isoformat(),
               '范围：当前账号在该工作区可见的全部目录、文档及关联原件/附件。',
               '完整盘点：' + ('尚未完成，体量只是已知下限' if totals['total_is_lower_bound'] else '目录及关联清单已核对完整'),
               '文件台账.csv：逐条文档、原件和附件，含内容ID、关系类型、大小、状态及平台目录。',
               '项目总体文件目录.txt：网站目录树（包含空目录）。',
               '目录范围汇总.csv：按目录及其全部子目录汇总，供人工选择施工范围。',
               '同一文件在每个目录子树内按内容ID去重；父子目录汇总相互包含，不能逐行相加。',
               '已人工批准全项目下载，正式入口按已保存阶段继续。' if approved else '尚未人工确认范围，正式入口不会开始批量下载；已有下载文件保留。',
               '范围以数据库中的人工确认记录为准；修改汇总CSV不会改变下载范围。',
               '完整平台字段保存在 runtime/ledger.sqlite3 中，当前版本不遍历已不可见的历史版本。',
               '已知内容体量：' + json.dumps(totals, ensure_ascii=False)]
    note = exports_dir(ledger.base_dir) / '人工范围确认说明.txt'
    note.with_suffix('.txt.tmp').write_text('\n'.join(message) + '\n', encoding='utf-8-sig')
    os.replace(note.with_suffix('.txt.tmp'), note)
