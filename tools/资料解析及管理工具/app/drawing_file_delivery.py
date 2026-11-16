"""Scheduled per-chat incremental drawing file delivery with durable receipts."""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile
import time
import uuid

from paths import HOME, resolve_path
from analysis_worker import worker_lock
from business_type import classify
from wechat_push.errors import DeliveryNotSent

LOCAL=timezone(timedelta(hours=8))
from drawing_delivery_settings import DEFAULT, validate, load


def resolved_scope(settings):
    from drawing_briefing import ima_scope
    shared=ima_scope()
    if settings['scope_mode']=='responsibility':
        return shared
    types=None if any(s.get('drawing_types') is None for s in shared) else sorted({v for s in shared for v in s.get('drawing_types',[])})
    formats=sorted({v for s in shared for v in s.get('file_formats',['PDF'])})
    return [{**settings['scope'],'drawing_types':types,'file_formats':formats}]


def chat_allows(settings,chat,discipline,drawing_type,unit='',file_format='PDF'):
    disciplines=settings['recipient_disciplines'].get(chat)
    types=settings['recipient_types'].get(chat)
    units=settings['recipient_units'].get(chat)
    formats=settings['recipient_formats'].get(chat)
    return ((disciplines is None or discipline in disciplines) and (types is None or drawing_type in types)
            and (units is None or unit.zfill(3) in units) and (formats is None or file_format in formats))


def candidates(base, *, settings=None, scopes=None):
    settings=validate(settings or {})
    if scopes is None:scopes=resolved_scope(settings)
    # A missing/empty scope grants no drawing file delivery.
    def in_scope(business,file_format):
        return bool(business.get('unit')) and any(business['discipline'] in scope['disciplines'] and
                   business.get('unit','').lstrip('0') in {v.lstrip('0') for v in scope['units']}
                   and (scope.get('drawing_types') is None or business['business_type'] in scope['drawing_types'])
                   and file_format in scope.get('file_formats',['PDF'])
                   for scope in scopes)
    database=Path(base)/'runtime/ledger.sqlite3'
    with closing(sqlite3.connect(database.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)) as db:
        db.row_factory=sqlite3.Row
        rows=db.execute("""SELECT d.project,d.id version_id,d.first_id,d.reference,d.revision document_revision,
            d.filename document_filename,d.metadata document_metadata,f.id content_id,f.filename,f.path,
            f.bytes,f.sha256,f.revision file_revision,f.relation FROM documents d
            JOIN document_files x ON x.project=d.project AND x.document_id=d.id AND x.folder_id=d.folder_id
            JOIN files f ON f.project=x.project AND f.id=x.file_id
            WHERE d.active=1 AND x.active=1 AND f.active=1 AND f.status='done'""").fetchall()
    families={}
    for raw in rows:
        row=dict(raw)
        metadata=json.loads(row.pop('document_metadata') or '{}')
        if metadata.get('isLatest') in (False,'false') or str(metadata.get('pmStatus','')).upper()=='CANCELLED':
            continue
        # Send original PDFs/DWG/DXF drawing files; no archives or rendered briefing images.
        suffix=Path(row['filename']).suffix.lower()
        if suffix not in ('.pdf','.dwg','.dxf'):
            continue
        business=classify(filename=row['filename'],reference=row['reference'] or '')
        if '图' not in business['business_type']:
            continue
        file_format=suffix[1:].upper()
        if not in_scope(business,file_format):continue
        if settings['recipients'] and not any(chat_allows(settings,chat,business['discipline'],business['business_type'],business.get('unit',''),file_format)
                                             for chat in settings['recipients']):
            continue
        if not row['path'] or not row['sha256']:
            continue
        path=Path(resolve_path(row['path'],base))
        if not path.is_file() or path.is_symlink() or path.stat().st_size!=row['bytes']:
            continue
        row['revision']=row['document_revision'] or row['file_revision'] or '未提供'
        # Revision-independent identity; document first_id distinguishes duplicate references.
        family=(row['project'],str(row['first_id'] or row['reference'] or row['version_id']),row['relation'] or '',
                'main' if row['filename']==row['document_filename'] else row['filename'].casefold())
        key=hashlib.sha256(json.dumps(family).encode()).hexdigest()
        row.update(key=key,source_path=str(path),drawing_type=business['business_type'],
                   discipline=business['discipline'],unit=business.get('unit',''),file_format=file_format)
        row['identity']=hashlib.sha256(json.dumps([row['version_id'],row['content_id'],row['revision'],row['sha256']],sort_keys=True).encode()).hexdigest()
        families.setdefault(key, {})[row['identity']] = row
    result={}
    for key, items in families.items():
        rows=list(items.values())
        # Distinct contents attached to one document version are separate files,
        # even when their platform filenames are identical. Never rank these as
        # competing document revisions or select only one of them.
        if len({row['version_id'] for row in rows}) > 1 or len({row['content_id'] for row in rows}) != len(rows):
            raise ValueError('图纸当前版本存在歧义，未自动选择：'+str(rows[0]['reference']))
        for row in rows:
            attachment_key = key if len(rows)==1 else hashlib.sha256(
                json.dumps([key, row['content_id']]).encode()).hexdigest()
            row['key']=attachment_key
            result[attachment_key]=row
    return result


def journal(base):
    path=Path(base)/'runtime/drawing_file_delivery.sqlite3'
    path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=5)
    db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript("""CREATE TABLE IF NOT EXISTS chats(name TEXT PRIMARY KEY,initialized_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS versions(chat TEXT,key TEXT,identity TEXT,state TEXT,payload TEXT,error TEXT,updated TEXT,
          PRIMARY KEY(chat,key,identity));
      CREATE TABLE IF NOT EXISTS attempts(day TEXT,chat TEXT,PRIMARY KEY(day,chat));
      CREATE TABLE IF NOT EXISTS chat_scopes(chat TEXT PRIMARY KEY,signature TEXT NOT NULL);""")
    db.execute('CREATE INDEX IF NOT EXISTS versions_content_receipt ON versions(chat,identity,state)')
    return db


def run_once(base, *, at=None, settings=None, sender_factory=None, cancelled=lambda:False, scheduled=True):
    base=Path(base).resolve()
    at=at or datetime.now(LOCAL)
    if at.tzinfo is None:
        raise ValueError('发送时间须包含时区')
    at=at.astimezone(LOCAL)
    settings=validate(settings) if settings is not None else load(base)
    if not settings['enabled'] or not settings['recipients']:
        return dict(state='disabled',submitted=0)
    runtime=base/'runtime'
    runtime.mkdir(exist_ok=True)
    with worker_lock(runtime/'drawing_file_delivery') as acquired:
        if not acquired:
            return dict(state='busy',submitted=0)
        hour,minute=map(int,settings['send_time_beijing'].split(':'))
        due=at.replace(hour=hour,minute=minute,second=0,microsecond=0)
        window=timedelta(minutes=settings['send_window_minutes'])
        if at<due and due-timedelta(days=1)<=at<due-timedelta(days=1)+window:
            due-=timedelta(days=1)
        in_window=due<=at<due+window
        result=dict(state='complete',submitted=0,baseline=0,uncertain=0,failed=0)
        with closing(journal(base)) as db:
            with db:
                db.execute("UPDATE versions SET state='uncertain',error='上次发送进程中断，需要人工核对' WHERE state IN ('preparing','sending_file','file_submitted')")
            chats=set(row[0] for row in db.execute('SELECT name FROM chats'))
            new_chats=set(settings['recipients'])-chats
            if scheduled and not in_window and not new_chats:
                return dict(state='outside_window',submitted=0)
            checked=set(row[0] for row in db.execute('SELECT chat FROM attempts WHERE day=?',(due.date().isoformat(),)))
            if scheduled and in_window and not new_chats and set(settings['recipients'])<=checked:
                return dict(state='already_checked',submitted=0)
            scopes=resolved_scope(settings)
            current=candidates(base,settings=settings,scopes=scopes)

            sender=None
            try:
                for chat in settings['recipients']:
                    if cancelled():break
                    types=settings['recipient_types'].get(chat)
                    selected={k:v for k,v in current.items() if chat_allows(settings,chat,v['discipline'],v['drawing_type'],v['unit'],v['file_format'])}
                    scope_identity=[scopes,settings['drawing_types'],types]
                    if settings['recipient_disciplines'].get(chat) is not None:
                        scope_identity.append(settings['recipient_disciplines'][chat])
                    for field in ('recipient_units','recipient_formats'):
                        if settings[field].get(chat) is not None:scope_identity.append({field:settings[field][chat]})
                    signature=hashlib.sha256(json.dumps(scope_identity,
                                                       sort_keys=True).encode()).hexdigest()
                    previous_scope=db.execute('SELECT signature FROM chat_scopes WHERE chat=?',(chat,)).fetchone()
                    initialized=db.execute('SELECT 1 FROM chats WHERE name=?',(chat,)).fetchone()
                    if not initialized or previous_scope and previous_scope[0]!=signature:
                        with db:
                            db.execute('INSERT OR IGNORE INTO chats VALUES(?,?)',(chat,at.isoformat()))
                            db.execute('INSERT OR REPLACE INTO chat_scopes VALUES(?,?)',(chat,signature))
                            db.executemany("INSERT OR IGNORE INTO versions VALUES(?,?,?,'baseline',?,NULL,?)",
                                [(chat,k,v['identity'],json.dumps(v,ensure_ascii=False),at.isoformat()) for k,v in selected.items()])
                            result['baseline']+=len(selected)
                        continue
                    if not previous_scope:
                        with db:db.execute('INSERT INTO chat_scopes VALUES(?,?)',(chat,signature))
                    if scheduled and not in_window:
                        result['state']='outside_window'
                        continue
                    day=due.date().isoformat()
                    if scheduled and db.execute('SELECT 1 FROM attempts WHERE day=? AND chat=?',(day,chat)).fetchone():continue
                    with db:db.execute('INSERT OR IGNORE INTO attempts VALUES(?,?)',(day,chat))
                    for key,item in selected.items():
                        if cancelled():break
                        # A same-name attachment can move between a single-file
                        # slot and a content-specific slot. Its receipt remains
                        # valid, including uncertain submissions and baselines.
                        if db.execute("SELECT 1 FROM versions WHERE chat=? AND identity=? AND state!='not_sent' LIMIT 1",
                                      (chat,item['identity'])).fetchone():continue
                        existing=db.execute('SELECT state FROM versions WHERE chat=? AND key=? AND identity=?',
                            (chat,key,item['identity'])).fetchone()
                        if existing and existing['state']!='not_sent':continue # Never auto-resend uncertain submissions.
                        payload=json.dumps(item,ensure_ascii=False)
                        with db:db.execute("INSERT INTO versions VALUES(?,?,?,'preparing',?,NULL,?) ON CONFLICT(chat,key,identity) DO UPDATE SET state='preparing',payload=excluded.payload,error=NULL,updated=excluded.updated",
                            (chat,key,item['identity'],payload,at.isoformat()))
                        state='preparing'
                        def save(value,error=None):
                            with db:db.execute('UPDATE versions SET state=?,error=?,updated=? WHERE chat=? AND key=? AND identity=?',
                                (value,error,datetime.now(LOCAL).isoformat(),chat,key,item['identity']))
                        try:
                            # Freeze a verified copy before touching the desktop.
                            staging=runtime/'drawing_file_staging'
                            staging.mkdir(exist_ok=True)
                            with tempfile.TemporaryDirectory(dir=staging) as temporary:
                                staged=Path(temporary)/Path(item['filename']).name
                                shutil.copyfile(item['source_path'],staged)
                                digest=hashlib.sha256()
                                with staged.open('rb') as stream:
                                    for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
                                if digest.hexdigest()!=item['sha256']:raise DeliveryNotSent('图纸已变化，未发送')
                                if cancelled():raise DeliveryNotSent('已取消，未发送')
                                if sender is None:
                                    if sender_factory:sender=sender_factory()
                                    else:
                                        from wechat_push.uia import WindowsUIASender
                                        sender=WindowsUIASender(resolve_path(settings['uia_profile'],base))
                                save('sending_file');state='sending_file'
                                sender.send_file(chat,staged,expected_sha=item['sha256'])
                                save('file_submitted');state='file_submitted'
                                if cancelled():raise RuntimeError('文件已提交，版本消息待人工核对')
                                sender.send(chat,f"图纸：{item['filename']}\n文档编号：{item['reference'] or '未提供'}\n业主平台修订号：{item['revision']}\n业主平台版本ID：{item['version_id']}")
                                save('submitted');result['submitted']+=1
                        except Exception as exc:
                            certain=state=='preparing' or (state=='sending_file' and isinstance(exc,DeliveryNotSent))
                            save('not_sent' if certain else 'uncertain',str(exc))
                            result['failed' if certain else 'uncertain']+=1
                            result['state']='needs_review'
                            break
            finally:
                if sender:
                    close=getattr(sender,'close',None)
                    if close:close()
        status=runtime/'drawing_file_delivery_status.json'
        temporary=status.with_suffix('.'+uuid.uuid4().hex+'.tmp')
        temporary.write_text(json.dumps(dict(result,at=at.isoformat()),ensure_ascii=False,indent=2),encoding='utf-8')
        os.replace(temporary,status)
        return result


def watch(base,exiting,log=print):
    while not exiting.is_set():
        try:
            result=run_once(base,cancelled=exiting.is_set)
            if result['state'] not in ('disabled','outside_window','busy','already_checked'):log('图纸文件发送：'+json.dumps(result,ensure_ascii=False))
        except Exception as exc:log(f'图纸文件发送失败：{type(exc).__name__}：{exc}')
        exiting.wait(30)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-dir',type=Path,default=HOME)
    args=parser.parse_args()
    print(json.dumps(run_once(args.base_dir,scheduled=False),ensure_ascii=False))

if __name__=='__main__':main()
