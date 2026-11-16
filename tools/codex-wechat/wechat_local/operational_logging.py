"""Daily, bounded UTF-8 diagnostics for both managed and standalone listeners."""
from datetime import datetime, timezone, timedelta
import logging
from pathlib import Path
import os
import sys
from .log_policy import DailyLog, redact, options


class DiagnosticFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        at=datetime.fromtimestamp(record.created,timezone(timedelta(hours=8)))
        return at.strftime(datefmt) if datefmt else at.isoformat(timespec='milliseconds')

    def format(self, record):
        text = super().format(record)
        return redact(text).replace('[已隐藏]', '[REDACTED]')


class DailyFileHandler(logging.Handler):
    def __init__(self, root):
        super().__init__()
        self.output = DailyLog(root, 'wechat', 'listener.log', captured=True)
        self.project_output = True
        self.warned = False

    def emit(self, record):
        try: self.output.write(self.format(record)+'\n')
        except Exception as exc:
            if not self.warned:
                print(f'微信日志写入失败：{type(exc).__name__}：{redact(exc)}；继续在窗口输出。', file=sys.stderr, flush=True)
                self.warned = True


def configure(config):
    base = Path(__file__).resolve().parents[1]
    state = Path(config.state_path)
    state = state if state.is_absolute() else base/state
    root = state.parent.parent if state.parent.name in ('data','state','runtime') else state.parent
    logger = logging.getLogger('wechat_local')
    if any(getattr(h,'project_output',False) for h in logger.handlers):
        return
    handler = DailyFileHandler(root)
    formatter=DiagnosticFormatter('[%(asctime)s] [%(levelname)s] [wechat PID=%(process)d run='+os.environ.get('PROJECT_TOOLS_RUN_ID','standalone')+'] %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    console=logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.project_output=True
    logger.addHandler(console)
    logger.propagate=False
    logger.setLevel(logging.DEBUG if options()['detailed'] else logging.INFO)
