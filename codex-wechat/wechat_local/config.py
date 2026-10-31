from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class WechatChat(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=128, pattern=r'^[a-zA-Z0-9_-]+$')
    name: str = Field(min_length=1, max_length=200)
    kind: Literal['private', 'group']
    reply: bool = False
    sender_aliases: dict[str, str] = Field(default_factory=dict)
    username: str = Field(default='', max_length=200, pattern=r'^[a-zA-Z0-9_@.\-]*$')
    persona: str = '你是一位友好、准确、简洁的聊天助手。'
    sync_sources: list[str] = Field(default_factory=list)


class SyncSource(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, pattern=r'^[a-zA-Z0-9_-]+$')
    root: str = Field(min_length=1)
    projects: list[str] = Field(min_length=1)


class LocalConfig(BaseModel):
    model_config = ConfigDict(extra='forbid')
    bot_id: str = 'wechat-codex'
    account_name: str = ''
    chats: list[WechatChat] = Field(default_factory=list)
    database_root: str = 'D:/xwechat_files'
    database_account: str = ''
    database_cache: str = 'data/wechat-db'
    uia_profile: str = 'data/wechat-uia-profile.json'
    initial_history: int = Field(50, ge=0, le=1000)
    poll_seconds: float = Field(1, ge=0.5, le=60)
    allow_send: bool = True
    file_send_enabled: bool = True
    file_send_max_mib: int = Field(900, ge=1, le=900)
    file_send_cleanup_days: int = Field(2, ge=0, le=365)
    send_max_attempts: int = Field(3, ge=1, le=10)
    send_retry_seconds: float = Field(5, ge=1, le=300)
    mention_reply_sender: bool = True
    query_progress_enabled: bool = True
    chat_memory_enabled: bool = True
    reply_target_chars: int = Field(600, ge=50, le=10000)
    codex_enabled: bool = True
    codex_command: list[str] = Field(default_factory=lambda: ['codex'])
    codex_model: str = ''
    codex_backend: Literal['app-server', 'exec'] = 'app-server'
    codex_session_rotate_percent: int = Field(70, ge=1, le=99)
    codex_session_prewarm: bool = True
    codex_session_prewarm_percent: int = Field(55, ge=1, le=98)
    codex_context_window_tokens: int = Field(0, ge=0, le=10000000)
    codex_session_unknown_max_turns: int = Field(20, ge=2, le=500)
    codex_transport: Literal['auto', 'websocket', 'https'] = 'auto'
    codex_auto_access_dirs: bool = True
    codex_access_dirs: list[str] = Field(default_factory=list)
    _config_dir: Path | None = PrivateAttr(default=None)
    codex_timeout_seconds: int = Field(600, ge=5, le=1800)
    codex_idle_timeout_seconds: int = Field(300, ge=0, le=1800)
    reply_max_age_seconds: int = Field(900, ge=10, le=3600)
    max_reply_chars: int = Field(2000, ge=50, le=10000)
    recent_messages: int = Field(6, ge=0, le=50)
    state_path: str = 'data/listener.sqlite3'
    workspaces: str = 'data/chats'
    sync_sources: list[SyncSource] = Field(default_factory=list)

    @model_validator(mode='after')
    def unique_chats(self):
        if len({c.id for c in self.chats}) != len(self.chats):
            raise ValueError('Chat IDs must be unique')
        if not self.codex_command or any(not part for part in self.codex_command):
            raise ValueError('codex_command must contain an executable')
        if any(not path.strip() for path in self.codex_access_dirs):
            raise ValueError('可访问目录不能为空字符串')
        sources = {s.id for s in self.sync_sources}
        if len(sources) != len(self.sync_sources):
            raise ValueError('Sync source IDs must be unique')
        if any(set(c.sync_sources) - sources for c in self.chats):
            raise ValueError('Chat references an unknown sync source')
        if self.codex_session_prewarm and self.codex_session_prewarm_percent >= self.codex_session_rotate_percent:
            raise ValueError('提前创建会话的比例必须小于会话轮换比例')
        return self


def load_config(path):
    path = Path(path).resolve()
    config = LocalConfig.model_validate_json(path.read_text(encoding='utf-8-sig'))
    from .codex_settings import access_directories
    config._config_dir = path.parent
    config.codex_access_dirs = access_directories(path.parent,
        None if config.codex_auto_access_dirs else config.codex_access_dirs)
    for key in ('database_cache', 'uia_profile', 'state_path', 'workspaces'):
        value = Path(getattr(config, key))
        setattr(config, key, str((path.parent / value).resolve()))
    if config.database_root:
        config.database_root = str((path.parent / config.database_root).resolve())
    for source in config.sync_sources:
        source.root = str((path.parent / source.root).resolve())
    return config
