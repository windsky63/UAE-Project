from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from manager import Manager,tail,decode_text


class LogEncodingTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='log-encoding-')
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)

    def test_utf8_utf16_powershell_and_gbk_logs(self):
        text='程序启动：中文路径\n失败原因：文件缺失\nRan 18 tests\nOK\n'
        for encoding in ('utf-8','utf-8-sig','utf-16','utf-16-be','utf-32','gb18030'):
            with self.subTest(encoding=encoding):
                path=self.root/(encoding+'.log')
                path.write_bytes(text.encode(encoding))
                self.assertEqual(tail(path),text)
                self.assertEqual(decode_text(path.read_bytes()),text)

    def test_bounded_unicode_tail_does_not_expose_partial_character(self):
        text='第一行：中文内容\n'*1000+'最后一行：正常结束\n'
        for encoding in ('utf-8','utf-16','utf-16-be','utf-16-le','utf-32','gb18030'):
            path=self.root/(encoding+'.log');path.write_bytes(text.encode(encoding))
            result=tail(path,size=201)
            self.assertTrue(result.endswith('最后一行：正常结束\n'))
            self.assertNotIn('\ufffd',result)
            self.assertNotIn('\x00',result)

    def test_log_filename_search_and_original_bytes_are_preserved(self):
        manager=Manager(self.root,self.root/'runtime')
        path=manager.runtime/'launcher_full_tests.log'
        raw='启动入口测试：中文路径正常\nRan 18 tests\nOK\n'.encode('utf-16')
        path.write_bytes(raw)
        rows=manager.logs(query='launcher_full_tests')['items']
        self.assertTrue(any('中文路径正常' in row['message'] for row in rows))
        self.assertTrue(all('\x00' not in row['message'] and '\ufffd' not in row['message'] for row in rows))
        self.assertEqual(path.read_bytes(),raw)

    def test_historical_logs_become_utf8_with_exact_backups_and_idempotence(self):
        from log_tools import normalize_logs
        folder=self.root/'管理中心/runtime'
        folder.mkdir(parents=True)
        text='历史测试日志：中文编码正常\n'
        raw=text.encode('utf-16')
        old=folder/'launcher_full_tests.log';old.write_bytes(raw)
        valid=folder/'current.log';valid.write_text(text,encoding='utf-8')
        result=normalize_logs(self.root)
        self.assertEqual(len(result['converted']),1)
        self.assertEqual(old.read_bytes(),text.encode('utf-8'))
        self.assertEqual((self.root/result['converted'][0]['backup']).read_bytes(),raw)
        self.assertEqual(normalize_logs(self.root)['converted'],[])


if __name__=='__main__':unittest.main()
