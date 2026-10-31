import sys
from types import SimpleNamespace

import pytest

from wechat_local.delivery import DeliveryNotSent
from wechat_local.wechat_uia import WindowsUIASender, ui_error_detail


@pytest.fixture
def ui(monkeypatch):
    import wechat_local.wechat_uia as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    clipboard = ['original']
    monkeypatch.setitem(sys.modules, 'pyperclip', SimpleNamespace(
        paste=lambda: clipboard[0], copy=lambda value: clipboard.__setitem__(0, value)))
    class Edit:
        Name, value, cleared = 'Group', '', 0
        def __init__(self):
            self.keys, self.sent = [], []
        def Click(self):
            pass
        def SendKeys(self, text, **kwargs):
            self.keys.append(text)
            if text == '@':
                self.value += '@'
            elif text == '{Ctrl}v':
                self.value += clipboard[0]
            elif text == '{Enter}':
                self.sent.append(self.value)
                self.value = ''
            else:
                raise AssertionError('Member lookup must not refocus the chat editor')
        def GetValuePattern(self):
            def clear(value):
                self.cleared += 1
                self.value = value
            return SimpleNamespace(IsReadOnly=False, SetValue=clear)
    sender = object.__new__(WindowsUIASender)
    root, edit = SimpleNamespace(ProcessId=42), Edit()
    sender.open_chat = lambda _: (root, edit)
    sender.input = lambda _: edit
    typed = []
    sender.auto = SimpleNamespace(GetFocusedControl=lambda: SimpleNamespace(ProcessId=42),
                                  SendKeys=lambda text, **kwargs: typed.append(text))
    candidate = SimpleNamespace(Name='Alice', Click=lambda: setattr(edit, 'value', edit.value.split('@', 1)[0] + '@Alice\u2005'))
    sender.mention_items = lambda _: {(1,): candidate} if '@' in edit.value else {}
    monkeypatch.setattr(module, 'read_value', lambda control: control.value)
    return sender, root, edit, typed, clipboard, candidate


def test_member_popup_keeps_keyboard_focus_and_selects_native_mention(ui):
    sender, _, edit, typed, _, _ = ui
    sender.send('Group', 'answer', mention='Alice')
    assert typed == ['Alice']
    assert edit.keys == ['@', '{Ctrl}v', '{Enter}']
    assert edit.sent == ['@Alice\u2005answer']


def test_failure_after_at_can_retry_after_clearing_only_owned_partial_draft(ui):
    sender, _, edit, _, _, candidate = ui
    original = candidate.Click
    candidate.Click = lambda: (_ for _ in ()).throw(RuntimeError('popup changed'))
    with pytest.raises(DeliveryNotSent, match='选择@成员'):
        sender.send('Group', 'answer', mention='Alice')
    assert edit.value == '@' and not edit.sent
    candidate.Click = original
    sender.send('Group', 'answer', mention='Alice')
    assert edit.cleared == 1
    assert edit.sent == ['@Alice\u2005answer']


def test_user_edited_partial_draft_is_cleared_before_retry(ui):
    sender, _, edit, _, _, candidate = ui
    original = candidate.Click
    candidate.Click = lambda: (_ for _ in ()).throw(RuntimeError('popup changed'))
    with pytest.raises(DeliveryNotSent):
        sender.send('Group', 'answer', mention='Alice')
    edit.value = '@Alice user-owned draft'
    candidate.Click = original
    sender.send('Group', 'answer', mention='Alice')
    assert edit.sent == ['@Alice\u2005answer'] and edit.cleared == 1


def test_untracked_draft_does_not_block_later_tasks(ui):
    sender, _, edit, _, _, _ = ui
    edit.value = 'old unknown draft from an earlier process'
    sender.send('Group', 'new task')
    assert edit.sent == ['new task'] and edit.cleared == 1


def test_draft_is_not_cleared_if_target_chat_changed(ui):
    sender, _, edit, _, _, _ = ui
    edit.value = 'existing draft'
    sender.input = lambda _: SimpleNamespace(Name='Different chat')
    with pytest.raises(DeliveryNotSent, match='清空草稿前目标会话发生变化'):
        sender.send('Group', 'new task')
    assert edit.value == 'existing draft' and not edit.cleared and not edit.sent


def test_failure_to_clear_draft_never_appends_or_submits(ui):
    sender, _, edit, _, _, _ = ui
    edit.value = 'existing draft'
    edit.GetValuePattern = lambda: SimpleNamespace(IsReadOnly=True)
    with pytest.raises(DeliveryNotSent, match='草稿无法清空'):
        sender.send('Group', 'new task')
    assert edit.value == 'existing draft' and not edit.keys and not edit.sent


def test_focus_outside_wechat_stops_before_typing_member_name(ui):
    sender, _, edit, typed, _, _ = ui
    sender.auto.GetFocusedControl = lambda: SimpleNamespace(ProcessId=99)
    with pytest.raises(DeliveryNotSent, match='焦点或会话已变化'):
        sender.send('Group', 'answer', mention='Alice')
    assert typed == [] and edit.sent == []


def test_error_keeps_stage_api_and_numeric_code_without_private_window_text(ui):
    sender, _, _, _, _, candidate = ui
    error = type('error', (Exception,), {})
    candidate.Click = lambda: (_ for _ in ()).throw(error(0, 'SetForegroundWindow', 'PRIVATE_MEMBER_NAME'))
    with pytest.raises(DeliveryNotSent) as caught:
        sender.send('Group', 'answer', mention='Alice')
    text = str(caught.value)
    assert '选择@成员' in text and 'code=0' in text and 'API=SetForegroundWindow' in text
    assert 'PRIVATE_MEMBER_NAME' not in text


def test_already_foreground_window_is_not_reactivated(monkeypatch):
    import wechat_local.wechat_uia as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        IsIconic=lambda _: False, GetForegroundWindow=lambda: 42,
        SetForegroundWindow=lambda _: pytest.fail('Redundant foreground activation')))
    object.__new__(WindowsUIASender).activate(42)


def test_reported_foreground_error_is_accepted_only_if_window_became_active(monkeypatch):
    import wechat_local.wechat_uia as module
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    active = [0]
    def foreground(hwnd):
        active[0] = hwnd
        raise OSError('Windows reported failure after activation')
    monkeypatch.setitem(sys.modules, 'win32gui', SimpleNamespace(
        IsIconic=lambda _: False, GetForegroundWindow=lambda: active[0], SetForegroundWindow=foreground))
    object.__new__(WindowsUIASender).activate(42)


def test_disappearing_uia_control_does_not_abort_candidate_enumeration():
    class Broken:
        @property
        def AutomationId(self):
            raise RuntimeError('element no longer available')
    row = SimpleNamespace(AutomationId='', ControlTypeName='ListItemControl', IsOffscreen=False,
                          GetRuntimeId=lambda: [7], GetChildren=lambda: [])
    root = SimpleNamespace(AutomationId='', ControlTypeName='PaneControl',
                           GetChildren=lambda: [Broken(), row])
    assert WindowsUIASender.list_items(root) == {(7,): row}


def test_member_row_label_can_be_exposed_as_a_child_text_node():
    label = SimpleNamespace(ControlTypeName='TextControl', Name='Alice')
    row = SimpleNamespace(Name='', GetChildren=lambda: [label])
    assert WindowsUIASender.mention_matches(row, 'Alice')
    assert not WindowsUIASender.mention_matches(row, 'Bob')


def test_clipboard_restore_failure_does_not_override_a_submitted_send(ui, monkeypatch):
    sender, _, edit, _, clipboard, _ = ui
    counter = [0]
    def paste():
        counter[0] += 1
        if counter[0] > 1:
            raise RuntimeError('clipboard is busy')
        return clipboard[0]
    monkeypatch.setitem(sys.modules, 'pyperclip', SimpleNamespace(
        paste=paste, copy=lambda value: clipboard.__setitem__(0, value)))
    sender.send('Group', 'answer', mention='Alice')
    assert edit.sent == ['@Alice\u2005answer']


def test_native_mention_is_inserted_only_in_recipient_header(ui):
    sender, _, edit, _, clipboard, _ = ui
    text = '任务编号：12\n回复对象：@wxid-alice\n正在思考中……'
    sender.send('Group', text, mention='Alice')
    assert edit.sent == ['任务编号：12\n回复对象：@Alice\u2005\n正在思考中……']
    assert clipboard[0] == 'original'


def test_native_mention_can_be_exposed_as_embedded_qt_object(ui):
    sender, _, edit, _, clipboard, candidate = ui
    candidate.Click = lambda: setattr(edit, 'value', edit.value.split('@', 1)[0] + '\ufffc')
    sender.send('Group', '任务编号：12\n回复对象：@wxid-alice\n正在思考中……', mention='Alice')
    assert edit.sent == ['任务编号：12\n回复对象：\ufffc\n正在思考中……']
    assert clipboard[0] == 'original'


def test_abandoned_at_does_not_block_a_different_task_in_same_chat(ui):
    sender, _, edit, _, _, candidate = ui
    original = candidate.Click
    candidate.Click = lambda: (_ for _ in ()).throw(RuntimeError('popup changed'))
    with pytest.raises(DeliveryNotSent):
        sender.send('Group', 'first task', mention='Alice')
    candidate.Click = original
    sender.send('Group', 'second task', mention='Alice')
    assert edit.cleared == 1
    assert edit.sent == ['@Alice\u2005second task']
