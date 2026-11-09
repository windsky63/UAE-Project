from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager
from log_progress import severity


class ProgressLogTests(unittest.TestCase):
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
