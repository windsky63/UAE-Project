"""Local daily audit files. No account credentials or cookies are logged."""

from paths import HOME, CODE
import csv
import json
import hashlib
import os
import sys
from datetime import timedelta, datetime
from fusion_client import HOME, now, LOCAL
from paths import resolve_path, relative_path
import re
import stat
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from collections import defaultdict
from file_inventory import collect_inventory, scan_files, relative_key, summary as inventory_summary
from runtime_logging import BoundedLog, EventGate, operation_log


def day_folder(base=HOME):
    folder = base / 'logs' / now().strftime('%Y-%m-%d')
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def record_download(base, item, status, size=0, path='', error=''):
    target = day_folder(base) / 'downloads.csv'
    new = not target.exists()
    action_column = new
    if not new:
        with target.open(encoding='utf-8-sig',newline='') as stream:
            action_column = '下载行为' in next(csv.reader(stream),[])
    with target.open('a', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        if new:
            writer.writerow(['北京时间', '工作区', '内容ID', '文件名', '结果', '字节', '本地路径', '错误', '下载行为'])
        values = [now().isoformat(), item['project'], item['id'], item['filename'], status, size, path, error]
        if action_column:
            values.append({'new':'新增','updated':'更新','unchanged':'内容未变'}.get(dict(item).get('download_action'),''))
        writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])


def download_activity(ledger, at=None, *, audit_path=None):
    """Daily successful transfers, classified by their pre-download directory snapshot."""
    from inventory import source_file_identity
    from file_layout import replacement_links
    at = (at or now()).astimezone(LOCAL)
    day = at.date().isoformat()
    tables = {r[0] for r in ledger.all("SELECT name FROM sqlite_master WHERE type='table'")}
    files = [dict(r) for r in ledger.all('SELECT * FROM files')]
    by_key = {(r['project'],r['id']):r for r in files}
    parents = {}
    def root(key):
        parents.setdefault(key,key)
        if parents[key]!=key:
            parents[key]=root(parents[key])
        return parents[key]
    def join(a,b):
        a,b=root(a),root(b)
        if a!=b:
            parents[max(a,b)]=min(a,b)
    for key,row in by_key.items():
        root(key)
        identity=source_file_identity(row)
        if identity:
            join(key,(key[0],'source:'+identity))
    mapping={(r['project'],r['id']):{'path':r['target_path'] or r['path'] or ''} for r in files if r['active']}
    for key,successors in replacement_links(ledger,files,mapping,conservative=False).items():
        for row in successors:
            join(key,(row['project'],row['id']))
    ignored = 0
    events, starts = [], {}
    if 'download_activity' in tables:
        start = at.replace(hour=0,minute=0,second=0,microsecond=0)
        for row in ledger.all('SELECT * FROM download_activity WHERE completed>=? AND completed<? ORDER BY id',
                              (start.isoformat(),(start+timedelta(days=1)).isoformat())):
            completed = datetime.fromisoformat(row['completed']).astimezone(LOCAL)
            if completed.date().isoformat()!=day or completed>at:
                continue
            started = datetime.fromisoformat(row['started']).astimezone(LOCAL)
            key = (row['project'],row['file_id'])
            starts[key] = min(starts.get(key,started),started)
            if row['source_identity']:
                join(key,(key[0],'source:'+row['source_identity']))
            kind=row['action']
            if kind not in ('new','updated','unchanged'):
                ignored += 1
                continue
            events.append(dict(key=key,at=completed,bytes=row['bytes'],kind=kind))
    log = Path(audit_path) if audit_path is not None else ledger.base_dir/'logs'/day/'downloads.csv'
    log_records = invalid = 0
    if log.is_file():
        with log.open(encoding='utf-8-sig',newline='') as stream:
            reader = csv.DictReader(stream)
            required = {'北京时间','工作区','内容ID','结果','字节','本地路径'}
            if not required <= set(reader.fieldnames or []):
                raise ValueError('下载日志缺少日报所需字段：'+str(log))
            for row in reader:
                if row.get('结果')!='done':
                    continue
                try:
                    completed = datetime.fromisoformat(row['北京时间'])
                    if completed.tzinfo is None:
                        raise ValueError('下载记录缺少时区')
                    completed = completed.astimezone(LOCAL)
                    if completed.date().isoformat()!=day or completed>at:
                        continue
                    size = int(row['字节'])
                    key = (row['工作区'],row['内容ID'])
                    path = relative_key(row['本地路径']) or ''
                    if size<0 or not all(key) or not path.startswith(('downloads/','runtime/download_ready/')):
                        raise ValueError('下载成功记录不完整')
                except (TypeError,ValueError,KeyError):
                    invalid += 1
                    continue
                log_records += 1
                # New clients commit the snapshot with success before writing
                # CSV. Do not count that mirrored CSV row as another transfer.
                if key in starts and completed>=starts[key]:
                    continue
                kind={'新增':'new','更新':'updated','内容未变':'unchanged'}.get(row.get('下载行为'))
                if kind is None:
                    ignored += 1
                    continue
                events.append(dict(key=key,at=completed,bytes=size,kind=kind))
    selected = {}
    for event in sorted(events,key=lambda event:event['at']):
        key=root(event['key'])
        if key not in selected:
            selected[key] = dict(event)
        else:
            # First successful transfer fixes the day's category; last success
            # supplies the final file size, including a same-day replacement.
            selected[key]['bytes'] = event['bytes']
            if selected[key]['kind']=='unchanged' and event['kind']!='unchanged':
                selected[key]['kind']=event['kind']
    def totals(kind=None):
        values = [r for r in selected.values() if kind is None or r['kind']==kind]
        return dict(files=len(values),bytes=sum(r['bytes'] for r in values))
    return dict(total=totals(),new=totals('new'),updated=totals('updated'),
                success_records=len(events),ignored_success_records=ignored,
                unchanged=totals('unchanged'),
                duplicate_records=len(events)-len(selected),log_success_records=log_records,
                invalid_success_rows=invalid,
                basis='仅使用成功下载时记录的明确行为；无行为的旧记录不计入；同一文件全天去重，按最后有行为实录的成功版本体量计量')


def volume(ledger, project=None):
    args = (project, project)
    main = ledger.one('SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM (SELECT project,id,MAX(size) size FROM documents WHERE active=1 AND (? IS NULL OR project=?) GROUP BY project,id)', args)
    known = ledger.one('''SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM (
      SELECT project,id,MAX(size) size FROM (
        SELECT project,id,size FROM documents WHERE active=1 AND (? IS NULL OR project=?) UNION ALL
        SELECT project,id,size FROM files WHERE active=1 AND (? IS NULL OR project=?)) GROUP BY project,id)''', args * 2)
    pending = ledger.one("SELECT COUNT(*) n FROM documents WHERE active=1 AND (? IS NULL OR project=?) AND related_state IN ('pending','failed')", args)['n']
    catalogue = ledger.one("SELECT COUNT(*) n FROM folders WHERE active=1 AND (? IS NULL OR project=?) AND (tree_state!='done' OR list_state!='done')", args)['n']
    content = ledger.one('''SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes,
      COALESCE(SUM(CASE WHEN status!='restricted' THEN size ELSE 0 END),0) readable_bytes
      FROM files WHERE active=1 AND (? IS NULL OR project=?)''', args)
    return {'scope': '当前账号、已盘点工作区可见文件；不是整个平台所有项目',
            'main_documents': dict(main), 'known_main_and_attachments': dict(known),
            'content_files': dict(content), 'content_GB': round(content['bytes'] / 1e9, 3),
            'associations_pending': pending, 'catalogue_incomplete': bool(catalogue),
            'total_is_lower_bound': bool(pending or catalogue),
            'known_GB': round(known['bytes'] / 1e9, 3), 'known_GiB': round(known['bytes'] / 1024**3, 3)}


def write_summary(ledger, run_id):
    run = dict(ledger.one('SELECT * FROM runs WHERE id=?', (run_id,)))
    completed = ledger.one('''SELECT COUNT(*) files,COALESCE(SUM(bytes),0) bytes FROM files
      WHERE download_date_source='下载完成实录' AND downloaded_at>=? AND downloaded_at<=?''',
      (run['started'], run['ended'] or now().isoformat()))
    data = {'run': run, 'downloaded_this_run': dict(completed),
            'ledger': ledger.summary(), 'volume': volume(ledger)}
    target = day_folder(ledger.base_dir) / f'run-{run_id}-summary.json'
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def build_push_text(ledger, at=None, *, persist=True):
    """Render current facts without requiring a pre-generated text file."""
    at = at or now()
    date = at.date().isoformat()
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    runs = ledger.all('SELECT * FROM runs WHERE started>=? AND started<? ORDER BY id',
                      (start.isoformat(), (start+timedelta(days=1)).isoformat()))
    stats = system_report_stats(ledger,at,persist=persist)
    running = any(r['status']=='running' for r in runs)
    owner, local = stats['owner'], stats['file_inventory']['downloads']
    owner_new, local_new = stats['owner_new'], stats['download_activity']['new']
    def total_line(label, values):
        return f'{label}{"：" if label=="业主系统" else "   "}{values["files"]:,}个文件，{values["bytes"]/1e9:.2f} GB，{values["bytes"]:,}字节'
    def new_line(label, values, operation='新增'):
        if values is None:
            return f'{label}{"：" if label=="业主系统" else "   "}{operation}文件—份、— MB、—字节'
        return f'{label}{"：" if label=="业主系统" else "   "}{operation}文件{values["files"]:,}份、{values["bytes"]/1e6:.2f} MB、{values["bytes"]:,}字节'
    local_line = ('NCC系统   统计待本轮业主盘点完成后核对' if not stats['owner_scope_matches'] else
                  total_line('NCC系统',local) if stats['directory_reconciled'] else
                  'NCC系统   统计待目录整理完成后核对')
    lines = ['【阿联酋项目资料下载日报】',at.strftime('%Y年%m月%d日'),'',
             '一、总体进展',total_line('业主系统',owner),local_line,'',
             '二、今日更新情况',new_line('业主系统',owner_new),
             new_line('业主系统',stats['owner_updated'],'更新'),new_line('NCC系统',local_new),
             new_line('NCC系统',stats['download_activity']['updated'],'更新')]
    directory = stats['download_directory']
    state = {'date': date, 'generated_at': at.isoformat(),
             'ready':bool(runs and not running and stats['owner_verified_today'] and stats['directory_reconciled']),
             'owner_inventory_complete_today':stats['owner_verified_today'],
             'owner_scope_matches':stats['owner_scope_matches'],
             'directory_reconciled':stats['directory_reconciled'],
             'pending_publication_files':stats['pending_publication_files'],
             'file_inventory':stats['file_inventory'],
             'effective_downloads':stats['effective_downloads'],
             'download_activity':stats['download_activity'],
             'download_directory': directory}
    return '\n'.join(lines)+'\n',state


def read_live_push_text(base, at=None, *, require_ready=False):
    """Read-only preview/send-time source; no output files or schema upgrades."""
    from file_layout import ReadLedger
    base = Path(base)
    database = base/'runtime/ledger.sqlite3'
    if not database.is_file():
        raise ValueError('下载台账尚未就绪')
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)) as db:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        text,state=build_push_text(ReadLedger(db,base),at,persist=False)
    if require_ready and not state['ready']:
        raise ValueError('下载日报尚未就绪，未推送')
    return text.strip(),state


def write_push_text(ledger, at=None):
    """Publish text and its authoritative statistics snapshot together."""
    text,state=build_push_text(ledger,at)
    folder=ledger.base_dir/'logs'/state['date']
    folder.mkdir(parents=True,exist_ok=True)
    target=folder/'微信推送文.txt'
    marker=folder/'推送状态.json'
    ready=state['ready']
    state.update(ready=False,text_file=target.name,text_sha256=None)
    marker_temp = marker.with_suffix('.json.tmp')
    marker_temp.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(marker_temp, marker)
    temp = target.with_suffix('.txt.tmp')
    temp.write_text(text, encoding='utf-8-sig')
    os.replace(temp, target)
    state.update(ready=ready,text_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    marker_temp.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(marker_temp, marker)
    state.update(snapshot_format='daily-file-snapshot-v1',text=text.strip(),
                 body_sha256=hashlib.sha256(text.strip().encode('utf-8')).hexdigest())
    snapshot=folder/'日报快照.json'
    temporary=folder/('日报快照-'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,snapshot)
    return target


def generate_daily_snapshot(base, at=None):
    from file_layout import ReadLedger
    from file_inventory import read_daily_snapshot
    base=Path(base)
    database=base/'runtime/ledger.sqlite3'
    if not database.is_file():
        raise ValueError('下载台账尚未就绪')
    with closing(sqlite3.connect(database,timeout=15)) as db:
        db.row_factory=sqlite3.Row
        write_push_text(ReadLedger(db,base),at)
    return read_daily_snapshot(base,(at or now()).date().isoformat())


def try_write_push_text(ledger):
    try:
        return write_push_text(ledger)
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        print(f'推送文生成失败（不影响下载和注销）：{type(exc).__name__}：{exc}', file=sys.stderr)
        return None


class Tee:
    def __init__(self, stream, log, *, important=False, timestamped=False):
        self.stream, self.log = stream, log
        self.gate, self.pending, self.important = EventGate(), '', important
        self.timestamped = timestamped
        self.log_warned = False

    def output(self, message):
        value = message + '\n'
        prefix = f'[{now().isoformat()}] [同步 PID={os.getpid()}] [{"ERROR" if self.important else "INFO"}] ' if self.timestamped else ''
        try:
            self.log.write(prefix + value)
        except OSError as exc:
            if not self.log_warned:
                self.stream.write(f'运行日志写入失败，继续执行并在窗口保留输出：{type(exc).__name__}：{exc}\n')
                self.log_warned = True
        self.stream.write(prefix + value)

    def write(self, value):
        self.pending += value
        while '\n' in self.pending:
            line, self.pending = self.pending.split('\n', 1)
            if line.startswith('@@PROGRESS '):
                # The scheduler needs every event; runtime logs need readable summaries.
                self.stream.write(line + '\n')
            elif line.lstrip().startswith(('{', '}', '[', ']', '"')):
                # Preserve multiline status JSON, including repeated closing braces.
                self.output(line)
            else:
                for message in self.gate.accept(line, important=self.important):
                    self.output(message)
        return len(value)

    def flush(self):
        self.log.flush()
        self.stream.flush()

    def finish(self):
        if self.pending:
            self.output(self.pending)
            self.pending = ''
        for message in self.gate.drain():
            self.output(message)
        self.flush()


def logged_main(main):
    stdout, stderr = sys.stdout, sys.stderr
    log = BoundedLog(operation_log('run.log', HOME))
    out, err = Tee(stdout, log, timestamped=True), Tee(stderr, log, important=True, timestamped=True)
    sys.stdout, sys.stderr = out, err
    try:
        print('\n任务启动（北京）：', now().isoformat(), flush=True)
        return main()
    except BaseException:
        import traceback
        traceback.print_exc()
        raise
    finally:
        try:
            out.finish()
            err.finish()
        finally:
            sys.stdout, sys.stderr = stdout, stderr


def init_report_tables(ledger):
    ledger.db.executescript('''
      CREATE TABLE IF NOT EXISTS report_remote_seed(project TEXT PRIMARY KEY);
      CREATE TABLE IF NOT EXISTS report_remote_contents(
        project TEXT,id TEXT,first_day TEXT,size INTEGER NOT NULL,PRIMARY KEY(project,id));
      CREATE TABLE IF NOT EXISTS report_remote_versions(
        project TEXT,id TEXT,fingerprint TEXT,size INTEGER NOT NULL,updated_day TEXT,
        PRIMARY KEY(project,id));
      CREATE TABLE IF NOT EXISTS report_owner_totals(
        project TEXT PRIMARY KEY,files INTEGER NOT NULL,bytes INTEGER NOT NULL,observed_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS report_owner_checks(
        project TEXT,day TEXT,snapshot INTEGER,PRIMARY KEY(project,day));
      CREATE TABLE IF NOT EXISTS report_local_paths(
        path TEXT PRIMARY KEY,first_day TEXT,size INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS report_local_versions(
        project TEXT,id TEXT,version TEXT,first_day TEXT,size INTEGER NOT NULL,path TEXT NOT NULL,
        PRIMARY KEY(project,id,version));
      CREATE TABLE IF NOT EXISTS report_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
    ''')


def seed_remote(ledger, at):
    projects = ledger.all('SELECT DISTINCT project FROM files')
    with ledger.db:
        for row in projects:
            project = row['project']
            if ledger.one('SELECT 1 FROM report_remote_seed WHERE project=?',(project,)):
                continue
            # Existing content IDs, including old/inactive versions, are the upgrade baseline.
            ledger.db.execute('INSERT OR IGNORE INTO report_remote_contents SELECT project,id,NULL,COALESCE(size,0) FROM files WHERE project=?',(project,))
            # A known uploaded main document is not new merely because its association failed earlier.
            for doc in ledger.all('SELECT id,size,metadata FROM documents WHERE project=?',(project,)):
                if json.loads(doc['metadata']).get('hasContent') in (True,'true'):
                    ledger.db.execute('INSERT OR IGNORE INTO report_remote_contents VALUES(?,?,NULL,?)',
                                      (project,doc['id'],doc['size'] or 0))
            totals = ledger.one('SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM files WHERE project=? AND active=1',(project,))
            ledger.db.execute('INSERT OR IGNORE INTO report_owner_totals VALUES(?,?,?,?)',(project,totals['n'],totals['bytes'],at.isoformat()))
            ledger.db.execute('INSERT INTO report_remote_seed VALUES(?)',(project,))
        # Existing IDs form the upgrade baseline. Keep the last confirmed size;
        # a newly discovered ID is only registered after a complete inventory.
        ledger.db.execute('''INSERT OR IGNORE INTO report_remote_versions
          SELECT f.project,f.id,f.fingerprint,r.size,NULL FROM files f
          JOIN report_remote_contents r ON r.project=f.project AND r.id=f.id''')


def local_presence(ledger, at, *, checks=None, persist=True):
    """Current effective content versions only; history stays in file_history.

    A done row binds its download hash to the current content fingerprint. The
    downloader resets completion on a fingerprint change. We check presence and
    size here, without rehashing every downloaded file on every report.
    """
    initialized = bool(ledger.one("SELECT 1 FROM report_settings WHERE key='local_seed'"))
    previous_days = {r['path']: r['first_day'] for r in ledger.all('SELECT path,first_day FROM report_local_paths')}
    known_contents = {(r['project'], r['id']) for r in ledger.all('SELECT project,id FROM report_local_versions')}
    legacy_days = {}
    if not initialized:
        # Last successful timestamp alone cannot distinguish an old file re-download.
        for audit in sorted((ledger.base_dir/'logs').glob('*/downloads.csv')):
            with audit.open(encoding='utf-8-sig',newline='') as stream:
                for row in csv.DictReader(stream):
                    if row.get('结果')!='done' or not row.get('本地路径') or not row.get('北京时间'):
                        continue
                    key = relative_path(row['本地路径'],ledger.base_dir).casefold()
                    date = datetime.fromisoformat(row['北京时间']).astimezone(LOCAL).date().isoformat()
                    legacy_days[key] = min(legacy_days.get(key,date),date)
    present = {}
    records = ledger.all("""SELECT project,id,fingerprint,path,size,bytes,sha256,downloaded_at,download_date_source
      FROM files WHERE active=1 AND status='done'""")
    updates = []
    for record in records:
        if not record['path'] or not record['sha256'] or record['bytes'] != record['size']:
            continue
        rel = relative_path(record['path'],ledger.base_dir)
        if not rel.lower().startswith('downloads/'):
            continue
        path = resolve_path(rel,ledger.base_dir)
        try:
            status = path.stat()
            valid = stat.S_ISREG(status.st_mode) and status.st_size == record['bytes']
        except FileNotFoundError:
            valid = False
        if checks is not None:
            checks[(record['project'], record['id'], record['path'], record['sha256'], record['bytes'])] = valid
        if not valid:
            continue
        # One count per workspace/content ID/current version, regardless of
        # how many directories reference it or where it was stored before.
        version = json.dumps([record['fingerprint'], record['sha256']], separators=(',', ':'))
        key = (record['project'], record['id'], version)
        present[key] = record['bytes']
        path_key = rel.casefold()
        first_day = legacy_days.get(path_key)
        if (record['project'], record['id']) not in known_contents and path_key in previous_days:
            first_day = previous_days[path_key]
        elif initialized and record['downloaded_at'] and record['download_date_source']=='下载完成实录':
            first_day = datetime.fromisoformat(record['downloaded_at']).astimezone(LOCAL).date().isoformat()
        updates.append((*key,first_day,record['bytes'],rel))
    if persist:
        with ledger.db:
            ledger.db.executemany('''INSERT INTO report_local_versions VALUES(?,?,?,?,?,?)
              ON CONFLICT(project,id,version) DO UPDATE SET size=excluded.size,path=excluded.path''', updates)
            ledger.db.execute("INSERT OR IGNORE INTO report_settings VALUES('local_seed',?)",(at.isoformat(),))
    return present


def establish_report_baseline(ledger, at=None, *, include_local=True):
    at = at or now()
    init_report_tables(ledger)
    seed_remote(ledger,at)
    return local_presence(ledger,at) if include_local else {}


def record_owner_inventory(ledger, project, snapshot, at=None):
    """Called only after a complete catalogue AND association round, never after partial scans."""
    at = at or now()
    init_report_tables(ledger)
    seeded = ledger.one('SELECT 1 FROM report_remote_seed WHERE project=?',(project,))
    day = at.date().isoformat()
    known = {r['id']:dict(r) for r in ledger.all(
        'SELECT * FROM report_remote_contents WHERE project=?',(project,))}
    versions = {r['id']:dict(r) for r in ledger.all(
        'SELECT * FROM report_remote_versions WHERE project=?',(project,))}
    rows = [dict(r) for r in ledger.all('SELECT * FROM files WHERE project=?',(project,))]
    predecessors = defaultdict(list)
    if seeded and any(r['active'] and r['id'] not in known for r in rows):
        from file_layout import replacement_links
        mapping = {(r['project'],r['id']):{'path':r['target_path'] or r['path'] or ''}
                   for r in rows if r['active']}
        for (_, identifier), successors in replacement_links(ledger,rows,mapping,conservative=False).items():
            if identifier in known:
                for successor in successors:
                    predecessors[successor['id']].append(known[identifier])
    contents, current_versions = [], []
    for row in rows:
        if not row['active']:
            continue
        identifier, size = row['id'], row['size'] or 0
        previous, version = known.get(identifier), versions.get(identifier)
        first_day = previous['first_day'] if previous else day if seeded else None
        updated_day = version['updated_day'] if version else None
        if previous:
            if (version and version['fingerprint'] is not None and
                    version['fingerprint'] != row['fingerprint']) or previous['size'] != size:
                updated_day = day
        elif predecessors[identifier]:
            # A changed content ID may still be a new version of a known file.
            # Preserve today's additions as additions, even after another revision.
            days = [old['first_day'] for old in predecessors[identifier]]
            first_day = day if all(value == day for value in days) else None
            updated_day = day
        contents.append((project,identifier,first_day,size))
        current_versions.append((project,identifier,row['fingerprint'],size,updated_day))
    with ledger.db:
        ledger.db.executemany('INSERT OR REPLACE INTO report_remote_contents VALUES(?,?,?,?)',contents)
        ledger.db.executemany('INSERT OR REPLACE INTO report_remote_versions VALUES(?,?,?,?,?)',current_versions)
        ledger.db.execute('INSERT OR IGNORE INTO report_remote_seed VALUES(?)',(project,))
        totals = ledger.one('SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM files WHERE project=? AND active=1',(project,))
        ledger.db.execute('INSERT OR REPLACE INTO report_owner_totals VALUES(?,?,?,?)',
                          (project,totals['n'],totals['bytes'],at.isoformat()))
        ledger.db.execute('INSERT OR REPLACE INTO report_owner_checks VALUES(?,?,?)',(project,at.date().isoformat(),snapshot))


def download_directory_stats(ledger, present, local_dates, date, *, physical_files=None, scanned_files=None):
    """Count physical files in downloads, including predecessors awaiting replacement.

    Latest completion is still based on the current verified ledger version.
    Unknown or incomplete bytes count as other files, never as latest downloads
    or today's successful additions. History and runtime are outside this tree.
    """
    files = scanned_files if scanned_files is not None else scan_files(ledger.base_dir,'downloads')
    latest, retained, additions = set(), set(), set()
    for row in ledger.all("SELECT project,id,fingerprint,sha256,path,bytes FROM files WHERE active=1 AND status='done'"):
        key = (row['project'], row['id'], json.dumps([row['fingerprint'],row['sha256']],separators=(',', ':')))
        if key not in present or not row['path']:
            continue
        path = relative_key(row['path'])
        if files.get(path) == row['bytes']:
            latest.add(path)
            if local_dates.get(key) == date:
                additions.add(path)
    old = ledger.all('SELECT project,file_id AS id,fingerprint,sha256,path,bytes FROM file_history')
    old += ledger.all("SELECT project,id,fingerprint,sha256,path,bytes FROM files WHERE active=0 AND status='done'")
    for record in ledger.all('SELECT project,id,version,path,size FROM report_local_versions'):
        try:
            fingerprint, digest = json.loads(record['version'])
        except (ValueError, TypeError):
            continue
        old.append(dict(project=record['project'],id=record['id'],fingerprint=fingerprint,
                        sha256=digest,path=record['path'],bytes=record['size']))
    for row in old:
        if not row['path'] or not row['sha256']:
            continue
        path = relative_key(row['path'])
        if files.get(path) != row['bytes']:
            continue
        retained.add(path)
        key = (row['project'],row['id'],json.dumps([row['fingerprint'],row['sha256']],separators=(',', ':')))
        if local_dates.get(key) == date:
            additions.add(path)
    retained -= latest
    def totals(paths):
        return dict(files=len(paths), bytes=sum(files[path] for path in paths))
    if physical_files is not None:
        physical_files.update(files)
    return dict(total=totals(files), latest=totals(latest), retained=totals(retained),
                other=totals(set(files)-latest-retained), new=totals(additions))


def effective_download_stats(ledger, local_dates, date, physical_files):
    """At most one downloaded representative for each current content ID.

    A verified latest version in downloads wins. Otherwise a
    registered predecessor still in downloads can stand in for it. Directory
    extras and versions with no current successor remain diagnostic data only.
    """
    from file_layout import replacement_links
    rows = [dict(row) for row in ledger.all('''SELECT project,id,document_id,folder_id,
      filename,relation,active,status,path,target_path,size,bytes,sha256,fingerprint,
      downloaded_at,download_date_source,updated FROM files''')]
    current = {(row['project'],row['id']): row for row in rows if row['active']}
    paths = {}
    def path_key(value):
        if not value:
            return None
        if value not in paths:
            text = str(value).replace('\\', '/')
            # This fast path only matches an already confined physical inventory;
            # it never opens a path. Legacy absolute paths retain full validation.
            if text.startswith(('downloads/', 'runtime/download_ready/')) and ':' not in text and all(
                    part not in ('', '.', '..') for part in text.split('/')):
                paths[value] = text.casefold()
            else:
                paths[value] = relative_path(value,ledger.base_dir).casefold()
        return paths[value]

    def first_day(row):
        key = (row['project'],row.get('id',row.get('file_id')),
               json.dumps([row.get('fingerprint'),row['sha256']],separators=(',', ':')))
        if key in local_dates:
            return local_dates[key]
        if str(row.get('download_date_source') or '').startswith('下载完成实录') and row.get('downloaded_at'):
            return datetime.fromisoformat(row['downloaded_at']).astimezone(LOCAL).date().isoformat()
        return None

    selected, used = {}, set()
    for key, row in current.items():
        if row['status'] != 'done' or not row['sha256'] or row['bytes'] != row['size']:
            continue
        path = path_key(row['path'])
        if not path:
            continue
        valid = physical_files.get(path) == row['bytes']
        if not path.startswith('downloads/'):
            continue
        if valid and path not in used:
            selected[key] = (path, row['bytes'], 'latest', first_day(row))
            used.add(path)

    mapping = {key: {'path': row['target_path'] or row['path'] or ''} for key,row in current.items()}
    links = replacement_links(ledger, rows, mapping, conservative=False)
    successors = {}
    for row in rows:
        key = (row['project'],row['id'])
        if key in current:
            successors[key] = [key]
            continue
        successors[key] = [(new['project'],new['id']) for new in links.get(key, ())]
    history = [dict(row) for row in ledger.all('''SELECT project,file_id AS id,fingerprint,
      path,bytes,sha256,archived,id AS history_id FROM file_history''')]
    for row in ledger.all('SELECT project,id,version,path,size FROM report_local_versions'):
        try:
            signature, digest = json.loads(row['version'])
        except (ValueError, TypeError):
            continue
        history.append(dict(project=row['project'],id=row['id'],fingerprint=signature,
                            path=row['path'],bytes=row['size'],sha256=digest,archived=''))
    history.extend(row for row in rows if not row['active'] and row['status'] == 'done')
    history.sort(key=lambda row: (str(row.get('archived') or row.get('updated')
        or row.get('downloaded_at') or ''), row.get('history_id',0)), reverse=True)
    for row in history:
        path = path_key(row['path'])
        if not path or path in used or not row['sha256'] or physical_files.get(path) != row['bytes']:
            continue
        for key in successors.get((row['project'],row['id']), ()):
            if key not in selected:
                selected[key] = (path, row['bytes'], 'retained', first_day(row))
                used.add(path)
                break  # One physical predecessor is never counted twice.
    def totals(values):
        values = list(values)
        return dict(files=len(values),bytes=sum(row[1] for row in values))
    return dict(total=totals(selected.values()),
                latest=totals(row for row in selected.values() if row[2]=='latest'),
                retained=totals(row for row in selected.values() if row[2]=='retained'),
                new=totals(row for row in selected.values() if row[3]==date),
                current_scope_files=len(current),
                excluded_directory_files=len(set(physical_files)-used),
                basis='当前有效内容ID去重：最新版优先，未下载则以目录内可对应的已登记旧版占位')


def system_report_stats(ledger, at=None, *, persist=True):
    at = at or now()
    present = establish_report_baseline(ledger,at) if persist else {}
    date = at.date().isoformat()
    owner = ledger.one('SELECT COALESCE(SUM(files),0) files,COALESCE(SUM(bytes),0) bytes FROM report_owner_totals')
    owner_new = ledger.one('''SELECT COUNT(*) files,COALESCE(SUM(r.size),0) bytes
      FROM report_remote_contents r JOIN files f ON f.project=r.project AND f.id=r.id
      WHERE r.first_day=? AND f.active=1''',(date,))
    owner_updated = None
    if ledger.one("SELECT 1 FROM sqlite_master WHERE type='table' AND name='report_remote_versions'"):
        owner_updated = dict(ledger.one('''SELECT COUNT(*) files,COALESCE(SUM(v.size),0) bytes
          FROM report_remote_versions v JOIN report_remote_contents r ON r.project=v.project AND r.id=v.id
          JOIN files f ON f.project=v.project AND f.id=v.id
          WHERE v.updated_day=? AND (r.first_day IS NULL OR r.first_day!=?) AND f.active=1''',(date,date)))
    missing = ledger.one('''SELECT COUNT(*) n FROM report_owner_totals t WHERE NOT EXISTS(
      SELECT 1 FROM report_owner_checks c WHERE c.project=t.project AND c.day=?)''',(date,))['n']
    observed = {row['project']:(row['files'],row['bytes']) for row in ledger.all('''SELECT project,
        COUNT(*) files,COALESCE(SUM(size),0) bytes FROM files WHERE active=1 GROUP BY project''')}
    confirmed = {row['project']:(row['files'],row['bytes']) for row in ledger.all('SELECT project,files,bytes FROM report_owner_totals')}
    scope_matches = all(observed.get(key,(0,0))==value for key,value in confirmed.items()) and not (observed.keys()-confirmed.keys())
    verified = bool(confirmed and not missing and scope_matches)
    local_dates = {(r['project'],r['id'],r['version']):r['first_day']
                   for r in ledger.all('SELECT project,id,version,first_day FROM report_local_versions')}
    inventory = collect_inventory(ledger.base_dir,ledger.db)
    physical = inventory['paths']['downloads']
    if not persist:
        for row in ledger.all("SELECT project,id,fingerprint,sha256,path,size,bytes FROM files WHERE active=1 AND status='done'"):
            if row['sha256'] and row['bytes']==row['size'] and physical.get(relative_key(row['path']))==row['bytes']:
                version=json.dumps([row['fingerprint'],row['sha256']],separators=(',',':'))
                present[(row['project'],row['id'],version)]=row['bytes']
    new_local = [size for key,size in present.items() if local_dates.get(key)==date]
    directory = download_directory_stats(ledger,present,local_dates,date,scanned_files=physical)
    effective = effective_download_stats(ledger,local_dates,date,physical)
    pending_publication = ledger.one("""SELECT COUNT(*) n FROM files WHERE active=1 AND status='done'
      AND replace(path,char(92),'/') LIKE 'runtime/download_ready/%'""")['n']
    reconciled = (effective['total']==directory['total'] and not pending_publication
                  and not (ledger.base_dir/'runtime/layout.pending').exists())
    activity = download_activity(ledger,at)
    effective.update(new=activity['new'],updated=activity['updated'])
    return {'owner':dict(owner),'owner_new':dict(owner_new) if verified else None,
            'owner_updated':owner_updated if verified else None,
            'owner_verified_today':verified,'owner_scope_matches':scope_matches,
            'local':{'files':len(present),'bytes':sum(present.values())},
            'local_new':{'files':len(new_local),'bytes':sum(new_local)},
            'directory_reconciled':reconciled,'pending_publication_files':pending_publication,
            'file_inventory':inventory_summary(inventory),
            'download_directory':directory,'effective_downloads':effective,'download_activity':activity}


PROFESSIONS = {
    'PRO':'工艺', 'CIV':'土建', 'PIP':'管道', 'ELC':'电气', 'INS':'仪表',
    'QAE':'质量工程', 'QAC':'质量控制', 'STC':'静设备', 'ROT':'动设备',
    'HSQ':'健康安全环保与质量', 'CON':'施工工程', 'HVC':'暖通', 'MET':'冶金',
    'TEL':'通信', 'STR':'结构', 'MEC':'机械', 'PTG':'涂装', 'BPE':'喷砂涂装',
    'COM':'调试', 'PPL':'长输管道', 'PNM':'采购与材料', 'PRM':'项目管理',
    'PEN':'项目工程', 'PCL':'项目控制', 'CTS':'合同管理', 'SAP':'SAP资料', 'VDN':'VDN专业',
}
REGION = re.compile(r'LT_HE-(GG|H\d)-\d{2,3}(?=[_-])', re.I)
FORMATS = ('PDF', 'DWG', '表格', '压缩包', '其他格式')


def profession(raw):
    if not isinstance(raw, str) or not raw.strip():
        return None
    code = raw.split(' - ', 1)[0].strip().upper()
    return PROFESSIONS.get(code, code if re.fullmatch(r'[A-Z0-9]{2,8}', code) else None)


def file_format(filename):
    ext = Path(filename).suffix.lower()
    if ext in ('.pdf', '.dwg'):
        return ext[1:].upper()
    if ext in ('.xlsx', '.xls', '.xlsm', '.xlsb', '.csv', '.ods'):
        return '表格'
    if ext in ('.zip', '.7z', '.rar', '.tar', '.gz', '.bz2', '.xz'):
        return '压缩包'
    return '其他格式'


def classify_downloads(ledger, successful, names):
    groups = {dimension:defaultdict(lambda:[0,0]) for dimension in ('profession','region','format')}
    if not successful:
        return groups
    filenames = {(r['project'],r['id']):r['filename'] for r in ledger.all('SELECT project,id,filename FROM files')}
    parents = defaultdict(set)
    for row in ledger.all('''SELECT x.project,x.file_id,d.metadata FROM document_files x JOIN documents d
      ON d.project=x.project AND d.id=x.document_id AND d.folder_id=x.folder_id
      WHERE x.active=1 AND d.active=1'''):
        key = row['project'], row['file_id']
        if key not in successful:
            continue
        label = profession(json.loads(row['metadata']).get('discipline'))
        if label:
            parents[key].add(label)
    for key, size in successful.items():
        filename = names.get(key) or filenames.get(key, '')
        disciplines = parents[key]
        discipline = next(iter(disciplines)) if len(disciplines)==1 else '跨专业共享' if disciplines else '未标注专业'
        regions = {r.upper() for r in REGION.findall(filename)}
        region = next(iter(regions)) if len(regions)==1 else '跨区域资料' if regions else '未识别区域'
        for dimension, label in (('profession',discipline),('region',region),('format',file_format(filename))):
            groups[dimension][label][0] += 1
            groups[dimension][label][1] += size
    # All dimensions partition the same content IDs; shared references never inflate totals.
    for group in groups.values():
        assert sum(v[0] for v in group.values()) == len(successful)
        assert sum(v[1] for v in group.values()) == sum(successful.values())
    return groups


def ranked(group, special):
    return sorted(((k,v) for k,v in group.items() if k not in special),
                  key=lambda pair:(-pair[1][0],-pair[1][1],pair[0]))


def entry(label, values):
    return f'{label} {values[0]:,}个（{values[1]/1e9:.2f} GB）'


def top_line(group, special, other):
    ordered = ranked(group, special)
    parts = [entry(k,v) for k,v in ordered[:3]]
    if len(ordered)>3:
        rest = ordered[3:]
        parts.append(entry(other,[sum(v[0] for _,v in rest),sum(v[1] for _,v in rest)]))
    parts.extend(entry(k,group[k]) for k in special if k in group)
    return '、'.join(parts)


def classification_lines(groups):
    if not groups['format']:
        return ['', '二、今日下载构成', '今日暂无成功下载。', '', '三、重点进展', '暂无当日下载进展。']
    prof_special = ('跨专业共享','未标注专业')
    area_special = ('跨区域资料','未识别区域')
    profs = ranked(groups['profession'],prof_special)
    areas = ranked(groups['region'],area_special)
    return ['', '二、今日下载构成',
            '按专业：'+top_line(groups['profession'],prof_special,'其他专业'),
            '按区域：'+top_line(groups['region'],area_special,'其他区域'),
            '按格式：'+'、'.join(entry(k,groups['format'][k]) for k in FORMATS if k in groups['format']),
            '', '三、重点进展',
            '今日主要专业：'+(entry(*profs[0]) if profs else '已下载资料暂无可识别专业'),
            '今日主要区域：'+(entry(*areas[0]) if areas else '已下载资料暂无可识别区域')]
