import logging

wxlog = logging.getLogger("wechat_local.wechat_db")
wxlog.addHandler(logging.NullHandler())
wxlog.propagate = False
