"""Approved daily inventory streams only freshly checked files to downloads."""
from datetime import datetime, timedelta
from contextlib import closing
import json
import sqlite3
import unittest
from unittest.mock import Mock, patch

import requests

from fusion_client import LOCAL
from paths import resolve_path
from reporting import system_report_stats
from workflow import Workflow
from tests.test_workflow import StagedFixture, content, document


class InterleavedDownloadTests(StagedFixture, unittest.TestCase):
    def test_later_discovered_priority_unit_precedes_first_ordinary_file(self):
        self.daily()
        self.related['v1'][0]['filename']='H4-278.pdf'
        self.related['v2'][0]['filename']='H4-213.pdf'
        with patch('sync.ASSOCIATION_BATCH_SIZE',1):
            self.assertEqual(self.run_stage(),('done',0))
        self.assertEqual([e for e in self.events if e.startswith('download:')],['download:v2','download:v1'])
        self.assertLess(self.events.index('related:v2'),self.events.index('download:v2'))

    def test_shared_metadata_round_trip_reuses_verified_original_without_transfer(self):
        self.assertEqual(self.run_stage(),('done',0))
        old=self.ledger.one("SELECT path FROM files WHERE id='v1'")[0]
        (self.base/'approved_download_plan.json').write_text(json.dumps({
            'project':'p','mode':'all','approved':True,'daily_inventory_before_download':True}),encoding='utf-8')
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.related['v1'][0]['versionLabel']='different-associated-view'
        self.related['v2'].append(content('v1'))
        self.events.clear()
        with patch('sync.ASSOCIATION_BATCH_SIZE',1):
            self.assertEqual(self.run_stage(),('done',0))
        self.assertNotIn('download:v1',self.events)
        self.assertEqual(resolve_path(old,self.base).read_bytes(),b'pdf')
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v1'")[0],'done')
        details=[json.loads(r[0]) for r in self.ledger.all("SELECT details FROM changes WHERE entity='file' AND kind='updated'")]
        self.assertTrue(any('versionLabel' in d['changed_fields'] for d in details))

    def test_rechecking_identical_associations_keeps_preparation_cache_valid(self):
        import file_layout
        self.run_stage()
        before = file_layout.download_structure_token(self.ledger)
        document = self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        self.ledger.save_related(document, self.related['v1'])
        self.assertEqual(file_layout.download_structure_token(self.ledger), before)
        # Removing a previously active association is a real structural change.
        self.ledger.save_related(dict(document, metadata='{}'), [], placeholder=True)
        self.assertNotEqual(file_layout.download_structure_token(self.ledger), before)

    def test_resume_checks_remaining_associations_before_transferring_file(self):
        self.daily()
        with patch.object(self.client,'download',side_effect=RuntimeError('temporary download failure')), \
             patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(max_associations=1),('inventory_incomplete',2))
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v1'")['status'],'pending')
        self.assertFalse(any(e.startswith('download:') for e in self.events))
        self.events.clear()
        def related(identifier,virtual=None):
            self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v1'")['status'],'pending')
            raise RuntimeError('unrelated metadata failure')
        with patch.object(self.client,'related',side_effect=related),patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(),('inventory_incomplete',2))
        self.assertEqual(self.events,['download:v1'])

    def test_missing_excel_dependency_does_not_block_verified_downloads(self):
        self.daily()
        from sync import finish_run
        with patch('sync.export_csv',side_effect=ImportError('fixture openpyxl unavailable')):
            self.assertEqual(self.run_stage(),('done',0))
            ledger=Mock()
            with patch('sync.write_summary'),patch('sync.RUNTIME',self.base/'empty-runtime'):
                finish_run(ledger,1,'done',True,None)
            ledger.end_run.assert_called_once_with(1,'done',True,None)

    def test_staged_update_survives_failed_round_and_next_batch_without_redownload(self):
        import hashlib
        self.run_stage()
        original_path=resolve_path(self.ledger.one("SELECT path FROM files WHERE id='v1'")['path'],self.base)
        (self.base/'approved_download_plan.json').write_text(json.dumps({
            'project':'p','mode':'all','approved':True,'daily_inventory_before_download':True}),encoding='utf-8')
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.docs[0]['revision']='1';self.related['v1'][0]['revision']='1'
        self.related['v2'].append(content('extra'))
        original_related,original_download=self.client.related,self.client.download
        calls=[]
        def download(item,target,**kwargs):
            calls.append(item['id'])
            if item['id']=='extra':self.assertEqual(original_path.read_bytes(),b'pdf')
            result=original_download(item,target,**kwargs)
            if item['id']=='v1':
                target.write_bytes(b'new');result['sha256']=hashlib.sha256(b'new').hexdigest()
            return result
        def related(identifier,virtual=None):
            if identifier=='v2':raise RuntimeError('temporary metadata outage')
            return original_related(identifier,virtual)
        with patch.object(self.client,'download',side_effect=download):
            with patch.object(self.client,'related',side_effect=related),patch('sync.time.sleep'):
                self.assertEqual(self.run_stage(),('inventory_incomplete',2))
            self.assertEqual(original_path.read_bytes(),b'pdf')
            self.reopen()
            self.assertEqual(self.run_stage(),('done',0))
        self.assertEqual(calls.count('v1'),1)
        self.assertEqual(calls.count('extra'),1)
        self.assertEqual(original_path.read_bytes(),b'new')

    def test_verified_replacement_keeps_old_formal_until_inventory_completes(self):
        self.run_stage()
        original_path=resolve_path(self.ledger.one("SELECT path FROM files WHERE id='v1'")['path'],self.base)
        (self.base/'approved_download_plan.json').write_text(json.dumps({
            'project':'p','mode':'all','approved':True,'daily_inventory_before_download':True}),encoding='utf-8')
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.docs[0]['revision']='1'
        self.related['v1'][0]['revision']='1'
        original_related=self.client.related
        original_download=self.client.download
        def related(identifier,virtual=None):
            if identifier=='v2':
                self.assertEqual(original_path.read_bytes(),b'pdf')
                row=self.ledger.one("SELECT path FROM files WHERE id='v1'")
                self.assertIsNone(row['path'])
            return original_related(identifier,virtual)
        def download(item,target,**kwargs):
            result=original_download(item,target,**kwargs)
            if item['id']=='v1':
                import hashlib
                target.write_bytes(b'new')
                result['sha256']=hashlib.sha256(b'new').hexdigest()
            return result
        with patch.object(self.client,'related',side_effect=related),patch.object(self.client,'download',side_effect=download):
            self.assertEqual(self.run_stage(max_files=1),('done',0))
        row=self.ledger.one("SELECT path FROM files WHERE id='v1'")
        self.assertFalse(row['path'].startswith('runtime/download_ready/'))
        self.assertEqual(original_path.read_bytes(),b'new')
        old=self.ledger.one('SELECT path FROM file_history')
        self.assertEqual(resolve_path(old['path'],self.base).read_bytes(),b'pdf')

    def test_download_time_budget_returns_to_associations_between_files(self):
        self.related['v1'].extend([content('extra-a'),content('extra-b')])
        self.daily()
        with patch('sync.INTERLEAVED_DOWNLOAD_SECONDS',0):
            self.assertEqual(self.run_stage(),('done',0))
        transfers=[index for index,event in enumerate(self.events) if event.startswith('download:')]
        self.assertEqual(len(transfers),4)
        self.assertLess(self.events.index('related:v2'),transfers[0])
        self.assertLess(self.events.index('related:v2'),transfers[1])

    def test_large_file_completion_keeps_downloading_until_extended_budget(self):
        self.related['v1'].extend([content('extra-a'), content('extra-b'), content('extra-c')])
        self.daily()
        clock = [0.0]
        original_related, original_download = self.client.related, self.client.download
        def related(identifier, virtual=None):
            if identifier == 'v2':
                self.events.append('failed-related:v2')
                raise RuntimeError('persistent document association failure')
            return original_related(identifier, virtual)
        def transfer(item, target, **kwargs):
            result = original_download(item, target, **kwargs)
            clock[0] += 600  # Each file takes ten minutes, without real waiting.
            return result
        with patch.object(self.client, 'related', side_effect=related), \
                patch.object(self.client, 'download', side_effect=transfer), \
                patch('sync.time.monotonic', side_effect=lambda: clock[0]), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(), ('inventory_incomplete', 2))
        transfers = [i for i, event in enumerate(self.events) if event.startswith('download:')]
        self.assertEqual(len(transfers), 3)
        self.assertLess(max(i for i, event in enumerate(self.events) if event == 'failed-related:v2'), transfers[0])
        self.assertEqual(self.ledger.one("SELECT COUNT(*) n FROM files WHERE status='pending' AND id!='v2'")['n'], 1)
        self.assertEqual(self.ledger.one("SELECT related_state FROM documents WHERE id='v2'")['related_state'], 'failed')

    def test_document_403_does_not_block_other_files_when_protected_access_is_healthy(self):
        self.daily()
        original=self.client.related
        calls=[]
        def related(identifier,virtual=None):
            calls.append(identifier)
            if identifier=='v1':
                response=requests.Response();response.status_code=403
                raise requests.HTTPError('document forbidden',response=response)
            return original(identifier,virtual)
        with patch.object(self.client,'related',side_effect=related):
            self.assertEqual(self.run_stage(),('inventory_incomplete',2))
        self.assertEqual(calls.count('v1'),1)
        self.assertIn('download:v2',self.events)
        self.assertNotIn('download:v1',self.events)

    def test_document_403_stops_when_protected_access_probe_also_fails(self):
        self.daily()
        original=self.client.related
        response=requests.Response();response.status_code=403
        def related(identifier,virtual=None):
            if identifier=='v1':
                # Directory scan is complete before this probe is installed.
                def denied(*args):raise requests.HTTPError('session forbidden',response=response)
                self.client.folders=denied
                raise requests.HTTPError('document forbidden',response=response)
            return original(identifier,virtual)
        with patch.object(self.client,'related',side_effect=related):
            with self.assertRaises(requests.HTTPError):self.run_stage()
        self.assertFalse(any(e.startswith('download:') for e in self.events))

    def test_retry_flag_does_not_reenable_permanent_file_errors(self):
        self.daily()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE files SET status='failed',retryable=0,attempts=3,error='HTTP 404' WHERE id='v1'")
        self.assertEqual(self.run_stage(),('done',0))
        self.assertNotIn('download:v1',self.events)
        row=self.ledger.one("SELECT status,retryable,error FROM files WHERE id='v1'")
        self.assertEqual(tuple(row),('failed',0,'HTTP 404'))

    def daily(self):
        self.run_stage('scan')
        (self.base/'approved_download_plan.json').write_text(json.dumps({
            'project':'p','mode':'all','approved':True,'daily_inventory_before_download':True}),encoding='utf-8')
        self.at=datetime(2026,10,21,3,tzinfo=LOCAL)
        self.events.clear()

    def test_all_associations_precede_the_global_download_queue(self):
        self.daily()
        progress=[]
        with patch('sync.ASSOCIATION_BATCH_SIZE',1), patch('sync.INTERLEAVED_DOWNLOAD_BATCH_SIZE',1), \
             patch('progress.emit',side_effect=lambda phase,**values:progress.append(values)):
            self.assertEqual(self.run_stage(),('done',0))
        self.assertLess(self.events.index('related:v2'),self.events.index('download:v1'))
        self.assertLess(self.events.index('related:v2'),self.events.index('download:v2'))
        counts=[row['downloads'] for row in progress if 'downloads' in row]
        self.assertEqual(counts,sorted(counts))
        self.assertEqual(counts[-1],2)

    def test_failed_association_does_not_block_good_file_or_complete_inventory(self):
        self.daily()
        original=self.client.related
        def related(identifier,virtual=None):
            if identifier=='v1':raise RuntimeError('fixture unavailable association')
            return original(identifier,virtual)
        with patch.object(self.client,'related',side_effect=related),patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(),('inventory_incomplete',2))
        self.assertIn('download:v2',self.events)
        self.assertNotIn('download:v1',self.events)
        self.assertEqual(self.state()['phase'],'associations')
        self.assertEqual(self.ledger.one('SELECT status FROM snapshots ORDER BY id DESC LIMIT 1')['status'],'catalogued')
        self.assertFalse(system_report_stats(self.ledger,self.at)['owner_verified_today'])
        snapshot=self.state()['snapshot_id']
        self.events.clear()
        self.reopen()
        self.assertEqual(self.run_stage(),('done',0))
        self.assertEqual(self.state()['snapshot_id'],snapshot)
        self.assertEqual(self.events,['page:0','related:v1','download:v1'])

    def test_failed_old_version_is_replaced_by_fresh_virtual_link_without_rechecking_good_files(self):
        self.daily()
        original = self.client.related
        def fail_old(identifier, virtual=None):
            if identifier == 'v1':
                raise RuntimeError('old version no longer queryable')
            return original(identifier, virtual)
        with patch.object(self.client, 'related', side_effect=fail_old), patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(), ('inventory_incomplete', 2))
        self.docs[0] = dict(self.docs[0], id='current-v1', revision='8',
                            isVirtualLink='true', virtualLinkId='current-link')
        self.related['current-v1'] = [content('current-v1')]
        self.events.clear()
        with patch.object(self.client, 'related', wraps=original) as related:
            self.assertEqual(self.run_stage(), ('done', 0))
        related.assert_called_once_with('current-v1', 'current-link')
        self.assertEqual(self.events, ['page:0', 'related:current-v1', 'download:current-v1'])
        self.assertEqual(self.ledger.one("SELECT active FROM documents WHERE id='v1'")['active'], 0)
        self.assertEqual(self.ledger.one("SELECT status FROM files WHERE id='v2'")['status'], 'done')

    def test_incomplete_failed_folder_refresh_never_changes_existing_inventory(self):
        self.daily()
        with self.ledger.db:
            self.ledger.db.execute("UPDATE documents SET related_state='failed' WHERE id='v1'")
            self.ledger.db.execute("UPDATE workflows SET phase='associations'")
        before = [tuple(row) for row in self.ledger.all('SELECT * FROM documents ORDER BY id')]
        def page(folder, start, limit):
            if start:
                raise requests.ReadTimeout('partial folder response')
            return 2, [dict(self.docs[0], id='current-v1')]
        with patch.object(self.client, 'document_page', side_effect=page):
            with self.assertRaises(requests.ReadTimeout):
                self.run_stage()
        self.assertEqual([tuple(row) for row in self.ledger.all('SELECT * FROM documents ORDER BY id')], before)
        self.assertEqual(self.ledger.one('SELECT COUNT(*) n FROM association_folder_refreshes')['n'], 0)

    def test_persistent_association_failure_refreshes_its_folder_only_every_thirty_minutes(self):
        self.daily()
        original = self.client.related
        def failure(identifier, virtual=None):
            if identifier == 'v1':
                raise RuntimeError('persistent failure')
            return original(identifier, virtual)
        with patch.object(self.client, 'related', side_effect=failure), patch('sync.time.sleep'):
            self.run_stage()
            self.events.clear()
            self.run_stage()
            self.assertIn('page:0', self.events)
            self.events.clear()
            self.run_stage()
            self.assertNotIn('page:0', self.events)
            self.at += timedelta(minutes=31)
            self.events.clear()
            self.run_stage()
            self.assertIn('page:0', self.events)

    def test_unchecked_replacement_and_removed_version_never_download(self):
        self.daily()
        self.docs[1]=document('v3')
        self.related['v3']=[content('v3')]
        original=self.client.related
        def related(identifier,virtual=None):
            if identifier=='v3':raise RuntimeError('unchecked new version')
            return original(identifier,virtual)
        with patch.object(self.client,'related',side_effect=related),patch('sync.time.sleep'):
            self.assertEqual(self.run_stage(),('inventory_incomplete',2))
        self.assertEqual([e for e in self.events if e.startswith('download:')],['download:v1'])
        self.assertEqual(self.ledger.one("SELECT active FROM files WHERE id='v2'")['active'],0)

    def test_max_files_is_global_across_batches(self):
        self.daily()
        with patch('sync.ASSOCIATION_BATCH_SIZE',1),patch('sync.INTERLEAVED_DOWNLOAD_BATCH_SIZE',1):
            self.assertEqual(self.run_stage(max_files=1),('download_pending',2))
        self.assertEqual(len([e for e in self.events if e.startswith('download:')]),1)
        self.assertIn('related:v2',self.events)

    def test_later_filename_collision_relocates_first_verified_download(self):
        self.daily()
        for values in self.related.values():values[0]['filename']='same.pdf'
        with patch('sync.ASSOCIATION_BATCH_SIZE',1):
            self.assertEqual(self.run_stage(),('done',0))
        rows=self.ledger.all("SELECT path,status FROM files WHERE active=1")
        self.assertEqual(len({row['path'].casefold() for row in rows}),2)
        for row in rows:
            self.assertEqual(row['status'],'done')
            self.assertEqual(resolve_path(row['path'],self.base).read_bytes(),b'pdf')

    def test_forbidden_file_is_not_retried_in_every_batch(self):
        self.daily()
        original=self.client.download
        calls=[]
        def download(item,*args,**kwargs):
            calls.append(item['id'])
            if item['id']=='v1':
                response=requests.Response();response.status_code=403
                raise requests.HTTPError('fixture forbidden',response=response)
            return original(item,*args,**kwargs)
        with patch.object(self.client,'download',side_effect=download),patch('sync.time.sleep'), \
             patch('sync.ASSOCIATION_BATCH_SIZE',1):
            self.assertEqual(self.run_stage(),('download_pending',2))
        self.assertEqual(calls.count('v1'),1)
        self.assertEqual(calls.count('v2'),1)
        self.assertEqual(self.run_stage(),('done',0))

    def test_scan_only_never_interleaves_downloads(self):
        self.daily()
        self.assertEqual(self.run_stage('scan'),('done',0))
        self.assertFalse(any(e.startswith('download:') for e in self.events))

    def test_association_visibility_update_does_not_write_unrelated_files(self):
        self.run_stage('scan')
        with self.ledger.db:
            self.ledger.db.executescript('''CREATE TEMP TABLE visibility_writes(id TEXT);
                CREATE TEMP TRIGGER visibility_audit AFTER UPDATE OF active ON files
                BEGIN INSERT INTO visibility_writes VALUES(new.id); END;''')
        doc=self.ledger.one("SELECT * FROM documents WHERE id='v1'")
        self.ledger.save_related(doc,[content('v1')])
        self.assertEqual({row[0] for row in self.ledger.all('SELECT id FROM visibility_writes')},{'v1'})
        self.assertEqual(self.ledger.one("SELECT active FROM files WHERE id='v2'")['active'],1)

    def test_shared_file_remains_visible_until_last_parent_removes_it(self):
        self.related['v1'].append(content('shared'))
        self.related['v2'].append(content('shared'))
        self.run_stage('scan')
        for identifier in ('v1','v2'):
            doc=self.ledger.one('SELECT * FROM documents WHERE id=?',(identifier,))
            self.ledger.save_related(doc,[content(identifier)])
            self.assertEqual(self.ledger.one("SELECT active FROM files WHERE id='shared'")['active'],int(identifier=='v1'))

    def test_missing_file_probe_does_not_hold_writer_lock_during_disk_scan(self):
        self.run_stage()
        rows=self.ledger.all("SELECT * FROM files WHERE project='p' AND active=1 AND status='done'")
        resolve_path(rows[0]['path'],self.base).unlink()
        with self.ledger.db:
            self.ledger.db.execute('CREATE TABLE writer_probe(value INTEGER)')
            self.ledger.db.execute('INSERT INTO writer_probe VALUES(0)')
        original=resolve_path
        def probe(path,base):
            if path==rows[1]['path']:
                with closing(sqlite3.connect(self.base/'runtime/ledger.sqlite3',timeout=.05)) as peer, peer:
                    peer.execute('UPDATE writer_probe SET value=1')
            return original(path,base)
        workflow=Workflow(self.ledger,self.client)
        with patch('reporting.resolve_path',side_effect=probe):
            self.assertEqual(workflow.repair_missing_downloads(),1)
        self.assertEqual(self.ledger.one('SELECT value FROM writer_probe')[0],1)
