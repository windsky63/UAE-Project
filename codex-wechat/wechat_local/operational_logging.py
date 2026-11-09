"""Daily, bounded UTF-8 diagnostics for both managed and standalone listeners."""
from datetime import datetime, timezone, timedelta
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import re
import os
import sys


class DiagnosticFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        at=datetime.fromtimestamp(record.created,timezone(timedelta(hours=8)))
        return at.strftime(datefmt) if datefmt else at.isoformat(timespec='milliseconds')

    def format(self, record):
        text = super().format(record)
        return re.sub(r'''(?i)((?:["']?(?:password|api[_-]?key|access[_-]?token|refresh[_-]?token|authorization)["']?)\s*[:=]\s*)(?:"[^"\r\n]*"|'[^'\r\n]*'|Bearer\s+\S+|[^\s,;}]+)''',
                      r'\1[REDACTED]', text)


class DailyFileHandler(logging.Handler):
    def __init__(self, root):
        super().__init__()
        self.root, self.day, self.output = Path(root), None, None

    def emit(self, record):
        try:
            day = datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
            if day != self.day:
                if self.output:
                    self.output.close()
                path = self.root / day / 'wechat' / 'listener.log'
                path.parent.mkdir(parents=True, exist_ok=True)
                self.output = RotatingFileHandler(path, maxBytes=5*1024*1024, backupCount=4, encoding='utf-8')
                self.output.namer = lambda name: name.rsplit('.log.',1)[0]+'.'+name.rsplit('.log.',1)[1]+'.log'
                self.output.setFormatter(self.formatter)
                self.day = day
            self.output.emit(record)
        except Exception:
            self.handleError(record)

    def close(self):
        if self.output:
            self.output.close()
        super().close()


def configure(config):
    base = Path(__file__).resolve().parents[1]
    project = base.parent
    root = project / 'logs' if (project / '管理中心').is_dir() else Path(config.state_path).resolve().parent / 'logs'
    logger = logging.getLogger('wechat_local')
    if any(getattr(h,'project_output',False) for h in logger.handlers):
        return
    handler = DailyFileHandler(root)
    formatter=DiagnosticFormatter('[%(asctime)s] [%(levelname)s] [PID=%(process)d] %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    console=logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    console.project_output=True
    logger.addHandler(console)
    logger.propagate=False
    logger.setLevel(logging.INFO)
