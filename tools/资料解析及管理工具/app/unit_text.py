"""Independent per-responsible-unit receipt text; never uses the download daily text."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid

LOCAL=timezone(timedelta(hours=8))
DEFAULT=dict(enabled=False,recipients=[],send_time_beijing='11:00',send_window_minutes=5,
             uia_profile='wechat_push/uia_profile.json')


def validate(data):
    if not isinstance(data,dict) or set(data)-set(DEFAULT):raise ValueError('单元资料推送文配置字段无效')
    data={**DEFAULT,**data}
    if type(data['enabled']) is not bool:raise ValueError('定时发送开关须为布尔值')
    if not isinstance(data['recipients'],list) or any(not isinstance(v,str) or not v.strip() or '\n' in v for v in data['recipients']):raise ValueError('接收会话须为完整名称列表')
    data['recipients']=list(dict.fromkeys(v.strip() for v in data['recipients']))
    if not isinstance(data['send_time_beijing'],str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',data['send_time_beijing']):raise ValueError('发送时间须为 HH:MM')
    if type(data['send_window_minutes']) is not int or not 1<=data['send_window_minutes']<=10:raise ValueError('发送窗口须为1至10分钟')
    if not isinstance(data['uia_profile'],str) or not data['uia_profile'].strip():raise ValueError('微信配置路径不能为空')
    return data


def load(base):
    from push_settings import load_settings
    return settings_from_shared(load_settings(base))


def settings_from_shared(shared):
    recipients=[n for n in shared.get('recipients',[]) if shared.get('recipient_publications',{}).get(n,{}).get('publish_unit_text',False)]
    return validate(dict(enabled=bool(recipients),recipients=recipients,
        send_time_beijing=shared.get('unit_text_send_time_beijing') or shared.get('text_send_time_beijing') or shared.get('send_time_beijing','11:00'),
        send_window_minutes=shared.get('send_window_minutes',5),uia_profile=shared.get('uia_profile',DEFAULT['uia_profile'])))


def atomic_json(path,data):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_suffix('.'+uuid.uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    temporary.replace(path)


def folder(base,day):return Path(base)/'logs'/day/'单元资料推送文'


def snapshot(base,day=None):
    day=day or datetime.now(LOCAL).date().isoformat()
    path=folder(base,day)/'快照.json'
    if not path.is_file():return dict(date=day,ready=False,text='',reason='尚未生成单元资料推送文')
    result=json.loads(path.read_text(encoding='utf-8'))
    if result.get('date')!=day or hashlib.sha256(result['text'].encode()).hexdigest()!=result.get('sha256'):raise ValueError('单元资料推送文快照校验失败')
    return result


def generate(base,*,at=None,units=None,members=None):
    from archive_index import current_members,readonly
    from local_analysis import unit_from
    from paths import resolve_path
    base=Path(base).resolve();at=(at or datetime.now(LOCAL)).astimezone(LOCAL);day=at.date().isoformat()
    if units is None:
        from drawing_briefing import ima_scope
        units=[v for scope in ima_scope() for v in scope['units']]
    units=sorted({str(v).zfill(3) for v in units},key=int)
    counts={u:{'total':dict(PCF=0,PDF=0,SHA=0),'today':dict(PCF=0,PDF=0,SHA=0)} for u in units}
    database=base/'runtime/ledger.sqlite3'
    with closing(readonly(database)) as db:
        files=[dict(r) for r in db.execute("SELECT * FROM files WHERE active=1 AND status='done'")]
    parents={(str(r['project']),str(r['id'])):r for r in files}
    queue=base/'runtime/file_analysis/queue.sqlite3';jobs={};parsed_units={}
    if queue.is_file():
        with closing(readonly(queue)) as db:
            tables={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'jobs' in tables:
                for r in db.execute('SELECT * FROM jobs WHERE active=1 ORDER BY updated DESC'):
                    key=(str(r['project']),str(r['file_id']))
                    if key not in jobs and key in parents and r['expected_sha']==parents[key].get('sha256'):jobs[key]=dict(r)
            if 'file_inventory' in tables:
                parsed_units={(r['job_key'],r['origin']):r['unit'] for r in db.execute('SELECT job_key,origin,unit FROM file_inventory')}
    extracted=current_members(base) if members is None else members
    member_parents={(str(m['project']),str(m['parent_file_id'])) for m in extracted}
    direct_units={}
    for (job_key,origin),unit in parsed_units.items():
        if '!/' not in origin:direct_units.setdefault(job_key,unit)
    seen=set();unknown_dates=[];unavailable=[];unidentified=[];pending=[]
    def unit_code(*values):
        unit=unit_from(*values)
        tail=unit.rsplit('-',1)[-1]
        return tail.zfill(3) if tail.isdigit() else None
    def register(parent,name,path,identity,context,parsed=None):
        kind=Path(name).suffix[1:].upper()
        if kind not in ('PCF','PDF','SHA'):return
        unit=unit_code(parsed or '',name,context)
        if unit not in counts:
            if unit is None:unidentified.append(name)
            return
        if identity in seen:return
        seen.add(identity)
        counts[unit]['total'][kind]+=1
        received=parent.get('downloaded_at')
        if not received:
            if '历史版本复用' not in str(parent.get('download_date_source','')):unknown_dates.append(name)
            return
        try:
            time=datetime.fromisoformat(received)
            time=time.replace(tzinfo=LOCAL) if time.tzinfo is None else time.astimezone(LOCAL)
        except (ValueError,TypeError):unknown_dates.append(name);return
        counts[unit]['today'][kind]+=time.date().isoformat()==day
    for key,row in parents.items():
        suffix=Path(row['filename']).suffix.lower()
        if suffix not in ('.pcf','.pdf','.sha','.zip','.7z','.rar'):continue
        inferred=unit_code(row['filename'],row.get('path',''))
        if inferred is not None and inferred not in counts:continue
        source=resolve_path(row['path'],base) if row.get('path') else None
        if not source or source.is_symlink() or not source.is_file() or source.stat().st_size!=row['bytes']:
            if inferred in counts:unavailable.append(row['filename'])
            continue
        job=jobs.get(key)
        if suffix in ('.zip','.7z','.rar'):
            unit=unit_code(row['filename'],row.get('path',''))
            if (unit is None or unit in counts) and (not job or job['state'] not in ('done','failed')):pending.append(row['filename'])
            elif members is None and (unit is None or unit in counts) and (not job.get('result_path') or not (base/'runtime/file_analysis'/job['result_path']).is_file()):pending.append(row['filename'])
            elif job and job['state']=='failed' and key not in member_parents and (unit is None or unit in counts):pending.append(row['filename'])
        parsed=direct_units.get(job['key']) if job and suffix in ('.pcf','.sha') else None
        register(row,row['filename'],source,(key[0],str(source)),row.get('path',''),parsed)
    for member in extracted:
        key=(str(member['project']),str(member['parent_file_id']));parent=parents.get(key)
        if not parent:continue
        if Path(member['filename']).suffix.lower() not in ('.pcf','.pdf','.sha'):continue
        parsed=parsed_units.get((member.get('job_key'),member.get('origin')))
        unit=unit_code(parsed or '',member['filename'],member.get('member_path',''),parent.get('path',''))
        if unit is not None and unit not in counts:continue
        if member.get('availability')!='ready':
            if unit in counts:unavailable.append(member['filename'])
            continue
        register(parent,member['filename'],member['path'],(key[0],str(Path(member['path']).resolve())),
                 member.get('member_path','')+' / '+str(parent.get('path','')),
                 parsed)
    lines=[]
    for unit in units:
        total,today=counts[unit]['total'],counts[unit]['today']
        lines.append(f'{int(unit)}单元PCF文件共收到{total["PCF"]}份，PDF文件共收到{total["PDF"]}份，SHA文件共收到{total["SHA"]}份；今日PCF更新数量{today["PCF"]}份，PDF更新数量{today["PDF"]}份，SHA更新数量{today["SHA"]}份。')
    text='\n'.join(lines)
    issues=[]
    if not units:issues.append('尚未选择负责单元')
    if pending:issues.append(f'{len(pending)} 个压缩包尚未完成文件登记')
    if unknown_dates:issues.append(f'{len(unknown_dates)} 个文件缺少可核对的下载日期')
    if unavailable:issues.append(f'{len(unavailable)} 个已登记文件在本机缺失或大小不符')
    result=dict(date=day,generated_at=at.isoformat(),ready=not issues,text=text,sha256=hashlib.sha256(text.encode()).hexdigest(),
                counts=counts,units=units,issues=issues,reason='；'.join(issues),unidentified_files=len(unidentified),
                basis='当前有效且下载完成的文件；压缩包成员按实际输出路径去重；今日按北京时间下载日期，成员沿用所属压缩包日期。')
    target=folder(base,day);target.mkdir(parents=True,exist_ok=True)
    temporary=target/('正文-'+uuid.uuid4().hex+'.tmp');temporary.write_text(text,encoding='utf-8');temporary.replace(target/'单元资料推送文.txt')
    atomic_json(target/'快照.json',result)
    return result


def run_once(base,*,at=None,manual=False,settings=None,sender_factory=None,cancelled=lambda:False,expected_sha=None):
    from analysis_worker import worker_lock
    from wechat_push.errors import DeliveryNotSent
    base=Path(base).resolve();at=(at or datetime.now(LOCAL)).astimezone(LOCAL);settings=validate(settings) if settings is not None else load(base)
    if cancelled():return dict(state='cancelled',submitted=0)
    if not settings['enabled'] and not manual:return dict(state='disabled',submitted=0)
    if not settings['recipients']:raise ValueError('单元资料推送文尚未配置接收会话')
    hour,minute=map(int,settings['send_time_beijing'].split(':'));due=at.replace(hour=hour,minute=minute,second=0,microsecond=0)
    if not manual and not due<=at<due+timedelta(minutes=settings['send_window_minutes']):return dict(state='outside_window',submitted=0)
    with worker_lock(base/'runtime/unit_text_send') as available:
        if not available:return dict(state='busy',submitted=0)
        receipt=base/'runtime/unit_text_deliveries.sqlite3';receipt.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(receipt)) as db:
            db.execute('CREATE TABLE IF NOT EXISTS deliveries(day,recipient,state,sha256,text,attempts INTEGER,error,updated,PRIMARY KEY(day,recipient))')
            with db:db.execute("UPDATE deliveries SET state='uncertain',error='上次发送中断，请核对微信记录' WHERE state='sending'")
            todo=[]
            for chat in settings['recipients']:
                old=db.execute('SELECT state,attempts FROM deliveries WHERE day=? AND recipient=?',(at.date().isoformat(),chat)).fetchone()
                if not old or old[0]=='not_sent' and old[1]<3:todo.append(chat)
            if not todo:return dict(state='already_checked',submitted=0)
            report=snapshot(base,at.date().isoformat()) if expected_sha else generate(base,at=at)
            if expected_sha and (not manual or report.get('sha256')!=expected_sha):raise ValueError('单元资料推送文已变化，请刷新预览后重新确认')
            if expected_sha:
                from drawing_briefing import ima_scope
                current_units=sorted({str(v).zfill(3) for s in ima_scope() for v in s['units']},key=int)
                if current_units!=report.get('units'):raise ValueError('负责单元已变化，请重新生成单元资料推送文')
            if not report['ready']:raise ValueError('单元资料推送文统计未就绪：'+report['reason'])
            if not report['text'] or len(report['text'])>14000:raise ValueError('单元资料推送文为空或超过单条消息长度')
            result=dict(state='complete',submitted=0,failed=0,uncertain=0)
            sender=None
            try:
                for chat in todo:
                    if cancelled():break
                    with db:db.execute("INSERT INTO deliveries VALUES(?,?,'sending',?,?,1,NULL,?) ON CONFLICT(day,recipient) DO UPDATE SET state='sending',sha256=excluded.sha256,text=excluded.text,attempts=deliveries.attempts+1,error=NULL,updated=excluded.updated",(report['date'],chat,report['sha256'],report['text'],at.isoformat()))
                    state,error='submitted',None
                    try:
                        if sender is None:
                            if sender_factory:sender=sender_factory()
                            else:
                                from wechat_push.uia import WindowsUIASender
                                profile=Path(settings['uia_profile'])
                                profile_base=Path(__file__).resolve().parent.parent if profile.as_posix()==DEFAULT['uia_profile'] else base
                                sender=WindowsUIASender(profile if profile.is_absolute() else profile_base/profile)
                        sender.send(chat,report['text']);result['submitted']+=1
                    except DeliveryNotSent as exc:state,error='not_sent',str(exc);result['failed']+=1
                    except Exception as exc:state,error='uncertain',str(exc);result['uncertain']+=1
                    with db:db.execute('UPDATE deliveries SET state=?,error=?,updated=? WHERE day=? AND recipient=?',(state,error,at.isoformat(),report['date'],chat))
            finally:
                if sender and getattr(sender,'close',None):sender.close()
            return result


def main():
    from paths import HOME
    parser=argparse.ArgumentParser(description='生成或发送独立的单元资料推送文')
    parser.add_argument('--send',action='store_true');parser.add_argument('--expected-sha');args=parser.parse_args()
    print(json.dumps(run_once(HOME,manual=True,expected_sha=args.expected_sha) if args.send else generate(HOME),ensure_ascii=False))


if __name__=='__main__':main()
