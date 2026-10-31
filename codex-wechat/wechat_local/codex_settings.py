"""Shared Codex model transport and project directory settings."""
import json
import os
from pathlib import Path
import queue
import subprocess
import threading


class CodexConfigTimeoutError(TimeoutError):
    """The local app-server did not answer a configuration RPC."""


class CodexGenerationTimeoutError(TimeoutError):
    """Generation exceeded its configured subprocess time limit."""


class CodexIdleTimeoutError(TimeoutError):
    """No generation progress was received within the idle limit."""


def access_directories(base, configured=None):
    base = Path(base).resolve()
    if configured is None:
        paths = [p for p in base.parent.iterdir() if p.is_dir() and p.resolve() != base]
    else:
        if not isinstance(configured, list) or any(not isinstance(p, str) or not p.strip() for p in configured):
            raise ValueError('可访问目录必须为非空路径组成的列表')
        paths = [base / p for p in configured]
    return sorted({str(p.resolve()) for p in paths}, key=str.casefold)


def toml_value(value):
    if isinstance(value, dict):
        return '{' + ','.join(json.dumps(k, ensure_ascii=False)+'='+toml_value(v)
                              for k,v in value.items() if v is not None) + '}'
    if isinstance(value, list):
        return '[' + ','.join(toml_value(v) for v in value) + ']'
    return json.dumps(value, ensure_ascii=False)


def permission_arguments(access_dirs=None, workspace_write=True):
    """Read the selected folders; editing only grants writes to those folders."""
    arguments = []
    for name, access in (('project_tools_read', 'read'), ('project_tools_edit', 'write')):
        filesystem = {':root': 'read' if os.name == 'nt' else 'deny', ':minimal': 'read',
                      ':workspace_roots': {'.': 'write' if access=='write' and workspace_write else 'read'}}
        filesystem.update({str(Path(p).resolve()): access for p in access_dirs or []})
        profile = dict(extends=':read-only', filesystem=filesystem, network=dict(enabled=False))
        arguments += ['-c', 'permissions.'+name+'='+toml_value(profile)]
    return arguments + ['-c', 'default_permissions="project_tools_read"']


def read_codex_config(command, cwd):
    """Read effective CLI configuration through app-server, without a model turn."""
    flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
    process = subprocess.Popen([*command, 'app-server', '--listen', 'stdio://'], cwd=cwd,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, encoding='utf-8', errors='replace', creationflags=flags)
    messages = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                try: messages.put(json.loads(line))
                except ValueError: pass
        finally: messages.put(None)
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    def rpc(identity, method, params):
        process.stdin.write(json.dumps(dict(id=identity, method=method, params=params))+'\n')
        process.stdin.flush()
        while True:
            try: message = messages.get(timeout=15)
            except queue.Empty:
                raise CodexConfigTimeoutError(f'读取 Codex 配置超时：{method} 等待响应超过 15 秒') from None
            if message is None: raise RuntimeError('无法读取 Codex 配置，请检查本机 CLI 配置与登录')
            if message.get('id') == identity:
                if 'error' in message: raise RuntimeError('Codex 拒绝读取连接配置')
                return message.get('result', {})
    try:
        rpc(1, 'initialize', dict(clientInfo=dict(name='wechat_project_settings', version='1'),
                                 capabilities=dict(experimentalApi=True)))
        process.stdin.write('{"method":"initialized","params":{}}\n')
        process.stdin.flush()
        return rpc(2, 'config/read', dict(includeLayers=False, cwd=str(cwd))).get('config', {})
    finally:
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=2)
        process.stdin.close()
        process.stdout.close()


def transport_arguments(config, transport):
    overrides = transport_overrides(config, transport)
    return [part for key,value in overrides.get('config', {}).items()
            for part in ('-c', key+'='+toml_value(value))]


def transport_overrides(config, transport):
    """Per-thread provider capability override; never rewrite the user's Codex config."""
    if transport not in ('auto', 'websocket', 'https'):
        raise ValueError('不支持的连接方式')
    if transport == 'auto':
        return dict(modelProvider=config.get('model_provider') or 'openai')
    provider_id = config.get('model_provider') or 'openai'
    if provider_id == 'openai':
        # Built-in IDs cannot be overridden in CLI 0.164. A named OpenAI provider
        # retains first-party auth and auth-dependent endpoint selection.
        provider_id = 'project_tools_openai_transport'
        if provider_id in (config.get('model_providers') or {}):
            raise ValueError('Codex 配置中存在同名连接配置 project_tools_openai_transport')
        provider = dict(name='OpenAI', wire_api='responses', requires_openai_auth=True,
                        supports_standalone_web_search=True, include_internal_metadata=True,
                        env_http_headers={'OpenAI-Organization': 'OPENAI_ORGANIZATION',
                                          'OpenAI-Project': 'OPENAI_PROJECT'})
        base_url = config.get('openai_base_url') or os.environ.get('OPENAI_BASE_URL')
        if base_url:
            provider['base_url'] = base_url
    else:
        provider = dict((config.get('model_providers') or {}).get(provider_id) or {})
        if not provider:
            raise ValueError('当前模型服务不支持切换连接方式，请使用“跟随 Codex 默认”')
    provider['supports_websockets'] = transport == 'websocket'
    return dict(modelProvider=provider_id, config={
        'model_provider': provider_id,
        'model_providers': {provider_id: provider},
    })


