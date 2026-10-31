"""One local publication job per process; timer threads never render or drive WeChat."""
from app_paths import HOME, CODE
from pathlib import Path
from datetime import datetime
import faulthandler
import json
import os
import subprocess
import sys
import threading
import time
import uuid


class PublicationJob:
    def __init__(self, base, kind, payload):
        self.folder = Path(base) / 'runtime/publication_jobs' / uuid.uuid4().hex
        self.folder.mkdir(parents=True)
        self.output = self.folder / 'result.json'
        request = self.folder / 'request.json'
        request.write_text(json.dumps(dict(base=str(base), kind=kind, **payload), ensure_ascii=False), encoding='utf-8')
        self.log = (self.folder / 'worker.log').open('a', encoding='utf-8')
        env = {k: v for k, v in os.environ.items() if k not in ('FUSIONLIVE_USERNAME', 'FUSIONLIVE_PASSWORD')}
        env.update(PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        try:
            self.child = subprocess.Popen([sys.executable, '-B', str(CODE/'publication_worker.py'), str(request)],
                cwd=base, stdin=subprocess.DEVNULL, stdout=self.log, stderr=subprocess.STDOUT,
                env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        except BaseException:
            self.log.close()
            raise
        self.kind = kind

    def poll(self):
        if self.child.poll() is None:
            return None
        self.log.close()
        try:
            return json.loads(self.output.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return dict(error='发布子进程异常结束，日志：' + str(self.folder/'worker.log'), not_sent=False)

    def close(self, log=print):
        if self.child.poll() is None:
            if self.kind not in ('delivery', 'recovery'):
                self.child.terminate()
                try:
                    self.child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.child.kill()
                    self.child.wait(timeout=5)
            else:
                # A sender may have submitted a UI action: preserve its journal.
                announced = 0
                while self.child.poll() is None:
                    if time.monotonic() >= announced:
                        label = '迁移恢复' if self.kind == 'recovery' else '微信投递'
                        log(f'等待{label}子进程保存记录，PID={self.child.pid}；日志：{self.folder / "worker.log"}')
                        announced = time.monotonic() + 30
                    time.sleep(.2)
        self.log.close()


def main(request):
    request = Path(request)
    job = json.loads(request.read_text(encoding='utf-8'))
    base = Path(job['base'])
    exiting = base / 'runtime/scheduler.exit.request'
    done = threading.Event()
    def diagnostics():
        while not done.wait(.5):
            if exiting.exists() and not done.wait(30):
                with (request.parent/'stop-stack.log').open('a') as stream:
                    faulthandler.dump_traceback(file=stream, all_threads=True)
                return
    threading.Thread(target=diagnostics, daemon=True).start()
    try:
        if exiting.exists():
            result = dict(error='已请求停止，未执行发布任务。', not_sent=True)
        elif job['kind'] == 'notice':
            from briefing import generate_pending_notice
            generate_pending_notice(Path(job['output']), job['day'], job['title'])
            result = dict(result=True)
        elif job['kind'] == 'recovery':
            from migration_recovery import recover
            result = dict(result=recover(base, cancelled=exiting.exists))
        else:
            from wechat_delivery import deliver
            from wechat_push.errors import DeliveryNotSent
            try:
                value = deliver(base, Path(job['marker']), job['settings'], datetime.fromisoformat(job['at']),
                                cancelled=exiting.exists)
                result = dict(result=value)
            except DeliveryNotSent as exc:
                result = dict(error=str(exc), not_sent=True)
    except Exception as exc:
        result = dict(error=type(exc).__name__ + ': ' + str(exc), not_sent=False)
    finally:
        done.set()
    temporary = request.parent/'result.json.tmp'
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, request.parent/'result.json')


if __name__ == '__main__':
    main(sys.argv[1])
