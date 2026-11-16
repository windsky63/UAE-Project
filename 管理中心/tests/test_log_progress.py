from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager
from log_progress import severity


class ProgressLogTests(unittest.TestCase):
    def test_unchanged_log_is_parsed_once_across_searches_and_updates_invalidate(self):
        from manager import tail
        self.path.write_text('[2026-11-12 11:00:00] ValueError: first\n', encoding='utf-8')
        with patch('manager.tail', wraps=tail) as read:
            self.assertEqual(len(self.manager.logs(level='ERROR', compact=False)['items']), 1)
            self.assertEqual(len(self.manager.logs(query='first', compact=False)['items']), 1)
            self.assertEqual(read.call_count, 1)
            self.path.write_text('[2026-11-12 11:00:01] ValueError: second\n', encoding='utf-8')
            rows = self.manager.logs(level='ERROR', compact=False)['items']
            self.assertIn('second', rows[0]['message'])
            self.assertEqual(read.call_count, 2)

    def test_analysis_start_notice_is_info_but_real_failures_remain_errors(self):
        notice = '启动后台解析；此文件保存标准错误，进度见 analysis.log。'
        self.assertEqual(severity(notice), 'INFO')
        self.assertEqual(severity('[2026-11-12T10:27:59+08:00] ' + notice), 'INFO')
        self.assertEqual(severity('[ERROR] ' + notice), 'ERROR')
        self.assertEqual(severity(notice + '\nTraceback (most recent call last):\nValueError: bad'), 'ERROR')
        self.assertEqual(severity('标准错误输出：ModuleNotFoundError: missing'), 'ERROR')
        self.path.write_text(notice + '\n' + notice + '\nTraceback (most recent call last):\n'
                             '  File "worker.py", line 1\nValueError: bad\n', encoding='utf-8')
        errors = self.manager.logs(level='ERROR', compact=False)['items']
        self.assertEqual(len(errors), 1)
        self.assertIn('ValueError: bad', errors[0]['message'])

    def test_real_child_output_has_timestamps_task_context_and_complete_traceback(self):
        import subprocess
        command=[sys.executable,'-u','-c',
                 'print("plain output",flush=True); raise ValueError("fixture failure")']
        with patch.object(self.manager,'command',return_value=(command,self.root)):
            task=self.manager.action('nas','check')
        self.manager.children[task['id']].wait(timeout=5)
        rows=self.manager.logs('nas',compact=False)['items']
        self.assertTrue(any('plain output' in row['message'] and row['message'].startswith('[') for row in rows))
        self.assertTrue(any('run='+task['id'] in row['message'] for row in rows))
        errors=[row for row in rows if row['level']=='ERROR' and 'fixture failure' in row['message']]
        self.assertEqual(len(errors),1)
        self.assertIn('Traceback',errors[0]['message'])
        self.assertIn('File "<string>"',errors[0]['message'])
        self.assertTrue(any('退出码=1' in row['message'] and row['level']=='ERROR' for row in rows))
        self.manager.tasks()

    def test_nas_heartbeats_merge_only_within_the_same_phase_number(self):
        self.path.write_text('\n'.join([
            '[2026-11-09 16:00:00] NAS处理心跳：阶段=本地预检；阶段序号=1；本阶段完成 2/10；当前文件：a.pdf',
            '[2026-11-09 16:00:30] NAS处理心跳：阶段=本地预检；阶段序号=1；本阶段完成 4/10；当前文件：b.pdf',
            '[2026-11-09 16:01:00] NAS处理心跳：阶段=NAS目录扫描；阶段序号=2；本阶段完成 12项（总数待扫描）',
            '[2026-11-09 16:01:30] NAS处理心跳：阶段=NAS目录扫描；阶段序号=3；本阶段完成 20项（总数待扫描）']),encoding='utf-8')
        rows=self.manager.logs()['items']
        self.assertEqual(len(rows),3)
        local=next(row for row in rows if row.get('progress',{}).get('phase')=='NAS/本地预检')
        self.assertEqual(local['progress']['updates'],2)
        self.assertEqual(local['progress']['current'],4)

    def test_prefixes_and_explicit_levels_keep_real_failures_visible(self):
        self.path.write_text('[2026-11-09 16:00:00] [解析 PID=10] [INFO] 开始文件：a.pdf\n'
            '[2026-11-09 16:00:01] [解析 PID=10] [INFO] 开始文件：b.pdf\n'
            '[2026-11-09 16:00:02] [ERROR] task failed\n',encoding='utf-8')
        rows=self.manager.logs()['items']
        self.assertEqual(len(rows),2)
        self.assertEqual(sum(row['level']=='ERROR' for row in rows),1)
        self.assertEqual(severity('待核实 0 条，冲突 0，失败 0'),'INFO')
        self.assertEqual(severity('任务输出结束：退出码=1'),'ERROR')
        self.assertEqual(severity('任务输出结束：退出码=2'),'WARNING')

    def test_traceback_search_and_error_filter_keep_location_and_cause(self):
        self.path.write_text('[2026-11-09 16:00:00] [ERROR] task failed\n'
            '[2026-11-09 16:00:00] Traceback (most recent call last):\n'
            '[2026-11-09 16:00:00]   File "worker.py", line 42, in run\n'
            '[2026-11-09 16:00:00]     raise ValueError("bad bytes")\n'
            '[2026-11-09 16:00:00] ValueError: bad bytes\n'
            '[2026-11-09 16:00:01] task finished\n',encoding='utf-8')
        rows=self.manager.logs(level='ERROR',query='worker.py')['items']
        self.assertEqual(len(rows),1)
        self.assertIn('line 42',rows[0]['message'])
        self.assertIn('ValueError: bad bytes',rows[0]['message'])
        self.assertIn('task failed',rows[0]['message'])

    def test_managed_capture_rotates_redacts_and_preserves_source_time(self):
        from log_tools import captured_line
        for i in range(50):
            captured_line(self.path,f'event {i}', '2026-11-09T16:00:00+08:00',max_bytes=256,backups=2)
        captured_line(self.path,'[2026-11-09 16:00:01] password=abc authorization=Bearer private-token',
                      '2026-11-09T17:00:00+08:00',max_bytes=256,backups=2)
        text=self.path.read_text(encoding='utf-8')
        self.assertNotIn('private-token',text)
        self.assertNotIn('password=abc',text)
        self.assertIn('[2026-11-09 16:00:01]',text)
        self.assertNotIn('17:00:00',text)
        self.assertLessEqual(len(list(self.path.parent.glob('progress*.log'))),3)

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manager=Manager(self.root,self.root/'admin/runtime')
        self.path=self.manager.runtime/'progress.log'

    def test_progress_updates_merge_and_errors_remain_individual(self):
        text='\n'.join('[2026-10-30 09:00:%02d] 阶段：文件下载；累计完成 %d/100 个'%(i,i) for i in range(30))
        text+='\n[2026-10-30 09:01:00] 下载失败：important.pdf\n[2026-10-30 09:02:00] WARNING disconnected\n'
        self.path.write_text(text,encoding='utf-8')
        rows=self.manager.logs()['items']
        self.assertEqual(len(rows),3)
        item=next(r for r in rows if r.get('progress'))
        self.assertEqual(item['progress']['updates'],30)
        self.assertEqual(item['progress']['current'],29)
        self.assertEqual(item['progress']['total'],100)
        self.assertEqual(self.manager.logs(compact=False)['total'],32)
        self.assertEqual(len(self.manager.logs(level='ERROR')['items']),1)
        self.assertEqual(self.path.read_text(encoding='utf-8'),text)

    def test_structured_progress_is_visible_and_stable_between_refreshes(self):
        self.path.write_text('@@PROGRESS {"phase":"文件下载","download_done_files":3,"download_total_files":10}\n',encoding='utf-8')
        first=self.manager.logs()['items'][0]
        with self.path.open('a',encoding='utf-8') as out:out.write('@@PROGRESS {"phase":"文件下载","download_done_files":4,"download_total_files":10}\n')
        last=self.manager.logs()['items'][0]
        self.assertEqual(first['id'],last['id'])
        self.assertEqual(last['progress']['current'],4)
        self.assertEqual(last['progress']['updates'],2)

    def test_search_finds_older_progress_before_collapsing(self):
        self.path.write_text('下载完成：target.pdf\n下载完成：other.pdf\n',encoding='utf-8')
        self.assertIn('target.pdf',self.manager.logs(query='target')['items'][0]['message'])

    def test_zero_failures_is_not_an_error_and_actual_conflict_is_visible(self):
        self.assertEqual(severity('本轮结束：失败 0，冲突 0'),'INFO')
        self.assertEqual(severity('本轮结束：失败 2，冲突 0'),'ERROR')
        self.assertEqual(severity('conflict：文件已变化'),'WARNING')
        self.assertEqual(severity('上传重试 1/3：file.pdf；OSError：connection reset'),'WARNING')

    def test_separate_runs_do_not_share_one_progress_row(self):
        self.path.write_text('下载完成：a.pdf\n本轮结束：complete\n下载完成：b.pdf\n',encoding='utf-8')
        self.assertEqual(sum(bool(x.get('progress')) for x in self.manager.logs()['items']),2)

    def test_each_line_keeps_its_own_timestamp(self):
        self.path.write_text('[2026-10-30 09:00:00] started\n[2026-10-30 09:01:00] finished\n',encoding='utf-8')
        rows=self.manager.logs()['items']
        self.assertIn('09:01:00',rows[0]['created_at'])
        self.assertIn('09:00:00',rows[1]['created_at'])

    def test_mirrored_nas_logs_merge_without_discarding_repeated_errors(self):
        folder=self.root/'logs/2026-11-02/nas'
        folder.mkdir(parents=True)
        error='[11-02 14:52:39] 上传失败：a.pdf；PermissionError：access denied\n'
        text=error+error+'[11-02 14:52:40] 正式目录旧副本已归档 1 个：a.pdf\n'
        paths=[folder/'run.log',folder/'2026-11-02-nas-start-abc.log']
        for path in paths:path.write_text(text,encoding='utf-8')
        result=self.manager.logs('nas',compact=False)
        self.assertEqual(result['total'],3)
        self.assertEqual(result['duplicates_merged'],3)
        self.assertEqual(sum(row['level']=='ERROR' for row in result['items']),2)
        self.assertTrue(all(len(row['duplicate_sources'])==2 for row in result['items']))
        self.assertEqual(self.manager.logs('nas',query='access denied')['total'],2)
        for path in paths:self.assertEqual(path.read_text(encoding='utf-8'),text)

    def test_legacy_nas_archival_lines_compact_as_progress(self):
        self.path.write_text('\n'.join(f'[11-02 14:52:{i:02d}] 正式目录旧副本已归档 1 个：file-{i}.pdf' for i in range(20)),encoding='utf-8')
        result=self.manager.logs()
        self.assertEqual(result['total'],1)
        self.assertEqual(result['items'][0]['progress']['updates'],20)

    def test_traceback_has_short_summary_and_expandable_original(self):
        self.path.write_text('[2026-11-15 12:00:00] Traceback (most recent call last):\n'
                             '  File "worker.py", line 42, in run\n'
                             'PermissionError: target is read only\n', encoding='utf-8')
        row = self.manager.logs(level='ERROR')['items'][0]
        self.assertEqual(row['summary'], 'PermissionError: target is read only')
        self.assertIn('line 42', row['diagnostic'])
        self.assertIn('Traceback', row['message'])

    def test_explicit_info_is_not_an_error_because_of_a_filename(self):
        self.assertEqual(severity('[INFO] 下载完成：error-report.pdf'), 'INFO')
        self.assertEqual(severity('[INFO] failed=0；当前文件 error.pdf'), 'INFO')
        self.assertEqual(severity('{"error": null, "failed": 0, "errors": []}'), 'INFO')
        self.assertEqual(severity('[CRITICAL] worker unavailable'), 'CRITICAL')
        from shared_services.log_policy import level as operation_level
        self.assertEqual(operation_level('下载完成：error-report.pdf'), 'INFO')
        self.assertEqual(operation_level('NAS处理心跳：当前文件：failed-report.pdf'), 'INFO')
        self.assertEqual(severity('[INFO] Traceback (most recent call last):\nValueError: bad'), 'ERROR')


def test_log_policy_standalone_ignores_nearby_manager_and_managed_uses_centre(tmp_path, monkeypatch):
    from shared_services import log_policy as policy
    import subprocess
    import os
    monkeypatch.delenv('PROJECT_MANAGER_HOME', raising=False)
    monkeypatch.delenv('PROJECT_LOG_ROOT', raising=False)
    (tmp_path/'管理中心').mkdir()
    tool = tmp_path/'tools/standalone'
    assert policy.log_path(tool, 'analysis', 'run.log').is_relative_to(tool/'logs')
    centre = Path(__file__).resolve().parents[1]
    monkeypatch.setenv('PROJECT_MANAGER_HOME', str(centre))
    monkeypatch.setenv('PROJECT_LOG_ROOT', str(tmp_path/'central-logs'))
    assert policy.log_path(tool, 'analysis', 'run.log').is_relative_to(tmp_path/'central-logs')
    local = centre.parent/'tools/资料解析及管理工具/app'
    result = subprocess.run([sys.executable, '-c',
        'import log_policy,inspect; print(inspect.getfile(log_policy.DailyLog))'],
        cwd=tmp_path, env={**os.environ, 'PYTHONPATH':str(local), 'PYTHONIOENCODING':'utf-8'},
        capture_output=True, text=True, encoding='utf-8', check=True)
    assert str(centre/'shared_services/log_policy.py') in result.stdout


def test_centre_policy_overrides_inherited_debug_and_validates_before_launch(tmp_path):
    from shared_services.log_policy import managed_environment
    import json
    (tmp_path/'config').mkdir()
    path = tmp_path/'config/logging.json'
    path.write_text(json.dumps(dict(progress_seconds=60, idle_seconds=600)), encoding='utf-8')
    env = dict(FUSIONLIVE_LOG_DETAIL='1', PROJECT_LOG_DETAILED='1')
    values = managed_environment(tmp_path, env)
    assert values['progress_seconds'] == 60
    assert env['PROJECT_LOG_IDLE_SECONDS'] == '600'
    assert env['PROJECT_LOG_DETAILED'] == env['FUSIONLIVE_LOG_DETAIL'] == '0'
    assert env['PROJECT_TOOLS_LOG_CAPTURED'] == '1'
    path.write_text('{"progress_seconds": 0}', encoding='utf-8')
    import pytest
    with pytest.raises(ValueError, match='progress_seconds'):
        managed_environment(tmp_path, {})


def test_daily_writer_rotates_by_beijing_date_and_keeps_utf8_bounds(tmp_path, monkeypatch):
    from shared_services import log_policy as policy
    from datetime import datetime
    monkeypatch.delenv('PROJECT_MANAGER_HOME', raising=False)
    monkeypatch.setenv('PROJECT_LOG_MAX_BYTES', '1024')
    monkeypatch.setenv('PROJECT_LOG_BACKUPS', '2')
    first = datetime(2026, 11, 15, 23, 59, tzinfo=policy.BEIJING)
    second = first.replace(day=16, hour=0, minute=0)
    output = policy.DailyLog(tmp_path, 'analysis', 'run.log')
    with patch.object(policy, 'datetime') as clock:
        clock.now.return_value = first
        for i in range(5): output.write('中文'*300+'\n')
        before = policy.log_path(tmp_path, 'analysis', 'run.log')
        clock.now.return_value = second
        output.write('新一天：正常\n')
        after = policy.log_path(tmp_path, 'analysis', 'run.log')
    assert before != after and before.exists() and after.exists()
    assert '新一天' not in before.read_text(encoding='utf-8')
    assert '新一天' in after.read_text(encoding='utf-8')
    files = list(before.parent.glob('*.log'))
    assert len(files) == 3
    assert all(p.stat().st_size <= 1024 and p.read_text(encoding='utf-8') for p in files)


def test_stream_redacts_console_and_disk_without_changing_stdout_json(tmp_path, monkeypatch):
    from shared_services.log_policy import StreamLog, DailyLog, log_path
    import io
    monkeypatch.delenv('PROJECT_MANAGER_HOME', raising=False)
    console = io.StringIO()
    stream = StreamLog(console, DailyLog(tmp_path, 'ima', 'run.log'), 'ima')
    stream.write('password=pri')
    stream.write('vate Cookie: a=secret; b=secret2\n')
    stream.write('Traceback (most recent call last):\n  File "x.py", line 5\nValueError: bad\n')
    stream.finish()
    for value in (console.getvalue(), log_path(tmp_path, 'ima', 'run.log').read_text(encoding='utf-8')):
        assert 'private' not in value and 'secret' not in value
        assert 'line 5' in value and 'ValueError: bad' in value
    stdout = io.StringIO()
    stream = StreamLog(stdout, DailyLog(tmp_path, 'ima', 'run.log'), 'ima')
    stream.write('{"files": []}\n')
    stream.finish()
    assert stdout.getvalue() == '{"files": []}\n'


def test_child_pipe_is_drained_with_bounded_progress_and_complete_errors():
    from shared_services.log_policy import capture_output
    import io, json
    lines = ['@@PROGRESS '+json.dumps(dict(phase='文件下载', download_done_files=i,
                                           download_total_files=100)) for i in range(101)]
    lines += ['@@PROGRESS '+json.dumps(dict(phase='文件下载',error='transfer lost')), 'Traceback (most recent call last):', '  File "download.py", line 9', 'OSError: file is locked']
    output = io.StringIO()
    reader = capture_output(io.BytesIO(('\n'.join(lines)+'\n').encode('utf-8')), output, 'sync')
    reader.join(timeout=2)
    assert not reader.is_alive()
    text = output.getvalue()
    assert text.count('] 阶段：文件下载') == 2
    assert 'transfer lost' in text
    assert '完成 100/100' in text and '@@PROGRESS' not in text
    assert 'line 9' in text and 'OSError: file is locked' in text


def test_log_read_budget_prioritizes_an_old_filename_match(tmp_path):
    import os
    manager = Manager(tmp_path, tmp_path/'runtime')
    for i in range(85):
        path = manager.runtime/f'entry-{i:03}.log'
        path.write_text(f'event {i}\n', encoding='utf-8')
        os.utime(path, (1000+i, 1000+i))
    result = manager.logs(compact=False)
    assert result['scanned_files'] == 80 and result['available_files'] == 85
    assert result['total'] == 80
    assert manager.logs(query='entry-000', compact=False)['total'] == 1
