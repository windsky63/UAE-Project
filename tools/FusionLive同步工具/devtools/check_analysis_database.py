"""Read-only audit of downloaded file versions and the analysis queue; no JSON results."""
import argparse
from collections import Counter
from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import sys

HOME = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOME/'app'))
from business_type import classify_pdf


def inspect(ledger, queue, output):
    ledger, queue = Path(ledger).resolve(), Path(queue).resolve()
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (ledger, queue)}
    with closing(sqlite3.connect(ledger.as_uri()+'?mode=ro', uri=True)) as db, \
         closing(sqlite3.connect(queue.as_uri()+'?mode=ro', uri=True)) as jobs:
        db.row_factory = jobs.row_factory = sqlite3.Row
        checks = [db.execute('PRAGMA quick_check').fetchone()[0], jobs.execute('PRAGMA quick_check').fetchone()[0]]
        files = {(r['project'], r['id']): dict(r) for r in db.execute('SELECT * FROM files WHERE active=1')}
        tasks = [dict(r) for r in jobs.execute('SELECT * FROM jobs WHERE active=1')]
        extensions = Counter((Path(r['filename']).suffix.lower(), r['status']) for r in files.values())
        pdfs = Counter(classify_pdf(r['filename'])[0] for r in files.values()
                       if r['status'] == 'done' and Path(r['filename']).suffix.lower() == '.pdf')
        mismatches = [r for r in tasks if (r['project'], r['file_id']) not in files or
            files[r['project'], r['file_id']]['status'] != 'done' or
            files[r['project'], r['file_id']].get('sha256') != r['expected_sha']]
        tables = [r[0] for r in jobs.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        lines = ['# 数据库统计检查', '', f'主台账：`{ledger}`', f'解析队列：`{queue}`', '',
                 '只读检查数据库，未读取 analysis.json，也未解压原始文件。', '',
                 f'- SQLite quick_check：主台账 {checks[0]}；解析队列 {checks[1]}。',
                 f'- 主台账最后更新：{db.execute("SELECT max(updated) FROM files").fetchone()[0]}。',
                 f'- 当前有效内容 ID：{len(files):,}。',
                 f'- 有效任务与主台账当前完成版本/SHA256不一致：{len(mismatches)}。', '',
                 '| 有效下载状态 | 文件数 | 已保存字节 | 应下载字节 |', '|---|---:|---:|---:|']
        for r in db.execute('SELECT status,count(*) n,sum(bytes) b,sum(size) s FROM files WHERE active=1 GROUP BY status'):
            lines.append(f"| {r['status']} | {r['n']:,} | {r['b']:,} | {r['s']:,} |")
        lines += ['', '## 直接下载文件，不含压缩包内部', '', '| 类型 | 已完成 | 未完成 |', '|---|---:|---:|']
        for suffix in ('.pcf', '.idf', '.sha', '.pdf', '.zip', '.7z'):
            lines.append(f'| {suffix} | {extensions[suffix, "done"]:,} | {sum(n for (e,s),n in extensions.items() if e == suffix and s != "done"):,} |')
        lines += ['', f'已完成 PDF 按其自身文件名的已确认规则分类：{dict(pdfs)}。',
                  '这只是文件名识别结果，不是PDF内容核验；待分类文件不能按0张单线图解释。电气单线图不计入管道单线图；13-61不直接当作单张单线图。', '',
                  '## 解析任务', '', '| 活跃 | 状态 | 数量 |', '|---|---|---:|']
        for r in jobs.execute('SELECT active,state,count(*) n FROM jobs GROUP BY active,state'):
            lines.append(f"| {r['active']} | {r['state']} | {r['n']} |")
        lines += ['', f'现有解析库表：{", ".join(tables)}。',
                  '旧库只有任务表时，done表示该源文件任务结束；SHA/7z在旧解析器中可能以暂不支持结束，不能把done任务数当作文件、管线或焊口数量。',
                  '旧库没有包内文件明细，不能仅凭这两份数据库计算全部包内PCF、IDF、单线图PDF及其管线/焊接量。新版重新分析后写入file_inventory和pipeline_inventory，并提供仅含有效任务的current_*查询视图。', '',
                  '| 失败文件内容ID | 次数 | 原错误 |', '|---|---:|---|']
        for r in tasks:
            if r['state'] == 'failed':
                error = str(r['error']).replace('|', '/').replace('\n', ' ')
                lines.append(f"| {r['file_id']} | {r['attempts']} | {error} |")
        lines += ['', '重名路径、PCF输入兼容、5层嵌套和7z支持已在新版实现，但旧库中的这些错误能否全部恢复，需要服务器重新解压解析后确认；CRC损坏和大小限制仍需按实际文件处理。', '',
                  '## 原数据库校验', '']
        for path, sha in before.items():
            if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
                raise RuntimeError('检查期间数据库发生变化：'+str(path))
            lines.append(f'- {path.name}：SHA256 `{sha}`，检查前后相同。')
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text('\n'.join(lines)+'\n', encoding='utf-8')
    print(str(output))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--queue', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    inspect(args.ledger, args.queue, args.output)
