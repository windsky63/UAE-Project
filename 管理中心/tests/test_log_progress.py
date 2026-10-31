from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager
from log_progress import severity


class ProgressLogTests(unittest.TestCase):
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

    def test_separate_runs_do_not_share_one_progress_row(self):
        self.path.write_text('下载完成：a.pdf\n本轮结束：complete\n下载完成：b.pdf\n',encoding='utf-8')
        self.assertEqual(sum(bool(x.get('progress')) for x in self.manager.logs()['items']),2)

    def test_each_line_keeps_its_own_timestamp(self):
        self.path.write_text('[2026-10-30 09:00:00] started\n[2026-10-30 09:01:00] finished\n',encoding='utf-8')
        rows=self.manager.logs()['items']
        self.assertIn('09:01:00',rows[0]['created_at'])
        self.assertIn('09:00:00',rows[1]['created_at'])
