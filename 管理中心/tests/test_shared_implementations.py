"""Shared behavior is tested by its owner; flag implementation drift here."""
import ast
from pathlib import Path
import pytest

@pytest.mark.parametrize('module',['business_type','reporting','ledger_workbook','runtime_logging'])
def test_bundled_shared_implementation_matches_download_tool(module):
    tools=Path(__file__).resolve().parents[2]/'tools'
    local=tools/'资料解析及管理工具/app'/(module+'.py')
    owner=tools/'FusionLive同步工具/app'/(module+'.py')
    assert ast.dump(ast.parse(local.read_text(encoding='utf-8-sig')))==ast.dump(ast.parse(owner.read_text(encoding='utf-8-sig'))), f'{module}已发生差异，请统一实现或为差异补充测试'


@pytest.mark.parametrize('folder', ['FusionLive同步工具/app', '资料解析及管理工具/app',
                                   '绿联云上传工具', 'Ima知识库管理/ima_manager', 'codex-wechat/wechat_local'])
def test_portable_logging_policy_matches_centre(folder):
    root = Path(__file__).resolve().parents[2]
    owner = root/'管理中心/shared_services/log_policy.py'
    local = root/'tools'/folder/'log_policy.py'
    assert local.read_bytes() == owner.read_bytes()


def test_sender_protection_copies_match_after_tool_split():
    root = Path(__file__).resolve().parents[2]/'tools'
    assert (root/'codex-wechat/wechat_local/desktop_guard.py').read_bytes() == (root/'资料解析及管理工具/wechat_push/desktop_guard.py').read_bytes()


def test_analysis_sender_public_methods_use_protection(monkeypatch):
    root = Path(__file__).resolve().parents[2]/'tools/资料解析及管理工具'
    monkeypatch.syspath_prepend(str(root))
    from wechat_push import uia, desktop_guard
    calls = []
    monkeypatch.setattr(desktop_guard, 'protected_send', lambda *args, **kwargs: calls.append(args[1]))
    sender = object.__new__(uia.WindowsUIASender)
    sender._protected = True
    sender.send('fixture', 'daily')
    sender.send_image('fixture', 'briefing.png')
    assert calls == ['send', 'send_image']
