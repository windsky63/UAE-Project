"""CLI and resident Beijing 12:00-00:00 SMB uploader; no Windows scheduled task required."""
import argparse
from contextlib import contextmanager, ExitStack
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time
import uuid
import traceback

_log_warned = False

_managed_pipeline = None
if os.environ.get('PROJECT_MANAGER_HOME'):
    sys.path.insert(0, os.environ['PROJECT_MANAGER_HOME'])
    import pipeline as _managed_pipeline

from nas_sync import HOME, VERSION, now, load_settings, connect, candidates, Uploader, DirectoryMirror, Stopped, Conflict, audit, LEDGER_WORKBOOK, ledger_workbook_candidate, ledger_workbook_due


def log(base, message):
    global _log_warned
    from log_policy import DailyLog, formatted
    line = formatted(message, 'nas')
    print(line, flush=True)
    try:
        DailyLog(base, 'nas', 'run.log', captured=True).write(line+'\n')
    except OSError as exc:
        if not _log_warned:
            print(f'上传日志无法写入，继续在窗口输出：{type(exc).__name__}：{exc}',file=sys.stderr,flush=True)
            _log_warned = True


class UploadEvents:
    """Bound frequent successes/progress; per-file evidence stays in upload audit."""
    LABELS = {'uploaded':'新上传', 'adopted':'相同文件接管', 'migrated':'旧路径迁移',
              'skipped':'跳过', 'archived':'正式目录旧副本归档'}

    def __init__(self, base, clock=time.monotonic, interval=None):
        from log_policy import options
        self.base, self.clock, self.interval = base, clock, options()['progress_seconds'] if interval is None else interval
        self.last_report = self.last_progress = float('-inf')
        self.pending, self.latest = {}, ''

    def record(self, category, item, count=1):
        self.pending[category] = self.pending.get(category, 0) + count
        self.latest = item.relative
        if self.clock() - self.last_report >= self.interval:
            self.flush()

    def progress(self, message):
        at = self.clock()
        if at - self.last_progress >= self.interval:
            log(self.base, message)
            self.last_progress = at

    def flush(self):
        if not self.pending:
            return
        counts = '，'.join(f'{self.LABELS.get(key,key)} {value} 个' for key,value in self.pending.items())
        log(self.base, f'文件处理汇总：{counts}；最近文件：{self.latest}；逐文件明细见上传审计和迁移记录。')
        self.pending.clear()
        self.last_report = self.clock()


@contextmanager
def migration_progress(base, total, interval=None):
    from log_policy import options
    interval = options()['progress_seconds'] if interval is None else interval
    """Report from another thread even when a synchronous SMB call is waiting."""
    stopped = threading.Event()
    lock = threading.Lock()
    started = time.monotonic()
    state = {'completed': 0, 'file': '准备扫描', 'since': started}

    def update(completed, item):
        with lock:
            state.update(completed=completed, file=item.relative, since=time.monotonic())

    def report():
        while not stopped.wait(interval):
            with lock:
                snapshot = dict(state)
            at = time.monotonic()
            log(base, f'旧路径扫描：已检查 {snapshot["completed"]}/{total}；'
                f'累计 {at-started:.0f} 秒；当前文件已耗时 {at-snapshot["since"]:.0f} 秒：{snapshot["file"]}')

    worker = threading.Thread(target=report, name='nas-migration-progress', daemon=True)
    worker.start()
    try:
        yield update
    finally:
        stopped.set()
        worker.join(timeout=1)


def integrity():
    manifest=json.loads((HOME/'release_manifest.json').read_text(encoding='utf-8'))
    if manifest['release']!=VERSION:
        raise RuntimeError('版本清单不匹配，请完整覆盖上传工具包。')
    required={'nas_sync.py','nas_history.py','nas_remote_hash.py','setup_nas_ssh.py','配置NAS本机校验.cmd','main.py','启动每日上传.cmd','安全退出.cmd','检查连接与清单.cmd','单文件上传测试.cmd','检查程序完整性.cmd'}
    if not required.issubset(manifest['files']):
        raise RuntimeError('发布清单缺少必要程序，请完整解压上传工具包。')
    for name,expected in manifest['files'].items():
        if Path(name).name!=name or name=='release_manifest.json':
            raise RuntimeError('发布清单路径无效。')
        data=(HOME/name).read_bytes()
        if len(data)!=expected['bytes'] or hashlib.sha256(data).hexdigest()!=expected['sha256']:
            raise RuntimeError('程序完整性检查失败：'+name)
    return manifest['release']


class UploadBusy(RuntimeError):
    pass


@contextmanager
def run_lock(base, name='uploader.lock'):
    runtime=base/'runtime';runtime.mkdir(parents=True,exist_ok=True)
    stream=(runtime/name).open('a+b')
    try:
        stream.seek(0,os.SEEK_END)
        if stream.tell()==0:stream.write(b'0');stream.flush()
        stream.seek(0)
        if os.name=='nt':
            import msvcrt
            try:msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:raise UploadBusy('已有上传进程正在运行，请勿重复打开。') from None
        else:
            import fcntl
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:raise UploadBusy('已有上传进程正在运行，请勿重复打开。') from None
        yield
    finally:
        stream.close()


def in_window(at):
    return at.hour>=12


def read_manual_request(settings):
    try:
        value=json.loads((settings.base/'runtime/manual-once.json').read_text(encoding='utf-8'))
        if (not isinstance(value,dict) or not isinstance(value.get('id'),str)
                or len(value['id'])!=32 or any(c not in '0123456789abcdef' for c in value['id'])
                or value.get('state') not in ('pending','running','finished','cancelled')
                or type(value.get('requested_ns')) is not int or value['requested_ns']<=0
                or (value.get('max_files') is not None and (type(value['max_files']) is not int or value['max_files']<=0))
                or type(value.get('verify_all',False)) is not bool):
            return {}
        return value
    except (FileNotFoundError,ValueError):
        return {}


def save_manual_request(settings, value):
    path=settings.base/'runtime/manual-once.json'
    temporary=path.with_name('manual-once-'+uuid.uuid4().hex+'.tmp')
    try:
        temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temporary,path)
    finally:
        temporary.unlink(missing_ok=True)


def execute_manual_round(settings, request, exiting):
    identity=request['id']
    def cancelled():
        current=read_manual_request(settings)
        return (exiting.is_set() or current.get('id')!=identity or current.get('state')=='cancelled'
                or (settings.base/'runtime'/('manual-once-'+identity+'.cancel')).exists())
    class ManualStop:
        def is_set(self):return cancelled()
    old_stop=None
    for name in ('exit.request','stop.request'):
        path=settings.base/'runtime'/name
        try:
            if path.stat().st_mtime_ns>=request['requested_ns']:
                request.update(state='finished',exit_code=2)
                save_manual_request(settings,request)
                return 2
            if name=='stop.request':old_stop=path.read_bytes()
            path.unlink()
        except FileNotFoundError:
            pass
    request.update(state='running')
    save_manual_request(settings,request)
    code=1
    try:
        code=run_upload(settings,'manual-once',ManualStop(),request.get('max_files'),request.get('verify_all',False))
        return code
    finally:
        request.update(state='finished',exit_code=code)
        save_manual_request(settings,request)
        if old_stop is not None:
            (settings.base/'runtime/stop.request').write_bytes(old_stop)


def manual_upload_once(settings, exiting, max_files=None, verify_all=False):
    """One request at a time; a resident scheduler can execute it under its lock."""
    with run_lock(settings.base,'manual-once.lock'):
        request=dict(id=uuid.uuid4().hex,state='pending',requested_ns=time.time_ns(),
                     max_files=max_files,verify_all=verify_all)
        save_manual_request(settings,request)
        log(settings.base,'手动上传一次：本轮允许在12:00—次日00:00以外执行，仍遵守台账更新间隔和安全停止；已有轮次时等待其完成。')
        try:
            while True:
                current=read_manual_request(settings)
                if current.get('id')!=request['id']:
                    raise RuntimeError('手动上传请求记录已变化，未重复执行。')
                if current.get('state')=='finished':
                    return int(current['exit_code'])
                if exiting.is_set():
                    return 2
                with ExitStack() as owned:
                    try:
                        owned.enter_context(run_lock(settings.base))
                    except UploadBusy:
                        pass
                    else:
                        current=read_manual_request(settings)
                        if current.get('id')!=request['id'] or current.get('state')=='cancelled':return 2
                        if current.get('state')=='finished':return int(current['exit_code'])
                        return execute_manual_round(settings,current,exiting)
                exiting.wait(.25)
        finally:
            current=read_manual_request(settings)
            if current.get('id')==request['id'] and current.get('state')!='finished':
                (settings.base/'runtime'/('manual-once-'+request['id']+'.cancel')).write_text('cancelled',encoding='utf-8')
                current['state']='cancelled'
                save_manual_request(settings,current)


def next_start(at):
    start=at.replace(hour=12,minute=0,second=0,microsecond=0)
    return start if at<start else start+timedelta(days=1)


@contextmanager
def upload_heartbeat(base, uploader, completed, total, interval=None):
    from log_policy import options
    interval = options()['progress_seconds'] if interval is None else interval
    """Expose a waiting SMB/SQLite operation without interrupting or retrying it."""
    stopped = threading.Event()
    labels = dict(file='逐文件处理', path_checks='路径检查', nas_hash='NAS回读校验',
                  local_hash='本地哈希', upload='上传写入', state='上传状态写入',
                  migration_finish='迁移记录确认', publish='NAS发布或设置时间')
    labels['backup_copy']='旧版备份复制及哈希'
    def message():
        value = uploader.phase_snapshot()
        if value and value['state']=='running':
            phase_total = value['total']
            done = f'{value["completed"]}/{phase_total}' if phase_total is not None else f'{value["completed"]}项（总数待扫描）'
            size = (f'；已校验 {value["completed_bytes"]:,}/{value["total_bytes"]:,} 字节'
                    if value['total_bytes'] is not None else '')
            current=(f'；本文件已读取 {value["current_bytes"]:,}/{value["current_total"]:,} 字节'
                     if value['current_bytes'] is not None else '；当前文件操作尚未返回结果')
            estimate=''
            checked=value['completed_bytes']+(value['current_bytes'] or 0)
            if value['total_bytes'] and value['elapsed_seconds']>=5 and checked>=64*1048576:
                rate=checked/value['elapsed_seconds']
                estimate=f'；有效核验均速 {rate/1e6:.2f} MB/s；按本阶段均速估计剩余 {max(0,value["total_bytes"]-checked)/rate/60:.1f} 分钟'
            return (f'NAS处理心跳：阶段={value["name"]}；阶段序号={value["number"]}；本阶段完成 {done}{size}'
                    f'；阶段耗时 {value["elapsed_seconds"]:.1f} 秒；距进度变化 {value["idle_seconds"]:.1f} 秒'
                    f'；当前文件：{value["current_file"] or "（尚未选择）"}{current}{estimate}')
        phase, since = uploader.activity
        return (f'NAS处理心跳：{labels.get(phase,phase)}当前操作已耗时 {time.monotonic()-since:.1f} 秒；'
                f'文件处理已完成 {completed()}/{total}；当前文件：{uploader.current_file}')
    def report():
        while not stopped.wait(interval):
            log(base,message())
    worker = threading.Thread(target=report, name='nas-upload-progress', daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)


def candidate_diagnostics(details, ignored):
    """Include exact exclusions in the existing round summary."""
    reasons = {}
    for entry in details:
        reasons[entry['reason']] = reasons.get(entry['reason'],0) + 1
    blocking = sum(bool(entry['blocks_mirror']) for entry in details) + max(0,ignored-len(details))
    return dict(ignored_records=ignored,blocking_records=blocking,
                excluded_records=ignored-blocking,reasons=reasons,records=details)


def run_upload(settings, mode, exiting, max_files=None, verify_all=False):
    runtime=settings.base/'runtime'
    start=now()
    mirror=None
    def check():
        if (settings.ledger.parent/'layout.pending').exists():
            raise Stopped('本地目录整理尚未完成，暂停NAS迁移和上传。')
        if exiting.is_set() or (runtime/'exit.request').exists() or (runtime/'stop.request').exists():
            raise Stopped('收到安全停止请求，保留已确认文件和本任务续传临时文件。')
        if _managed_pipeline is None and mode not in ('test-upload','manual-once') and (not in_window(now()) or now().date()!=start.date()):
            raise Stopped('已到北京时间00:00，保存上传进度，等待下次运行。')
        if mirror:
            mirror.check()
    counts={'uploaded':0,'adopted':0,'migrated':0,'skipped':0,'failed':0,'conflict':0}
    summary={'version':VERSION,'started':start.isoformat(),'ended':None,'status':'running',
             'source':str(settings.source),'target':str(settings.target),'files':counts,'candidate_files':0,'candidate_bytes':0,'confirmed_bytes':0,'archived_legacy_copies':0}
    summary['verification_method']=settings.verification_method
    uploader=None
    resources=ExitStack()
    events=UploadEvents(settings.base)
    def progress(v):
        speed = '当前阶段未发送新内容'
        if v['phase'] in ('开始上传','继续上传','上传','上传完成'):
            value=v['recent_upload_MBps']
            speed=f'近期上传速度 {value:.2f} MB/s' if value is not None else '近期上传速度待采样'
        elif v['phase']=='校验续传部分':
            value=v['resume_read_MBps']
            speed=(f'续传校验读取速度 {value:.2f} MB/s' if value is not None else '续传校验读取速度待采样')+'（读取NAS已有部分，未发送新内容）'
        elif v['phase']=='SSH续传校验':
            speed='NAS本机计算已有临时文件SHA256，等待结果，不通过SMB读回文件内容'
        elif v['phase']=='本地续传校验':
            speed='读取本地对应部分核对SHA256，未发送新内容'
        events.progress(f'{v["phase"]}：{v["file"]}；当前 {v["current_bytes"]/1e6:.2f}/{v["current_total"]/1e6:.2f} MB；{speed}；本次已发送 {v["sent_bytes"]/1e9:.3f} GB，整轮发送均速 {v["average_MBps"]:.2f} MB/s（含扫描与校验）；NAS校验已读取 {v["nas_read_bytes"]/1e9:.3f} GB（其中续传校验 {v["resume_read_bytes"]/1e9:.3f} GB），已结束的路径检查 {v["timings_seconds"]["path_checks"]:.1f} 秒、NAS校验 {v["timings_seconds"]["nas_hash"]:.1f} 秒；完成 {counts["uploaded"]+counts["adopted"]+counts["migrated"]+counts["skipped"]}/{summary["candidate_files"]}')
    try:
        check();connect(settings.target)
        partial = _managed_pipeline is not None and not _managed_pipeline.mirror_ready(settings)
        if partial:
            log(settings.base, '下载尚未完成：本轮上传已有的已校验文件，暂缓NAS全目录镜像整理。')
        if mode!='test-upload' and max_files is None and not partial:
            mirror=DirectoryMirror(settings)
        details=[]
        items,ignored=candidates(settings,check,diagnostics=details, **({'allow_incomplete': True} if partial else {}))
        if mirror:
            due,ready=ledger_workbook_due(settings,now())
            workbook=ledger_workbook_candidate(settings,resources,check) if due else None
            if workbook is not None:
                if any(item.relative.casefold()==LEDGER_WORKBOOK.casefold() for item in items):
                    raise Conflict('根目录文件台账与业务文件路径冲突，未开始整理和上传。')
                items.insert(0,workbook)
                summary['ledger_workbook']=dict(relative=LEDGER_WORKBOOK,status='pending',sha256=workbook.sha256)
                log(settings.base,'文件台账已固定本轮快照，将优先同步至NAS根目录：'+LEDGER_WORKBOOK)
            elif not due:
                summary['ledger_workbook']=dict(relative=LEDGER_WORKBOOK,status='interval_wait',next_update_after=ready.isoformat())
                log(settings.base,f'文件台账距上次成功同步未超过{settings.ledger_update_interval_minutes}分钟，保留NAS现有台账；下次可更新时间：{ready.isoformat()}。')
            else:
                summary['ledger_workbook']=dict(relative=LEDGER_WORKBOOK,status='not_generated')
                log(settings.base,'尚无已发布的Excel文件台账，本轮保留NAS既有台账并继续处理资料。')
        if mode=='test-upload':
            items=sorted(items,key=lambda x:(x.size,x.relative))[:1]
        summary.update(candidate_files=len(items),candidate_bytes=sum(i.size for i in items),ignored_records=ignored)
        blocking=0
        if ignored:
            report=candidate_diagnostics(details,ignored)
            blocking=report['blocking_records']
            summary['candidate_diagnostics']=report
            report_path=settings.base/'logs'/start.strftime('%Y-%m-%d')/('upload-'+start.strftime('%H%M%S-%f')+'-summary.json')
            decision='暂停本轮上传和目录镜像' if blocking else '正常排除，不影响处理'
            log(settings.base,f'候选记录核对：正常排除 {report["excluded_records"]} 条，待核实 {blocking} 条；'
                f'原因统计：{json.dumps(report["reasons"],ensure_ascii=False)}；'
                f'{decision}；完整路径明细见本轮报告：{report_path}')
            for entry in sorted(details,key=lambda v:not v['blocks_mirror'])[:5]:
                log(settings.base,f'候选记录明细：{entry["reason"]}；文件ID={entry["file_id"]}；路径={entry["path"] or "（台账未填写）"}')
        log(settings.base,f'本轮候选文件 {len(items)} 个，{summary["candidate_bytes"]:,} 字节；当前文件恢复完整平台目录，历史文件单独放在history目录，Excel文件台账同步到NAS根目录。')
        if blocking:
            raise Stopped(f'存在 {blocking} 条源范围内记录无法核实，暂停本轮上传和目录镜像；具体原因及路径见本轮报告。')
        uploader=Uploader(settings,check,progress)
        uploader.reserve_hash_proofs(len(items))
        uploader.status=lambda message:log(settings.base,message)
        log(settings.base, '本轮模式：SSH本机校验与目录整理；续传前缀在NAS本机校验，文件上传及已知旧路径迁移使用SMB。' if settings.remote_hash else '本轮模式：完全SMB；目录整理、内容校验、续传及上传均使用SMB，不建立SSH会话。')
        if mirror:
            log(settings.base,'完整目录镜像：以同步台账为准核对全部文件夹和文件名；被替换或多余的内容保留归档，不直接删除。')
            if settings.remote_hash:
                log(settings.base,'SSH目录整理已启用：扫描、批量创建、大小写更名、多余文件归档和空目录清理在NAS本机执行；新增文件上传及已知旧路径迁移仍使用SMB。')
            with upload_heartbeat(settings.base,uploader,lambda:sum(counts[key] for key in ('uploaded','adopted','migrated','skipped')),len(items)):
                mirror.configure(uploader,items,blocking)
                mirror.prepare()
        if settings.remote_hash:
            log(settings.base,'已选择SSH本机SHA256校验；SSH会话按需建立并复用，失败不会回退SMB。')
        uploader.protected={item.relative.casefold() for item in items}
        settled=set()
        log(settings.base,'阶段一：先迁移全部已知旧路径；完成本阶段后才开始新增上传。')
        scan_started=time.monotonic()
        scanned=0
        with migration_progress(settings.base,len(items)) as scanning:
            for item in items:
                check()
                scanning(scanned,item)
                try:
                    result=uploader.migrate(item)
                    if result:
                        archived=uploader.archive_legacy_copies(item)
                        summary['archived_legacy_copies']+=archived
                        if archived:events.record('archived',item,archived)
                        counts['migrated']+=1
                        summary['confirmed_bytes']+=item.size
                        settled.add(item.relative)
                        audit(settings,item,'migrated',0)
                        events.record('migrated',item)
                except (Conflict,OSError) as exc:
                    status='conflict' if isinstance(exc,Conflict) else 'failed'
                    counts[status]+=1
                    settled.add(item.relative)
                    audit(settings,item,status,0,str(exc))
                    label='冲突' if status=='conflict' else '失败'
                    log(settings.base,f'旧路径处理{label}：{item.relative}；{type(exc).__name__}：{exc}')
                scanned+=1
                if max_files and counts['migrated']>=max_files:
                    break
        uploader.scan_directories.clear()
        log(settings.base,f'旧路径扫描结束：已检查 {scanned}/{len(items)}，迁移 {counts["migrated"]} 个，耗时 {time.monotonic()-scan_started:.1f} 秒。')
        events.flush()
        if mirror:
            if counts['failed'] or counts['conflict']:
                raise Conflict('旧路径迁移存在失败或冲突，未开始上传前整理及新增上传。')
            log(settings.base,'上传前目录整理：先归档多余文件、清理空目录并核对目录和名称；有效续传文件保留。')
            with upload_heartbeat(settings.base,uploader,lambda:sum(counts.values()),len(items)):
                mirror.cleanup_before_upload()
            summary['preupload_directory_cleanup']=dict(archived=mirror.archived,renamed=mirror.renamed)
            log(settings.base,f'上传前目录整理完成：归档 {mirror.archived} 项，纠正名称 {mirror.renamed} 项；缺失或变更内容随后上传。')
        log(settings.base,'阶段二：上传新增或变更文件，已迁移文件不再重传。')
        with upload_heartbeat(settings.base,uploader,lambda:sum(counts.values()),len(items)):
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
                        archived=uploader.archive_legacy_copies(item)
                        summary['archived_legacy_copies']+=archived
                        if archived:events.record('archived',item,archived)
                        counts[result]+=1
                        if item.relative==LEDGER_WORKBOOK:
                            summary['ledger_workbook'].update(status=result,sent_bytes=sent)
                        summary['confirmed_bytes']+=item.size
                        audit(settings,item,result,sent)
                        events.record(result,item)
                        break
                    except Stopped:
                        audit(settings,item,'stopped',uploader.wire-before)
                        raise
                    except Conflict as exc:
                        counts['conflict']+=1
                        audit(settings,item,'conflict',uploader.wire-before,str(exc))
                        log(settings.base,f'上传冲突：{item.relative}；{type(exc).__name__}：{exc}')
                        break
                    except OSError as exc:
                        audit(settings,item,'retry' if attempt+1<settings.attempts else 'failed',uploader.wire-before,str(exc))
                        if attempt+1==settings.attempts:
                            counts['failed']+=1
                            log(settings.base,f'上传失败，保留续传进度：{item.relative}；{type(exc).__name__}：{exc}')
                        else:
                            log(settings.base,f'上传重试 {attempt+1}/{settings.attempts}：{item.relative}；{type(exc).__name__}：{exc}')
                            for _ in range(10):
                                check();time.sleep(0.2)
                if max_files and counts['uploaded']+counts['adopted']+counts['migrated']>=max_files:
                    break
        processed=sum(counts.values())
        summary['status']='complete' if not counts['failed'] and not counts['conflict'] and processed==len(items) else 'pending'
        if mirror and summary['status']=='complete':
            log(settings.base,'阶段三：核验全部正式文件和最终结构，收尾本轮生成的临时文件及旧版备份。')
            with upload_heartbeat(settings.base,uploader,lambda:processed,len(items)):
                summary['directory_mirror']=mirror.reconcile()
            log(settings.base,f'目录镜像完成：归档 {mirror.archived} 项，纠正名称 {mirror.renamed} 项；归档目录：{mirror.archive_settings.target}')
    except Stopped as exc:
        summary['status']='stopped';summary['message']=str(exc)
        log(settings.base,str(exc))
    except Exception as exc:
        summary['status']='failed';summary['error']=str(exc)
        log(settings.base,f'本轮上传异常：{type(exc).__name__}：{exc}\n{traceback.format_exc()}')
    finally:
        events.flush()
        summary['ended']=now().isoformat()
        summary['sent_bytes']=uploader.wire if uploader else 0
        if uploader:
            summary['remote_hash_files']=uploader.remote_hash_files
            summary['remote_hash_bytes']=uploader.remote_hash_bytes
            summary['remote_hash_fell_back']=uploader.remote_hash_disabled
            summary['hash_cache_hits']=uploader.hash_cache_hits
            summary['nas_read_bytes']=uploader.nas_read_bytes
            summary['timings_seconds']={key:round(value,3) for key,value in uploader.timings.items()}
            summary['last_phase']=uploader.phase_snapshot()
            summary['current_file']=uploader.current_file
            log(settings.base,f'阶段耗时：路径检查 {uploader.timings["path_checks"]:.1f} 秒，NAS回读校验 {uploader.timings["nas_hash"]:.1f} 秒，本地哈希 {uploader.timings["local_hash"]:.1f} 秒，上传写入 {uploader.timings["upload"]:.1f} 秒；NAS校验已读取 {uploader.nas_read_bytes/1e9:.3f} GB。')
            if settings.remote_hash:
                log(settings.base,f'NAS本机校验：{uploader.remote_hash_files} 次，{uploader.remote_hash_bytes/1e9:.3f} GB；SMB校验回读 {uploader.nas_read_bytes/1e9:.3f} GB；本轮回退SMB：{uploader.remote_hash_disabled}。')
            log(settings.base,f'本轮哈希结果复用 {uploader.hash_cache_hits} 次；文件变化后自动重新校验。')
        if uploader:uploader.close()
        if mirror:mirror.close()
        resources.close()
        folder=settings.base/'logs'/start.strftime('%Y-%m-%d');folder.mkdir(parents=True,exist_ok=True)
        name='upload-'+start.strftime('%H%M%S-%f')+'-summary.json'
        (folder/name).write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        log(settings.base,f'本轮结束：{summary["status"]}；新上传 {counts["uploaded"]}，相同文件接管 {counts["adopted"]}，旧路径迁移 {counts["migrated"]}，正式目录旧副本归档 {summary["archived_legacy_copies"]}，跳过 {counts["skipped"]}，失败 {counts["failed"]}，冲突 {counts["conflict"]}。')
    return 0 if summary['status']=='complete' else 2 if summary['status'] in ('stopped','pending') else 1


def scheduled_upload(settings, exiting, max_files=None, verify_all=False):
    """Keep the resident scheduler alive even if final summary/cleanup fails."""
    try:
        return run_upload(settings,'once',exiting,max_files,verify_all)
    except Exception as exc:
        log(settings.base,f'上传轮次未正常收尾，保留常驻重试：{type(exc).__name__}：{exc}\n{traceback.format_exc()}')
        return 1


def managed_schedule(settings, exiting, max_files=None, verify_all=False):
    log(settings.base, '链式上传调度启动：解析完成即上传，最迟12:00启动；运行后不限制结束时间，成功后当天不重复运行。')
    retry_at = 0
    stopped_day = None
    while not exiting.is_set():
        if (settings.base/'runtime/exit.request').exists():
            break
        manual = read_manual_request(settings)
        if manual.get('state') == 'pending':
            started = now()
            code = execute_manual_round(settings, manual, exiting)
            if code == 0:
                _managed_pipeline.upload_finished(settings, started)
            continue
        at = now()
        day = at.date().isoformat()
        if stopped_day != day and time.monotonic() >= retry_at and _managed_pipeline.upload_due(settings, at):
            (settings.base/'runtime/stop.request').unlink(missing_ok=True)
            code = scheduled_upload(settings, exiting, max_files, verify_all)
            if code == 0:
                _managed_pipeline.upload_finished(settings, at)
                log(settings.base, '本轮上传已完成，已保存完成状态，等待下一运行日。')
            elif (settings.base/'runtime/stop.request').exists():
                stopped_day = day
                _managed_pipeline.save(settings.base/'runtime/pipeline_upload.json', dict(date=day, stopped=True))
            else:
                retry_at = time.monotonic() + 300
                log(settings.base, '仍有未完成任务，5分钟后重新比较并重试，已确认且未变化的文件跳过。')
        exiting.wait(.5)
    log(settings.base, '链式上传调度已安全退出。')
    return 0


def main(argv=None):
    from log_policy import utf8_streams
    utf8_streams()
    if sys.version_info<(3,10):
        raise RuntimeError('需要Python3.10或更高版本。')
    if hasattr(sys.stdout,'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8',errors='replace')
        sys.stderr.reconfigure(encoding='utf-8',errors='replace')
    parser=argparse.ArgumentParser(description='绿联云SMB上传工具；北京时间12:00至00:00')
    parser.add_argument('action',choices=['check','plan','test-upload','once','manual-once','schedule','stop','integrity','history-plan','history-migrate','remote-hash-check'])
    parser.add_argument('--config',type=Path,default=HOME/'config.json')
    parser.add_argument('--max-files',type=int)
    parser.add_argument('--verify-all',action='store_true',help='跳过快速缓存，按所选SMB或SSH方式逐个比较本地/远端SHA256')
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
    if args.action=='remote-hash-check':
        if not settings.remote_hash:
            raise ValueError('请先配置并启用remote_hash。')
        from nas_remote_hash import RemoteHasher
        connect(settings.target)
        worker=RemoteHasher(settings.remote_hash,settings.target)
        try:
            worker.start()
            print('SSH认证、NAS Python3及SMB目录映射检查通过；32字节随机探针已清理，未修改业务文件。')
        finally:
            worker.close()
        return 0
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
    if args.action in ('schedule','manual-once'):integrity()
    exiting=threading.Event()
    def commands():
        for line in sys.stdin:
            command=line.strip().lower()
            if command in ('q','quit','exit'):exiting.set()
            elif command in ('s','stop'):
                (settings.base/'runtime/stop.request').write_text(now().isoformat(),encoding='utf-8')
    signal.signal(signal.SIGINT,lambda *_:exiting.set())
    signal.signal(signal.SIGTERM,lambda *_:exiting.set())
    threading.Thread(target=commands,daemon=True).start()
    if args.action=='manual-once':
        return manual_upload_once(settings,exiting,args.max_files,args.verify_all)
    with run_lock(settings.base):
        for name in ('exit.request','stop.request'):(settings.base/'runtime'/name).unlink(missing_ok=True)
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
        if _managed_pipeline is not None:
            return managed_schedule(settings, exiting, args.max_files, args.verify_all)
        log(settings.base,'常驻上传调度启动：每天北京时间12:00至次日00:00；Q回车/Ctrl+C安全退出，S回车停止今天。')
        last_day=announced=None
        while not exiting.is_set():
            if (settings.base/'runtime/exit.request').exists():break
            manual=read_manual_request(settings)
            if manual.get('state')=='pending':
                execute_manual_round(settings,manual,exiting)
                if in_window(now()):last_day=now().date().isoformat()
                announced=None
                continue
            at=now()
            if in_window(at) and last_day!=at.date().isoformat():
                last_day=at.date().isoformat()
                (settings.base/'runtime/stop.request').unlink(missing_ok=True)
                code=scheduled_upload(settings,exiting,args.max_files,args.verify_all)
                # Ordinary failures are retried within the daily window; manual stops end today.
                if code and not exiting.is_set() and in_window(now()) and not (settings.base/'runtime/stop.request').exists():
                    log(settings.base,'仍有未同步文件，5分钟后重新比较并重试；到00:00或手动停止时结束。')
                    until=time.monotonic()+300
                    while time.monotonic()<until and not exiting.is_set() and in_window(now()):
                        if (settings.base/'runtime/stop.request').exists() or (settings.base/'runtime/exit.request').exists():break
                        if read_manual_request(settings).get('state')=='pending':break
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
        log(HOME, f'启动失败：{type(exc).__name__}：{exc}')
        raise SystemExit(1)
    except Exception as exc:
        log(HOME, f'上传程序异常：{type(exc).__name__}：{exc}\n{traceback.format_exc()}')
        raise SystemExit(1)
