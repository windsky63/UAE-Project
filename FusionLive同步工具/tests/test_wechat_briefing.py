"""Only fake desktop controls/senders; never log in, download, or send a real message."""
from datetime import datetime, timezone
from contextlib import nullcontext, closing
import hashlib
import json
import sqlite3
from pathlib import Path
import tempfile
import sys
import types
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from briefing_schedule import DailyBriefing, LOCAL
from push_settings import DEFAULT, load_settings, save_settings
from wechat_delivery import DeliveryJournal, deliver, snapshot_push_text
from wechat_push.errors import DeliveryNotSent, DeliveryUncertain
from wechat_push.uia import WindowsUIASender


class BriefingPushTests(unittest.TestCase):
    def test_text_can_send_before_image_preparation_time_without_prebuilt_text(self):
        save_settings({**self.settings,'prepare_time_beijing':'10:40',
                       'text_send_time_beijing':'10:00','briefing_send_time_beijing':'12:00'},self.base)
        service,factory,send=self.service()
        service.tick(self.at.replace(hour=9,minute=59))
        send.assert_not_called()
        service.tick(self.at.replace(hour=10))
        send.assert_called_once()
        factory.assert_not_called()
        self.assertTrue(send.call_args.args[2]['publish_text'])
        self.assertFalse(send.call_args.args[2]['publish_briefing'])
    def test_send_creates_backup_before_desktop_and_retry_reuses_it(self):
        path=self.folder/'文字发送备份.json'
        self.assertFalse(path.exists())
        sender=Mock()
        def fail_after_check(recipient,text):
            self.assertTrue(path.is_file())
            self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['text'],text)
            raise DeliveryNotSent('known not sent')
        sender.send.side_effect=fail_after_check
        first=deliver(self.base,self.marker,self.settings,self.at,lambda:sender)
        self.assertFalse(first['complete'])
        frozen=path.read_bytes()
        original=self.daily_text
        self.daily_text='new statistics after the failed attempt'
        sender.send.side_effect=None
        result=deliver(self.base,self.marker,self.settings,self.at,lambda:sender)
        self.assertTrue(result['complete'])
        self.assertEqual(sender.send.call_args.args[1],original)
        self.assertEqual(path.read_bytes(),frozen)

    def test_corrupt_send_backup_blocks_sender_and_is_not_regenerated(self):
        path=self.folder/'文字发送备份.json'
        path.write_text('{"bad":"backup"}',encoding='utf-8')
        sender=Mock()
        with self.assertRaisesRegex(DeliveryNotSent,'备份校验失败'):
            deliver(self.base,self.marker,self.settings,self.at,lambda:sender)
        sender.send.assert_not_called()

    def test_report_only_ledger_writes_do_not_force_final_report(self):
        database = self.base/'runtime/ledger.sqlite3'
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE files(project,id,filename,path,status,bytes,sha256,active)')
            db.execute("INSERT INTO files VALUES('p','one','one.pdf',NULL,'pending',0,NULL,1)")
            db.execute('CREATE TABLE report_local_versions(value INTEGER)')
            db.execute('INSERT INTO report_local_versions VALUES(0)')
        service, factory, _ = self.service()
        self.prepare(service)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('UPDATE report_local_versions SET value=1')
        service.tick(self.at.replace(hour=10, minute=55))
        factory.assert_called_once()
        self.assertEqual(service.state['generation_slots']['final']['status'], 'reused')

    def test_successful_draft_is_not_regenerated_each_minute_or_after_restart(self):
        service, factory, _ = self.service()
        self.prepare(service)
        for minute in range(41, 55):
            service.tick(self.at.replace(hour=10, minute=minute))
        factory.assert_called_once()
        restarted, again, _ = self.service()
        restarted.tick(self.at.replace(hour=10, minute=54))
        restarted.tick(self.at.replace(hour=10, minute=55))
        again.assert_not_called()
        self.assertEqual(restarted.state['generation_slots']['final']['status'], 'reused')

    def test_changed_analysis_gets_only_one_final_update(self):
        service, factory, _ = self.service()
        self.prepare(service)
        database = self.base/'runtime/file_analysis/queue.sqlite3'
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE analysis_exports(name TEXT PRIMARY KEY,revision INTEGER)')
            db.execute("INSERT INTO analysis_exports VALUES('results',1)")
        service.tick(self.at.replace(hour=10, minute=55))
        service.tick(self.at.replace(hour=10, minute=55, second=2))
        self.assertEqual(factory.call_count, 2)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE analysis_exports SET revision=2 WHERE name='results'")
        for minute in (56, 57, 58, 59):
            service.tick(self.at.replace(hour=10, minute=minute))
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(service.state['generation_slots']['final']['status'], 'complete')

    def test_export_metadata_writes_do_not_force_final_report(self):
        database = self.base/'runtime/file_analysis/queue.sqlite3'
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute('CREATE TABLE analysis_exports(name TEXT PRIMARY KEY,revision INTEGER)')
            db.executemany('INSERT INTO analysis_exports VALUES(?,?)', [('results',1),('ledger',0)])
        service, factory, _ = self.service()
        self.prepare(service)
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("UPDATE analysis_exports SET revision=1 WHERE name='ledger'")
        service.tick(self.at.replace(hour=10, minute=55))
        factory.assert_called_once()
        self.assertEqual(service.state['generation_slots']['final']['status'], 'reused')

    def test_text_is_not_pre_generated_each_minute(self):
        service, _, _ = self.service()
        self.prepare(service)
        path = self.folder/'微信推送文快照.json'
        self.assertFalse(path.exists())
        service.tick(self.at.replace(hour=10, minute=41))
        self.assertFalse(path.exists())
        self.assertFalse((self.folder/'文字发送备份.json').exists())

    def test_generation_failures_have_bounded_attempts_per_slot(self):
        service, factory, _ = self.service()
        factory.return_value.returncode = 1
        for minute in range(40, 55):
            service.tick(self.at.replace(hour=10, minute=minute))
            service.tick(self.at.replace(hour=10, minute=minute, second=2))
        self.assertEqual(factory.call_count, 3)
        self.assertEqual(service.state['generation_slots']['draft']['attempts'], 3)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.at = datetime(2026, 10, 23, 11, tzinfo=LOCAL)
        self.daily_text='fixture live daily'
        self.daily_ready=True
        self.live_source=patch('file_inventory.read_daily_snapshot',side_effect=self.snapshot_daily)
        self.live_source.start()
        self.addCleanup(self.live_source.stop)
        self.settings = {**DEFAULT, 'recipients': ['示例接收会话']}
        save_settings(self.settings, self.base)
        self.folder = self.base / 'logs/2026-10-23/管道简报'
        self.folder.mkdir(parents=True)
        self.marker = self.folder / '简报推送状态.json'
        self.picture = self.folder / 'report.png'
        Image.new('RGB', (32, 32), 'white').save(self.picture)
        self.state = dict(date='2026-10-23', ready=True, demo=False, analysis_complete=True,
            generated_at='2026-10-23T10:40:01+08:00', images=[dict(path=str(self.picture), sha256=hashlib.sha256(self.picture.read_bytes()).hexdigest())])
        self.write_marker()
        self.socket = patch('socket.socket', side_effect=AssertionError('network forbidden'))
        self.socket.start()
        self.addCleanup(self.socket.stop)

    def live_daily(self, base, at=None, *, require_ready=False):
        if require_ready and not self.daily_ready:
            raise ValueError('实时日报尚未就绪')
        return self.daily_text,dict(date='2026-10-23',generated_at=self.at.isoformat(),ready=self.daily_ready)

    def snapshot_daily(self, base, day=None, *, require_ready=False):
        text,state=self.live_daily(base,require_ready=require_ready)
        return dict(state,text=text)

    def test_daily_snapshot_generated_once_and_reused_after_restart(self):
        service,_,_=self.service()
        with patch('reporting.generate_daily_snapshot',return_value=dict(ready=True,generated_at=self.at.isoformat())) as generate:
            self.prepare(service)
            service.tick(self.at.replace(hour=10,minute=50))
            restarted,_,_=self.service()
            restarted.tick(self.at.replace(hour=10,minute=51))
            generate.assert_called_once()

    def write_marker(self):
        self.marker.write_text(json.dumps(self.state, ensure_ascii=False), encoding='utf-8')

    def test_manual_outside_window_keeps_dedup_and_date_validation(self):
        at = self.at.replace(hour=18)
        self.state['generated_at'] = '2026-10-23T17:00:00+08:00'
        self.write_marker()
        sender = Mock()
        with self.assertRaises(DeliveryNotSent):
            deliver(self.base, self.marker, self.settings, at, lambda:sender)
        sender.send.assert_not_called()
        result = deliver(self.base, self.marker, self.settings, at, lambda:sender, manual=True)
        self.assertTrue(result['complete'])
        sender.send.assert_called_once()
        sender.send_image.assert_called_once()
        again = Mock()
        result = deliver(self.base, self.marker, self.settings, at, lambda:again, manual=True)
        self.assertEqual(result['already_submitted'],2)
        again.send.assert_not_called()
        again.send_image.assert_not_called()
        self.state['date'] = '2026-10-22'
        self.write_marker()
        with self.assertRaises(DeliveryNotSent):
            deliver(self.base, self.marker, self.settings, at, lambda:again, manual=True)

    def test_manual_retry_does_not_resend_uncertain_submission(self):
        sender = Mock()
        sender.send.side_effect = RuntimeError('fixture ambiguous UI')
        first = deliver(self.base, self.marker, self.settings, self.at, lambda:sender, manual=True)
        self.assertEqual(first['uncertain'],1)
        again = Mock()
        second = deliver(self.base, self.marker, self.settings, self.at, lambda:again, manual=True)
        self.assertEqual(second['uncertain'],1)
        again.send.assert_not_called()
        again.send_image.assert_not_called()

    def service(self, result=None):
        process = Mock(returncode=0)
        process.poll.return_value = 0
        factory = Mock(return_value=process)
        send = Mock(return_value=result or dict(complete=True, submitted=1, failed=0, uncertain=0))
        service = DailyBriefing(self.base, Mock(), process_factory=factory, delivery=send, analysis_start=Mock())
        self.addCleanup(service.close)
        return service, factory, send

    def test_independent_text_and_image_times_survive_restart_without_duplicates(self):
        save_settings({**self.settings,'text_send_time_beijing':'11:00','briefing_send_time_beijing':'12:00'},self.base)
        service,_,send=self.service()
        self.prepare(service)
        service.tick(self.at)
        self.assertEqual(send.call_count,1)
        self.assertTrue(send.call_args.args[2]['publish_text'])
        self.assertFalse(send.call_args.args[2]['publish_briefing'])
        service.tick(self.at.replace(hour=12))
        self.assertEqual(send.call_count,2)
        self.assertFalse(send.call_args.args[2]['publish_text'])
        self.assertTrue(send.call_args.args[2]['publish_briefing'])
        restarted,_,again=self.service()
        restarted.tick(self.at.replace(hour=12,minute=1))
        again.assert_not_called()

    def test_images_can_be_sent_before_text_and_time_changes_are_read_live(self):
        save_settings({**self.settings,'text_send_time_beijing':'12:00','briefing_send_time_beijing':'11:00'},self.base)
        service,_,send=self.service()
        self.prepare(service)
        service.tick(self.at)
        self.assertEqual(send.call_count,1)
        self.assertTrue(send.call_args.args[2]['publish_briefing'])
        saved=load_settings(self.base)
        save_settings({**saved,'text_send_time_beijing':'12:30'},self.base)
        service.tick(self.at.replace(hour=12))
        self.assertEqual(send.call_count,1)
        service.tick(self.at.replace(hour=12,minute=30))
        self.assertEqual(send.call_count,2)
        self.assertTrue(send.call_args.args[2]['publish_text'])
        self.assertFalse(send.call_args.args[2]['publish_briefing'])

    def test_legacy_custom_send_time_is_inherited_by_both_publications(self):
        save_settings({**self.settings,'send_time_beijing':'13:20'},self.base)
        saved=load_settings(self.base)
        self.assertEqual(saved['text_send_time_beijing'],'13:20')
        self.assertEqual(saved['briefing_send_time_beijing'],'13:20')

    def test_invalid_independent_time_is_rejected_before_overwriting_configuration(self):
        path=self.base/'runtime/wechat_push.json'
        before=path.read_bytes()
        with self.assertRaises(ValueError):
            save_settings({**self.settings,'text_send_time_beijing':'25:00'},self.base)
        self.assertEqual(path.read_bytes(),before)

    def test_text_uncertain_delivery_is_not_retried_at_its_new_time(self):
        save_settings({**self.settings,'text_send_time_beijing':'11:00','briefing_send_time_beijing':'12:00'},self.base)
        service,_,send=self.service(dict(complete=False,submitted=0,failed=0,uncertain=1))
        self.prepare(service)
        service.tick(self.at)
        self.assertEqual(service.state['text_publication']['phase'],'uncertain')
        save_settings({**load_settings(self.base),'text_send_time_beijing':'11:30'},self.base)
        service.tick(self.at.replace(minute=30))
        self.assertEqual(send.call_count,1)

    def prepare(self, service):
        service.tick(self.at.replace(hour=10, minute=40))
        service.tick(self.at.replace(hour=10, minute=40, second=2))

    def test_prepare_before_eleven_send_at_eleven_beijing_and_restart_dedup(self):
        service, factory, send = self.service()
        service.tick(self.at.replace(hour=10, minute=39))
        factory.assert_not_called()
        self.prepare(service)
        self.assertEqual(service.state['phase'], 'prepared')
        send.assert_not_called()
        service.tick(self.at.astimezone(timezone.utc))
        send.assert_called_once()
        self.assertEqual(service.state['phase'], 'submitted')
        restarted, _, send_again = self.service()
        restarted.tick(self.at.replace(minute=1))
        send_again.assert_not_called()

    def test_invalid_report_is_replaced_by_pending_notice_before_deadline(self):
        for change in ({'ready': False}, {'generated_at': '2026-10-23T11:00:01+08:00'}, {'demo': True}):
            self.state.update(change)
            self.write_marker()
            service, _, send = self.service()
            with patch('briefing.datetime', wraps=datetime) as clock:
                clock.now.return_value = self.at.replace(hour=10, minute=40)
                self.prepare(service)
            service.tick(self.at)
            send.assert_called_once()
            self.assertEqual(service.state['phase'], 'submitted')
            frozen = json.loads(send.call_args.args[1].read_text(encoding='utf-8'))
            self.assertTrue(frozen['fallback_notice'])
            self.assertFalse(frozen['analysis_complete'])
            self.state.update(ready=True, demo=False, generated_at='2026-10-23T10:40:01+08:00')
            service.state_path.unlink(missing_ok=True)

    def test_migration_keeps_text_and_image_publication_and_restart_dedup(self):
        marker = self.base / 'runtime/layout.pending'
        marker.write_text('unfinished migration', encoding='utf-8')
        (self.base / 'runtime/ledger.sqlite3').touch()
        sender = Mock()
        service, factory, send = self.service()
        send.side_effect = lambda *args, **kwargs: deliver(*args, **kwargs, sender_factory=lambda: sender)
        self.prepare(service)
        factory.assert_called_once()
        self.assertFalse((self.folder / '微信推送文快照.json').exists())
        service.tick(self.at)
        self.assertEqual(service.state['phase'], 'submitted')
        sender.send.assert_called_once()
        sender.send_image.assert_called_once()
        service.analysis_start.assert_not_called()
        self.assertEqual(marker.read_text(encoding='utf-8'), 'unfinished migration')
        self.assertIn('推送文和缓存简报继续', (self.folder / '定时生成.log').read_text(encoding='utf-8'))
        restarted, _, send_again = self.service()
        restarted.tick(self.at.replace(minute=1))
        send_again.assert_not_called()
        marker.unlink()
        restarted.tick(self.at.replace(minute=2))
        restarted.analysis_start.assert_called_once()

    def test_migration_still_generates_pending_notice_without_a_ready_report(self):
        (self.base / 'runtime/layout.pending').touch()
        self.marker.unlink()
        service, factory, send = self.service()
        with patch('briefing.datetime', wraps=datetime) as clock:
            clock.now.return_value = self.at.replace(hour=10, minute=40)
            self.prepare(service)
        factory.assert_called_once()
        service.tick(self.at)
        send.assert_called_once()
        frozen = json.loads(send.call_args.args[1].read_text(encoding='utf-8'))
        self.assertTrue(frozen['fallback_notice'])
        self.assertTrue(frozen['images'])
        self.assertFalse(frozen['analysis_complete'])

    def test_migration_keeps_text_only_publication_when_image_generation_fails(self):
        (self.base / 'runtime/layout.pending').touch()
        save_settings({**self.settings, 'publish_text': True, 'publish_briefing': False}, self.base)
        self.marker.unlink()
        service, _, send = self.service()
        with patch('briefing.generate_pending_notice', side_effect=OSError('image unavailable')):
            service.tick(self.at.replace(hour=10, minute=40))
        service.tick(self.at)
        send.assert_called_once()
        self.assertEqual(service.state['phase'], 'submitted')
        frozen = json.loads(send.call_args.args[1].read_text(encoding='utf-8'))
        self.assertEqual(frozen['images'], [])
        self.assertNotIn('wechat_push_text',frozen)

    def test_known_not_sent_retries_bounded_and_uncertain_stops(self):
        service, _, send = self.service()
        self.prepare(service)
        send.side_effect = DeliveryNotSent('desktop unavailable')
        for second in (0, 30, 60):
            from datetime import timedelta
            service.tick(self.at + timedelta(seconds=second))
        self.assertEqual(send.call_count, 3)
        self.assertEqual(service.state['phase'], 'skipped')

    def test_popup_settings_persist_targets_without_receiver_configuration(self):
        save_settings({**self.settings, 'briefing_title': '  项目管道简报  ', 'publish_text': False, 'publish_briefing': False,
                       'known_chats': ['示例接收会话', '其他会话']}, self.base)
        saved = load_settings(self.base)
        self.assertEqual(saved['recipients'], ['示例接收会话'])
        self.assertEqual(saved['briefing_title'], '项目管道简报')
        self.assertFalse(saved['enabled'])
        self.assertEqual(saved['known_chats'], ['示例接收会话', '其他会话'])
        self.assertEqual(saved['send_time_beijing'], '11:00')
        self.assertNotIn('database_root', saved)
        self.assertNotIn('listen_chats', saved)

    def test_saved_title_reaches_scheduled_generation(self):
        save_settings({**self.settings, 'briefing_title': '项目管道简报'}, self.base)
        service, factory, _ = self.service()
        service.tick(self.at.replace(hour=10, minute=40))
        command = factory.call_args.args[0]
        self.assertEqual(command[command.index('--title') + 1], '项目管道简报')

    def test_offline_analysis_waits_until_briefing_send_time_even_after_publication_finishes(self):
        (self.base / 'runtime/ledger.sqlite3').touch()
        service, _, _ = self.service()
        service.state = dict(date='2026-10-23', phase='submitted')
        for hour in (1, 12, 23):
            service.tick(self.at.replace(hour=hour))
        self.assertEqual(service.analysis_start.call_count, 2)

    def test_analysis_dispatch_follows_independent_briefing_time(self):
        save_settings({**self.settings, 'text_send_time_beijing': '11:00',
                       'briefing_send_time_beijing': '12:15'}, self.base)
        (self.base / 'runtime/ledger.sqlite3').touch()
        service, _, _ = self.service()
        service.state = dict(date='2026-10-23', phase='submitted')
        for hour, minute in ((10, 30), (11, 0), (12, 14)):
            service.tick(self.at.replace(hour=hour, minute=minute))
        service.analysis_start.assert_not_called()
        service.tick(self.at.replace(hour=12, minute=15))
        service.analysis_start.assert_called_once()

    def test_independent_publication_switch_combinations(self):
        for text, image in ((False, False), (True, False), (False, True), (True, True)):
            with self.subTest(text=text, image=image):
                settings = {**self.settings, 'publish_text': text, 'publish_briefing': image,
                            'recipients': [f'会话{text}{image}']}
                sender = Mock()
                if not (text or image):
                    with self.assertRaises(DeliveryNotSent):
                        deliver(self.base, self.marker, settings, self.at, lambda:sender)
                else:
                    result = deliver(self.base, self.marker, settings, self.at, lambda:sender)
                    self.assertTrue(result['complete'])
                self.assertEqual(sender.send.call_count, int(text))
                self.assertEqual(sender.send_image.call_count, int(image))
                if text and not image:
                    self.assertNotIn('以下发送简报图片', sender.send.call_args.args[1])

    def test_legacy_disabled_setting_migrates_to_both_switches_off(self):
        path = self.base / 'runtime/wechat_push.json'
        path.write_text(json.dumps({'enabled': False, 'recipients': []}), encoding='utf-8')
        saved = load_settings(self.base)
        self.assertFalse(saved['publish_text'])
        self.assertFalse(saved['publish_briefing'])

    def test_each_chat_selects_its_own_content_and_restart_deduplicates(self):
        settings = {**self.settings, 'recipients': ['文字群', '简报群', '全发群', '关闭群'],
                    'recipient_publications': {
                        '文字群': dict(publish_text=True, publish_briefing=False),
                        '简报群': dict(publish_text=False, publish_briefing=True),
                        '全发群': dict(publish_text=True, publish_briefing=True),
                        '关闭群': dict(publish_text=False, publish_briefing=False)}}
        save_settings(settings, self.base)
        saved = load_settings(self.base)
        self.assertEqual(saved['recipient_publications'], settings['recipient_publications'])
        sender = Mock()
        result = deliver(self.base, self.marker, saved, self.at, lambda: sender)
        self.assertEqual(result['submitted'], 4)
        self.assertEqual([c.args[0] for c in sender.send.call_args_list], ['文字群', '全发群'])
        self.assertEqual([c.args[0] for c in sender.send_image.call_args_list], ['简报群', '全发群'])
        again = Mock()
        result = deliver(self.base, self.marker, saved, self.at, lambda: again)
        self.assertEqual(result['already_submitted'], 4)
        again.send.assert_not_called()
        again.send_image.assert_not_called()

    def test_per_chat_text_only_schedule_and_global_switches(self):
        settings = {**self.settings, 'recipient_publications': {
            '示例接收会话': dict(publish_text=True, publish_briefing=False)}}
        save_settings(settings, self.base)
        self.picture.unlink()
        sender = Mock()
        deliver(self.base, self.marker, load_settings(self.base), self.at, lambda: sender)
        sender.send.assert_called_once()
        sender.send_image.assert_not_called()
        self.marker.unlink()
        service, _, send = self.service()
        with patch('briefing.generate_pending_notice', side_effect=OSError('no image')):
            service.tick(self.at.replace(hour=10, minute=40))
        service.tick(self.at)
        send.assert_called_once()
        save_settings({**settings, 'publish_text': False}, self.base)
        self.assertFalse(load_settings(self.base)['enabled'])

    def test_invalid_chat_switch_is_rejected(self):
        with self.assertRaises(ValueError):
            save_settings({**self.settings, 'recipient_publications': {
                '示例接收会话': dict(publish_text='false')}}, self.base)

    def test_text_only_publication_does_not_require_valid_images(self):
        self.picture.unlink()
        settings = {**self.settings, 'publish_text': True, 'publish_briefing': False}
        sender = Mock()
        result = deliver(self.base, self.marker, settings, self.at, lambda:sender)
        self.assertTrue(result['complete'])
        sender.send.assert_called_once()
        sender.send_image.assert_not_called()

    def test_text_only_schedule_survives_report_generation_failure(self):
        save_settings({**self.settings, 'publish_text': True, 'publish_briefing': False}, self.base)
        self.marker.unlink()
        service, factory, send = self.service()
        with patch('briefing.generate_pending_notice', side_effect=OSError('image unavailable')):
            service.tick(self.at.replace(hour=10, minute=40))
        factory.assert_not_called()
        self.assertFalse((self.folder/'文字发送备份.json').exists())
        service.tick(self.at)
        send.assert_called_once()
        frozen = json.loads(send.call_args.args[1].read_text(encoding='utf-8'))
        self.assertEqual(frozen['images'], [])
        self.assertNotIn('wechat_push_text',frozen)

    def test_start_at_1036_waits_until_prepare_and_records_process_lifecycle(self):
        service, factory, _ = self.service()
        service.tick(self.at.replace(hour=10, minute=36))
        factory.assert_not_called()
        self.prepare(service)
        factory.assert_called_once()
        content = (self.folder / '定时生成.log').read_text(encoding='utf-8')
        self.assertIn('准备=2026-10-23T10:40:00+08:00', content)
        self.assertIn('生成进程开始', content)
        self.assertIn('生成进程结束：退出码=0', content)
        self.assertIn('耗时=', content)

    def test_previous_day_skipped_does_not_block_restart_at_1036(self):
        path = self.base / 'runtime/briefing_schedule.json'
        path.write_text(json.dumps(dict(date='2026-10-22', phase='skipped', send_attempts=0,
            reason='当天简报及待分析通知均未能提前生成，请检查生成故障')), encoding='utf-8')
        service, factory, _ = self.service()
        service.tick(self.at.replace(hour=10, minute=36))
        persisted = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(persisted['date'], '2026-10-23')
        self.assertEqual(persisted['phase'], 'waiting')
        self.assertNotIn('reason', persisted)
        factory.assert_not_called()
        self.prepare(service)
        factory.assert_called_once()
        self.assertEqual(service.state['phase'], 'prepared')

    def test_failed_notice_retries_are_throttled_without_blocking_formal_report(self):
        self.marker.unlink()
        service, factory, _ = self.service()
        factory.return_value.poll.return_value = None
        with patch('briefing.generate_pending_notice', side_effect=OSError('notice failed')) as notice:
            service.tick(self.at.replace(hour=10, minute=40))
            service.tick(self.at.replace(hour=10, minute=40, second=1))
            notice.assert_called_once()
            service.tick(self.at.replace(hour=10, minute=41))
            self.assertEqual(notice.call_count, 2)
        factory.assert_called_once()

    def test_disabled_sending_still_prepares_a_local_report(self):
        save_settings({**self.settings, 'publish_text': False, 'publish_briefing': False}, self.base)
        service, factory, send = self.service()
        self.prepare(service)
        factory.assert_called_once()
        service.tick(self.at)
        send.assert_not_called()
        self.assertEqual(service.state['phase'], 'disabled')

    def test_emergency_pause_does_not_consume_scheduled_attempts(self):
        service, _, send = self.service()
        self.prepare(service)
        with patch('briefing_schedule.desktop_paused', return_value=True):
            service.tick(self.at)
            service.tick(self.at.replace(minute=1))
        self.assertEqual(service.state['send_attempts'], 0)
        send.assert_not_called()
        service.tick(self.at.replace(minute=2))
        send.assert_called_once()
        self.assertEqual(service.state['phase'], 'submitted')

    def test_emergency_pause_prevents_journal_claim_and_ui_submission(self):
        sender = Mock()
        with patch('wechat_delivery.desktop_paused', return_value=True):
            with self.assertRaises(DeliveryNotSent):
                deliver(self.base, self.marker, self.settings, self.at, lambda: sender)
        sender.send.assert_not_called()
        self.assertFalse((self.base / 'runtime/wechat_delivery/deliveries.sqlite3').exists())

    def test_emergency_pause_between_text_and_image_stops_remaining_items(self):
        sender = Mock()
        with patch('wechat_delivery.desktop_paused', side_effect=[False, False, True]):
            result = deliver(self.base, self.marker, self.settings, self.at, lambda: sender)
        self.assertEqual(result['submitted'], 1)
        self.assertEqual(result['failed'], 1)
        sender.send.assert_called_once()
        sender.send_image.assert_not_called()

    def test_delivery_uses_fake_sender_and_does_not_resubmit_same_day(self):
        sender = Mock()
        result = deliver(self.base, self.marker, self.settings, self.at, sender_factory=lambda:sender)
        self.assertTrue(result['complete'])
        self.assertFalse(result['delivery_verified'])
        sender.send_image.assert_called_once_with('示例接收会话', str(self.picture))
        self.assertEqual([call[0] for call in sender.method_calls[:2]], ['send', 'send_image'])
        second = Mock()
        result = deliver(self.base, self.marker, self.settings, self.at, sender_factory=lambda:second)
        second.send_image.assert_not_called()
        self.assertEqual(result['already_submitted'], 2)

    def test_uncertain_submission_is_not_repeated(self):
        sender = Mock()
        sender.send.side_effect = DeliveryUncertain('native UI changed after paste')
        first = deliver(self.base, self.marker, self.settings, self.at, sender_factory=lambda:sender)
        self.assertEqual(first['uncertain'], 1)
        sender.send_image.reset_mock()
        second = deliver(self.base, self.marker, self.settings, self.at, sender_factory=lambda:sender)
        sender.send_image.assert_not_called()
        self.assertEqual(second['uncertain'], 1)

    def test_partial_report_pushes_text_then_images_at_eleven(self):
        daily = '【阿联酋项目资料下载日报】\n2026年10月23日\nNCC系统：100个文件'
        self.daily_text=daily
        content = daily.encode('utf-8-sig')
        folder = self.folder.parent
        (folder / '微信推送文.txt').write_bytes(content)
        (folder / '推送状态.json').write_text(json.dumps(dict(date='2026-10-23', ready=True,
            text_file='微信推送文.txt', generated_at='2026-10-23T10:30:00+08:00',
            text_sha256=hashlib.sha256(content).hexdigest())), encoding='utf-8')
        self.state.update(analysis_complete=False, pending_analysis_files=42, failed_analysis_files=2,
                          unavailable_local_files=3, unresolved_analysis_groups=1)
        self.write_marker()
        service, _, send = self.service()
        self.prepare(service)
        service.tick(self.at.replace(hour=10, minute=59, second=45))
        send.assert_not_called()
        service.tick(self.at)
        frozen = send.call_args.args[1]
        sender = Mock()
        result = deliver(self.base, frozen, self.settings, self.at, lambda:sender)
        self.assertTrue(result['complete'])
        self.assertEqual([call[0] for call in sender.method_calls[:2]], ['send', 'send_image'])
        self.assertEqual(sender.send.call_args.args[1], daily)

    def test_text_failure_prevents_images_and_retry_does_not_repeat_submitted_text(self):
        sender = Mock()
        sender.send.side_effect = DeliveryNotSent('not ready')
        first = deliver(self.base, self.marker, self.settings, self.at, lambda:sender)
        self.assertFalse(first['complete'])
        sender.send_image.assert_not_called()
        sender.send.side_effect = None
        sender.send_image.side_effect = DeliveryNotSent('image unavailable')
        second = deliver(self.base, self.marker, self.settings, self.at, lambda:sender)
        self.assertFalse(second['complete'])
        sender.reset_mock()
        sender.send_image.side_effect = None
        last = deliver(self.base, self.marker, self.settings, self.at, lambda:sender)
        self.assertTrue(last['complete'])
        sender.send.assert_not_called()
        sender.send_image.assert_called_once()

    def test_daily_download_text_uses_live_facts_and_ignores_old_files(self):
        folder = self.folder.parent
        content = '【项目资料下载日报】\n当日已记录下载情况'.encode('utf-8-sig')
        self.daily_text=content.decode('utf-8-sig')
        (folder / '微信推送文.txt').write_bytes(content)
        marker = dict(date='2026-10-23', ready=False, text_file='微信推送文.txt',
                      generated_at='2026-10-23T10:30:00+08:00', text_sha256=hashlib.sha256(content).hexdigest())
        (folder / '推送状态.json').write_text(json.dumps(marker), encoding='utf-8')
        text = snapshot_push_text(self.base, self.state, self.at)
        self.assertTrue(text.startswith('【项目资料下载日报】'))
        self.assertEqual(text,content.decode('utf-8-sig'))
        (folder / '微信推送文.txt').write_text('tampered', encoding='utf-8')
        self.assertEqual(snapshot_push_text(self.base,self.state,self.at),self.daily_text)
        self.state['wechat_push_text'] = '过时文字及解析附录'
        self.write_marker()
        sender = Mock()
        result = deliver(self.base,self.marker,self.settings,self.at,lambda:sender)
        self.assertTrue(result['complete'])
        self.assertEqual(sender.send.call_args.args[1],self.daily_text)
        sender.send_image.assert_called_once()
        self.daily_ready=False
        self.assertIn('暂未就绪',snapshot_push_text(self.base,self.state,self.at))

    def test_stuck_renderer_cannot_block_prebuilt_snapshot_and_snapshot_is_immutable(self):
        service, factory, send = self.service()
        factory.return_value.poll.return_value = None
        service.tick(self.at.replace(hour=10, minute=40))
        service.tick(self.at.replace(hour=10, minute=59, second=45))
        factory.return_value.terminate.assert_called_once()
        frozen = self.marker.with_name('定时推送快照.json')
        before = frozen.read_bytes()
        self.state['pending_analysis_files'] = 999
        self.write_marker()
        service.tick(self.at)
        send.assert_called_once()
        self.assertEqual(before, frozen.read_bytes())

    def test_validate_dates_hashes_and_submission_window_before_ui(self):
        sender = Mock()
        with self.assertRaises(DeliveryNotSent):
            deliver(self.base, self.marker, self.settings, self.at.replace(hour=10, minute=59), lambda:sender)
        self.picture.write_bytes(b'tampered')
        with self.assertRaises(DeliveryNotSent):
            deliver(self.base, self.marker, self.settings, self.at, lambda:sender)
        sender.send_image.assert_not_called()

    def test_journal_crash_and_different_content_are_not_auto_replayed(self):
        journal = DeliveryJournal(self.base / 'journal.sqlite3')
        try:
            journal.claim('2026-10-23', 'target', 1, 'one')
            with self.assertRaises(DeliveryUncertain):
                journal.claim('2026-10-23', 'target', 1, 'one')
            with self.assertRaises(DeliveryUncertain):
                journal.claim('2026-10-23', 'target', 1, 'two')
        finally:
            journal.close()

    def test_image_ui_path_uses_bitmap_and_explicit_send_button_not_enter(self):
        class Node:
            def __init__(self, name='', kind='', children=()):
                self.Name, self.ControlTypeName, self.children = name, kind, children
                self.ProcessId, self.ClassName = 123, 'imagepreview'
                self.Click = Mock()
            def GetChildren(self):
                return self.children
        button, image = Node('发送', 'ButtonControl'), Node('', 'ImageControl')
        dialog = Node('发送图片', children=[image, button])
        edit = Mock(Name='示例接收会话')
        root = Node('微信')
        sender = WindowsUIASender.__new__(WindowsUIASender)
        sender.open_chat = Mock(return_value=(root, edit))
        sender.input = Mock(return_value=edit)
        sender.auto = Mock()
        sender.auto.GetRootControl.return_value.GetChildren.return_value = [dialog]
        data = {13: 'original clipboard text'}
        clipboard = types.SimpleNamespace(OpenClipboard=lambda:None, CloseClipboard=lambda:None,
            EmptyClipboard=lambda:data.clear(), IsClipboardFormatAvailable=lambda kind:kind in data,
            GetClipboardData=lambda kind:data[kind], SetClipboardData=lambda kind,value:data.__setitem__(kind,value))
        with patch.dict('sys.modules', {'win32clipboard': clipboard, 'win32con': types.SimpleNamespace(CF_UNICODETEXT=13, CF_DIB=8)}), \
             patch('wechat_push.uia.read_value', return_value=''), patch('wechat_push.uia.time.sleep'):
            sender.send_image('示例接收会话', self.picture)
        button.Click.assert_called_once()
        edit.SendKeys.assert_called_once_with('{Ctrl}v', waitTime=.1)
        self.assertEqual(data, {13: 'original clipboard text'})

    def test_inline_image_only_submits_single_owned_object_draft(self):
        class Node:
            Name = '微信'
            ClassName = 'mmui::MainWindow'
            ControlTypeName = 'WindowControl'
            AutomationId = ''
            ProcessId = 123
            def __init__(self, children=()):
                self.children = children
            def GetChildren(self):
                return self.children
        for draft, allowed in (('\ufffc', True), ('changed draft', False), ('\ufffc\ufffc', False)):
            with self.subTest(draft=draft):
                button = Node()
                button.Name, button.ClassName, button.ControlTypeName = '发送', 'mmui::XOutlineButton', 'ButtonControl'
                button.Click = Mock()
                root = Node([button])
                edit = Mock(Name='示例接收会话')
                sender = WindowsUIASender.__new__(WindowsUIASender)
                sender.open_chat = Mock(return_value=(root, edit))
                sender.input = Mock(return_value=edit)
                sender.auto = Mock()
                sender.auto.GetRootControl.return_value.GetChildren.return_value = [root]
                data = {}
                clipboard = types.SimpleNamespace(OpenClipboard=lambda:None, CloseClipboard=lambda:None,
                    EmptyClipboard=lambda:data.clear(), IsClipboardFormatAvailable=lambda kind:kind in data,
                    GetClipboardData=lambda kind:data[kind], SetClipboardData=lambda kind,value:data.__setitem__(kind,value))
                with patch.dict('sys.modules', {'win32clipboard': clipboard, 'win32con': types.SimpleNamespace(CF_UNICODETEXT=13, CF_DIB=8)}), \
                     patch('wechat_push.uia.read_value', side_effect=['', draft, draft]), patch('wechat_push.uia.time.sleep'):
                    if allowed:
                        sender.send_image('示例接收会话', self.picture)
                        button.Click.assert_called_once()
                    else:
                        with self.assertRaises(DeliveryUncertain):
                            sender.send_image('示例接收会话', self.picture)
                        button.Click.assert_not_called()

    def test_scheduler_login_pause_preserves_offline_publisher_without_credentials(self):
        import scheduler
        runtime = self.base / 'runtime'
        (runtime / 'login.paused').write_text('paused', encoding='utf-8')
        calls = [0]
        def consume(messages, exiting):
            calls[0] += 1
            if calls[0] >= 2:
                exiting.set()
        with patch('scheduler.RUNTIME', runtime), patch('scheduler.HOME', self.base), \
             patch('scheduler.RunLock', return_value=nullcontext()), patch('scheduler.log'), \
             patch('scheduler.consume_commands', side_effect=consume), patch('scheduler.eligible', return_value=True), \
             patch('scheduler.get_credentials', side_effect=AssertionError('credentials must not be read')), \
             patch('scheduler.run_worker', side_effect=AssertionError('download task forbidden')), \
             patch('scheduler.threading.Thread') as threads, patch.object(sys, 'argv', ['scheduler.py']):
            threads.return_value.is_alive.return_value = False
            self.assertEqual(scheduler.main(), 0)
        self.assertTrue(any(call.kwargs.get('name') == 'daily-briefing' for call in threads.call_args_list))

    def test_safe_exit_cancels_remaining_fake_submissions(self):
        sender = Mock()
        result = deliver(self.base, self.marker, self.settings, self.at,
                         sender_factory=lambda:sender, cancelled=lambda:True)
        sender.send_image.assert_not_called()
        self.assertFalse(result['complete'])


if __name__ == '__main__':
    unittest.main()
