"""CLI and resident Beijing 12:00-00:00 SMB uploader; no Windows scheduled task required."""
import argparse
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from nas_sync import HOME, VERSION, now, load_settings, connect, candidates, Uploader, Stopped, Conflict, audit


def log(base, message):
    folder=base/'logs'/now().strftime('%Y-%m-%d')
    folder.mkdir(parents=True,exist_ok=True)
    line=now().strftime('[%m-%d %H:%M:%S] ')+message
    print(line,flush=True)
    with (folder/'run.log').open('a',encoding='utf-8') as stream:
        stream.write(line+'\n')


def integrity():
    manifest=json.loads((HOME/'release_manifest.json').read_text(encoding='utf-8'))
    if manifest['release']!=VERSION:
        raise RuntimeError('版本清单不匹配，请完整覆盖上传工具包。')
    required={'nas_sync.py','nas_history.py','main.py','启动每日上传.cmd','安全退出.cmd','检查连接与清单.cmd','单文件上传测试.cmd','检查程序完整性.cmd'}
    if not required.issubset(manifest['files']):
        raise RuntimeError('发布清单缺少必要程序，请完整解压上传工具包。')
    for name,expected in manifest['files'].items():
        if Path(name).name!=name or name=='release_manifest.json':
            raise RuntimeError('发布清单路径无效。')
        data=(HOME/name).read_bytes()
        if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise RuntimeError('程序完整性检查失败：'+name)
    return manifest['release']


@contextmanager
def run_lock(base):
    runtime=base/'runtime';runtime.mkdir(parents=True,exist_ok=True)
    stream=(runtime/'uploader.lock').open('a+b')
    try:
        stream.seek(0);stream.write(b'0');stream.flush();stream.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise RuntimeError('已有上传进程正在运行，请勿重复打开。') from None
        else:
            import fcntl
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield
    finally:
        stream.close()


def in_window(at):
    return at.hour>=12


def next_start(at):
    start=at.replace(hour=12,minute=0,second=0,microsecond=0)
    return start if at<start else start+timedelta(days=1)


def run_upload(settings, mode, exiting, max_files=None, verify_all=False):
    runtime=settings.base/'runtime'
    start=now()
    def check():
        if (settings.ledger.parent/'layout.pending').exists():
            raise Stopped('本地目录整理尚未完成，暂停NAS迁移和上传。')
        if exiting.is_set() or (runtime/'exit.request').exists() or (runtime/'stop.request').exists():
            raise Stopped('收到安全停止请求，保留已确认文件和本任务续传临时文件。')
        if mode!='test-upload' and (not in_window(now()) or now().date()!=start.date()):
            raise Stopped('已到北京时间00:00，保存上传进度，等待下次运行。')
    counts={'uploaded':0,'adopted':0,'migrated':0,'skipped':0,'failed':0,'conflict':0}
    summary={'version':VERSION,'started':start.isoformat(),'ended':None,'status':'running',
             'source':str(settings.source),'target':str(settings.target),'files':counts,'candidate_files':0,'candidate_bytes':0,'confirmed_bytes':0}
    uploader=None
    def progress(v):
        log(settings.base,f'{v["phase"]}：{v["file"]}；当前 {v["current_bytes"]/1e6:.2f}/{v["current_total"]/1e6:.2f} MB；本次已发送 {v["sent_bytes"]/1e9:.3f} GB，均速 {v["average_MBps"]:.2f} MB/s；完成 {counts["uploaded"]+counts["adopted"]+counts["migrated"]+counts["skipped"]}/{summary["candidate_files"]}')
    try:
        check();connect(settings.target)
        items,ignored=candidates(settings,check)
        if mode=='test-upload':
            items=sorted(items,key=lambda x:(x.size,x.relative))[:1]
        summary.update(candidate_files=len(items),candidate_bytes=sum(i.size for i in items),ignored_records=ignored)
        log(settings.base,f'本轮已完成下载候选文件 {len(items)} 个，{summary["candidate_bytes"]:,} 字节；当前文件恢复完整平台目录，历史文件单独放在history目录。')
        uploader=Uploader(settings,check,progress)
        uploader.protected={item.relative.casefold() for item in items}
        settled=set()
        log(settings.base,'阶段一：先迁移全部已知旧路径；完成本阶段后才开始新增上传。')
        for item in items:
            check()
            try:
                result=uploader.migrate(item)
                if result:
                    counts['migrated']+=1
                    summary['confirmed_bytes']+=item.size
                    settled.add(item.relative)
                    audit(settings,item,'migrated',0)
                    log(settings.base,f'migrated：{item.relative}（{item.size:,}字节）')
            except (Conflict,OSError) as exc:
                status='conflict' if isinstance(exc,Conflict) else 'failed'
                counts[status]+=1
                settled.add(item.relative)
                audit(settings,item,status,0,str(exc))
                log(settings.base,str(exc))
            if max_files and counts['migrated']>=max_files:
                break
        log(settings.base,'阶段二：上传新增或变更文件，已迁移文件不再重传。')
        for item in items:
            if max_files and counts['uploaded']+counts['adopted']+counts['migrated']>=max_files:
                break
            if item.relative in settled:
                continue
            check()
            for attempt in range(settings.attempts):
                try:
                    before=uploader.wire
                    result,sent=uploader.one(item,verify_all)
                    counts[result]+=1
                    summary['confirmed_bytes']+=item.size
                    audit(settings,item,result,sent)
                    log(settings.base,f'{result}：{item.relative}（{item.size:,}字节）')
                    break
                except Stopped:
                    audit(settings,item,'stopped',uploader.wire-before)
                    raise
                except Conflict as exc:
                    counts['conflict']+=1
                    audit(settings,item,'conflict',uploader.wire-before,str(exc))
                    log(settings.base,str(exc))
                    break
                except OSError as exc:
                    audit(settings,item,'retry' if attempt+1<settings.attempts else 'failed',uploader.wire-before,str(exc))
                    if attempt+1==settings.attempts:
                        counts['failed']+=1
                        log(settings.base,f'上传失败，保留续传进度：{item.relative}；{exc}')
                    else:
                        for _ in range(10):
                            check();time.sleep(0.2)
            if max_files and counts['uploaded']+counts['adopted']+counts['migrated']>=max_files:
                break
        processed=sum(counts.values())
        summary['status']='complete' if not counts['failed'] and not counts['conflict'] and processed==len(items) else 'pending'
    except Stopped as exc:
        summary['status']='stopped';summary['message']=str(exc)
        log(settings.base,str(exc))
    except Exception as exc:
        summary['status']='failed';summary['error']=str(exc)
        log(settings.base,'本轮上传异常：'+str(exc))
    finally:
        summary['ended']=now().isoformat()
        summary['sent_bytes']=uploader.wire if uploader else 0
        if uploader:uploader.close()
        folder=settings.base/'logs'/start.strftime('%Y-%m-%d');folder.mkdir(parents=True,exist_ok=True)
        name='upload-'+start.strftime('%H%M%S-%f')+'-summary.json'
        (folder/name).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        log(settings.base,f'本轮结束：{summary["status"]}；新上传 {counts["uploaded"]}，相同文件接管 {counts["adopted"]}，旧路径迁移 {counts["migrated"]}，跳过 {counts["skipped"]}，失败 {counts["failed"]}，冲突 {counts["conflict"]}。')
    return 0 if summary['status']=='complete' else 2 if summary['status'] in ('stopped','pending') else 1


def main(argv=None):
    if sys.version_info<(3,10):
        raise RuntimeError('需要Python3.10或更高版本。')
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
        sys.stderr.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='绿联云SMB上传工具；北京时间12:00至00:00')
    parser.add_argument('action',choices=['check','plan','test-upload','once','schedule','stop','integrity','history-plan','history-migrate'])
    parser.add_argument('--config',type=Path,default=HOME/'config.json')
    parser.add_argument('--max-files',type=int)
    parser.add_argument('--verify-all',action='store_true',help='跳过快速缓存，逐个回读并比较本地/远端SHA256')
    args=parser.parse_args(argv)
    if args.max_files is not None and args.max_files<=0:
        parser.error('--max-files必须大于0')
    if args.action=='stop':
        base=args.config.resolve().parent
        (base/'runtime').mkdir(exist_ok=True)
        (base/'runtime/exit.request').write_text(now().isoformat(),encoding='utf-8')
        print('已请求安全退出；请等待当前SMB读写返回并保存进度。')
        return 0
    if args.action=='integrity':
        print('程序完整性检查通过：'+integrity());return 0
    settings=load_settings(args.config)
    if args.action in ('check','plan'):
        connect(settings.target)
        items,ignored=candidates(settings)
        print(f'SMB可访问；已验证本地候选 {len(items)} 个，{sum(x.size for x in items):,} 字节；忽略记录 {ignored} 条。')
        for item in items[:10]:
            print(item.relative)
            if item.legacy_relatives:print('  原NAS路径：'+'；'.join(item.legacy_relatives))
            if item.mapping_error:print('  待核对：'+item.mapping_error)
        print('目标：'+str(settings.target))
        return 0
    if args.action=='schedule':integrity()
    exiting=threading.Event()
    def commands():
        for line in sys.stdin:
            command=line.strip().lower()
            if command in ('q','quit','exit'):exiting.set()
            elif command in ('s','stop'):
                (settings.base/'runtime/stop.request').write_text(now().isoformat(),encoding='utf-8')
    with run_lock(settings.base):
        for name in ('exit.request','stop.request'):(settings.base/'runtime'/name).unlink(missing_ok=True)
        signal.signal(signal.SIGINT,lambda *_:exiting.set())
        signal.signal(signal.SIGTERM,lambda *_:exiting.set())
        threading.Thread(target=commands,daemon=True).start()
        if args.action in ('history-plan','history-migrate'):
            import nas_history
            def history_check():
                if exiting.is_set() or (settings.base/'runtime/exit.request').exists() or (settings.base/'runtime/stop.request').exists():
                    raise Stopped('历史整理已停止，保留迁移日志，可用原清单继续。')
                if (settings.ledger.parent/'layout.pending').exists():
                    raise Stopped('本地目录整理尚未完成，请完成后重新预览。')
            uploader=None
            try:
                history_check()
                connect(settings.target)
                uploader=Uploader(settings,history_check)
                path=settings.base/'runtime/nas_history_plan.json'
                if args.action=='history-plan':
                    items,_=candidates(settings,history_check)
                    result=nas_history.plan(uploader,items)
                    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                else:
                    if not path.is_file():
                        raise RuntimeError('请先运行history-plan生成并检查历史整理清单。')
                    result=nas_history.apply(uploader,json.loads(path.read_text(encoding='utf-8')))
                    path=settings.base/'runtime/nas_history_result.json'
                    path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                states={state:sum(e['status']==state for e in result['entries']) for state in ('ready','moved','recovered','conflict')}
                print(json.dumps(states,ensure_ascii=False)+'；详细清单：'+str(path))
                return 2 if states['conflict'] else 0
            except Stopped as exc:
                print(str(exc));return 2
            except Conflict as exc:
                print('历史整理保留现场：'+str(exc));return 2
            finally:
                if uploader:uploader.close()
        if args.action!='schedule':
            return run_upload(settings,args.action,exiting,args.max_files,args.verify_all)
        log(settings.base,'常驻上传调度启动：每天北京时间12:00至次日00:00；Q回车/Ctrl+C安全退出，S回车停止今天。')
        last_day=announced=None
        while not exiting.is_set():
            if (settings.base/'runtime/exit.request').exists():break
            at=now()
            if in_window(at) and last_day!=at.date().isoformat():
                last_day=at.date().isoformat()
                (settings.base/'runtime/stop.request').unlink(missing_ok=True)
                code=run_upload(settings,'once',exiting,args.max_files,args.verify_all)
                # Ordinary failures are retried within the daily window; manual stops end today.
                if code and not exiting.is_set() and in_window(now()) and not (settings.base/'runtime/stop.request').exists():
                    log(settings.base,'仍有未同步文件，5分钟后重新比较并重试；到00:00或手动停止时结束。')
                    until=time.monotonic()+300
                    while time.monotonic()<until and not exiting.is_set() and in_window(now()):
                        if (settings.base/'runtime/stop.request').exists() or (settings.base/'runtime/exit.request').exists():break
                        exiting.wait(0.5)
                    if (not exiting.is_set() and in_window(now())
                        and not (settings.base/'runtime/stop.request').exists()
                        and not (settings.base/'runtime/exit.request').exists()):
                        last_day=None
                announced=None
            else:
                target=next_start(at).isoformat()
                if announced!=target:
                    log(settings.base,'当日任务已结束或未到上传时段，等待：'+target)
                    announced=target
                exiting.wait(0.5)
        log(settings.base,'上传调度已安全退出。')
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError) as exc:
        print('启动失败：'+str(exc),file=sys.stderr)
        raise SystemExit(1)
