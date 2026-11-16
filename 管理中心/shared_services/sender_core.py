import os
from pathlib import Path

def find_control(root, predicate, max_depth=30):
    pending = [(root, 0)]
    while pending:
        node, depth = pending.pop(0)
        if predicate(node): return node
        if depth < max_depth:
            pending.extend((child, depth+1) for child in node.GetChildren())
    return None

def aid(control, name):
    return name in (control.AutomationId or '').split('.')

class SenderCore:
    def __init__(self, profile_path):
        if os.name != 'nt': raise RuntimeError('WeChat sending requires Windows')
        import uiautomation
        self.auto = uiautomation
        self.auto.InitializeUIAutomationInCurrentThread()
        self.auto.SetGlobalSearchTimeout(2)
        self.profile_path = Path(profile_path)
        self._protected = True

    def window(self):
        import psutil
        windows = []
        for node in self.auto.GetRootControl().GetChildren():
            if node.ClassName not in ('Qt51514QWindowIcon', 'mmui::MainWindow'): continue
            if not any(label in (node.Name or '') for label in ('微信', 'Weixin')): continue
            if psutil.Process(node.ProcessId).name().lower() != 'weixin.exe': continue
            windows.append(node)
        if len(windows) != 1: raise self.delivery_error('Exactly one logged-in WeChat window is required')
        return windows[0]

    def input(self, root):
        return find_control(root, lambda c:aid(c, 'chat_input_field'))

