"""UTF-8 test output and backed-up normalization of historical application logs."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid


HOME=Path(__file__).resolve().parent


def log_files(directory, recursive=False):
    """Never follow directory aliases into downloads, state, or another tree."""
    directory = Path(directory)
    if (directory.is_symlink() or directory.is_junction() or
            directory.resolve() != directory.absolute() or not directory.is_dir()):
        return
    for path in directory.iterdir():
        if path.is_symlink() or path.is_junction():
            continue
        if recursive and path.is_dir():
            yield from log_files(path, True)
        elif path.is_file() and path.suffix.lower() == '.log':
            yield path


def normalize_logs(root=None):
    from manager import decode_text
    root=Path(root or HOME.parent).resolve()
    admin=root/'管理中心'
    candidates=set((admin/'runtime').glob('*.log'))
    for folder in ('FusionLive同步工具','绿联云上传工具','codex-wechat'):
        base=root/folder
        candidates.update((base/'logs').rglob('*.log'))
        candidates.update((base/'runtime').glob('*.log'))
        if folder=='FusionLive同步工具':candidates.update((base/'runtime/file_analysis').glob('*.log'))
    backup=admin/'runtime/log-encoding-backups'/datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    report={'converted':[],'skipped':[],'encoding':'UTF-8 without BOM'}
    for path in sorted(candidates):
        if path.is_symlink() or not path.resolve().is_relative_to(root):continue
        temporary=None
        try:
            before=path.stat()
            raw=path.read_bytes()
            try:
                plain=raw.decode('utf-8')
                if not plain.startswith('\ufeff') and '\x00' not in plain:continue
            except UnicodeDecodeError:pass
            text=decode_text(raw).lstrip('\ufeff')
            if '\ufffd' in text or '\x00' in text:
                report['skipped'].append(dict(path=str(path.relative_to(root)),reason='原文件包含无法可靠恢复的字符，保留原件'))
                continue
            relative=path.relative_to(root)
            saved=backup/relative.with_suffix(relative.suffix+'.original')
            saved.parent.mkdir(parents=True,exist_ok=True)
            saved.write_bytes(raw)
            temporary=path.with_name(path.name+'.utf8-'+uuid.uuid4().hex[:8]+'.tmp')
            temporary.write_bytes(text.encode('utf-8'))
            current=path.stat()
            if (current.st_size,current.st_mtime_ns)!=(before.st_size,before.st_mtime_ns):
                raise OSError('日志仍在写入，暂不转换')
            os.replace(temporary,path)
            report['converted'].append(dict(path=str(relative),backup=str(saved.relative_to(root))))
        except OSError as exc:
            report['skipped'].append(dict(path=str(path.relative_to(root)),reason=str(exc)))
        finally:
            if temporary is not None:temporary.unlink(missing_ok=True)
    if report['converted'] or report['skipped']:
        backup.mkdir(parents=True,exist_ok=True)
        (backup/'normalization.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return report


def run_tests(tool='manager',log=None):
    folder=HOME if tool=='manager' else HOME.parent/'FusionLive同步工具'
    output=Path(log) if log else folder/'runtime'/'tests-utf8.log'
    output.parent.mkdir(parents=True,exist_ok=True)
    env={**os.environ,'PYTHONUTF8':'1','PYTHONIOENCODING':'utf-8','PYTHONUNBUFFERED':'1'}
    if tool=='sync':env['PYTHONPATH']=str(folder/'app')
    command=[sys.executable,'-m','unittest','discover','-s','tests']
    with output.open('w',encoding='utf-8',newline='') as target:
        child=subprocess.Popen(command,cwd=folder,env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,
            text=True,encoding='utf-8',bufsize=1,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            for line in child.stdout:
                target.write(line);target.flush()
                print(line,end='',flush=True)
            return child.wait()
        finally:
            if child.poll() is None:child.terminate();child.wait(timeout=5)
            child.stdout.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description='统一 UTF-8 日志；测试入口不使用 PowerShell 重定向')
    parser.add_argument('action',choices=['normalize','test'])
    parser.add_argument('--tool',choices=['manager','sync'],default='manager')
    parser.add_argument('--log',type=Path)
    args=parser.parse_args()
    if args.action=='normalize':
        result=normalize_logs()
        print(json.dumps(result,ensure_ascii=False,indent=2))
    else:raise SystemExit(run_tests(args.tool,args.log))
