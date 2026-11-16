"""Cross-sender protocol and failure cleanup; never intercept real input."""
import io
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from wechat_local import desktop_guard as guard
from wechat_local.delivery import DeliveryNotSent
from wechat_local.wechat_uia import WindowsUIASender


@pytest.fixture(autouse=True)
def isolated_pause(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, 'pause_file', lambda: tmp_path / 'paused.json')


def records(*states):
    return '\n'.join(guard.PREFIX + json.dumps(dict(state=state, detail='fixture')) for state in states)


@pytest.mark.parametrize('states,returncode,error', [
    (('not_sent',), 0, DeliveryNotSent),
    (('submitting', 'uncertain'), 0, RuntimeError),
    (('submitting',), 124, RuntimeError),
    (('protected',), 124, DeliveryNotSent),
    (('submitting', 'not_sent'), 0, RuntimeError),
])
def test_worker_failure_never_retries_ambiguous_submission(monkeypatch, states, returncode, error):
    process = SimpleNamespace(communicate=lambda *a, **kw: (records(*states), ''),
                              poll=lambda: returncode, returncode=returncode)
    monkeypatch.setattr(guard.subprocess, 'Popen', lambda *a, **kw: process)
    sender = SimpleNamespace(profile_path=Path('profile.json'))
    with pytest.raises(error) as caught:
        guard.protected_send(sender, 'send', 'test', 'text', not_sent=DeliveryNotSent)
    if error is RuntimeError:
        assert type(caught.value) is RuntimeError


def test_timed_out_worker_is_killed_and_classified_from_submit_marker(monkeypatch):
    class Process:
        returncode = None
        killed = False
        def communicate(self, data=None, timeout=None):
            if data is not None:
                raise subprocess.TimeoutExpired('fixture', 1)
            return records('submitting'), ''
        def kill(self):
            self.killed = True
            self.returncode = 124
        def poll(self):
            return self.returncode
    process = Process()
    monkeypatch.setattr(guard.subprocess, 'Popen', lambda *a, **kw: process)
    with pytest.raises(RuntimeError, match='已进入提交阶段'):
        guard.protected_send(SimpleNamespace(profile_path=Path('profile')), 'send', 'test', 'text')
    assert process.killed


def test_failure_to_start_worker_is_known_not_sent(monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError('fixture')
    monkeypatch.setattr(guard.subprocess, 'Popen', unavailable)
    with pytest.raises(DeliveryNotSent):
        guard.protected_send(SimpleNamespace(profile_path=Path('profile')), 'send', 'test', 'text', not_sent=DeliveryNotSent)


def test_cleanup_requests_hook_release_even_when_body_raises():
    calls = []
    protection = guard.InputProtection('fixture')
    protection.thread = SimpleNamespace(join=lambda **kw: calls.append(protection.stop.is_set()))
    try:
        raise RuntimeError('send failed')
    except RuntimeError:
        protection.__exit__(None, None, None)
    assert calls == [True] and protection.stop.is_set()


def test_public_sender_uses_protection_with_mention(monkeypatch):
    sender = object.__new__(WindowsUIASender)
    sender._protected = True
    calls = []
    monkeypatch.setattr(guard, 'protected_send', lambda *a, **kw: calls.append((a[1], kw)))
    sender.send('test', 'text', mention='Alice')
    assert calls[0][0] == 'send' and calls[0][1]['mention'] == 'Alice'


def test_worker_protects_entire_operation_and_marks_before_submission(monkeypatch):
    events = []
    class Scope:
        mode = 'fixture'
        def __init__(self, name):
            self.name = name
        def __enter__(self):
            events.append('enter:' + self.name)
            return self
        def __exit__(self, *args):
            events.append('exit:' + self.name)
    class Sender:
        def __init__(self, profile):
            events.append('sender-created')
        def send(self, *a, **kw):
            assert self._desktop_worker
            events.append('open-chat-and-paste')
            self._before_submit()
            events.append('submit')
    request = dict(module='wechat_local.wechat_uia', profile='profile', operation='send', name='test', value='text')
    monkeypatch.setattr(guard.sys, 'stdin', io.StringIO(json.dumps(request)))
    monkeypatch.setattr(guard.importlib, 'import_module', lambda _: SimpleNamespace(WindowsUIASender=Sender))
    monkeypatch.setattr(guard, 'DesktopMutex', lambda: Scope('mutex'))
    monkeypatch.setattr(guard, 'InputProtection', lambda _: Scope('input'))
    monkeypatch.setattr(guard, 'emit', lambda state, **kw: events.append(state))
    guard.worker()
    assert events.index('enter:mutex') < events.index('enter:input') < events.index('sender-created')
    assert events.index('open-chat-and-paste') < events.index('submitting') < events.index('submit')
    assert events.index('submit') < events.index('exit:input') < events.index('exit:mutex') < events.index('success')






def test_automation_modifier_release_does_not_erase_physical_hotkey():
    shortcut = guard.EmergencyShortcut()
    shortcut.feed(0xA2, True)
    shortcut.feed(0x11, True, injected=True)
    shortcut.feed(0x11, False, injected=True)
    shortcut.feed(0xA0, True)
    assert shortcut.feed(0x7B, True)


def test_releasing_one_control_does_not_erase_other_control():
    shortcut = guard.EmergencyShortcut()
    shortcut.feed(0xA2, True)
    shortcut.feed(0xA3, True)
    shortcut.feed(0xA2, False)
    shortcut.feed(0xA0, True)
    assert shortcut.feed(0x7B, True)






@pytest.mark.parametrize('ctrl,shift', [(0x11, 0x10), (0xA2, 0xA0), (0xA3, 0xA1)])
def test_emergency_shortcut_tracks_suppressed_modifier_keys(ctrl, shift):
    shortcut = guard.EmergencyShortcut()
    assert not shortcut.feed(0x7B, True)
    assert not shortcut.feed(ctrl, True)
    assert not shortcut.feed(0x7B, True)
    assert not shortcut.feed(shift, True)
    assert not shortcut.feed(0x7B, False)
    assert shortcut.feed(0x7B, True)
    shortcut.feed(ctrl, False)
    assert not shortcut.feed(0x7B, True)


def test_emergency_shortcut_handles_modifiers_held_before_protection():
    assert guard.EmergencyShortcut(ctrl=True, shift=True).feed(0x7B, True)


def test_legacy_pause_is_ignored_and_optional_resume_cleans_it(monkeypatch):
    from contextlib import nullcontext
    guard.pause_file().write_text('{"reason":"emergency-hotkey"}')
    assert not guard.desktop_paused()
    guard.require_unpaused()
    locks = []
    monkeypatch.setattr(guard, 'DesktopMutex', lambda: (locks.append(True) or nullcontext()))
    guard.resume_desktop()
    assert locks and not guard.pause_file().exists()


@pytest.mark.parametrize('submitted,error', [(False, DeliveryNotSent), (True, RuntimeError)])
def test_emergency_cancellation_only_aborts_current_send_and_preserves_uncertainty(monkeypatch, submitted, error):
    states = ('submitting', 'emergency_cancelled') if submitted else ('emergency_cancelled',)
    processes = iter([
        SimpleNamespace(communicate=lambda *a, **kw: (records(*states), ''),poll=lambda:125,returncode=125),
        SimpleNamespace(communicate=lambda *a, **kw: (records('success'), ''),poll=lambda:0,returncode=0),
    ])
    launches = []
    monkeypatch.setattr(guard.subprocess, 'Popen', lambda *a, **kw: (launches.append(True) or next(processes)))
    sender = SimpleNamespace(profile_path=Path('profile'))
    with pytest.raises(error) as caught:
        guard.protected_send(sender, 'send', 'test', 'text', not_sent=DeliveryNotSent)
    assert type(caught.value) is error and not guard.desktop_paused()
    assert not guard.pause_file().exists()
    guard.protected_send(sender, 'send', 'test', 'next text', not_sent=DeliveryNotSent)
    assert len(launches) == 2


@pytest.mark.parametrize('emit_fails',[False,True])
def test_worker_emergency_exit_never_writes_a_pause_file(monkeypatch,emit_fails):
    def emit(*args,**kwargs):
        if emit_fails:raise OSError('broken output pipe')
    def exit_worker(code):raise SystemExit(code)
    monkeypatch.setattr(guard,'emit',emit)
    monkeypatch.setattr(guard.os,'_exit',exit_worker)
    with pytest.raises(SystemExit) as caught:guard.emergency_cancel()
    assert caught.value.code==125
    assert not guard.pause_file().exists()


def test_legacy_pause_file_does_not_block_a_new_worker(monkeypatch):
    guard.pause_file().write_text('{"reason":"emergency-hotkey"}')
    process=SimpleNamespace(communicate=lambda *a,**kw:(records('success'),''),poll=lambda:0,returncode=0)
    launches=[]
    monkeypatch.setattr(guard.subprocess,'Popen',lambda *a,**kw:(launches.append(True) or process))
    guard.protected_send(SimpleNamespace(profile_path=Path('profile')),'send','test','new text')
    assert len(launches)==1
