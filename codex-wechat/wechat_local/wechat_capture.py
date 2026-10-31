"""Local database capture integration; no external automation package at runtime."""
import contextlib
import hashlib
import importlib
import io
import json
import logging
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone

from wechat_local.delivery import DeliveryNotSent

def load_backend():
    return importlib.import_module("wechat_local.wechat_db.reader")


def resolve_database_account(config, backend):
    data_dir = config.database_root or backend.auto_detect_db_dir()
    if not data_dir:
        raise RuntimeError('未找到微信4.x数据目录。请确认微信已登录；若使用自定义存储位置，'
                           '请在 wechat.codex.json 设置 database_root 为账号文件夹的上级目录。')
    accounts = backend.list_accounts(data_dir)
    if not accounts:
        raise RuntimeError(f'微信数据目录 {data_dir} 中未找到含 db_storage 的账号文件夹。'
                           '请核对 database_root 和微信版本；当前采集器使用微信4.x数据库结构。')
    names = [entry['account'] for entry in accounts]
    account = config.database_account or (names[0] if len(names) == 1 else '')
    if not account:
        raise RuntimeError('发现多个微信账号目录，请在 wechat.codex.json 设置 database_account 为当前登录账号的目录名。'
                           '候选目录：' + json.dumps(names, ensure_ascii=False))
    if account not in names:
        raise RuntimeError('database_account 与本机账号目录不匹配。请填写完整目录名，而非昵称或显示微信号。'
                           '候选目录：' + json.dumps(names, ensure_ascii=False))
    return data_dir, account


class LocalDatabaseReader:
    def __init__(self, config):
        if os.name != "nt": raise RuntimeError("Local WeChat database reading requires Windows")
        backend = load_backend()
        cache = Path(config.database_cache).resolve()
        cache.mkdir(parents=True, exist_ok=True)
        self.cache = cache
        # Third-party diagnostics can include account paths or cipher material.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            data_dir, account = resolve_database_account(config, backend)

            class Database(backend.WeChatDB):
                def _stable_key_dirs(self): return [str(cache / 'keys')]
                def _try_other_accounts(self): return False
                def _select_account_by_keys(self, candidates): return False
                def _collect_db_files(self):
                    return [item for item in super()._collect_db_files()
                            if Path(item[1]).name == 'contact.db'
                            or re.fullmatch(r'message_\d+\.db', Path(item[1]).name)]
                def _warn_once(self, key, msg, *args):
                    raise RuntimeError("Incomplete WeChat shard read")

            self.db = Database(db_dir=data_dir, account=account,
                               workdir=str(cache / 'db-cache' / account))
            own = self.db.get_self_info()
        self.account_id, self.account_name = own.get('username', ''), own.get('nick_name', '')
        if not self.account_id or not self.account_name:
            raise RuntimeError("Cannot identify the logged-in account")

    def contacts(self, name):
        return self.db.search_contact(name)

    def members(self, username):
        return self.db.get_group_members(username)

    def catalog(self):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            conn = self.db._contact_conn()
            if conn is None: raise RuntimeError('Contact catalog unavailable')
            try:
                rows = conn.execute("SELECT username,nick_name,remark FROM contact ORDER BY username LIMIT 5000").fetchall()
            finally: conn.close()
        result = [{'username':'filehelper','name':'文件传输助手','kind':'private'}]
        for row in rows:
            user = row['username'] or ''
            name = row['remark'] or row['nick_name'] or user
            if not user or user in ('filehelper',self.account_id) or user.startswith('gh_'): continue
            if not re.fullmatch(r'[a-zA-Z0-9_@.\-]{1,200}',user) or len(name)>200: continue
            result.append({'username':user,'name':name,'kind':'group' if user.endswith('@chatroom') else 'private'})
        return result[:5000]

    def read(self, username, cursors, initial_limit):
        """Read each shard by monotonic local_id; ties across shards cannot skip rows."""
        def query(tables):
            checkpoints, result = dict(cursors or {}), []
            maps = self.db._shard_maps(tables)
            for conn, table in tables:
                shard = self.db._shard_label(conn)
                if not shard: raise RuntimeError("Missing shard identity")
                highest = int(conn.execute(f'SELECT coalesce(max(local_id),0) FROM {table}').fetchone()[0])
                previous = checkpoints.get(shard)
                if previous is not None and highest < previous:
                    raise RuntimeError("WeChat database was replaced; capture cursor needs review")
                if previous is None:
                    rows = conn.execute(f'SELECT *, ? AS shard FROM {table} ORDER BY local_id DESC LIMIT ?',
                                        (shard, initial_limit)).fetchall()
                    checkpoints[shard] = highest
                else:
                    rows = conn.execute(f'SELECT *, ? AS shard FROM {table} WHERE local_id>? ORDER BY local_id ASC LIMIT 500',
                                        (shard, previous)).fetchall()
                    checkpoints[shard] = max([previous] + [int(row['local_id']) for row in rows])
                result.extend(self.db._msg_row_to_dict(row, maps) for row in rows)
            result.sort(key=lambda row: (int(row.get('sort_seq') or 0), row['shard'], row['local_id']))
            return result, checkpoints
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            result = self.db._run_msg_query(username, query)
        return result if result is not None else ([], dict(cursors or {}))

    def recent(self, username):
        return self.db.get_messages(username, limit=30)


class LocalDatabaseDriver:
    capture_backend = "local_db"

    def __init__(self, config, reader=None, sender=None):
        self.config = config
        self.reader = reader or LocalDatabaseReader(config)
        self.account_id = self.reader.account_id
        self.account_name = self.reader.account_name
        if config.account_name and config.account_name != self.account_name:
            raise RuntimeError("WeChat account name does not match")
        self.started = time.time()
        self.sender = sender
        self.targets = {}
        self.seen_sources = set()
        binding_path = Path(config.database_cache) / 'desktop-binding.local.json'
        identity = {'bot_id': config.bot_id, 'account_id': self.account_id}
        if binding_path.exists() and json.loads(binding_path.read_text(encoding='utf-8')) != identity:
            raise RuntimeError("Use a new bot id, replica cache, and spool when changing accounts")
        binding_path.parent.mkdir(parents=True, exist_ok=True)
        binding_path.write_text(json.dumps(identity), encoding='utf-8')
        for chat in config.chats:
            if chat.username == 'filehelper' and chat.name == '文件传输助手' and chat.kind == 'private':
                self.targets[chat.id] = 'filehelper'
                continue
            candidates = [c for c in self.reader.contacts(chat.name)
                          if chat.name in (c.get('nick_name'), c.get('remark'))
                          and c['username'].endswith('@chatroom') == (chat.kind == 'group')]
            if chat.username: candidates = [c for c in candidates if c['username'] == chat.username]
            if len(candidates) != 1: raise RuntimeError("Chat name is missing or ambiguous; configure its username")
            self.targets[chat.id] = candidates[0]['username']

    def verify_identity(self):
        # Sender checks the live process before every action; reading never opens UI.
        if self.reader.account_id != self.account_id:
            raise RuntimeError("WeChat account changed")

    def catalog(self):
        return self.reader.catalog()

    def reconfigure(self, chats):
        config = self.config.model_validate({**self.config.model_dump(), 'chats':chats})
        replacement = LocalDatabaseDriver(config, self.reader, self.sender)
        self.config, self.targets = replacement.config, replacement.targets
        # Preserve existing progress; newly selected sources receive a historical baseline.

    def capture(self, chat_id, spool):
        target = self.targets[chat_id]
        source = hashlib.sha256(json.dumps([self.config.bot_id, self.account_id, chat_id, target]).encode()).hexdigest()
        cursors = spool.database_cursor(source)
        rows, updated = self.reader.read(target, cursors, self.config.initial_history)
        first = source not in self.seen_sources
        events = []
        for row in rows:
            native = row.get('sender_username') or ''
            timestamp = float(row.get('create_time') or 0)
            # The bundled reader returns display labels, while older readers used
            # native numeric codes. Normalize both at the integration boundary.
            content_type = {1:'text', 3:'image', 34:'voice', 49:'file',
                            '文本':'text', '图片':'image', '语音':'voice',
                            '文件/链接/卡片':'file'}.get(row.get('type'), 'other')
            direction = 'outgoing' if native == self.account_id else 'incoming' if native else 'system'
            identifier = hashlib.sha256(json.dumps([source, row['shard'], row['local_id']]).encode()).hexdigest()
            events.append({'id':identifier, 'chat_id':chat_id, 'sender':native or 'unknown',
                           'sender_id':native, 'direction':direction, 'content_type':content_type,
                           'text':str(row.get('content') or '')[:16000],
                           'historical':first or cursors is None or timestamp <= self.started,
                           'observed_at':datetime.fromtimestamp(timestamp, timezone.utc).isoformat()})
        spool.capture_database(source, events, updated)
        self.seen_sources.add(source)

    def mention_name(self, chat_id, username):
        chat = next(c for c in self.config.chats if c.id == chat_id)
        members = self.reader.members(self.targets[chat_id])
        matches = [m for m in members if m.get('username') == username]
        if len(matches) != 1:
            raise DeliveryNotSent('无法唯一确认需要 @ 的群成员')
        member = matches[0]
        name = chat.sender_aliases.get(username) or member.get('remark') or member.get('nick_name')
        if not name or name.strip() != name or len(name) > 200 or any(c in name for c in '\r\n\t'):
            raise DeliveryNotSent('需要 @ 的群成员显示名称不可用')
        if name in ('所有人', 'all', 'All'):
            raise DeliveryNotSent('禁止将提问人解析为 @所有人')
        if any(m.get('username') != username and name in (
                chat.sender_aliases.get(m.get('username')), m.get('remark'), m.get('nick_name')) for m in members):
            raise DeliveryNotSent('群成员显示名称重复，无法安全 @ 提问人')
        return name

    @staticmethod
    def message_matches(content, text):
        # WeChat uses U+2005 after native mentions; readers may normalize it.
        def normalize(value):
            text = str(value or '').replace('\r\n', '\n').replace('\u2005', ' ')
            # Qt inserts a separator before an inline native mention as well
            # as after it. Ignore only that recipient-header separator.
            return re.sub(r'(^回复对象：) +(?=@)', r'\1', text, flags=re.MULTILINE)
        return normalize(content) == normalize(text)

    def delivery_confirmed(self, chat_id, text, since, mention_user=None):
        from .delivery import mentioned_text
        self.verify_identity()
        if mention_user:
            text = mentioned_text(text, self.mention_name(chat_id, mention_user))
        rows = self.reader.recent(self.targets[chat_id])
        return any(m.get('sender_username') == self.account_id and self.message_matches(m.get('content'), text)
                   # WeChat timestamps have whole-second precision; the spool
                   # records a fractional time just before submitting the UI.
                   and float(m.get('create_time') or 0) >= int(since) for m in rows)

    def send(self, chat_id, text, mention_user=None):
        self.verify_identity()
        chat = next((c for c in self.config.chats if c.id == chat_id), None)
        if not self.config.allow_send or not chat or not chat.reply:
            raise DeliveryNotSent("Replies are disabled for this chat")
        # Exact display name must still resolve to this stable target before UI actions.
        matches = [c for c in self.reader.contacts(chat.name)
                   if chat.name in (c.get('nick_name'), c.get('remark'))]
        if len(matches) != 1 or matches[0]['username'] != self.targets[chat_id]:
            raise DeliveryNotSent("Chat name is ambiguous or its identity changed")
        mention = self.mention_name(chat_id, mention_user) if mention_user and chat.kind == 'group' else None
        if self.sender is None:
            from wechat_local.wechat_uia import WindowsUIASender
            self.sender = WindowsUIASender(self.config.uia_profile)
        started = time.time()
        before = {(m.get('shard'), m.get('local_id')) for m in self.reader.recent(self.targets[chat_id])}
        if mention:
            self.sender.send(chat.name, text, mention=mention)
            from .delivery import mentioned_text
            text = mentioned_text(text, mention)
        else:
            self.sender.send(chat.name, text)
        # Database writes may lag the UI. Check once and let the durable spool
        # reconcile on later ticks instead of blocking capture for eight seconds.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            rows = self.reader.recent(self.targets[chat_id])
        if any(m.get('sender_username') == self.account_id and self.message_matches(m.get('content'), text)
               and (m.get('shard'), m.get('local_id')) not in before
               and float(m.get('create_time') or 0) >= started-1 for m in rows): return
        raise RuntimeError("UI submitted; local DB confirmation unavailable")

    @staticmethod
    def file_matches(content, filename, size):
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(str(content or ''))
            app = root if root.tag == 'appmsg' else root.find('.//appmsg')
            return (app is not None and app.findtext('type') == '6'
                    and app.findtext('title') == filename
                    and int(app.findtext('appattach/totallen', '-1')) == size)
        except (ET.ParseError, ValueError, TypeError):
            return False

    def file_delivery_confirmed(self, chat_id, filename, size, since):
        self.verify_identity()
        return any(m.get('sender_username') == self.account_id
                   and self.file_matches(m.get('content'), filename, size)
                   and float(m.get('create_time') or 0) >= int(since)
                   for m in self.reader.recent(self.targets[chat_id]))

    def send_file(self, chat_id, path, expected_sha):
        from .file_transfer import verify_staged
        self.verify_identity()
        chat = next((c for c in self.config.chats if c.id == chat_id), None)
        if not self.config.allow_send or not self.config.file_send_enabled or not chat or not chat.reply:
            raise DeliveryNotSent('此聊天的文件发送未启用')
        matches = [c for c in self.reader.contacts(chat.name) if chat.name in (c.get('nick_name'), c.get('remark'))]
        if len(matches) != 1 or matches[0]['username'] != self.targets[chat_id]:
            raise DeliveryNotSent('聊天名称不唯一或身份发生变化')
        path = Path(path)
        if path.stat().st_size > self.config.file_send_max_mib * 1024**2:
            raise DeliveryNotSent('文件超过发送上限')
        try:
            verify_staged(path, expected_sha)
        except (OSError, ValueError):
            raise DeliveryNotSent('待发送文件校验失败') from None
        if self.sender is None:
            from .wechat_uia import WindowsUIASender
            self.sender = WindowsUIASender(self.config.uia_profile)
        started = time.time()
        before = {(m.get('shard'), m.get('local_id')) for m in self.reader.recent(self.targets[chat_id])}
        self.sender.send_file(chat.name, str(path), expected_sha=expected_sha)
        if any(m.get('sender_username') == self.account_id
               and self.file_matches(m.get('content'), path.name, path.stat().st_size)
               and (m.get('shard'), m.get('local_id')) not in before
               and float(m.get('create_time') or 0) >= int(started)
               for m in self.reader.recent(self.targets[chat_id])):
            return
        raise RuntimeError('文件已提交微信，等待本地消息记录确认；不会自动重发')
