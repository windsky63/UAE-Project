from pathlib import Path
import os,shutil,subprocess,sys
import pytest

ROOT=Path(__file__).resolve().parents[2]

@pytest.mark.parametrize('stale_rules', [False, True])
def test_analysis_queue_uses_installed_rules_with_external_data_home(tmp_path, stale_rules):
    target = tmp_path / 'parser'
    shutil.copytree(ROOT/'tools/资料解析及管理工具', target,
        ignore=shutil.ignore_patterns('tests','runtime','logs','downloads','history','exports',
                                     '__pycache__','.pytest_cache'))
    data = tmp_path / 'external-data'
    data.mkdir()
    if stale_rules:
        (data/'material_split_rules.json').write_text('invalid stale data-side rules')
        (data/'document_coding_rules.json').write_text('invalid stale data-side rules')
    env = {k:v for k,v in os.environ.items()
           if k not in ('PROJECT_MANAGER_HOME','PROJECT_DATA_HOME','PYTHONPATH')}
    env.update(PYTHONUTF8='1', PROJECT_DATA_HOME=str(data),
               PYTHONPATH=os.pathsep.join([str(target/'app'),str(target)]))
    code = '''
from paths import HOME, CODE
from parse_idf_to_json import PIPE_SPLIT_RULES_PATH, load_material_split_rules
from analysis_worker import AnalysisQueue, parser_version
assert HOME != CODE.parent
assert PIPE_SPLIT_RULES_PATH == CODE.parent/'material_split_rules.json'
assert load_material_split_rules()
queue = AnalysisQueue(HOME/'runtime/ledger.sqlite3', base_dir=HOME)
assert queue.version == parser_version()
queue.db.close()
print(parser_version())
'''
    external = subprocess.run([sys.executable,'-c',code],cwd=target,env=env,
                              check=True,capture_output=True)
    env.pop('PROJECT_DATA_HOME')
    local = subprocess.run([sys.executable,'-c',
                            'from analysis_worker import parser_version; print(parser_version())'],
                           cwd=target,env=env,check=True,capture_output=True)
    assert external.stdout == local.stdout

@pytest.mark.parametrize('tool,code',[
 ('资料解析及管理工具','from drawing_briefing import ima_scope;import snapshot_service;from runtime_logging import operation_log;from pathlib import Path;assert ima_scope();assert operation_log("analysis.log",Path.cwd()).parent.name=="analysis"'),
 ('Ima知识库管理','from ima_manager.collection_rules import responsibility_scope,drawing_types;assert drawing_types();assert responsibility_scope()'),
 ('codex-wechat','from pathlib import Path;from wechat_local.sync_files import business_classifier,archive_index;assert business_classifier(Path.cwd()).load_rules();archive_index(Path.cwd())'),
])
def test_tool_imports_and_reads_rules_without_manager_or_siblings(tmp_path,tool,code):
    target=tmp_path/'isolated-tool'
    shutil.copytree(ROOT/'tools'/tool,target,ignore=shutil.ignore_patterns('tests','runtime','logs','downloads','history','exports','releases','__pycache__','.pytest_cache','data'))
    env={k:v for k,v in os.environ.items() if k not in ('PROJECT_MANAGER_HOME','PROJECT_DATA_HOME','PYTHONPATH')}
    env.update(PYTHONUTF8='1',PYTHONPATH=os.pathsep.join([str(target/'app'),str(target)]))
    subprocess.run([sys.executable,'-c',code],cwd=target,env=env,check=True,capture_output=True)


@pytest.mark.parametrize('tool,code',[
 ('Ima知识库管理','from ima_manager.database_source import sync_module;module=sync_module("business_type");assert str(module.__file__).startswith(os.environ["PROJECT_MANAGER_HOME"])'),
 ('codex-wechat','from pathlib import Path;from wechat_local.sync_files import business_classifier;module=business_classifier(Path.cwd());assert str(module.__file__).startswith(os.environ["PROJECT_MANAGER_HOME"])'),
])
def test_managed_tools_use_centre_reading_implementation(tmp_path,tool,code):
    env={**os.environ,'PROJECT_MANAGER_HOME':str(ROOT/'管理中心'),'PYTHONPATH':str(ROOT/'tools'/tool)}
    subprocess.run([sys.executable,'-c','import os;'+code],cwd=tmp_path,env=env,check=True,capture_output=True)

@pytest.mark.parametrize('tool,code',[
 ('Ima知识库管理','from ima_manager.database_source import sync_module;sync_module("business_type")'),
 ('codex-wechat','from pathlib import Path;from wechat_local.sync_files import business_classifier;business_classifier(Path.cwd())'),
])
def test_missing_centre_does_not_fall_back_to_tool_copy(tmp_path,tool,code):
    env={**os.environ,'PROJECT_MANAGER_HOME':str(tmp_path/'missing-centre'),'PYTHONPATH':str(ROOT/'tools'/tool)}
    result=subprocess.run([sys.executable,'-c',code],cwd=tmp_path,env=env,capture_output=True)
    assert result.returncode!=0
