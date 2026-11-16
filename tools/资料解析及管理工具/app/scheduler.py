from paths import HOME
import signal, sys, threading
from analysis_worker import ProgressLog
from runtime_logging import operation_log
from analysis_worker import worker_lock, resume_background_analysis, request_analysis_stop, wait_for_analysis_exit
from briefing_schedule import run_daily_briefing

def main():
    from log_policy import utf8_streams
    utf8_streams()
    log = ProgressLog(operation_log('scheduler.log', HOME), daily_base=HOME)
    stop = HOME / 'runtime/publication.stop'
    stop.parent.mkdir(parents=True, exist_ok=True)
    if '--stop' in sys.argv:
        stop.touch()
        request_analysis_stop(HOME)
        return 0
    exiting = threading.Event()
    def shutdown(*_):
        stop.touch()
        exiting.set()
    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    with worker_lock(HOME / 'runtime/publication_scheduler') as acquired:
        if not acquired:
            raise RuntimeError('解析调度已在运行')
        stop.unlink(missing_ok=True)
        log('解析及图纸发送调度启动。')
        resume_background_analysis(HOME)
        def monitor():
            while not exiting.wait(.5):
                if stop.exists(): exiting.set()
        threading.Thread(target=monitor, daemon=True).start()
        from drawing_file_delivery import watch
        delivery_thread=threading.Thread(target=watch,args=(HOME,exiting,log),name='drawing-file-delivery')
        delivery_thread.start()
        try:
            run_daily_briefing(HOME, exiting, log)
        finally:
            exiting.set()
            delivery_thread.join()
            request_analysis_stop(HOME)
            wait_for_analysis_exit(HOME, log)
            log('解析及图纸发送调度已安全退出。')
    return 0
if __name__ == '__main__':
    raise SystemExit(main())
