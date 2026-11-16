from pathlib import Path
import os,json,subprocess,sys

def test_managed_consumers_reload_scope_from_explicit_centre(tmp_path):
    root=Path(__file__).resolve().parents[2]
    centre=tmp_path/'centre';(centre/'config').mkdir(parents=True)
    (centre/'drawing_scope.py').write_bytes((root/'管理中心/drawing_scope.py').read_bytes())
    (centre/'shared_services').mkdir()
    (centre/'shared_services/log_policy.py').write_bytes((root/'管理中心/shared_services/log_policy.py').read_bytes())
    env={**os.environ,'PROJECT_MANAGER_HOME':str(centre),'PYTHONPATH':os.pathsep.join([str(root/'tools/资料解析及管理工具/app'),str(root/'tools/Ima知识库管理')])}
    code='from drawing_briefing import ima_scope;from ima_manager.collection_rules import responsibility_scope;assert ima_scope()==[responsibility_scope()];assert responsibility_scope()["units"]==["213"]'
    (centre/'config/drawing_scope.json').write_text(json.dumps(dict(disciplines=['管道'],units=['213'])),encoding='utf-8')
    subprocess.run([sys.executable,'-c',code],env=env,check=True)
