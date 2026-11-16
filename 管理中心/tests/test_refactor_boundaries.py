from pathlib import Path
import os
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]

def test_download_handoff_never_starts_analysis(tmp_path):
    app = ROOT / 'tools/FusionLive同步工具/app'
    code = "import sys;sys.path.insert(0,sys.argv[1]);from download_handoff import notify_download_completed;notify_download_completed(sys.argv[2], 'ledger.sqlite3')"
    subprocess.run([sys.executable, '-c', code, str(app), str(tmp_path)], check=True)
    assert (tmp_path / 'runtime/download_completed.json').is_file()
    assert not (tmp_path / 'runtime/file_analysis').exists()

def test_managed_sender_uses_central_implementation():
    for folder, module in [('codex-wechat', 'wechat_local.wechat_uia'), ('资料解析及管理工具', 'wechat_push.uia')]:
        env = {**os.environ, 'PROJECT_MANAGER_HOME': str(ROOT / '管理中心'), 'PYTHONPATH': str(ROOT / 'tools' / folder)}
        code = f'from {module} import WindowsUIASender;assert WindowsUIASender.__module__.startswith("shared_services.")'
        subprocess.run([sys.executable, '-c', code], env=env, check=True)

def test_standalone_sender_uses_local_implementation():
    env = {k:v for k,v in os.environ.items() if k != 'PROJECT_MANAGER_HOME'}
    env['PYTHONPATH'] = str(ROOT / 'tools/资料解析及管理工具')
    subprocess.run([sys.executable, '-c', 'from wechat_push.uia import WindowsUIASender;assert WindowsUIASender.__module__ == "wechat_push.uia"'], env=env, check=True)


def test_managed_desktop_worker_can_resolve_central_sender():
    for folder,package,module in [('codex-wechat','wechat_local','shared_services.codex_sender'),('资料解析及管理工具','wechat_push','shared_services.publication_sender')]:
        env={**os.environ,'PROJECT_MANAGER_HOME':str(ROOT/'管理中心'),'PYTHONPATH':os.pathsep.join([str(ROOT/'tools'/folder),str(ROOT/'管理中心')])}
        code=f'from {package}.desktop_guard import ALLOWED_SENDERS;assert "{module}" in ALLOWED_SENDERS;from {module} import WindowsUIASender;assert hasattr(WindowsUIASender,"send_file")'
        subprocess.run([sys.executable,'-c',code],env=env,check=True)


def test_analysis_launcher_rejects_missing_data_directory_cleanly():
    result=subprocess.run([sys.executable,str(ROOT/'tools/资料解析及管理工具/main.py'),'briefing','--data-home'],capture_output=True)
    assert result.returncode==2
    assert b'Traceback' not in result.stderr
