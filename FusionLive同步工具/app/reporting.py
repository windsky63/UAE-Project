"""Local daily audit files. No account credentials or cookies are logged."""

from app_paths import HOME, CODE
import csv
import json
import hashlib
import os
import sys
from datetime import timedelta, datetime
from fusion_client import HOME, now, LOCAL
from paths import resolve_path, relative_path
import re
from pathlib import Path
from collections import defaultdict
from runtime_logging import BoundedLog, EventGate


def day_folder(base=HOME):
    folder = base / 'logs' / now().strftime('%Y-%m-%d')
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def record_download(base, item, status, size=0, path='', error=''):
    target = day_folder(base) / 'downloads.csv'
    new = not target.exists()
    with target.open('a', encoding='utf-8-sig', newline='') as out:
        writer = csv.writer(out)
        if new:
            writer.writerow(['北京时间', '工作区', '内容ID', '文件名', '结果', '字节', '本地路径', '错误'])
        values = [now().isoformat(), item['project'], item['id'], item['filename'], status, size, path, error]
        writer.writerow(["'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for v in values])


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
    try_write_push_text(ledger)


def write_push_text(ledger):
    """Leader-facing figures only; RPA readiness is stored in a separate JSON file."""
    at = now()
    date = at.date().isoformat()
    folder = ledger.base_dir / 'logs' / date
    folder.mkdir(parents=True, exist_ok=True)
    start = at.replace(hour=0, minute=0, second=0, microsecond=0)
    runs = ledger.all('SELECT * FROM runs WHERE started>=? AND started<? ORDER BY id',
                      (start.isoformat(), (start+timedelta(days=1)).isoformat()))
    stats = system_report_stats(ledger,at)
    running = any(r['status']=='running' for r in runs)
    owner, local = stats['owner'], stats['local']
    owner_new, local_new = stats['owner_new'], stats['local_new']
    def total_line(label, values):
        return f'{label}{"：" if label=="业主系统" else "   "}{values["files"]:,}个文件，{values["bytes"]/1e9:.2f} GB，{values["bytes"]:,}字节'
    def new_line(label, values):
        if values is None:
            return f'{label}{"：" if label=="业主系统" else "   "}新增文件—份、— MB、—字节'
        return f'{label}{"：" if label=="业主系统" else "   "}新增文件{values["files"]:,}份、{values["bytes"]/1e6:.2f} MB、{values["bytes"]:,}字节'
    lines = ['【阿联酋项目资料下载日报】',at.strftime('%Y年%m月%d日'),'',
             '一、总体进展',total_line('业主系统',owner),total_line('NCC系统',local),'',
             '二、今日更新情况',new_line('业主系统',owner_new),new_line('NCC系统',local_new)]
    target = folder / '微信推送文.txt'
    # Invalidate the prior readiness marker before replacing the text, even if RPA locks it.
    marker = folder / '推送状态.json'
    state = {'date': date, 'generated_at': at.isoformat(), 'ready': False,
             'text_file': target.name, 'text_sha256': None,
             'owner_inventory_complete_today':stats['owner_verified_today']}
    marker_temp = marker.with_suffix('.json.tmp')
    marker_temp.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(marker_temp, marker)
    temp = target.with_suffix('.txt.tmp')
    temp.write_text('\n'.join(lines)+'\n', encoding='utf-8-sig')
    os.replace(temp, target)
    state.update(ready=bool(runs and not running and stats['owner_verified_today']), text_sha256=hashlib.sha256(target.read_bytes()).hexdigest())
    marker_temp.write_text(json.dumps(state, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    os.replace(marker_temp, marker)
    return target


def try_write_push_text(ledger):
    try:
        return write_push_text(ledger)
    except (OSError, ValueError, KeyError, csv.Error) as exc:
        print('推送文生成失败（不影响下载和注销）：', type(exc).__name__, file=sys.stderr)
        return None


class Tee:
    def __init__(self, stream, log, *, important=False):
        self.stream, self.log = stream, log
        self.gate, self.pending, self.important = EventGate(), '', important

    def output(self, message):
        value = message + '\n'
        self.log.write(value)
        self.stream.write(value)

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
    log = BoundedLog(day_folder() / 'run.log')
    out, err = Tee(stdout, log), Tee(stderr, log, important=True)
    sys.stdout, sys.stderr = out, err
    try:
        print('\n任务启动（北京）：', now().isoformat(), flush=True)
        return main()
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


def local_presence(ledger, at):
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
    with ledger.db:
        for record in records:
            if not record['path'] or not record['sha256'] or record['bytes'] != record['size']:
                continue
            rel = relative_path(record['path'],ledger.base_dir)
            if not rel.lower().startswith('downloads/'):
                continue
            path = resolve_path(rel,ledger.base_dir)
            try:
                if not path.is_file() or path.stat().st_size != record['bytes']:
                    continue
            except FileNotFoundError:
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
            ledger.db.execute('''INSERT INTO report_local_versions VALUES(?,?,?,?,?,?)
              ON CONFLICT(project,id,version) DO UPDATE SET size=excluded.size,path=excluded.path''',
                              (*key,first_day,record['bytes'],rel))
        ledger.db.execute("INSERT OR IGNORE INTO report_settings VALUES('local_seed',?)",(at.isoformat(),))
    return present


def establish_report_baseline(ledger, at=None):
    at = at or now()
    init_report_tables(ledger)
    seed_remote(ledger,at)
    return local_presence(ledger,at)


def record_owner_inventory(ledger, project, snapshot, at=None):
    """Called only after a complete catalogue AND association round, never after partial scans."""
    at = at or now()
    init_report_tables(ledger)
    seeded = ledger.one('SELECT 1 FROM report_remote_seed WHERE project=?',(project,))
    first_day = at.date().isoformat() if seeded else None
    with ledger.db:
        ledger.db.execute('''INSERT INTO report_remote_contents(project,id,first_day,size)
          SELECT project,id,?,COALESCE(size,0) FROM files WHERE project=? AND active=1
          ON CONFLICT(project,id) DO UPDATE SET size=excluded.size''',(first_day,project))
        ledger.db.execute('INSERT OR IGNORE INTO report_remote_seed VALUES(?)',(project,))
        totals = ledger.one('SELECT COUNT(*) n,COALESCE(SUM(size),0) bytes FROM files WHERE project=? AND active=1',(project,))
        ledger.db.execute('INSERT OR REPLACE INTO report_owner_totals VALUES(?,?,?,?)',
                          (project,totals['n'],totals['bytes'],at.isoformat()))
        ledger.db.execute('INSERT OR REPLACE INTO report_owner_checks VALUES(?,?,?)',(project,at.date().isoformat(),snapshot))


def system_report_stats(ledger, at=None):
    at = at or now()
    present = establish_report_baseline(ledger,at)
    date = at.date().isoformat()
    owner = ledger.one('SELECT COALESCE(SUM(files),0) files,COALESCE(SUM(bytes),0) bytes FROM report_owner_totals')
    owner_new = ledger.one('SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes FROM report_remote_contents WHERE first_day=?',(date,))
    missing = ledger.one('''SELECT COUNT(*) n FROM report_owner_totals t WHERE NOT EXISTS(
      SELECT 1 FROM report_owner_checks c WHERE c.project=t.project AND c.day=?)''',(date,))['n']
    verified = bool(ledger.one('SELECT COUNT(*) n FROM report_owner_totals')['n'] and not missing)
    local_dates = {(r['project'],r['id'],r['version']):r['first_day']
                   for r in ledger.all('SELECT project,id,version,first_day FROM report_local_versions')}
    new_local = [size for key,size in present.items() if local_dates[key]==date]
    return {'owner':dict(owner),'owner_new':dict(owner_new) if verified else None,
            'owner_verified_today':verified,
            'local':{'files':len(present),'bytes':sum(present.values())},
            'local_new':{'files':len(new_local),'bytes':sum(new_local)}}


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
