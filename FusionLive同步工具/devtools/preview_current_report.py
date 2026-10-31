"""Read-only database preview; never checks a remote filesystem or sends reports."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import sqlite3


def preview(database, output):
    database, output = Path(database).resolve(), Path(output).resolve()
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest()
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute('BEGIN')
        run = db.execute('SELECT ended FROM runs WHERE ended IS NOT NULL ORDER BY id DESC LIMIT 1').fetchone()
        day = datetime.fromisoformat(run['ended']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
        owner = dict(db.execute('SELECT SUM(files) files,SUM(bytes) bytes FROM report_owner_totals').fetchone())
        projects = db.execute('SELECT project FROM report_owner_totals').fetchall()
        verified = bool(projects) and all(db.execute(
            'SELECT 1 FROM report_owner_checks WHERE project=? AND day=?', (r['project'], day)).fetchone()
            for r in projects)
        owner_new = dict(db.execute('SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes FROM report_remote_contents WHERE first_day=?', (day,)).fetchone()) if verified else None
        dates = {r['path']: r['first_day'] for r in db.execute('SELECT path,first_day FROM report_local_paths')}
        local = dict(files=0, bytes=0)
        local_new = dict(files=0, bytes=0)
        unknown_dates = 0
        for row in db.execute("SELECT * FROM files WHERE active=1 AND status='done'"):
            if not row['path'] or not row['sha256'] or row['bytes'] != row['size']:
                continue
            path = row['path'].replace('\\', '/').casefold()
            if not path.startswith('downloads/'):
                raise ValueError('预览仅支持台账中的downloads相对路径')
            local['files'] += 1
            local['bytes'] += row['bytes']
            if path in dates:
                first_day = dates[path]
            elif row['downloaded_at'] and row['download_date_source'] == '下载完成实录':
                first_day = datetime.fromisoformat(row['downloaded_at']).astimezone(timezone(timedelta(hours=8))).date().isoformat()
            else:
                first_day = None
            unknown_dates += first_day is None
            if first_day == day:
                local_new['files'] += 1
                local_new['bytes'] += row['bytes']
        inactive = dict(db.execute("SELECT COUNT(*) files,COALESCE(SUM(bytes),0) bytes FROM files WHERE active=0 AND status='done'").fetchone())
        history = dict(db.execute('SELECT COUNT(*) files,COALESCE(SUM(bytes),0) bytes FROM file_history').fetchone())
        remaining = dict(db.execute("SELECT COUNT(*) files,COALESCE(SUM(size),0) bytes FROM files WHERE active=1 AND status!='done'").fetchone())
        observed = [r['observed_at'] for r in db.execute('SELECT observed_at FROM report_owner_totals')]
        db.rollback()
    assert hashlib.sha256(database.read_bytes()).hexdigest() == original_hash

    def total(label, values):
        return f'{label}{"：" if label=="业主系统" else "   "}{values["files"]:,}个文件，{values["bytes"]/1e9:.2f} GB，{values["bytes"]:,}字节'

    def new(label, values):
        prefix = label + ('：' if label == '业主系统' else '   ')
        if values is None:
            return prefix + '新增文件—份、— MB、—字节'
        return f'{prefix}新增文件{values["files"]:,}份、{values["bytes"]/1e6:.2f} MB、{values["bytes"]:,}字节'

    lines = ['【阿联酋项目资料下载日报】', datetime.fromisoformat(day).strftime('%Y年%m月%d日'), '',
             '一、总体进展', total('业主系统', owner), total('NCC系统', local), '',
             '二、今日更新情况', new('业主系统', owner_new), new('NCC系统', local_new), '',
             '预览说明：依据复制的服务器数据库中当前有效版本的下载完成记录计算，未在本机复查服务器实体文件；仅供查看，未发送。']
    output.write_text('\n'.join(lines)+'\n', encoding='utf-8-sig')
    notes = [
        '微信推送文统计核对说明', f'数据日期：{day}', f'数据库：{database}',
        f'业主盘点时刻：{"；".join(observed)}', f'最新任务结束：{run["ended"]}', '',
        f'当前有效内容：{owner["files"]:,}个，{owner["bytes"]:,}字节。',
        f'其中当前版本下载完成：{local["files"]:,}个，{local["bytes"]:,}字节。',
        f'当前有效但尚未完成：{remaining["files"]:,}个，{remaining["bytes"]:,}字节。',
        f'文件数完成比例：{local["files"]/owner["files"]:.2%}；字节数完成比例：{local["bytes"]/owner["bytes"]:.2%}。', '',
        f'不计入新推送文的历史版本：{history["files"]:,}条，{history["bytes"]:,}字节。',
        f'不计入新推送文的已失效已下载内容：{inactive["files"]:,}条，{inactive["bytes"]:,}字节。',
        f'旧口径记录合计：{local["files"]+history["files"]+inactive["files"]:,}个，{local["bytes"]+history["bytes"]+inactive["bytes"]:,}字节，与根目录原推送文相符。', '',
        f'当前版本首次保存日期未知：{unknown_dates}个，仅计总量，不冒充当日新增。',
        '本次仅以只读方式查询数据库，未修改原数据库、历史记录或原推送文。',
        '预览不执行实体文件存在/大小检查；服务器正式推送仍会检查。',
        f'原数据库SHA256（生成前后相同）：{original_hash}',
    ]
    output.with_name('微信推送文-统计核对说明.txt').write_text('\n'.join(notes)+'\n', encoding='utf-8-sig')
    print('\n'.join(lines))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    preview(args.database, args.output)
