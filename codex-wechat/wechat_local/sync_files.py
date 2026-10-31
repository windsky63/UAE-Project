"""Read-only FusionLive file bridge; also runs standalone inside a chat workspace."""
import argparse
from contextlib import closing
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
import zipfile

TEXT_TYPES = {'.txt', '.md', '.csv', '.json', '.log', '.pcf', '.idf', '.xml'}
MAX_TEXT = 2 * 1024 * 1024
MAX_RESULT = 64 * 1024 * 1024
MAX_SOURCE = 256 * 1024 * 1024


def readonly(path):
    db = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def download_path(root, name):
    base = (Path(root) / 'downloads').resolve()
    supplied = Path(root) / str(name).replace('\\', '/')
    path = supplied.resolve()
    if not path.is_relative_to(base) or path == base:
        raise ValueError('File path is outside the configured downloads directory')
    return path


def business_classifier(root):
    """Use the sync tool's reviewed classification rules, including parent links."""
    spec = importlib.util.spec_from_file_location('wechat_project_business', root / 'app/business_type.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.RULES_PATH = root / 'document_coding_rules.json'
    module.load_rules()
    return module


def archive_index(root):
    spec = importlib.util.spec_from_file_location('wechat_project_archive_index', Path(root) / 'app/archive_index.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prepare_catalog(config, chat, workspace, access_dirs=None):
    """Refresh just this chat's permitted current files; never mutate source data."""
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='sync-', suffix='.sqlite3', dir=workspace)
    os.close(fd)
    temp = Path(name)
    health, count, member_count = [], 0, 0
    sources = [source for source in config.sync_sources if source.id in chat.sync_sources]
    discovery_warnings = []
    if access_dirs is not None:
        allowed = [Path(p).resolve() for p in access_dirs]
        def permitted(path):
            resolved = Path(path).resolve()
            return any(resolved.is_relative_to(base) for base in allowed)
        discovery_warnings.extend(dict(source=s.id, state='outside_shared_directories')
                                  for s in sources if not permitted(s.root))
        sources = [source for source in sources if permitted(source.root)]
        # No per-chat source mapping is needed for tools already in the shared directory grant.
        if not chat.sync_sources:
            for root in allowed:
                ledger = root / 'runtime/ledger.sqlite3'
                if not ledger.is_file():
                    continue
                try:
                    with closing(readonly(ledger)) as db:
                        projects = [str(r[0]) for r in db.execute('SELECT DISTINCT project FROM files WHERE active=1')]
                    sources.append(SimpleNamespace(id='directory-'+hashlib.sha256(str(root).encode()).hexdigest()[:12],
                                                   root=str(root), projects=projects))
                except (OSError, sqlite3.Error):
                    discovery_warnings.append(dict(directory=str(root), state='ledger_unavailable'))
    try:
        with closing(sqlite3.connect(temp)) as out:
            out.executescript('''CREATE TABLE files(
              id TEXT PRIMARY KEY,source TEXT,root TEXT,project TEXT,file_id TEXT,
              filename TEXT,path TEXT,bytes INTEGER,sha256 TEXT,availability TEXT,
              job_key TEXT,analysis_state TEXT,result_path TEXT,business TEXT,
              kind TEXT DEFAULT 'platform',parent_id TEXT DEFAULT '',member_id TEXT DEFAULT '',
              member_path TEXT DEFAULT '',archive TEXT DEFAULT '',parent_sha256 TEXT DEFAULT '',member_origin TEXT DEFAULT '');
              CREATE TABLE health(source TEXT,state TEXT,classification_state TEXT,projects TEXT);
            ''')
            for source in sources:
                root = Path(source.root).resolve()
                classifier = None
                classification_state = 'unavailable'
                try:
                    classifier = business_classifier(root)
                    classification_state = 'ready'
                except (OSError, ImportError, AttributeError, ValueError):
                    pass
                try:
                    jobs = {}
                    queue_path = root / 'runtime/file_analysis/queue.sqlite3'
                    if queue_path.is_file():
                        with closing(readonly(queue_path)) as queue:
                            for job in queue.execute('SELECT * FROM jobs WHERE active=1 ORDER BY updated'):
                                jobs[str(job['project']), str(job['file_id']), job['expected_sha']] = dict(job)
                    with closing(readonly(root / 'runtime/ledger.sqlite3')) as db:
                        contexts = classifier.ledger_file_contexts(db) if classifier else {}
                        rows = db.execute("SELECT project,id,filename,path,bytes,sha256 FROM files WHERE active=1 AND status='done'")
                        staged = []
                        for row in rows:
                            project = str(row['project'])
                            if project not in source.projects:
                                continue
                            try:
                                path = download_path(root, row['path'])
                                availability = ('ready' if path.is_file() and path.stat().st_size == row['bytes']
                                                else 'missing_or_size_mismatch')
                            except (ValueError, OSError, TypeError):
                                availability = 'invalid_path'
                            job = jobs.get((project, str(row['id']), row['sha256']), {})
                            identity = hashlib.sha256(json.dumps([source.id, project, str(row['id'])]).encode()).hexdigest()[:24]
                            business = (classifier.file_business(dict(row), contexts.get((project, row['id']), ()))
                                        if classifier else dict(business_type='规则未就绪', status='unavailable'))
                            staged.append((identity, source.id, str(root), project, str(row['id']),
                                row['filename'], row['path'], row['bytes'], row['sha256'], availability,
                                job.get('key'), job.get('state', 'not_indexed'), job.get('result_path'),
                                json.dumps(business, ensure_ascii=False)))
                    out.executemany('INSERT INTO files(id,source,root,project,file_id,filename,path,bytes,sha256,availability,job_key,analysis_state,result_path,business) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)', staged)
                    count += len(staged)
                    parent_ids = {(r[3], r[4]): r[0] for r in staged if r[9] == 'ready'}
                    if (root / 'app/archive_index.py').is_file():
                        try:
                            extracted = archive_index(root).current_members(root)
                        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                            extracted = []
                            discovery_warnings.append(dict(source=source.id, state='archive_index_unavailable'))
                        for member in extracted:
                            parent_id = parent_ids.get((member['project'], member['parent_file_id']))
                            if not parent_id:
                                continue
                            business = (classifier.classify(filename=member['filename']) if classifier else
                                        dict(business_type='规则未就绪',status='unavailable'))
                            member_id = hashlib.sha256(json.dumps([source.id,member['id']]).encode()).hexdigest()[:24]
                            out.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (
                                member_id, source.id, str(root), member['project'], member['parent_file_id'],
                                member['filename'], member['path'], member['bytes'], member['sha256'], member['availability'],
                                member['job_key'], member['analysis_state'], member['result_path'],
                                json.dumps(business,ensure_ascii=False), 'extracted', parent_id, member['id'],
                                member['member_path'], member['parent_filename'], member['parent_sha256'], member['origin']))
                            member_count += 1
                    state = 'ready'
                except (OSError, sqlite3.Error):
                    state = 'source_unavailable'
                health.append(dict(source=source.id, state=state))
                out.execute('INSERT INTO health VALUES(?,?,?,?)',
                            (source.id, state, classification_state, json.dumps(source.projects, ensure_ascii=False)))
            out.commit()
        os.replace(temp, workspace / 'sync_catalog.sqlite3')
    finally:
        temp.unlink(missing_ok=True)
    return dict(files=count+member_count, platform_files=count, extracted_files=member_count,
                sources=health, discovery_warnings=discovery_warnings,
                helper='.agents/skills/wechat-chat/sync_files.py')


def search(root, query='', offset=0, limit=8):
    with closing(readonly(Path(root) / 'sync_catalog.sqlite3')) as db:
        condition = 'instr(lower(filename || char(10) || project || char(10) || path || char(10) || member_path || char(10) || archive), lower(?))>0'
        total = db.execute('SELECT count(*) FROM files WHERE ' + condition, (query,)).fetchone()[0]
        rows = db.execute('SELECT id,source,project,filename,bytes,availability,analysis_state,business,kind,parent_id,member_path,archive FROM files WHERE '
                          + condition + ' ORDER BY source,project,filename,id LIMIT ? OFFSET ?', (query, limit, offset)).fetchall()
        return dict(items=[{**dict(row), 'business': json.loads(row['business'])} for row in rows], total=total, offset=offset,
                    sources=[dict(r) for r in db.execute('SELECT * FROM health')])


def summary(root, project=''):
    """Aggregate the full authorized ledger snapshot, never just a search page."""
    with closing(readonly(Path(root) / 'sync_catalog.sqlite3')) as db:
        where = "WHERE kind='platform'" + (' AND project=?' if project else '')
        params = (project,) if project else ()
        rows = db.execute('SELECT source,project,availability,business FROM files ' + where, params)
        groups, total, missing = {}, 0, 0
        for row in rows:
            total += 1
            if row['availability'] != 'ready':
                missing += 1
                continue
            business = json.loads(row['business'])
            key = (row['source'], row['project'], business['business_type'], business['status'])
            groups[key] = groups.get(key, 0) + 1
        sources = [{**dict(r), 'projects': json.loads(r['projects'])} for r in db.execute('SELECT * FROM health')]
        children = list(db.execute("SELECT source,project,path,availability,business FROM files WHERE kind='extracted'" +
                                  (' AND project=?' if project else ''), params))
        child_groups = {}
        for child in children:
            business = json.loads(child['business'])
            key = child['source'], child['project'], business['business_type'], business['status']
            child_groups[key] = child_groups.get(key, 0) + 1
        return dict(as_of=datetime.now(timezone.utc).isoformat(), project=project or None,
                    basis="同步工具台账 active=1 AND status='done'，按来源、项目、文件ID去重；业务类型复用同步工具编码规则和关联文档，不按扩展名归类",
                    scope='仅本聊天已授权的来源和项目，不代表未授权项目', sources=sources,
                    downloaded_records=total, available_files=total-missing, missing_or_invalid=missing,
                    extracted_records=len(children), available_extracted_files=len({(r['source'],r['project'],r['path']) for r in children if r['availability']=='ready'}),
                    extracted_business_types=[dict(source=k[0],project=k[1],business_type=k[2],classification_status=k[3],records=v)
                                              for k,v in sorted(child_groups.items())],
                    extracted_basis='已解压文件单独统计，不计入平台下载数量；包含嵌套包和未接入内容解析的格式',
                    items=[dict(source=k[0], project=k[1], business_type=k[2], classification_status=k[3], files=v)
                           for k, v in sorted(groups.items())])


def current_file(root, identity):
    with closing(readonly(Path(root) / 'sync_catalog.sqlite3')) as db:
        row = db.execute('SELECT * FROM files WHERE id=?', (identity,)).fetchone()
    if row is None:
        raise ValueError('File ID is not authorized for this chat')
    item = dict(row)
    if item.get('kind') == 'extracted':
        with closing(readonly(Path(root) / 'sync_catalog.sqlite3')) as db:
            parent = db.execute('SELECT * FROM files WHERE id=? AND kind=\'platform\'', (item['parent_id'],)).fetchone()
        if parent is None:
            raise ValueError('Parent archive is not authorized for this chat')
        current_file(root, item['parent_id'])
        member, path = archive_index(item['root']).verified_member(item['root'], item['member_id'])
        if member['sha256'] != item['sha256'] or str(path) != item['path']:
            raise ValueError('Extracted file version changed; refresh the chat catalog')
        return item, path
    with closing(readonly(Path(item['root']) / 'runtime/ledger.sqlite3')) as db:
        actual = db.execute('SELECT * FROM files WHERE project=? AND id=? AND active=1 AND status=\'done\'',
                            (item['project'], item['file_id'])).fetchone()
    if actual is None or any(actual[k] != item[k] for k in ('path', 'sha256', 'bytes')):
        raise ValueError('File version changed; refresh the chat catalog')
    path = download_path(item['root'], item['path'])
    if not path.is_file() or path.stat().st_size != item['bytes']:
        raise ValueError('Downloaded file is missing or incomplete')
    if not item['sha256']:
        raise ValueError('Downloaded file has no verified content hash')
    return item, path


def verify_hash(item, path):
    if path.stat().st_size > MAX_SOURCE:
        raise ValueError('File exceeds the 256 MiB read limit')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    if digest.hexdigest() != item['sha256']:
        raise ValueError('Downloaded content hash mismatch')


def members(root, identity, offset=0, limit=8):
    item, path = current_file(root, identity)
    verify_hash(item, path)
    if path.suffix.lower() != '.zip':
        raise ValueError('Archive browsing currently supports ZIP only')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        return dict(filename=item['filename'], total=len(entries), items=[
            dict(member=i.filename, bytes=i.file_size, directory=i.is_dir())
            for i in entries[offset:offset + limit]])


def read_file(root, identity, offset=0, limit=4000, member=''):
    item, path = current_file(root, identity)
    verify_hash(item, path)
    if member:
        if path.suffix.lower() != '.zip' or Path(member).suffix.lower() not in TEXT_TYPES:
            raise ValueError('Only text members of ZIP files can be read')
        with zipfile.ZipFile(path) as archive:
            matches = [i for i in archive.infolist() if i.filename == member]
            if len(matches) != 1:
                raise ValueError('ZIP member is missing or ambiguous')
            if matches[0].file_size > MAX_TEXT:
                raise ValueError('Text member exceeds 2 MiB')
            data = archive.read(matches[0])
    else:
        if path.suffix.lower() not in TEXT_TYPES:
            raise ValueError('Content extraction supports text/PCF/IDF only; use analysis for parsed results')
        if path.stat().st_size > MAX_TEXT:
            raise ValueError('Text file exceeds 2 MiB')
        data = path.read_bytes()
    text = data.decode('utf-8-sig', errors='replace')
    return dict(filename=item['filename'], sha256=item['sha256'], member=member,
                text=text[offset:offset + limit], offset=offset, total_chars=len(text))


def analysis(root, identity, section='groups', offset=0, limit=8, query=''):
    item, source_path = current_file(root, identity)
    if item.get('kind') == 'extracted':
        raise ValueError('成员解析结果请使用所属压缩包ID ' + item['parent_id'] + ' 查询；包级汇总不能当作单个成员的汇总')
    verify_hash(item, source_path)
    base = (Path(item['root']) / 'runtime/file_analysis').resolve()
    with closing(readonly(base / 'queue.sqlite3')) as db:
        job = db.execute('SELECT * FROM jobs WHERE key=? AND active=1 AND project=? AND file_id=? AND expected_sha=?',
                         (item['job_key'], item['project'], item['file_id'], item['sha256'])).fetchone()
    if job is None or job['state'] not in ('done', 'failed') or not job['result_path']:
        raise ValueError('No committed analysis result for the current file')
    path = (base / job['result_path']).resolve()
    if not path.is_relative_to(base / 'jobs') or path.name != 'analysis.json':
        raise ValueError('Analysis result path is outside the job cache')
    if path.stat().st_size > MAX_RESULT:
        raise ValueError('Analysis result exceeds 64 MiB')
    report = json.loads(path.read_text(encoding='utf-8'))
    rows = [r for r in report.get(section, []) if str(r.get('project', r.get('项目', ''))) == item['project']]
    if section == 'pipeline_results':
        rows = [{k: v for k, v in row.items() if k != 'rows'} for row in rows]
    rows = [r for r in rows if query.casefold() in json.dumps(r, ensure_ascii=False).casefold()]
    return dict(filename=item['filename'], state=job['state'], error=job['error'],
                section=section, total=len(rows), items=rows[offset:offset + limit],
                notes=report.get('notes', []), basis='Per-source cached results; do not add overlapping pipelines across files')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['search', 'read', 'members', 'analysis', 'summary'])
    parser.add_argument('--project', default='')
    parser.add_argument('--workspace', default='', help='Explicit authorized snapshot workspace')
    parser.add_argument('--id', default='')
    parser.add_argument('--query', default='')
    parser.add_argument('--member', default='')
    parser.add_argument('--section', choices=['groups', 'files', 'welds', 'pipeline_results'], default='groups')
    parser.add_argument('--offset', type=int, default=0)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    limit = args.limit if args.limit is not None else (4000 if args.action == 'read' else 8)
    if args.offset < 0 or not 1 <= limit <= (4000 if args.action == 'read' else 20):
        parser.error('Invalid page size or offset')
    try:
        root = Path(args.workspace).resolve() if args.workspace else Path.cwd()
        if args.action == 'summary':
            result = summary(root, args.project)
        elif args.action == 'search':
            result = search(root, args.query, args.offset, limit)
        elif args.action == 'read':
            result = read_file(root, args.id, args.offset, limit, args.member)
        elif args.action == 'members':
            result = members(root, args.id, args.offset, limit)
        else:
            result = analysis(root, args.id, args.section, args.offset, limit, args.query)
    except (OSError, ValueError, KeyError, sqlite3.Error, zipfile.BadZipFile, RuntimeError) as exc:
        print(json.dumps(dict(error=str(exc)), ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
