import hashlib
from pathlib import Path
import unittest

from sync import download, priority_download_keys, priority_unit_match
from tests import test_download_replacement as replacement


class UnitMatchingTests(unittest.TestCase):
    def test_requested_units_match_zero_padded_project_codes(self):
        for number in ('213','027','037','038','039','27','37','38','39'):
            with self.subTest(number=number):
                self.assertTrue(priority_unit_match('30201_50150_LT_HE-H2-'+number+'-14-33-001.pdf'))

    def test_isometrics_and_explicit_unit_folders_are_recognized(self):
        for text in ('4-213-52-0001-CD-PAD.pdf','2-27-52-0001-CD-PAD.pdf',
                     'downloads/HABSHANDBN/Unit 037/attachments.zip','单元：038','39单元/资料.zip'):
            with self.subTest(text=text):self.assertTrue(priority_unit_match(text))

    def test_numbers_in_dates_ids_and_drawing_serials_are_not_units(self):
        for text in ('report_20261027.pdf','file213.pdf','H4-127-001.pdf','H4-2130-001.pdf',
                     '30201_50150_LT_HE-H4-262-14-33-027.pdf','H2-0370.pdf','Unit 0278','127单元'):
            with self.subTest(text=text):self.assertFalse(priority_unit_match(text))


class PriorityQueueTests(unittest.TestCase):
    def setUp(self):
        self.case=replacement.ReplacementTests()
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        self.calls=[]
        def receive(item,target,progress=None,verified=None):
            self.calls.append(item['id'])
            target=Path(target);target.write_bytes(b'new')
            return dict(bytes=3,path=str(target),sha256=hashlib.sha256(b'new').hexdigest())
        self.case.client.download.side_effect=receive

    def scan(self, entries, old_revision='0', old_name='drawing.pdf'):
        docs=[replacement.doc(revision=old_revision,fileName=old_name)]
        files=[replacement.content(revision=old_revision,filename=old_name)]
        for identifier, filename, reference in entries:
            docs.append(replacement.doc(identifier,firstVersionId=identifier,fileName=filename,reference=reference))
            files.append(replacement.content(identifier,filename=filename))
        self.case.scan(docs,files)

    def test_priority_group_precedes_other_files_even_if_inserted_later(self):
        self.scan([('ordinary','H4-262.pdf','OTHER'),('unit213','H4-213.pdf','FIRST'),
                   ('unit27','H2-027.pdf','SECOND'),('unit39','H1-039.pdf','THIRD')])
        download(self.case.client,self.case.ledger)
        self.assertEqual(self.calls,['unit213','unit27','unit39','ordinary'])

    def test_three_tiers_are_priority_units_then_new_then_updated(self):
        self.scan([('ordinary','ordinary.pdf','OTHER'),('priority38','H2-038.pdf','PRIORITY')],old_revision='1')
        download(self.case.client,self.case.ledger)
        self.assertEqual(self.calls,['priority38','ordinary','v1'])

    def test_attachment_inherits_priority_from_parent_reference(self):
        self.scan([('ordinary','ordinary.pdf','OTHER'),('attachment','attachments.zip','608200-DWG-H4-213-0018')])
        download(self.case.client,self.case.ledger,max_files=1)
        self.assertEqual(self.calls,['attachment'])

    def test_generic_attachment_in_unit_folder_is_prioritized(self):
        self.scan([('ordinary','ordinary.pdf','OTHER'),('attachment','attachments.zip','GENERIC')])
        with self.case.ledger.db:
            self.case.ledger.db.execute("INSERT INTO folders(project,id,name,path,active) VALUES('p','unit','Unit 038','project/Unit 038',1)")
            self.case.ledger.db.execute("UPDATE documents SET folder_id='unit' WHERE id='attachment'")
            self.case.ledger.db.execute("UPDATE document_files SET folder_id='unit' WHERE file_id='attachment'")
        self.assertIn(('p','attachment'),priority_download_keys(self.case.ledger,'p'))
        self.assertNotIn(('p','ordinary'),priority_download_keys(self.case.ledger,'p'))
        self.assertEqual(priority_download_keys(self.case.ledger,'other-project'),[])

    def test_priority_update_precedes_ordinary_new_file_and_preserves_old_version(self):
        self.scan([('ordinary','ordinary.pdf','OTHER')],old_revision='1',old_name='H4-213.pdf')
        download(self.case.client,self.case.ledger)
        self.assertEqual(self.calls,['v1','ordinary'])
        old_path=self.case.ledger.one('SELECT path FROM file_history')[0]
        self.assertEqual((self.case.base/old_path).read_bytes(),b'old')

    def test_within_priority_group_new_file_still_precedes_update(self):
        self.scan([('new27','H2-027.pdf','PRIORITY')],old_revision='1',old_name='H4-213.pdf')
        download(self.case.client,self.case.ledger)
        self.assertEqual(self.calls,['new27','v1'])
