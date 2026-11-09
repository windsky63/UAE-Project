"""Interactive first login on the upload server; persist a dedicated key, never a password."""
from pathlib import Path, PureWindowsPath, PurePosixPath
import argparse
import base64
import csv
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime

from nas_remote_hash import RemoteHashSettings, RemoteHasher, RemoteHashError


def target_parts(value):
    path = PureWindowsPath(value)
    if not str(path).startswith('\\\\') or not path.is_absolute():
        raise ValueError('target_root须为NAS的UNC共享目录。')
    host, share = path.drive.lstrip('\\').split('\\', 1)
    return host, share, list(path.parts[1:])


def public_key(path):
    parts = Path(path).read_text(encoding='ascii').strip().split()
    if len(parts) < 2 or parts[0] != 'ssh-ed25519' or not re.fullmatch(r'[A-Za-z0-9+/=]+', parts[1]):
        raise ValueError('专用公钥格式无效，保留已有文件，不自动覆盖。')
    return ' '.join(parts[:2])


def protect_key(path):
    if os.name != 'nt':
        path.chmod(0o600)
        return
    result = subprocess.run(['whoami','/user','/fo','csv','/nh'],check=True,capture_output=True,text=True)
    sid = next(csv.reader(result.stdout.splitlines()))[-1]
    if not re.fullmatch(r'S-1-[0-9-]+',sid):
        raise RuntimeError('无法识别当前Windows用户SID。')
    subprocess.run(['icacls',str(path),'/inheritance:r','/grant:r','*'+sid+':F'],
                   check=True,stdout=subprocess.DEVNULL)


def prepare_key(folder):
    folder.mkdir(parents=True,exist_ok=True)
    key = folder/'nas_hash_ed25519'
    if not key.exists():
        if key.with_suffix('.pub').exists():
            raise RuntimeError('只有公钥而缺少私钥，保留现场；请恢复原密钥或换用新的配置目录。')
        subprocess.run(['ssh-keygen','-q','-t','ed25519','-f',str(key),'-N','',
                        '-C','fusionlive-nas-local-hash'],check=True)
    protect_key(key)
    if not key.with_suffix('.pub').exists():
        result = subprocess.run(['ssh-keygen','-y','-f',str(key)],check=True,
                                capture_output=True,text=True)
        key.with_suffix('.pub').write_text(result.stdout,encoding='ascii')
    return key,public_key(key.with_suffix('.pub'))


DISCOVER = r'''
import os,sys,json,subprocess
data=json.loads(__import__('base64').b64decode(PAYLOAD))
share=data['share'];sub=data['parts'];roots=[]
try:
    p=subprocess.run(['testparm','-s','--section-name='+share,'--parameter-name=path'],
        stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,universal_newlines=True,timeout=10)
    if p.returncode==0 and p.stdout.strip().startswith('/'):
        roots.append(os.path.join(p.stdout.strip(),*sub))
except (OSError,subprocess.TimeoutExpired):pass
roots.extend(os.path.join('/volume'+str(i),share,*sub) for i in range(1,17))
roots=[os.path.realpath(p) for p in roots if os.path.isdir(p)]
print('FUSIONLIVE_SSH_SETUP='+json.dumps({'python':sys.executable,
    'roots':list(dict.fromkeys(roots))},ensure_ascii=True))
'''


def install_script(key, share, parts):
    # Only a validated public key and base64 JSON enter the remote shell. No
    # password, private key or unquoted user-controlled path is interpolated.
    encoded = base64.b64encode(json.dumps({'share':share,'parts':parts},ensure_ascii=True).encode()).decode()
    code = DISCOVER.replace('PAYLOAD',repr(encoded))
    loader = 'import base64;exec(base64.b64decode('+repr(base64.b64encode(code.encode()).decode())+'))'
    key_line = 'restrict '+key+' fusionlive-nas-local-hash'
    return '\n'.join([
        'set -eu', 'umask 077',
        'if [ -L "$HOME/.ssh" ] || [ -L "$HOME/.ssh/authorized_keys" ]; then echo "SSH目录存在链接，未修改" >&2; exit 2; fi',
        'mkdir -p "$HOME/.ssh"', 'chmod 700 "$HOME/.ssh"',
        'touch "$HOME/.ssh/authorized_keys"', 'chmod 600 "$HOME/.ssh/authorized_keys"',
        'grep -qF -- '+shlex.quote(key)+' "$HOME/.ssh/authorized_keys" || printf "%s\\n" '+shlex.quote(key_line)+' >> "$HOME/.ssh/authorized_keys"',
        'if ! command -v python3 >/dev/null 2>&1; then echo "NAS未找到Python3，公钥已登记，上传配置未改变。" >&2; exit 3; fi',
        'python3 -c '+shlex.quote(loader), ''
    ])


def first_login(host, port, username, key, known_hosts, script):
    # Inherit the real console's stdin. Sending the installer through a PIPE
    # can prevent interactive password entry in Windows-hosted SSH sessions.
    # The installer contains only public data and is quoted as a remote command.
    command = ['ssh','-T','-o','StrictHostKeyChecking=ask','-o','BatchMode=no',
        '-o','UserKnownHostsFile='+str(known_hosts),'-o','ConnectTimeout=10',
        '-o','IdentitiesOnly=yes','-o','NumberOfPasswordPrompts=1',
        '-i',str(key),'-p',str(port),'-l',username,host,'sh -c '+shlex.quote(script)]
    result = subprocess.run(command,text=True,encoding='utf-8',stdout=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError('首次SSH登录或NAS环境检查未通过；上传配置未改变。请查看终端中的SSH提示。')
    marker = 'FUSIONLIVE_SSH_SETUP='
    lines = [line[len(marker):] for line in result.stdout.splitlines() if line.startswith(marker)]
    if len(lines) != 1:
        raise RuntimeError('未收到NAS环境检查结果；上传配置未改变。')
    info = json.loads(lines[0])
    if not isinstance(info,dict) or not isinstance(info.get('roots'),list) or not isinstance(info.get('python'),str):
        raise RuntimeError('NAS环境检查结果无效。')
    return info


def save_config(path, original, settings):
    if path.read_bytes() != original:
        raise RuntimeError('配置在向导运行期间已被其他程序修改，未覆盖，请重新运行。')
    data = json.loads(original.decode('utf-8-sig'))
    data['remote_hash'] = settings
    data['verification_method'] = 'ssh'
    backup = path.with_name('config.before-ssh-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json.bak')
    backup.write_bytes(original)
    temporary = path.with_name('config.ssh-setup.tmp')
    temporary.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    os.replace(temporary,path)
    return backup


def main(argv=None):
    parser = argparse.ArgumentParser(description='在实际上传服务器上配置NAS SSH本机校验。')
    parser.add_argument('--config',type=Path,default=Path(__file__).with_name('config.json'))
    args = parser.parse_args(argv)
    if not sys.stdin.isatty():
        raise RuntimeError('请在服务器终端或双击配置CMD运行，以便安全输入用户名、密码和主机确认。')
    if not shutil.which('ssh') or not shutil.which('ssh-keygen'):
        raise RuntimeError('服务器缺少Windows OpenSSH客户端，请先安装该可选功能。')
    path = args.config.resolve()
    original = path.read_bytes()
    raw = json.loads(original.decode('utf-8-sig'))
    host, share, parts = target_parts(raw['target_root'])
    prior = raw.get('remote_hash') or {}
    default_user = prior.get('username') or 'windsky'
    user = input('NAS SSH用户名 ['+default_user+']：').strip() or default_user
    port_text = input('SSH端口 ['+str(prior.get('port',22))+']：').strip()
    port = int(port_text or prior.get('port',22))
    # Validate endpoint before generating files or issuing remote commands.
    RemoteHashSettings.parse(dict(enabled=True,host=host,username=user,port=port,
                                  root_path='/configuration-probe'),path.parent)
    print('将在这台上传服务器生成专用密钥；密码由SSH直接在终端提示输入，不保存密码。',flush=True)
    folder = path.parent/'runtime/ssh'
    key, public = prepare_key(folder)
    known = folder/'known_hosts'
    print('连接NAS：'+user+'@'+host+':'+str(port)+'。首次请核对主机指纹并确认。',flush=True)
    print('出现password提示后输入NAS密码并按回车；输入时不显示字符或星号，这是正常现象。',flush=True)
    info = first_login(host,port,user,key,known,install_script(public,share,parts))
    roots = info['roots']
    if not roots:
        entered = input('未自动定位目录，请输入NAS内部实际目标路径（留空退出）：').strip()
        if entered:roots=[entered]
    selected = None
    for root in roots:
        candidate = dict(enabled=True,host=host,port=port,username=user,root_path=root,
            identity_file=key.relative_to(path.parent).as_posix(),
            known_hosts_file=known.relative_to(path.parent).as_posix(),
            python_command=info['python'],timeout_seconds=3600,fallback_to_smb=False)
        cfg = RemoteHashSettings.parse(candidate,path.parent)
        worker = RemoteHasher(cfg,Path(raw['target_root']))
        try:
            print('检查密钥认证及目录映射：'+root,flush=True)
            worker.start()
            selected=candidate
            break
        except (RemoteHashError,OSError) as exc:
            print('该目录未通过验证：'+str(exc),flush=True)
        finally:
            worker.close()
    if selected is None:
        raise RuntimeError('密钥认证或目录映射未通过，未启用远端校验，原上传配置保留。')
    backup = save_config(path,original,selected)
    print('配置完成：NAS本机SHA256已启用，密码未保存。原配置备份：'+str(backup),flush=True)
    print('从原入口重新启动上传工具即可；请保留本服务器的runtime/ssh和runtime/upload.sqlite3。',flush=True)
    return 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError,subprocess.SubprocessError) as exc:
        print('配置未完成：'+str(exc),file=sys.stderr)
        raise SystemExit(1)
