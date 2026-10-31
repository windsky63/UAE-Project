"""Read-only enriched ledger catalogue and filters over the whole result set."""
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

FIELDS = {
    'kind':'记录类型','parent_archive':'所属压缩包','member_path':'包内完整路径',
    'extraction_status':'解压状态','parent_file_id':'所属压缩包内容ID',
    'name':'文件名称','project':'项目','business_type':'业务类型','discipline':'业务专业',
    'plant':'厂区','unit':'单元','unit_name':'单元名称','reference':'文档编号','title':'文档标题',
    'revision':'修订','extension':'扩展名','relation':'原件/附件类型','download_status':'下载状态',
    'analysis_status':'后台解析状态','state':'本机状态','bytes':'已保存字节','expected_bytes':'平台文件字节',
    'downloaded_at':'下载日期（北京）','date_source':'日期来源','modified':'更新时间',
    'folder_path':'平台目录','path':'本地路径','business_status':'业务识别状态',
    'business_source':'业务识别来源','business_basis':'编码规则依据','business_reason':'业务待核对原因',
    'matched_number':'命中编号','sha256':'SHA256','error':'错误信息','file_id':'内容ID',
}
NUMERIC = {'bytes','expected_bytes'}
DATES = {'downloaded_at','modified'}
DOWNLOAD = {'done':'已完成','pending':'待处理','downloading':'下载中','failed':'失败','no_content':'未上传内容',
            'restricted':'无读取权限','not_enumerated':'待枚举'}
BUSINESS = {'recognized':'已识别','inherited':'继承关联文档（内容未核验）','discipline_only':'仅识别专业',
            'unrecognized':'未识别','conflict':'冲突待核对'}
ANALYSIS = {'done':'后台处理完成','pending':'待解析','running':'解析中','failed':'解析失败'}


def readonly(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def archive_index(base):
    spec = importlib.util.spec_from_file_location('project_archive_index', Path(base)/'app/archive_index.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def stamp(path):
    try:
        info=path.stat()
        return info.st_mtime_ns,info.st_size
    except OSError:return None


def joined(rows,key):
    return '；'.join(dict.fromkeys(str(r.get(key) or '') for r in rows if r.get(key)))


def local_date(value):
    if not value:return ''
    try:
        at=datetime.fromisoformat(str(value).replace('Z','+00:00'))
        if at.tzinfo:at=at.astimezone(timezone(timedelta(hours=8)))
        return at.isoformat(sep=' ',timespec='seconds')
    except ValueError:return str(value)


class FileCatalog:
    def __init__(self,manager):
        self.manager=manager
        self.lock=threading.Lock()
        self.cached=None
        self.signature=None
        self.loaded=0

    def snapshot(self,refresh=False):
        base=self.manager.folder('sync')
        ledger=base/'runtime/ledger.sqlite3'
        queue=base/'runtime/file_analysis/queue.sqlite3'
        code=base/'app/business_type.py'
        rules=base/'document_coding_rules.json'
        paths=[ledger,Path(str(ledger)+'-wal'),queue,Path(str(queue)+'-wal'),code,rules,base/'app/archive_index.py']
        signature=tuple(stamp(p) for p in paths)
        with self.lock:
            if not refresh and self.cached is not None and signature==self.signature and time.monotonic()-self.loaded<30:
                return self.cached
            result=self.read(base,ledger,queue,code,rules)
            self.signature,self.cached,self.loaded=signature,result,time.monotonic()
            return result

    def read(self,base,ledger,queue,code,rules):
        if not ledger.is_file():return dict(items=[],available=False,classification_available=False)
        classifier=None
        warning=''
        try:
            spec=importlib.util.spec_from_file_location('project_business_classifier',code)
            classifier=importlib.util.module_from_spec(spec)
            spec.loader.exec_module(classifier)
            classifier.RULES_PATH=rules
            classifier.load_rules()
        except (OSError,ImportError,AttributeError,ValueError) as exc:
            classifier=None
            warning='业务类型规则未就绪，请完整更新同步工具：'+str(exc)[:180]
        with closing(readonly(ledger)) as db:
            rows=[dict(r) for r in db.execute('SELECT * FROM files WHERE active=1 ORDER BY project,filename,id')]
            contexts=classifier.ledger_file_contexts(db) if classifier else {}
        jobs={}
        if queue.is_file():
            try:
                with closing(readonly(queue)) as db:
                    for job in db.execute('SELECT project,file_id,state,error FROM jobs WHERE active=1 ORDER BY updated'):
                        jobs[job['project'],job['file_id']]=dict(job)
            except sqlite3.Error:
                warning+='；解析队列暂不可读，解析状态待核对'
        items=[]
        downloads_root=(base/'downloads').resolve()
        unit_names={}
        for row in rows:
            parents=contexts.get((row['project'],row['id']),())
            business=classifier.file_business(row,parents) if classifier else {}
            unit=business.get('unit','')
            plant=business.get('plant','')
            if plant and unit:unit=plant+'-'+unit
            if not unit:
                matches=set(re.findall(r'(?<![A-Z0-9])(H[A-Z0-9]-\d{3})(?!\d)',
                    (str(row.get('filename') or '')+' '+joined(parents,'reference')).upper()))
                if len(matches)==1:unit=next(iter(matches));plant=unit.split('-')[0]
            unit_name=''
            if classifier and re.fullmatch(r'H[A-Z0-9]-\d{3}',unit):
                if unit not in unit_names:unit_names[unit]=classifier.unit_names(unit)['chinese']
                unit_name=unit_names[unit]
            supplied=str(row.get('path') or '').replace('\\','/')
            try:
                candidate=(base/supplied).resolve()
                allowed=bool(supplied) and candidate.is_relative_to(downloads_root)
            except (OSError,ValueError):
                candidate=base/'downloads/.invalid'
                allowed=False
            status=row.get('status','')
            state='未下载'
            present=False
            if status=='done':
                try:
                    exists=allowed and candidate.is_file()
                    present=exists and candidate.stat().st_size==row.get('bytes')
                    state='可读取' if present else '大小不符' if exists else '本机缺失' if allowed else '路径不可读取'
                except OSError:state='本机不可读取'
            identity='download-'+hashlib.sha256(json.dumps([row['project'],row['id']]).encode()).hexdigest()[:24]
            self.manager.artifacts[identity]=dict(path=candidate,project=row['project'],file_id=row['id'],
                kind='downloads',sha256=row.get('sha256'),bytes=row.get('bytes'))
            job=jobs.get((row['project'],row['id']),{})
            item=dict(id=identity,name=row['filename'],project=row['project'],file_id=row['id'],
                bytes=row.get('bytes'),expected_bytes=row.get('size'),state='ready' if present else 'missing',
                local_status=state,kind='原始下载文件',download_status=DOWNLOAD.get(status,status),
                analysis_status=ANALYSIS.get(job.get('state'),'未排队' if status=='done' else '未下载'),
                reference=joined(parents,'reference'),title=joined(parents,'title'),revision=row.get('revision') or joined(parents,'revision'),
                extension=Path(row['filename']).suffix.lower(),relation=row.get('relation') or '',
                downloaded_at=local_date(row.get('downloaded_at')),date_source=row.get('download_date_source') or '',
                modified=local_date(row.get('updated')),folder_path=joined(parents,'folder_path'),path=row.get('path') or '',
                sha256=row.get('sha256') or '',error='；'.join(v for v in (row.get('error'),job.get('error')) if v),
                business_type=business.get('business_type','规则未就绪'),discipline=business.get('discipline',''),
                plant=plant,unit=unit,unit_name=unit_name,business_status=BUSINESS.get(business.get('status'),'规则未就绪'),
                business_source=business.get('source',''),business_basis=business.get('basis',''),
                business_reason=business.get('reason',''),matched_number=business.get('matched_number',''))
            item.update(parent_archive='',member_path='',extraction_status='',parent_file_id='')
            items.append(item)
        platform_total=len(items)
        parents={(str(r['project']),str(r['file_id'])):r for r in items}
        if (base/'app/archive_index.py').is_file():
            try:
                for member in archive_index(base).current_members(base):
                    parent=parents.get((member['project'],member['parent_file_id']))
                    if not parent:continue
                    business=classifier.classify(filename=member['filename']) if classifier else {}
                    unit=classifier.pdf_unit(member['filename'],business.get('unit','')) if classifier else ''
                    plant=unit.split('-')[0] if '-' in unit else ''
                    identity='member-'+member['id']
                    item={**parent,'id':identity,'name':member['filename'],'file_id':member['id'],
                        'kind':'解压文件','parent_archive':member['parent_filename'],'member_path':member['member_path'],
                        'extraction_status':member['extraction_state'],'parent_file_id':member['parent_file_id'],
                        'path':member['path'],'bytes':member['bytes'],'expected_bytes':None,'sha256':member['sha256'],
                        'extension':Path(member['filename']).suffix.lower(),'relation':'压缩包成员',
                        'state':'ready' if member['availability']=='ready' else 'missing',
                        'local_status':'可读取' if member['availability']=='ready' else '解压文件缺失或大小不符',
                        'download_status':'随压缩包下载（非独立下载）','analysis_status':member['analysis_state'],
                        'business_type':business.get('business_type','规则未就绪'),'discipline':business.get('discipline',''),
                        'unit':unit,'plant':plant,'unit_name':classifier.unit_names(unit)['chinese'] if classifier and plant else '',
                        'business_status':BUSINESS.get(business.get('status'),'规则未就绪'),
                        'business_source':business.get('source',''),'business_basis':business.get('basis',''),
                        'business_reason':business.get('reason',''),'matched_number':business.get('matched_number',''),
                        'error':'' if member['availability']=='ready' else '解压文件缺失或大小不符'}
                    self.manager.artifacts[identity]=dict(kind='archive_members',path=Path(member['path']),member_id=member['id'])
                    items.append(item)
            except (OSError,ValueError,KeyError,TypeError,sqlite3.Error) as exc:
                warning+='；解压索引暂不可读取：'+str(exc)[:180]
        return dict(items=items,available=True,platform_total=platform_total,extracted_total=len(items)-platform_total,
                    classification_available=classifier is not None,warning=warning.strip('；'))


def value(item,key):
    return item.get('local_status',item.get('state','')) if key=='state' else item.get(key)


def validate(filters,sort,direction):
    if sort not in FIELDS or direction not in ('asc','desc'):raise ValueError('排序字段或方向无效')
    if not isinstance(filters,dict) or len(filters)>len(FIELDS):raise ValueError('筛选格式无效')
    operators={'contains','not_contains','equals','not_equal','starts_with','ends_with','empty','not_empty','gte','lte','gt','lt','between'}
    for key,rule in filters.items():
        if key not in FIELDS or not isinstance(rule,dict):raise ValueError('筛选列无效')
        if set(rule)-{'values','exclude_values','operator','value','value2'}:raise ValueError('未知筛选条件')
        if rule.get('operator','contains') not in operators:raise ValueError('筛选操作无效')
        for name in ('values','exclude_values'):
            if name in rule and (not isinstance(rule[name],list) or len(rule[name])>5000 or
                                 any(not isinstance(v,str) or len(v)>4096 for v in rule[name])):raise ValueError('筛选值无效')
        for name in ('value','value2'):
            if name in rule and (not isinstance(rule[name],str) or len(rule[name])>4096):raise ValueError('筛选文本无效')
        if rule.get('operator')=='between' and (not rule.get('value') or not rule.get('value2')):
            raise ValueError('介于条件须同时填写下限和上限')
        if key in DATES and rule.get('operator') in {'gte','lte','gt','lt','between'}:
            from datetime import date
            for name in ('value','value2') if rule['operator']=='between' else ('value',):
                try:date.fromisoformat(rule.get(name,''))
                except ValueError:raise ValueError('日期筛选须填写 YYYY-MM-DD')
        if key in NUMERIC and rule.get('operator') in {'gte','lte','gt','lt','between'}:
            import math
            for name in ('value','value2') if rule['operator']=='between' else ('value',):
                try:valid=math.isfinite(float(rule.get(name,'')))
                except ValueError:valid=False
                if not valid:raise ValueError('字节数筛选须填写有效数字')


def matches(item,key,rule):
    raw=value(item,key)
    text='' if raw is None else str(raw)
    if 'values' in rule and text not in rule['values']:return False
    if text in rule.get('exclude_values',[]):return False
    operator=rule.get('operator','contains')
    term=rule.get('value','').casefold()
    folded=text.casefold()
    if operator=='empty':return not text
    if operator=='not_empty':return bool(text)
    if not term:return True
    if operator in ('gte','lte','gt','lt','between'):
        if raw is None or text=='':return False
        if key in NUMERIC:left,right=float(raw),float(term)
        else:left,right=(text[:10] if key in DATES else folded),term
        if operator=='gte':return left>=right
        if operator=='lte':return left<=right
        if operator=='gt':return left>right
        if operator=='lt':return left<right
        upper=float(rule.get('value2','')) if key in NUMERIC else rule.get('value2','').casefold()
        return right<=left<=upper
    return {'contains':lambda:term in folded,'not_contains':lambda:term not in folded,
        'equals':lambda:term==folded,'not_equal':lambda:term!=folded,
        'starts_with':lambda:folded.startswith(term),'ends_with':lambda:folded.endswith(term)}[operator]()


def filter_items(items,query,filters,skip=None):
    term=query.casefold().strip()
    return [item for item in items if (not term or any(term in str(value(item,key) or '').casefold() for key in FIELDS))
            and all(matches(item,key,rule) for key,rule in filters.items() if key!=skip)]


def page_result(snapshot,query,filters,sort,direction,offset,limit,facet='',facet_search=''):
    validate(filters,sort,direction)
    items=snapshot['items']
    if facet:
        if facet not in FIELDS:raise ValueError('筛选列无效')
        pool=filter_items(items,query,filters,skip=facet)
        counts=Counter('' if value(item,facet) is None else str(value(item,facet)) for item in pool)
        choices=sorted((v for v in counts if facet_search.casefold() in v.casefold()),key=str.casefold)
        return dict(items=[dict(value=v,count=counts[v]) for v in choices[offset:offset+limit]],total=len(choices),records=len(pool))
    filtered=filter_items(items,query,filters)
    known=[r for r in filtered if value(r,sort) not in (None,'')]
    empty=[r for r in filtered if value(r,sort) in (None,'')]
    known.sort(key=lambda r:float(value(r,sort)) if sort in NUMERIC else str(value(r,sort)).casefold(),reverse=direction=='desc')
    ordered=known+empty
    return {**{k:v for k,v in snapshot.items() if k!='items'},'items':ordered[offset:offset+limit],
            'total':len(filtered),'unfiltered_total':len(items),'filters':filters,'sort':sort,'direction':direction}
