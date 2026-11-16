from paths import HOME, exports_dir
from fusion_client import now
import csv, os

def export_ledger(ledger, *, supplemental=True, quiet=False):
    from business_type import EXPORT_FIELDS, classify, file_business, ledger_file_contexts
    contexts = ledger_file_contexts(ledger.db)
    target = exports_dir(ledger.base_dir) / '文件台账.xlsx'
    columns = ['工作区', '目录', '文档编号', '文档名称', '版本ID', '修订', '文件名', '关联文件状态', '内容ID', '文件类型', '下载状态', '文件大小', '已保存字节', '本地路径', 'SHA256', '错误', '下载日期（北京）', '日期来源', '平台内容状态', '文件读取权限', '平台内容创建时间', '平台内容修改时间']
    rows = ledger.all('''SELECT d.project,f.path,d.reference,d.title,d.id,d.revision,
      COALESCE(i.filename,d.filename),d.related_state,i.id,i.relation,
      COALESCE(i.status,CASE WHEN json_extract(d.metadata,'$.hasContent')='false' AND COALESCE(json_extract(d.metadata,'$.hasAttachments'),'false')!='true' THEN '未上传内容（占位记录）' WHEN d.related_state='no_content' THEN '未上传内容（占位记录）' ELSE 'not_enumerated' END),COALESCE(i.size,d.size),i.bytes,i.path,i.sha256,
      CASE WHEN d.error IS NOT NULL AND i.error IS NOT NULL THEN d.error || '; ' || i.error ELSE COALESCE(d.error,i.error) END,i.downloaded_at,i.download_date_source,
      CASE WHEN json_extract(d.metadata,'$.hasContent')='false' THEN '未上传主文件' WHEN json_extract(d.metadata,'$.hasContent')='true' THEN '已上传主文件' ELSE '待核对' END,
      json_extract(i.metadata,'$.readAccessStatus'),json_extract(i.metadata,'$.creationDate'),
      COALESCE(json_extract(i.metadata,'$.modifiedDate'),json_extract(i.metadata,'$.lastModifiedDate'),json_extract(i.metadata,'$.updatedDate'),json_extract(i.metadata,'$.uploadDate'))
      FROM documents d JOIN folders f ON f.project=d.project AND f.id=d.folder_id
      LEFT JOIN document_files x ON x.project=d.project AND x.document_id=d.id AND x.folder_id=d.folder_id AND x.active=1
      LEFT JOIN files i ON i.project=x.project AND i.id=x.file_id AND i.active=1
      WHERE d.active=1 AND f.active=1 ORDER BY f.path,d.reference''')
    import uuid
    from archive_index import current_members
    extra_columns = ['记录类型', '所属压缩包内容ID', '所属压缩包文件名', '包内完整路径', '解压状态', '包内条目序号', '内容解析状态']
    temp = target.with_suffix('.' + uuid.uuid4().hex + '.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(columns + list(EXPORT_FIELDS) + extra_columns)
        for row in rows:
            business = (file_business(dict(filename=row[6], path=row[13]), contexts.get((row[0], row[8]), ()))
                        if row[8] else classify(filename=row[6] or '', reference=row[2] or ''))
            row = list(row) + [business[key] for key in EXPORT_FIELDS.values()] + ['平台文件', '', '', '', '', '', '']
            # Prevent spreadsheet formula interpretation of platform-controlled text.
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
        for member in current_members(ledger.base_dir):
            parents = contexts.get((member['project'], member['parent_file_id']), ())
            business = classify(filename=member['filename'])
            values = {key: '' for key in columns}
            values.update({'工作区':member['project'], '目录':'；'.join(dict.fromkeys(str(p.get('folder_path') or '') for p in parents)),
                '文档编号':'；'.join(dict.fromkeys(str(p.get('reference') or '') for p in parents)),
                '文档名称':'；'.join(dict.fromkeys(str(p.get('title') or '') for p in parents)),
                '文件名':member['filename'], '内容ID':member['id'], '文件类型':'压缩包成员',
                '下载状态':'随压缩包下载（非独立下载）', '文件大小':member['bytes'], '已保存字节':member['bytes'],
                '本地路径':member['path'], 'SHA256':member['sha256'],
                '错误':'' if member['availability']=='ready' else '解压文件缺失或大小不符'})
            row = [values[key] for key in columns] + [business[key] for key in EXPORT_FIELDS.values()] + [
                '解压文件', member['parent_file_id'], member['parent_filename'], member['member_path'],
                member['extraction_state'], member['entry'], member['analysis_state']]
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
    from ledger_workbook import export_workbook
    try:
        actual_target = export_workbook(temp, target)
    finally:
        temp.unlink(missing_ok=True)
    legacy = target.with_suffix('.csv')
    if legacy.is_file() and not legacy.is_symlink():
        history = target.parent / '历史导出'
        if history.resolve().is_relative_to(target.parent.resolve()):
            history.mkdir(exist_ok=True)
            saved = history / ('文件台账-' + now().strftime('%Y%m%d-%H%M%S-%f') + '.csv')
            try:
                os.replace(legacy, saved)
            except PermissionError:
                pass  # An open historical CSV need not block the new Excel export.
    if supplemental:
        export_directory(ledger)
        export_changes(ledger)
        export_association_failures(ledger)
    if not quiet:
        print('Excel 台账：', actual_target, flush=True)
    return actual_target

def export_directory(ledger):
    folders = ledger.all('''SELECT f.*,COUNT(d.id) document_count FROM folders f
      LEFT JOIN documents d ON d.project=f.project AND d.folder_id=f.id AND d.active=1
      WHERE f.active=1 GROUP BY f.project,f.id ORDER BY f.name,f.id''')
    children = {}
    for folder in folders:
        children.setdefault((folder['project'], folder['parent']), []).append(folder)
    lines = ['FusionLive 当前账号可见的项目目录树',
             '导出时间（北京）：' + now().isoformat(),
             '括号内为该目录直接包含的文档记录数，含未上传内容的占位记录。', '']
    def emit(folder, prefix='', branch=''):
        lines.append(prefix + branch + folder['name'] + f'  [ID {folder["id"]}；文档 {folder["document_count"]}]')
        nodes = children.get((folder['project'], folder['id']), [])
        for index, child in enumerate(nodes):
            last = index == len(nodes) - 1
            emit(child, prefix + (('    ' if branch == '└── ' else '│   ') if branch else ''), '└── ' if last else '├── ')
    for folder in folders:
        if folder['parent'] is None:
            emit(folder)
    target = exports_dir(ledger.base_dir) / '项目总体文件目录.txt'
    temp = target.with_suffix('.txt.tmp')
    temp.write_text('\n'.join(lines) + '\n', encoding='utf-8-sig')
    os.replace(temp, target)

def export_changes(ledger):
    target = exports_dir(ledger.base_dir) / '本次盘点变更.csv'
    latest = ledger.one('SELECT id FROM snapshots ORDER BY id DESC LIMIT 1')
    rows = ledger.all('SELECT entity,entity_id,folder_id,kind,details,created FROM changes WHERE snapshot=? ORDER BY id', (latest['id'],)) if latest else []
    with target.with_suffix('.csv.tmp').open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['对象类型', '对象ID', '目录ID', '变化类型', '详情', '北京时间'])
        for row in rows:
            writer.writerow(["'" + value if isinstance(value, str) and value.startswith(('=', '+', '-', '@')) else value for value in row])
    os.replace(target.with_suffix('.csv.tmp'), target)

def export_association_failures(ledger):
    target = exports_dir(ledger.base_dir) / '关联核对异常.csv'
    rows = ledger.all('''SELECT d.project,d.reference,d.id,f.path,d.revision,d.filename,d.size,d.error
      FROM documents d JOIN folders f ON f.project=d.project AND f.id=d.folder_id
      WHERE d.active=1 AND f.active=1 AND d.related_state='failed' ORDER BY d.project,f.path,d.reference''')
    temp = target.with_suffix('.csv.tmp')
    with temp.open('w', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        writer.writerow(['工作区', '文档编号', '版本ID', '平台目录', '修订', '文件名', '主文件字节', '关联核对错误'])
        for row in rows:
            writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in row])
    os.replace(temp, target)

export_csv = export_ledger
