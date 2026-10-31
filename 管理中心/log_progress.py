"""Read-only presentation of bounded log tails; never discard warnings or errors."""
from datetime import datetime
import json
import re

ERROR = re.compile(r'失败|未完成|错误|异常|Traceback|\b(?:error|failed|exception)\b',re.I)


def severity(line):
    evidence=re.sub(r'(?:失败|冲突)\s*[:：]?\s*0\b','',line)
    if ERROR.search(evidence) or re.search(r'失败\s*[:：]?\s*[1-9]\d*',evidence):return 'ERROR'
    if re.search(r'警告|\bwarning\b|\bconflict\b|冲突|待核对',evidence,re.I):return 'WARNING'
    return 'INFO'


def timestamp(line, modified, zone):
    fallback=datetime.fromtimestamp(modified,zone)
    match=re.search(r'\[?(\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:\.\d+)?(?:[+-]\d\d:\d\d)?)',line)
    try:
        if match:
            value=datetime.fromisoformat(match[1])
            return value.replace(tzinfo=zone).isoformat() if value.tzinfo is None else value.isoformat()
        match=re.match(r'\[(?:(\d\d-\d\d) )?(\d\d:\d\d:\d\d)\]',line)
        if match:
            date=str(fallback.year)+'-'+match[1] if match[1] else fallback.date().isoformat()
            return datetime.fromisoformat(date+'T'+match[2]).replace(tzinfo=zone).isoformat()
    except ValueError:pass
    return fallback.isoformat()


def progress(line):
    text=re.sub(r'^\[[^\]]+\]\s*','',line).strip()
    if text.startswith('@@PROGRESS '):
        try:
            value=json.loads(text[len('@@PROGRESS '):])
            phase=str(value.get('phase') or '处理进度')
            done=value.get('download_done_files',value.get('associated_done'))
            total=value.get('download_total_files',value.get('associated_total'))
            label=phase+(f'：{done}/{total}' if done is not None and total is not None else '')
            if value.get('current_file'):label+='；当前：'+str(value['current_file'])
            return dict(phase=phase,message=label,current=done,total=total)
        except (ValueError,TypeError):return None
    if severity(text)!='INFO':return None
    categories=(
        ('目录整理',r'^(?:启动|检查数据库|备份数据库|保存迁移|校验源文件|校验暂存文件|扫描历史|扫描已登记|检查当前文件|检查迁移目标|更新原名|更新解析队列|写入路径映射|清理空|迁移完成).*；本阶段|^路径已迁移：'),
        ('后台解析',r'^(?:扫描进度|开始文件|文件大小|读取并解析|校验文件内容|开始解压|解压完成|包内进度|解析 PCF|解析 IDF|文件结束|文件结果|保存解析队列|保存解析结果|生成/读取焊口|管线元件解析完成|分类小计|仍在处理|空闲等待|日志合并：)'),
        ('NAS上传',r'^(?:uploaded|adopted|migrated|skipped)：|^(?:上传|回读校验|比较远端文件|核对旧路径文件|继续上传|开始上传|回读NAS校验|上传完成|旧路径迁移完成)：.*(?:当前|本次已发送)'),
        ('文件下载',r'^阶段：文件下载|^下载完成：'),
        ('原件/附件核对',r'^原件/附件核对|^阶段：原件/附件核对'),
        ('目录盘点',r'^目录已保存|^清单已保存|^阶段：目录'),
    )
    for phase,pattern in categories:
        if re.search(pattern,text):
            match=re.search(r'(?:完成|已完成|已检查)\s*([\d,]+)\s*/\s*([\d,]+)',text)
            return dict(phase=phase,message=text,current=int(match[1].replace(',','')) if match else None,
                        total=int(match[2].replace(',','')) if match else None)
    return None


def compact(rows):
    groups={};result=[]
    for row in rows:  # chronological within one source
        value=progress(row['message'])
        if value:
            phase=value['phase']
            group=groups.get(phase)
            count=(group.get('progress',{}).get('updates',0) if group else 0)+1
            latest=dict(row,message=value.pop('message'),progress=dict(value,updates=count))
            latest['id']='progress-'+row['source']+'-'+phase+'-'+str(len(result) if group is None else group['_slot'])
            if group is None:
                latest['_slot']=len(result);result.append(latest)
            else:
                latest['_slot']=group['_slot'];result[group['_slot']]=latest
            groups[phase]=latest
        else:
            result.append(row)
            # Lifecycle boundaries prevent unrelated runs from merging together.
            if re.search(r'本轮结束|本次运行结束|当日任务结束|调度.*启动|开始执行：|目录整理进程退出码',row['message']):groups={}
    for row in result:row.pop('_slot',None)
    return result
