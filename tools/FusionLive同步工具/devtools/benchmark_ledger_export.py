"""Compare workbook rendering on synthetic data without touching live ledgers."""
import argparse
import csv
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
import ledger_workbook


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path)
    parser.add_argument('--rows',type=int,default=5000)
    args=parser.parse_args()
    jobs=[]
    if args.baseline:
        spec=importlib.util.spec_from_file_location('ledger_baseline',args.baseline)
        baseline=importlib.util.module_from_spec(spec);spec.loader.exec_module(baseline)
        jobs.append(('before_both_trees',baseline.export_workbook,{}))
    jobs.extend([('optimized_both_trees',ledger_workbook.export_workbook,{'include_local_tree':True}),
                 ('optimized_default_directory_tree',ledger_workbook.export_workbook,{})])
    with tempfile.TemporaryDirectory() as directory:
        source=Path(directory)/'input.csv'
        fields=['工作区','目录','文件名','记录类型','内容ID','业务类型','业务专业',
                '下载状态','文件大小','本地路径','文档编号','文档名称']
        with source.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream);writer.writerow(fields)
            for i in range(args.rows):
                writer.writerow(['benchmark',f'工程文件/管道/区域{i%100:03}',f'ISO-{i:06}.pdf',
                    '平台文件',str(i),'管道单线图','管道','done',1048576,
                    f'D:/downloads/管道/区域{i%100:03}/ISO-{i:06}.pdf',f'ISO-{i:06}','管道施工图'])
        results=[]
        for name,export,options in jobs:
            ledger_workbook.text_width.cache_clear();ledger_workbook.path_parts.cache_clear()
            started=time.perf_counter()
            target=export(source,Path(directory)/(name+'.xlsx'),**options)
            results.append(dict(case=name,rows=args.rows,seconds=round(time.perf_counter()-started,3),bytes=target.stat().st_size))
        print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':
    main()
