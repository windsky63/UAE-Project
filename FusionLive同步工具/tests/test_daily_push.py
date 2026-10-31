"""Leader-facing daily figures and separate RPA readiness."""
import json
import hashlib
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from reporting import record_download, write_push_text, try_write_push_text, write_summary
from fusion_client import LOCAL
from ledger import Ledger
from reporting import establish_report_baseline, record_owner_inventory, system_report_stats
from reporting import classify_downloads, classification_lines, file_format


class DailyPushTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.ledger = Ledger(self.base/'runtime/ledger.sqlite3', base_dir=self.base)
        self.at = datetime(2026,10,22,10,31,tzinfo=LOCAL)
        self.clock = patch('reporting.now', side_effect=lambda:self.at)
        self.clock.start()
        client = Mock(project_id='p', cabinet_id='r', project_name='project')
        self.ledger.root(client)
        self.ledger.save_page('p','r',2,[{'id':'v1','hasContent':'true','contentSize':'1000000000'},
                                      {'id':'v2','hasContent':'true','contentSize':'2000000000'}],2,True)
        for doc in self.ledger.all('SELECT * FROM documents'):
            self.ledger.save_related(doc,[{'id':doc['id'],'filename':doc['id']+'.zip',
                'contentSize':str(doc['size']),'readAccessStatus':'Available'}])
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO workflows(project,stage,phase,updated) VALUES('p','bulk_download','download',?)",(self.at.isoformat(),))

    def tearDown(self):
        self.clock.stop()
        self.ledger.close()
        self.temp.cleanup()

    def run_record(self, status='done', confirmed=1):
        with self.ledger.db:
            return self.ledger.db.execute('INSERT INTO runs(action,started,ended,status,logout_confirmed) VALUES(?,?,?,?,?)',
                ('auto',self.at.replace(hour=2).isoformat(),None if status=='running' else self.at.isoformat(),status,confirmed)).lastrowid

    def event(self, identifier, status, size=0, filename=None):
        record_download(self.base,{'project':'p','id':identifier,'filename':filename or identifier+'.zip'},status,size)

    def report(self):
        path = write_push_text(self.ledger)
        self.assertEqual(path,self.base/'logs/2026-10-22/微信推送文.txt')
        return path.read_text(encoding='utf-8-sig')

    def test_same_day_multiple_runs_and_retry_success_are_deduplicated(self):
        self.run_record('download_pending')
        self.run_record()
        self.event('v1','failed')
        self.event('v1','done',1000000000)
        self.event('v1','done',1000000000)
        self.event('v2','failed')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='done' WHERE id='v1'")
        text = self.report()
        for expected in ('业主系统：2个文件，3.00 GB，3,000,000,000字节','NCC系统   0个文件，0.00 GB，0字节'):
            self.assertIn(expected,text)
        self.assertNotIn('本地路径',text)

    def test_running_report_is_not_ready_and_has_no_program_details(self):
        self.run_record('running',0)
        text = self.report()
        self.assertFalse(json.loads((self.base/'logs/2026-10-22/推送状态.json').read_text(encoding='utf-8'))['ready'])
        for forbidden in ('登录','注销','阶段','异常','报送状态','统计口径','生成时间','运行'):
            self.assertNotIn(forbidden,text)

    def test_logout_and_interruption_status_do_not_appear_in_leader_text(self):
        self.run_record('interrupted',0)
        self.run_record('done',1)
        text = self.report()
        self.assertNotIn('退出登录',text)
        self.assertNotIn('中断',text)
        self.assertFalse(json.loads((self.base/'logs/2026-10-22/推送状态.json').read_text(encoding='utf-8'))['ready'])

    def test_yesterday_is_excluded_and_today_zero_is_explicit(self):
        self.at = datetime(2026,10,21,10,31,tzinfo=LOCAL)
        self.event('v1','done',1000000000)
        self.run_record()
        self.at = datetime(2026,10,22,10,31,tzinfo=LOCAL)
        text = self.report()
        self.assertIn('NCC系统   新增文件0份、0.00 MB、0字节',text)
        self.assertFalse(json.loads((self.base/'logs/2026-10-22/推送状态.json').read_text(encoding='utf-8'))['ready'])

    def test_summary_generates_text_after_recording_logout(self):
        self.run_record('stopped',1)
        write_summary(self.ledger,1)
        text = self.report()
        self.assertIn('NCC系统   0个文件，0.00 GB，0字节',text)
        self.assertNotIn('账号',text)
        self.assertTrue((self.base/'logs/2026-10-22/run-1-summary.json').exists())

    def test_atomic_replace_failure_preserves_previous_complete_text_without_breaking_download(self):
        self.run_record()
        text = self.report()
        with patch('reporting.os.replace',side_effect=PermissionError('RPA locked file')):
            self.assertIsNone(try_write_push_text(self.ledger))
        self.assertEqual((self.base/'logs/2026-10-22/微信推送文.txt').read_text(encoding='utf-8-sig'),text)

    def test_restricted_and_partial_files_are_not_claimed_as_completed(self):
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='restricted' WHERE id='v1'")
            self.ledger.db.execute("UPDATE files SET status='downloading',bytes=500000000 WHERE id='v2'")
        self.run_record('running',0)
        text = self.report()
        self.assertIn('业主系统：2个文件，3.00 GB，3,000,000,000字节',text)
        self.assertIn('NCC系统   0个文件，0.00 GB，0字节',text)
        self.assertNotIn('总体进度：',text)

    def test_shared_content_is_counted_once_and_professions_are_not_inferred_from_region(self):
        with self.ledger.db:
            for row in self.ledger.all('SELECT id,metadata FROM documents'):
                data = json.loads(row['metadata'])
                data['discipline'] = 'PIP - Piping Engineering & Design' if row['id']=='v1' else 'CIV - Civil Engineering & Design'
                self.ledger.db.execute('UPDATE documents SET metadata=? WHERE id=?',(json.dumps(data),row['id']))
            self.ledger.db.execute("INSERT INTO document_files SELECT project,id,folder_id,'v1',1 FROM documents WHERE id='v2'")
        name = '30201_50150_LT_HE-H4-072-13-00-001.pdf'
        self.event('v1','done',1000000000,name)
        self.event('v1','done',1000000000,name)
        groups = classify_downloads(self.ledger,{('p','v1'):1000000000},{('p','v1'):name})
        self.assertEqual(dict(groups['profession']),{'跨专业共享':[1,1000000000]})
        self.assertEqual(dict(groups['region']),{'H4':[1,1000000000]})
        self.assertEqual(dict(groups['format']),{'PDF':[1,1000000000]})
        text = self.report()
        self.assertNotIn('按专业',text)
        self.assertNotIn('今日主要区域',text)

    def test_unknown_region_still_has_a_known_profession_and_missing_content_is_retained(self):
        with self.ledger.db:
            row = self.ledger.one("SELECT metadata FROM documents WHERE id='v1'")
            data = json.loads(row[0]);data['discipline']='PIP - Piping Engineering & Design'
            self.ledger.db.execute("UPDATE documents SET metadata=? WHERE id='v1'",(json.dumps(data),))
        successful = {('p','v1'):100,('p','not-in-db'):200}
        groups = classify_downloads(self.ledger,successful,{('p','v1'):'vendor.xlsx',('p','not-in-db'):'archive.zip'})
        self.assertEqual(dict(groups['profession']),{'管道':[1,100],'未标注专业':[1,200]})
        self.assertEqual(dict(groups['region']),{'未识别区域':[2,300]})
        self.assertEqual(dict(groups['format']),{'表格':[1,100],'压缩包':[1,200]})

    def test_top_three_and_other_keep_unknown_separate_and_are_ranked_by_file_count(self):
        groups = {'profession':{'管道':[10,100],'土建':[8,80],'电气':[6,60],'仪表':[4,40],'暖通':[2,20],'未标注专业':[20,200]},
                  'region':{'H4':[10,100],'未识别区域':[40,400]},'format':{'PDF':[50,500]}}
        text='\n'.join(classification_lines(groups))
        self.assertIn('其他专业 6个（0.00 GB）',text)
        self.assertIn('未标注专业 20个（0.00 GB）',text)
        self.assertNotIn('仪表 4个',text)
        self.assertIn('今日主要专业：管道 10个',text)
        self.assertIn('今日主要区域：H4 10个',text)

    def test_formats_are_grouped_by_extension_without_inspecting_archives(self):
        for name,expected in [('a.PDF','PDF'),('b.dwg','DWG'),('c.xlsm','表格'),('d.csv','表格'),('e.tar.gz','压缩包'),('f.7z','压缩包'),('g.docx','其他格式')]:
            self.assertEqual(file_format(name),expected)
        groups=classify_downloads(self.ledger,{('p','v1'):3},{('p','v1'):'LT_HE-H1-032-01_LT_HE-H2-057-01.zip'})
        self.assertEqual(dict(groups['region']),{'跨区域资料':[1,3]})
        self.assertEqual(dict(groups['format']),{'压缩包':[1,3]})

    def save_local(self, identifier='v1', filename='one.pdf', data=b'pdf'):
        path=self.base/'downloads'/filename
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='done',path=?,size=?,bytes=?,sha256=?,downloaded_at=?,download_date_source='下载完成实录' WHERE id=?",
                ('downloads/'+filename,len(data),len(data),hashlib.sha256(data).hexdigest(),self.at.isoformat(),identifier))
        return path

    def test_new_format_encoding_and_ready_flag_require_completed_owner_inventory(self):
        establish_report_baseline(self.ledger,self.at)
        self.run_record()
        self.save_local()
        text=self.report()
        self.assertIn('业主系统：新增文件—份、— MB、—字节',text)
        record_owner_inventory(self.ledger,'p',1,self.at)
        text=self.report()
        expected=['【阿联酋项目资料下载日报】','2026年10月22日','','一、总体进展','业主系统：2个文件，2.00 GB，2,000,000,003字节',
                  'NCC系统   1个文件，0.00 GB，3字节','','二、今日更新情况',
                  '业主系统：新增文件0份、0.00 MB、0字节','NCC系统   新增文件1份、0.00 MB、3字节']
        self.assertEqual(text.splitlines(),expected)
        folder=self.base/'logs/2026-10-22'
        raw=(folder/'微信推送文.txt').read_bytes()
        self.assertTrue(raw.startswith(b'\xef\xbb\xbf'))
        marker=json.loads((folder/'推送状态.json').read_text(encoding='utf-8'))
        self.assertTrue(marker['ready'])
        self.assertTrue(marker['owner_inventory_complete_today'])
        self.assertEqual(marker['text_sha256'],hashlib.sha256(raw).hexdigest())
        self.at=self.at.replace(day=23)
        self.assertEqual(write_push_text(self.ledger).read_text(encoding='utf-8-sig').splitlines()[:2],['【阿联酋项目资料下载日报】','2026年10月23日'])

    def test_owner_additions_exclude_initial_baseline_and_same_id_changes(self):
        establish_report_baseline(self.ledger,self.at)
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        old=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")[0])
        new={'id':'attachment','filename':'extra.pdf','readAccessStatus':'Available','contentSize':'5'}
        self.ledger.save_related(doc,[old,new])
        self.assertIsNone(system_report_stats(self.ledger,self.at)['owner_new'])
        record_owner_inventory(self.ledger,'p',1,self.at)
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['owner_new'],{'files':1,'bytes':5})
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET size=size+100 WHERE id='v2'")
        record_owner_inventory(self.ledger,'p',2,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_new'],{'files':1,'bytes':5})
        self.at=self.at.replace(day=23)
        self.assertIsNone(system_report_stats(self.ledger,self.at)['owner_new'])
        record_owner_inventory(self.ledger,'p',3,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_new'],{'files':0,'bytes':0})

    def test_local_missing_or_redownloaded_same_path_is_not_a_new_file(self):
        establish_report_baseline(self.ledger,self.at)
        path=self.save_local()
        for _ in range(2):
            stats=system_report_stats(self.ledger,self.at)
            self.assertEqual(stats['local'],{'files':1,'bytes':3})
            self.assertEqual(stats['local_new'],{'files':1,'bytes':3})
        self.at=self.at.replace(day=23)
        path.unlink()
        self.assertEqual(system_report_stats(self.ledger,self.at)['local'],{'files':0,'bytes':0})
        self.save_local()
        self.assertEqual(system_report_stats(self.ledger,self.at)['local_new'],{'files':0,'bytes':0})
        path.write_bytes(b'wrong-size')
        self.assertEqual(system_report_stats(self.ledger,self.at)['local'],{'files':0,'bytes':0})

    def test_retained_old_local_version_is_recorded_but_excluded_from_report(self):
        establish_report_baseline(self.ledger,self.at)
        self.save_local()
        system_report_stats(self.ledger,self.at)
        with self.ledger.db:
            row=self.ledger.one("SELECT * FROM files WHERE id='v1'")
            self.ledger.db.execute('INSERT INTO file_history(project,file_id,path,bytes,sha256,archived) VALUES(?,?,?,?,?,?)',
                ('p','v1',row['path'],row['bytes'],row['sha256'],self.at.isoformat()))
        self.assertEqual(system_report_stats(self.ledger,self.at)['local']['files'],1)
        self.save_local(filename='new.pdf',data=b'new-version')
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local'],{'files':1,'bytes':11})
        self.assertEqual(stats['local_new'],{'files':1,'bytes':11})
        self.assertTrue((self.base/'downloads/one.pdf').is_file())
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM file_history')['n'],1)

    def test_first_ever_owner_inventory_is_baseline_not_all_new(self):
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_new'],{'files':0,'bytes':0})

    def test_inactive_contents_and_unfinished_replacements_never_count(self):
        establish_report_baseline(self.ledger,self.at)
        path=self.save_local()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET active=0 WHERE id='v1'")
        self.assertEqual(system_report_stats(self.ledger,self.at)['local']['files'],0)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET active=1,status='pending' WHERE id='v1'")
        self.assertEqual(system_report_stats(self.ledger,self.at)['local']['files'],0)
        self.assertTrue(path.is_file())

    def test_wrong_current_version_size_never_counts_even_if_done(self):
        self.save_local()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET size=4 WHERE id='v1'")
        self.assertEqual(system_report_stats(self.ledger,self.at)['local']['files'],0)

    def test_inventory_revision_change_requires_download_of_current_version(self):
        establish_report_baseline(self.ledger,self.at)
        old_path=self.save_local()
        system_report_stats(self.ledger,self.at)
        self.at=self.at.replace(day=23)
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        content=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")['metadata'])
        content.update(contentSize='3',revision='new')
        self.ledger.save_related(doc,[content])
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local']['files'],0)
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM file_history')['n'],1)
        self.assertTrue(old_path.is_file())
        self.save_local(filename='current.pdf')
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['local'],{'files':1,'bytes':3})
        self.assertEqual(system_report_stats(self.ledger,self.at)['local_new']['files'],1)

    def test_upgrade_migrates_current_version_dates_without_counting_all_as_today(self):
        from reporting import init_report_tables
        init_report_tables(self.ledger)
        self.save_local()
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO report_settings VALUES('local_seed','old-baseline')")
            self.ledger.db.execute("INSERT INTO report_local_paths VALUES('downloads/one.pdf','2026-10-21',3)")
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local'],{'files':1,'bytes':3})
        self.assertEqual(stats['local_new'],{'files':0,'bytes':0})
        self.assertEqual(self.ledger.one('SELECT first_day FROM report_local_versions')['first_day'],'2026-10-21')

    def test_upgrade_keeps_unknown_old_download_date_as_baseline(self):
        from reporting import init_report_tables
        init_report_tables(self.ledger)
        self.save_local()
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO report_settings VALUES('local_seed','old-baseline')")
            self.ledger.db.execute("INSERT INTO report_local_paths VALUES('downloads/one.pdf',NULL,3)")
        self.assertEqual(system_report_stats(self.ledger,self.at)['local_new']['files'],0)

    def test_redownload_same_content_version_to_different_path_is_not_new(self):
        establish_report_baseline(self.ledger,self.at)
        self.save_local()
        system_report_stats(self.ledger,self.at)
        self.at=self.at.replace(day=23)
        self.save_local(filename='relocated.pdf')
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local'],{'files':1,'bytes':3})
        self.assertEqual(stats['local_new']['files'],0)

    def test_changed_current_version_counts_once_even_if_bytes_identical(self):
        establish_report_baseline(self.ledger,self.at)
        self.save_local()
        system_report_stats(self.ledger,self.at)
        self.at=self.at.replace(day=23)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET fingerprint='new-current-version' WHERE id='v1'")
            self.ledger.db.execute("INSERT INTO report_local_paths VALUES('downloads/one.pdf','2026-10-22',3)")
        self.save_local()
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local'],{'files':1,'bytes':3})
        self.assertEqual(stats['local_new']['files'],1)
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM report_local_versions')['n'],2)

    def test_current_content_id_count_does_not_expand_shared_directory_references(self):
        establish_report_baseline(self.ledger,self.at)
        self.save_local()
        with self.ledger.db:
            self.ledger.db.execute("INSERT INTO document_files SELECT project,id,folder_id,'v1',1 FROM documents WHERE id='v2'")
        record_owner_inventory(self.ledger,'p',1,self.at)
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local']['files'],1)
        self.assertLessEqual(stats['local']['files'],stats['owner']['files'])

    def test_upgrade_uses_earliest_path_log_instead_of_latest_redownload_timestamp(self):
        path=self.save_local()
        item={'project':'p','id':'v1','filename':'one.pdf'}
        self.at=self.at.replace(day=21)
        record_download(self.base,item,'done',3,'downloads/one.pdf')
        self.at=self.at.replace(day=22)
        record_download(self.base,item,'done',3,'downloads/one.pdf')
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['local'],{'files':1,'bytes':3})
        self.assertEqual(stats['local_new'],{'files':0,'bytes':0})


if __name__ == '__main__':
    unittest.main()
