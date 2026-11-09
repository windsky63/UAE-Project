"""Leader-facing daily figures and separate RPA readiness."""
import json
import hashlib
import csv
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
    def test_live_text_reads_current_entities_without_generating_fixed_files(self):
        import importlib.util
        from reporting import read_live_push_text
        source=Path(__file__).resolve().parents[2]/'管理中心/daily_publications.py'
        spec=importlib.util.spec_from_file_location('daily_push_dashboard_adapter',source)
        adapter=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(adapter)
        self.save_local(data=b'old')
        archived=self.base/'history/old.pdf'
        archived.parent.mkdir()
        archived.write_bytes(b'archived')
        # Repeated references must never turn one physical file into two files.
        with self.ledger.db:
            for _ in range(2):
                self.ledger.db.execute("INSERT INTO file_history(project,file_id,path,bytes,sha256,archived) VALUES('p','v1','history/old.pdf',8,?,'2026-10-21')",
                                       (hashlib.sha256(b'archived').hexdigest(),))
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.run_record()
        system_report_stats(self.ledger,self.at)
        before={p.relative_to(self.base).as_posix() for p in self.base.rglob('*')}
        text,report=read_live_push_text(self.base,self.at,require_ready=True)
        self.assertIn('NCC系统   1个文件',text)
        self.assertTrue(report['ready'])
        self.assertEqual({p.relative_to(self.base).as_posix() for p in self.base.rglob('*')},before)
        self.assertFalse((self.base/'logs/2026-10-22/微信推送文.txt').exists())
        self.assertIsNone(adapter.version_progress(self.base/'runtime/ledger.sqlite3'))
        write_push_text(self.ledger)
        live=adapter.version_progress(self.base/'runtime/ledger.sqlite3')
        for scope in ('total','downloads','history','pending_publication'):
            self.assertEqual(live[scope],report['file_inventory'][scope])
        self.assertEqual(live['total'],dict(files=2,bytes=11))
        self.assertEqual(live['downloads'],report['download_directory']['total'])
        archived.unlink()
        self.assertEqual(adapter.version_progress(self.base/'runtime/ledger.sqlite3')['total'],dict(files=2,bytes=11))
        (self.base/'downloads/one.pdf').unlink()
        _,state=read_live_push_text(self.base,self.at)
        self.assertEqual(state['file_inventory']['total'],dict(files=0,bytes=0))
        write_push_text(self.ledger)
        self.assertEqual(adapter.version_progress(self.base/'runtime/ledger.sqlite3')['total'],dict(files=0,bytes=0))

    def test_report_reads_directory_without_moving_files_and_blocks_extras(self):
        self.save_local()
        extra=self.base/'downloads/extra.pdf'
        extra.write_bytes(b'extra')
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.run_record()
        with patch('file_layout.prepare_downloads',side_effect=AssertionError('report must not organize files')), \
             patch('file_layout.apply',side_effect=AssertionError('report must not move files')):
            text=self.report()
        self.assertIn('统计待目录整理完成后核对',text)
        self.assertEqual(extra.read_bytes(),b'extra')
        state=json.loads((self.base/'logs/2026-10-22/推送状态.json').read_text(encoding='utf-8'))
        self.assertFalse(state['ready'])
        self.assertFalse(state['directory_reconciled'])
        extra.unlink()
        self.report()
        state=json.loads((self.base/'logs/2026-10-22/推送状态.json').read_text(encoding='utf-8'))
        self.assertTrue(state['ready'])
        self.assertEqual(state['effective_downloads']['total'],state['download_directory']['total'])

    def test_new_then_multiple_replacements_counts_once_as_new_at_final_size(self):
        self.save_local(data=b'a')
        self.at=self.at.replace(minute=32)
        self.save_local(data=b'changed')
        self.at=self.at.replace(minute=33)
        self.save_local(data=b'changed')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=1,bytes=7))
        self.assertEqual(stats['updated']['files'],0)
        self.assertEqual(stats['success_records'],3)
        self.assertEqual(stats['duplicate_records'],2)

    def test_same_bytes_do_not_count_as_update_but_later_changed_bytes_do(self):
        self.save_local(data=b'old')
        self.at=self.at.replace(day=23)
        self.save_local(data=b'old')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['updated']['files'],0)
        self.assertEqual(stats['unchanged']['files'],1)
        self.assertEqual(self.ledger.one('SELECT action FROM download_activity ORDER BY id DESC LIMIT 1')[0],'unchanged')
        self.at=self.at.replace(minute=32)
        self.save_local(data=b'changed')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['updated'],dict(files=1,bytes=7))

    def test_history_only_copy_is_new_in_formal_directory(self):
        old=self.save_local(data=b'old')
        self.at=self.at.replace(day=23)
        (self.base/'history').mkdir()
        old.rename(self.base/'history/one.pdf')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET path='history/one.pdf'")
        self.save_local(data=b'old')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=1,bytes=3))
        self.assertEqual(stats['updated']['files'],0)

    def set_source_family(self, identifier, first='family', relation='Document'):
        row=self.ledger.one('SELECT metadata FROM files WHERE id=?',(identifier,))
        data=json.loads(row[0]);data['firstVersionId']=first
        with self.ledger.db:
            self.ledger.db.execute('UPDATE files SET metadata=?,relation=? WHERE id=?',(json.dumps(data),relation,identifier))

    def test_changed_content_id_in_source_family_remains_one_daily_new_file(self):
        self.set_source_family('v1');self.set_source_family('v2')
        self.save_local(data=b'old')
        self.at=self.at.replace(minute=32)
        self.save_local(identifier='v2',filename='replacement.pdf',data=b'newer')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=1,bytes=5))
        self.assertEqual(stats['updated']['files'],0)
        self.assertEqual(self.ledger.one('SELECT action FROM download_activity ORDER BY id DESC LIMIT 1')[0],'updated')

    def test_distinct_attachment_members_are_not_merged_by_parent_family(self):
        self.set_source_family('v1',relation='Attachment');self.set_source_family('v2',relation='Attachment')
        self.save_local(data=b'a')
        self.save_local(identifier='v2',filename='two.pdf',data=b'bb')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=2,bytes=3))

    def test_failed_attempt_is_not_counted_and_csv_mirror_is_not_counted_twice(self):
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        self.ledger.begin_download_activity(item,None,started=self.at.isoformat())
        self.save_local(data=b'a')
        record_download(self.base,item,'done',1,'downloads/one.pdf')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=1,bytes=1))
        self.assertEqual(stats['success_records'],1)
        self.assertEqual(stats['log_success_records'],1)

    def test_old_csv_without_action_is_ignored_despite_source_addition(self):
        with self.ledger.db:
            self.ledger.db.execute('UPDATE changes SET created=?',(self.at.replace(hour=2).isoformat(),))
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        record_download(self.base,item,'done',3,'downloads/one.pdf')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=0,bytes=0))
        self.assertEqual(stats['total']['files'],0)
        self.assertEqual(stats['ignored_success_records'],1)

    def test_explicit_csv_action_takes_precedence_over_source_inference(self):
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        item['download_action']='updated'
        record_download(self.base,item,'done',3,'downloads/one.pdf')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['updated'],dict(files=1,bytes=3))
        self.assertEqual(stats['ignored_success_records'],0)
        with (self.base/'logs/2026-10-22/downloads.csv').open(encoding='utf-8-sig',newline='') as stream:
            self.assertEqual(next(csv.DictReader(stream))['下载行为'],'更新')

    def test_recorded_action_does_not_query_legacy_download_history(self):
        from reporting import download_activity
        self.save_local(data=b'a')
        original=self.ledger.all
        def read(sql,*args):
            if 'FROM file_history' in sql or 'FROM report_local_versions' in sql or 'FROM changes' in sql:
                raise AssertionError('recorded action must not invoke legacy reconstruction')
            return original(sql,*args)
        with patch.object(self.ledger,'all',side_effect=read):
            self.assertEqual(download_activity(self.ledger,self.at)['new'],dict(files=1,bytes=1))

    def test_missing_database_action_is_not_reconstructed_from_snapshot_or_csv(self):
        self.save_local(data=b'a')
        with self.ledger.db:
            self.ledger.db.execute('UPDATE download_activity SET action=NULL')
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        item['download_action']='new'
        record_download(self.base,item,'done',1,'downloads/one.pdf')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['total']['files'],0)
        self.assertEqual(stats['ignored_success_records'],1)
        self.assertEqual(stats['log_success_records'],1)

    def test_legacy_csv_does_not_override_later_recorded_action_or_size(self):
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        record_download(self.base,item,'done',100,'downloads/one.pdf')
        self.at=self.at.replace(minute=32)
        self.save_local(data=b'new')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['new'],dict(files=1,bytes=3))
        self.assertEqual(stats['ignored_success_records'],1)

    def test_appending_to_old_csv_preserves_eight_column_schema(self):
        log=self.base/'logs/2026-10-22/downloads.csv'
        log.parent.mkdir(parents=True)
        with log.open('w',encoding='utf-8-sig',newline='') as stream:
            csv.writer(stream).writerow(['北京时间','工作区','内容ID','文件名','结果','字节','本地路径','错误'])
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        item['download_action']='new'
        record_download(self.base,item,'done',3,'downloads/one.pdf')
        with log.open(encoding='utf-8-sig',newline='') as stream:
            self.assertEqual([len(row) for row in csv.reader(stream)],[8,8])

    def test_legacy_archive_is_not_used_and_report_has_no_unclassified_line(self):
        self.save_local(data=b'old')
        self.at=self.at.replace(day=23)
        with self.ledger.db:
            self.ledger.db.execute('DELETE FROM download_activity')
            self.ledger.db.execute('''INSERT INTO file_history(project,file_id,path,bytes,sha256,archived)
                VALUES('p','v1','history/old.pdf',3,?,?)''',
                (hashlib.sha256(b'old').hexdigest(),self.at.replace(day=22).isoformat()))
        item=dict(self.ledger.one("SELECT * FROM files WHERE id='v1'"))
        record_download(self.base,item,'done',3,'downloads/one.pdf')
        stats=system_report_stats(self.ledger,self.at)['download_activity']
        self.assertEqual(stats['total']['files'],0)
        self.assertEqual(stats['ignored_success_records'],1)
        self.assertEqual(stats['updated']['files'],0)
        self.assertNotIn('分类待核对',write_push_text(self.ledger).read_text(encoding='utf-8-sig'))

    def test_today_new_and_updated_files_have_identical_units_and_separate_counts(self):
        establish_report_baseline(self.ledger,self.at)
        self.save_local(data=b'old')
        system_report_stats(self.ledger,self.at)
        self.at=self.at.replace(day=23)
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        content=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")[0])
        content.update(revision='new',contentSize='5')
        self.ledger.save_related(doc,[content])
        self.save_local(filename='updated.pdf',data=b'newer')
        self.save_local(identifier='v2',filename='brand-new.pdf',data=b'abc')
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.run_record()
        for _ in range(2):
            stats=system_report_stats(self.ledger,self.at)['effective_downloads']
            self.assertEqual(stats['new'],{'files':1,'bytes':3})
            self.assertEqual(stats['updated'],{'files':1,'bytes':5})
            text=write_push_text(self.ledger).read_text(encoding='utf-8-sig')
            self.assertIn('NCC系统   新增文件1份、0.00 MB、3字节',text)
            self.assertIn('NCC系统   更新文件1份、0.00 MB、5字节',text)

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

    def test_summary_does_not_pre_generate_fixed_text(self):
        self.run_record('stopped',1)
        write_summary(self.ledger,1)
        self.assertFalse((self.base/'logs/2026-10-22/微信推送文.txt').exists())
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
        from file_layout import download_directory_copy
        item=dict(self.ledger.one('SELECT * FROM files WHERE id=?',(identifier,)))
        previous=download_directory_copy(self.ledger,item,path)
        activity_id=self.ledger.begin_download_activity(item,previous,started=self.at.isoformat())
        path.write_bytes(data)
        with self.ledger.db:
            self.ledger.finish_download_activity(activity_id,dict(bytes=len(data),sha256=hashlib.sha256(data).hexdigest()),self.at.isoformat())
            self.ledger.db.execute("UPDATE files SET status='done',path=?,size=?,bytes=?,sha256=?,downloaded_at=?,download_date_source='下载完成实录' WHERE id=?",
                ('downloads/'+filename,len(data),len(data),hashlib.sha256(data).hexdigest(),self.at.isoformat(),identifier))
        return path

    def test_new_format_encoding_and_ready_flag_require_completed_owner_inventory(self):
        establish_report_baseline(self.ledger,self.at)
        self.run_record()
        self.save_local()
        text=self.report()
        self.assertIn('业主系统：新增文件—份、— MB、—字节',text)
        self.assertIn('业主系统：更新文件—份、— MB、—字节',text)
        record_owner_inventory(self.ledger,'p',1,self.at)
        text=self.report()
        expected=['【阿联酋项目资料下载日报】','2026年10月22日','','一、总体进展','业主系统：2个文件，2.00 GB，2,000,000,003字节',
                  'NCC系统   1个文件，0.00 GB，3字节','','二、今日更新情况',
                  '业主系统：新增文件0份、0.00 MB、0字节','业主系统：更新文件1份、0.00 MB、3字节',
                  'NCC系统   新增文件1份、0.00 MB、3字节',
                  'NCC系统   更新文件0份、0.00 MB、0字节']
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

    def test_directory_report_includes_retained_predecessor_until_new_version_is_published(self):
        establish_report_baseline(self.ledger,self.at)
        old_path=self.save_local()
        system_report_stats(self.ledger,self.at)
        self.at=self.at.replace(day=23)
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        content=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")['metadata'])
        content.update(contentSize='3',revision='new')
        self.ledger.save_related(doc,[content])
        stats=system_report_stats(self.ledger,self.at)
        directory=stats['download_directory']
        self.assertEqual(stats['local']['files'],0)
        self.assertEqual(directory['total'],{'files':1,'bytes':3})
        self.assertEqual(directory['retained'],{'files':1,'bytes':3})
        self.assertEqual(directory['new']['files'],0)
        record_owner_inventory(self.ledger,'p',1,self.at)
        text=write_push_text(self.ledger).read_text(encoding='utf-8-sig')
        self.assertIn('NCC系统   1个文件',text)
        self.assertNotIn('待替换',text)
        self.assertNotIn('最新文件',text)
        staged=self.base/'runtime/download_ready/new/content'
        staged.parent.mkdir(parents=True)
        staged.write_bytes(b'new')
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='done',path=?,size=3,bytes=3,sha256=? WHERE id='v1'",
                (staged.relative_to(self.base).as_posix(),hashlib.sha256(b'new').hexdigest()))
        directory=system_report_stats(self.ledger,self.at)['download_directory']
        self.assertEqual(directory['total']['files'],1)
        self.assertEqual(directory['latest']['files'],0)
        effective=system_report_stats(self.ledger,self.at)['effective_downloads']
        self.assertEqual(effective['latest'],{'files':0,'bytes':0})
        self.assertEqual(effective['retained']['files'],1)
        self.assertFalse(system_report_stats(self.ledger,self.at)['directory_reconciled'])
        self.assertIn('统计待目录整理完成后核对',write_push_text(self.ledger).read_text(encoding='utf-8-sig'))
        self.save_local(filename='current.pdf',data=b'new')
        directory=system_report_stats(self.ledger,self.at)['download_directory']
        self.assertEqual(directory['total'],{'files':2,'bytes':6})
        self.assertEqual(directory['latest']['files'],1)
        self.assertEqual(directory['retained']['files'],1)
        self.assertEqual(system_report_stats(self.ledger,self.at)['effective_downloads']['total'],{'files':1,'bytes':3})
        archived=self.base/'history/old.pdf'
        archived.parent.mkdir()
        old_path.rename(archived)
        self.assertEqual(system_report_stats(self.ledger,self.at)['download_directory']['total']['files'],1)

    def test_directory_report_counts_physical_unknown_files_without_claiming_latest_or_new(self):
        self.save_local()
        duplicate=self.ledger.one("SELECT * FROM files WHERE id='v1'")
        with self.ledger.db:
            self.ledger.db.execute('INSERT INTO file_history(project,file_id,fingerprint,path,bytes,sha256,archived) VALUES(?,?,?,?,?,?,?)',
                ('p','v1',duplicate['fingerprint'],duplicate['path'],3,duplicate['sha256'],self.at.isoformat()))
        (self.base/'downloads/untracked.pdf').write_bytes(b'unknown')
        (self.base/'downloads/incomplete.part').write_bytes(b'part')
        history=self.base/'history'
        history.mkdir()
        (history/'excluded.pdf').write_bytes(b'history')
        directory=system_report_stats(self.ledger,self.at)['download_directory']
        self.assertEqual(directory['total'],{'files':3,'bytes':14})
        self.assertEqual(directory['latest']['files'],1)
        self.assertEqual(directory['retained']['files'],0)
        self.assertEqual(directory['other'],{'files':2,'bytes':11})
        marker=json.loads((write_push_text(self.ledger).parent/'推送状态.json').read_text(encoding='utf-8'))
        self.assertEqual(marker['download_directory'],directory)
        self.assertEqual(marker['effective_downloads']['total'],{'files':1,'bytes':3})
        self.assertIn('统计待目录整理完成后核对',self.report())
        self.assertFalse(marker['ready'])

    def test_pending_current_counts_newest_history_once_and_excludes_orphans(self):
        self.save_local()
        establish_report_baseline(self.ledger,self.at)
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        item=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")[0])
        item.update(contentSize='9',revision='latest')
        self.ledger.save_related(doc,[item])
        for identifier,name,data,stamp in [('v1','newer.pdf',b'newer','2099-01-02'),
                                          ('v1','older.pdf',b'older-copy','2099-01-01'),
                                          ('removed','orphan.pdf',b'orphan','2099-01-03')]:
            (self.base/'downloads'/name).write_bytes(data)
            with self.ledger.db:
                self.ledger.db.execute('''INSERT INTO file_history
                  (project,file_id,path,bytes,sha256,archived) VALUES(?,?,?,?,?,?)''',
                  ('p',identifier,'downloads/'+name,len(data),hashlib.sha256(data).hexdigest(),stamp))
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['download_directory']['total']['files'],4)
        self.assertEqual(stats['effective_downloads']['total'],{'files':1,'bytes':5})
        self.assertEqual(stats['effective_downloads']['retained'],{'files':1,'bytes':5})
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.assertIn('统计待目录整理完成后核对',self.report())

    def test_owner_snapshot_mismatch_defers_comparison_until_inventory_is_confirmed(self):
        establish_report_baseline(self.ledger,self.at)
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.save_local(data=b'updated-size')
        text=self.report()
        self.assertIn('NCC系统   统计待本轮业主盘点完成后核对',text)
        marker=json.loads((self.base/'logs/2026-10-22/推送状态.json').read_text(encoding='utf-8'))
        self.assertFalse(marker['ready'])
        self.assertFalse(marker['owner_scope_matches'])
        record_owner_inventory(self.ledger,'p',2,self.at)
        self.assertIn('NCC系统   1个文件',self.report())

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

    def test_owner_updates_are_separate_and_repeated_inventory_does_not_duplicate(self):
        establish_report_baseline(self.ledger,self.at)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET fingerprint='revision-2',size=2000000 WHERE id='v1'")
        record_owner_inventory(self.ledger,'p',1,self.at)
        record_owner_inventory(self.ledger,'p',2,self.at)
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['owner_new'],{'files':0,'bytes':0})
        self.assertEqual(stats['owner_updated'],{'files':1,'bytes':2000000})
        self.assertIn('业主系统：更新文件1份、2.00 MB、2,000,000字节',self.report())
        self.at=self.at.replace(day=23)
        self.assertIsNone(system_report_stats(self.ledger,self.at)['owner_updated'])
        record_owner_inventory(self.ledger,'p',3,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_updated'],{'files':0,'bytes':0})

    def test_owner_same_size_revision_counts_as_update(self):
        establish_report_baseline(self.ledger,self.at)
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET fingerprint='same-size-revision' WHERE id='v1'")
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_updated'],{'files':1,'bytes':1000000000})

    def test_owner_new_file_revised_today_stays_new_with_latest_size(self):
        establish_report_baseline(self.ledger,self.at)
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        original=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")[0])
        added={'id':'extra','filename':'extra.pdf','readAccessStatus':'Available','contentSize':'5'}
        self.ledger.save_related(doc,[original,added])
        record_owner_inventory(self.ledger,'p',1,self.at)
        added['contentSize']='9'
        self.ledger.save_related(doc,[original,added])
        record_owner_inventory(self.ledger,'p',2,self.at)
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['owner_new'],{'files':1,'bytes':9})
        self.assertEqual(stats['owner_updated'],{'files':0,'bytes':0})

    def test_owner_attachment_with_new_content_id_is_update_not_addition(self):
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        original=json.loads(self.ledger.one("SELECT metadata FROM files WHERE id='v1'")[0])
        attachment={'id':'old-attachment','filename':'design.pdf','readAccessStatus':'Available','contentSize':'5'}
        self.ledger.save_related(doc,[original,attachment])
        establish_report_baseline(self.ledger,self.at)
        replacement={**attachment,'id':'new-attachment','contentSize':'7'}
        self.ledger.save_related(doc,[original,replacement])
        record_owner_inventory(self.ledger,'p',1,self.at)
        stats=system_report_stats(self.ledger,self.at)
        self.assertEqual(stats['owner_new'],{'files':0,'bytes':0})
        self.assertEqual(stats['owner_updated'],{'files':1,'bytes':7})

    def test_owner_legacy_baseline_initializes_versions_without_false_updates(self):
        establish_report_baseline(self.ledger,self.at)
        with self.ledger.db:
            self.ledger.db.execute('DROP TABLE report_remote_versions')
        record_owner_inventory(self.ledger,'p',1,self.at)
        self.assertEqual(system_report_stats(self.ledger,self.at)['owner_updated'],{'files':0,'bytes':0})

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
