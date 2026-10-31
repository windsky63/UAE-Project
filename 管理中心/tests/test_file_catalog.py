from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager,TOOLS


class FileCatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='file-catalog-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.manager=Manager(self.root,self.root/'admin-runtime')
        self.base=self.manager.folder('sync')
        for name in ('runtime','downloads','app'):(self.base/name).mkdir(parents=True,exist_ok=True)
        actual=Path(__file__).resolve().parents[2]/TOOLS['sync']['folder']
        for name in ('app/business_type.py','document_coding_rules.json'):
            shutil.copyfile(actual/name,self.base/name)
        self.ledger=self.base/'runtime/ledger.sqlite3'
        self.layout='30201_50150_LT_HE-H4-262-13-20-001'
        self.electric='30201_50150_LT_HE-H1-001-16-60-100'
        with closing(sqlite3.connect(self.ledger)) as db,db:
            db.executescript('''
              CREATE TABLE folders(project,id,path,active);
              CREATE TABLE documents(project,id,folder_id,reference,title,revision,active);
              CREATE TABLE document_files(project,document_id,folder_id,file_id,active);
              CREATE TABLE files(project,id,filename,path,bytes,size,sha256,status,active,revision,relation,downloaded_at,updated,error);
              INSERT INTO folders VALUES('p','folder','Engineering/Piping',1);
              INSERT INTO documents VALUES('p','layout','folder','''+"'"+self.layout+"'"+''','布局标题','A',1);
              INSERT INTO documents VALUES('p','electric','folder','''+"'"+self.electric+"'"+''','桥架标题','B',1);
            ''')
            for index in range(32):
                parent='layout' if index%2==0 or index==31 else 'electric'
                number=self.layout if parent=='layout' else self.electric
                filename=number+f'__{index:03d}.pdf' if index<30 else f'vendor-{index}.zip'
                body=b'x'*(index+1)
                (self.base/'downloads'/filename).write_bytes(body)
                db.execute('INSERT INTO files VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('p',str(index),filename,
                    'downloads/'+filename,len(body),len(body),hashlib.sha256(body).hexdigest(),
                    'pending' if index==29 else 'done',1,'R'+str(index),'attachment',
                    '2026-10-27T01:00:00Z' if index<30 else None,'2026-10-27T10:00:00+08:00',None))
                db.execute('INSERT INTO document_files VALUES(?,?,?,?,1)',('p',parent,'folder',str(index)))
            db.execute("INSERT INTO document_files VALUES('p','electric','folder','30',1)")
            db.execute("INSERT INTO document_files VALUES('p','electric','folder','31',0)")
            db.execute("INSERT INTO document_files VALUES('p','electric','folder','0',1)")

    def test_business_fields_active_parents_and_readonly(self):
        before=self.ledger.read_bytes()
        result=self.manager.files(limit=100)
        self.assertEqual(result['total'],32)
        self.assertTrue(result['classification_available'])
        by_id={r['file_id']:r for r in result['items']}
        self.assertEqual(by_id['0']['business_type'],'管道布置平面及立面图')
        self.assertEqual(by_id['0']['business_source'],'文件名')
        self.assertEqual(by_id['0']['unit'],'H4-262')
        self.assertEqual(by_id['0']['downloaded_at'],'2026-10-27 09:00:00+08:00')
        self.assertEqual(by_id['30']['business_status'],'冲突待核对')
        self.assertEqual(by_id['31']['business_status'],'继承关联文档（内容未核验）')
        self.assertIn('未核验',by_id['31']['business_reason'])
        self.assertEqual(by_id['29']['download_status'],'待处理')
        self.assertNotEqual(by_id['29']['state'],'ready')
        self.assertEqual(self.ledger.read_bytes(),before)

    def test_cross_column_filters_and_sort_cover_all_pages(self):
        filters={'business_type':{'values':['管道布置平面及立面图']},'extension':{'values':['.pdf']}}
        result=self.manager.files(filters=filters,sort='bytes',direction='desc',limit=2,offset=10)
        self.assertEqual(result['total'],15)
        self.assertEqual([r['bytes'] for r in result['items']],[9,7])
        self.assertEqual(result['unfiltered_total'],32)
        self.assertEqual(self.manager.files(filters={'business_type':{'values':[]}})['total'],0)
        self.assertEqual(self.manager.files(filters={'extension':{'exclude_values':['.pdf']}})['total'],2)
        self.assertEqual(self.manager.files(query='桥架标题',filters={'extension':{'values':['.pdf']}})['total'],16)

    def test_facets_ignore_their_own_filter_and_keep_other_columns(self):
        filters={'business_type':{'values':['管道布置平面及立面图']},'extension':{'values':['.pdf']}}
        values=self.manager.files(filters=filters,facet='business_type',limit=100)
        self.assertEqual({r['value']:r['count'] for r in values['items']},
            {'管道布置平面及立面图':15,'电缆桥架布置图':15})
        types=self.manager.files(filters=filters,facet='extension')
        self.assertEqual({r['value']:r['count'] for r in types['items']},{'.pdf':15,'.zip':1})
        self.assertEqual(self.manager.files(facet='name',facet_search='vendor',limit=1)['total'],2)

    def test_numeric_date_blank_filters_and_validation(self):
        self.assertEqual(self.manager.files(filters={'bytes':{'operator':'between','value':'20','value2':'25'}})['total'],6)
        self.assertEqual(self.manager.files(filters={'downloaded_at':{'operator':'lte','value':'2026-10-26'}})['total'],0)
        self.assertEqual(self.manager.files(filters={'downloaded_at':{'operator':'empty'}})['total'],2)
        for kwargs in ({'sort':'name;DROP TABLE files'}, {'filters':{'bad':{'values':['x']}}},
                       {'filters':{'bytes':{'operator':'gte','value':'nan'}}}):
            with self.assertRaises(ValueError):self.manager.files(**kwargs)

    def test_missing_rules_are_explicit_and_never_guessed(self):
        (self.base/'document_coding_rules.json').unlink()
        result=self.manager.files(refresh=True)
        self.assertFalse(result['classification_available'])
        self.assertIn('完整更新',result['warning'])
        self.assertTrue(all(r['business_type']=='规则未就绪' for r in result['items']))

    def test_member_ledger_search_filters_preview_and_parent_version_checks(self):
        actual=Path(__file__).resolve().parents[2]/TOOLS['sync']['folder']
        shutil.copyfile(actual/'app/archive_index.py',self.base/'app/archive_index.py')
        with closing(sqlite3.connect(self.ledger)) as db, db:
            parent=db.execute("SELECT filename,path FROM files WHERE id='30'").fetchone()
            archive=self.base/parent[1]
            with zipfile.ZipFile(archive,'w') as z:z.writestr('notes/inside.txt','verified member')
            sha=hashlib.sha256(archive.read_bytes()).hexdigest()
            db.execute("UPDATE files SET bytes=?,size=?,sha256=? WHERE id='30'",(archive.stat().st_size,archive.stat().st_size,sha))
        cache=self.base/'runtime/file_analysis'
        run=cache/'jobs/job/run';(run/'extracted/files').mkdir(parents=True)
        child=run/'extracted/files/inside.txt';child.write_text('verified member',encoding='utf-8')
        origin=parent[0]+' / '+str(archive)
        report=dict(files=[dict(origin=origin,path=str(archive),sha256=sha)],archive_members=[dict(
            project='p',archive=origin,entry=1,member='notes/inside.txt',output='notes/inside.txt',
            extracted_path=str(child),size=child.stat().st_size,sha256=hashlib.sha256(child.read_bytes()).hexdigest(),state='已解压')])
        (run/'analysis.json').write_text(json.dumps(report),encoding='utf-8')
        with closing(sqlite3.connect(cache/'queue.sqlite3')) as db, db:
            db.execute('CREATE TABLE jobs(key,project,file_id,path,origin,expected_sha,state,active,updated,result_path,error)')
            db.execute("INSERT INTO jobs VALUES('job','p','30',?,?,?,'done',1,1,'jobs/job/run/analysis.json',NULL)",(str(archive),origin,sha))
        result=self.manager.files(query='inside.txt',refresh=True)
        self.assertEqual(result['total'],1)
        item=result['items'][0]
        self.assertEqual(item['kind'],'解压文件')
        self.assertEqual(item['parent_file_id'],'30')
        self.assertEqual(item['member_path'],'notes/inside.txt')
        self.assertEqual(self.manager.preview(item['id'])['text'],'verified member')
        self.assertEqual(self.manager.files(filters={'kind':{'values':['解压文件']}})['total'],1)
        with closing(sqlite3.connect(self.ledger)) as db, db:db.execute("UPDATE files SET sha256='new' WHERE id='30'")
        with self.assertRaises(ValueError):self.manager.artifact(item['id'])
        self.assertEqual(self.manager.files(query='inside.txt',refresh=True)['total'],0)


if __name__=='__main__':unittest.main()
