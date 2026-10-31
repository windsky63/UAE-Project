"""Offline manual-test artifact selection and ordered delivery, using fake senders."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from wechat_test_send import latest_artifacts, send_latest_test
from wechat_push.errors import DeliveryNotSent, DeliveryUncertain


class ManualTestSendTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.socket = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.socket.start()
        self.addCleanup(self.socket.stop)

    def artifacts(self, day, *, demo=False, fixed=False):
        folder = self.base / 'logs' / ('test_push' if fixed else day)
        report = folder / '管道简报'
        report.mkdir(parents=True)
        data = ('测试推送文 ' + day).encode('utf-8-sig')
        (folder / '微信推送文.txt').write_bytes(data)
        marker = dict(date=day, generated_at=day+'T10:45:00+08:00', ready=False,
                      text_file='微信推送文.txt', text_sha256=hashlib.sha256(data).hexdigest())
        (folder / '推送状态.json').write_text(json.dumps(marker), encoding='utf-8')
        picture = report / 'image.png'
        Image.new('RGB', (10, 10), 'white').save(picture)
        state = dict(date=day, generated_at=day+'T10:50:00+08:00', ready=not demo, demo=demo,
                     images=[dict(path=str(picture), sha256=hashlib.sha256(picture.read_bytes()).hexdigest())])
        (report / '简报推送状态.json').write_text(json.dumps(state), encoding='utf-8')
        return folder, picture

    def test_fixed_samples_are_fallback_only_even_if_newer(self):
        self.artifacts('2026-10-21', demo=True, fixed=True)
        text, report = latest_artifacts(self.base)
        self.assertTrue(text['fallback'])
        self.assertTrue(report['fallback'])
        self.artifacts('2026-10-20')
        text, report = latest_artifacts(self.base)
        self.assertEqual((text['date'], report['date']), ('2026-10-20', '2026-10-20'))
        self.assertFalse(text['fallback'])
        self.assertFalse(report['fallback'])

    def test_each_missing_item_can_independently_use_fixed_sample(self):
        self.artifacts('2026-10-21', demo=True, fixed=True)
        folder, _ = self.artifacts('2026-10-20')
        (folder / '管道简报/简报推送状态.json').unlink()
        text, report = latest_artifacts(self.base)
        self.assertFalse(text['fallback'])
        self.assertTrue(report['fallback'])

    def test_fixed_marker_relative_paths_work_after_deployment(self):
        folder, picture = self.artifacts('2026-10-21', demo=True, fixed=True)
        marker = picture.parent / '简报推送状态.json'
        data = json.loads(marker.read_text(encoding='utf-8'))
        data['images'][0]['path'] = picture.name
        marker.write_text(json.dumps(data), encoding='utf-8')
        sender = Mock()
        result = send_latest_test(self.base, '文件传输助手', 'profile', lambda:sender)
        self.assertEqual(result['state'], 'submitted')
        self.assertIn('推送文使用固定测试文件', sender.send.call_args.args[1])
        self.assertIn('简报使用固定测试文件', sender.send.call_args.args[1])
        sender.send_image.assert_called_once_with('文件传输助手', str(picture.resolve()))

    def test_latest_valid_sources_are_selected_and_send_text_before_images(self):
        self.artifacts('2026-10-20')
        folder, picture = self.artifacts('2026-10-21', demo=True)
        sender = Mock()
        result = send_latest_test(self.base, '文件传输助手', 'profile', lambda:sender)
        self.assertEqual(result['state'], 'submitted')
        self.assertEqual([call[0] for call in sender.method_calls], ['send', 'send_image', 'close'])
        self.assertIn('2026-10-21', sender.send.call_args.args[1])
        self.assertIn('模拟预览', sender.send.call_args.args[1])
        sender.send_image.assert_called_once_with('文件传输助手', str(picture.resolve()))
        self.assertFalse((self.base / 'runtime/wechat_delivery/deliveries.sqlite3').exists())

    def test_invalid_newer_artifacts_do_not_replace_valid_older_sources(self):
        self.artifacts('2026-10-20')
        folder, picture = self.artifacts('2026-10-21')
        picture.write_bytes(b'tampered')
        (folder / '微信推送文.txt').write_text('changed', encoding='utf-8')
        text, report = latest_artifacts(self.base)
        self.assertEqual((text['date'], report['date']), ('2026-10-20', '2026-10-20'))

    def test_missing_or_external_image_prevents_any_message(self):
        sender = Mock()
        with self.assertRaises(DeliveryNotSent):
            send_latest_test(self.base, '文件传输助手', 'profile', lambda:sender)
        folder, picture = self.artifacts('2026-10-20')
        external = self.base / 'outside.png'
        external.write_bytes(picture.read_bytes())
        marker = folder / '管道简报/简报推送状态.json'
        state = json.loads(marker.read_text(encoding='utf-8'))
        state['images'][0]['path'] = str(external)
        marker.write_text(json.dumps(state), encoding='utf-8')
        with self.assertRaises(DeliveryNotSent):
            send_latest_test(self.base, '文件传输助手', 'profile', lambda:sender)
        sender.send.assert_not_called()

    def test_uncertain_text_stops_images_and_is_not_automatically_retried(self):
        self.artifacts('2026-10-20')
        sender = Mock()
        sender.send.side_effect = DeliveryUncertain('manual inspection needed')
        with self.assertRaises(DeliveryUncertain):
            send_latest_test(self.base, '文件传输助手', 'profile', lambda:sender)
        sender.send.assert_called_once()
        sender.send_image.assert_not_called()
        result = json.loads((self.base / 'runtime/wechat_test/latest_result.json').read_text(encoding='utf-8'))
        self.assertEqual(result['state'], 'uncertain')


if __name__ == '__main__':
    unittest.main()
