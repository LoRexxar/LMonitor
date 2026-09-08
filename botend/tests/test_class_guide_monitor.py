"""验证攻略复用统一调度，以及单次导入不依赖翻译服务。"""
import json
import uuid
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch
from zipfile import ZipFile

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, SimpleTestCase, override_settings
from django.utils import timezone

from LMonitor.config import Monitor_Type_BaseObject_List
from botend.controller.plugins.wow.MaxrollClassGuideMonitor import MaxrollClassGuideMonitor
from botend.guide_models import ClassGuide, ClassGuideSyncRun
from botend.models import MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost
from botend.plugin_sync import claim_monitor_task, claim_next_monitor_task, complete_monitor_task_lease, sync_monitortasks_from_plugin_list
from botend.services.class_guide_maxroll import MaxrollClient
from botend.services.class_guide_monitor import get_guide_monitor_task
from botend.services.class_guide_service import import_post, sync_guides
from botend.tests.test_class_guides import source_post


@override_settings(SECURE_SSL_REDIRECT=False)
class GuideMonitorTests(TestCase):
    def setUp(self):
        self.task = get_guide_monitor_task()
        self.user = get_user_model().objects.create_superuser(username='monitor_editor', password='test-only')
        self.client.force_login(self.user)

    def test_dashboard_uses_same_task_and_backend_restart_preserves_interval(self):
        self.assertFalse(self.task.is_active)
        self.assertEqual(self.task.wait_time, 21600)
        self.assertIs(Monitor_Type_BaseObject_List[self.task.type], MaxrollClassGuideMonitor)
        response = self.client.post('/dashboard/', json.dumps({
            'action': 'update_table_row', 'table_name': 'MonitorTask', 'row_id': self.task.pk,
            'update_data': {'is_active': True, 'wait_time': 5400, 'notes': '测试授权'},
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        sync_monitortasks_from_plugin_list(Monitor_Type_BaseObject_List)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_active)
        self.assertEqual(self.task.wait_time, 5400)
        self.assertEqual(self.task.notes, '测试授权')
        self.assertEqual(self.client.get('/api/dashboard/class-guides/feed/').status_code, 404)

    def test_monitor_table_can_enable_new_task_with_blank_flag(self):
        self.assertIsNone(self.task.flag)
        for flag in (None, ''):
            with self.subTest(flag=flag):
                response = self.client.post('/dashboard/', json.dumps({
                    'action': 'update_table_row', 'table_name': 'MonitorTask',
                    'row_id': self.task.pk,
                    'update_data': {'is_active': True, 'flag': flag},
                }), content_type='application/json')
                self.assertEqual(response.status_code, 200, response.content)
                self.assertEqual(response.json()['status'], 'success')
                self.task.refresh_from_db()
                self.assertTrue(self.task.is_active)
                self.assertIsNone(self.task.flag)

    def test_database_can_manage_authorization_and_read_batches(self):
        def post(data):
            return self.client.post('/dashboard/', json.dumps(data), content_type='application/json')
        response = post({'action': 'get_table_data', 'table_name': 'ClassGuideFeed'})
        self.assertEqual(response.status_code, 404)
        run = ClassGuideSyncRun.objects.create(status='completed')
        response = post({'action': 'get_table_data', 'table_name': 'ClassGuideSyncRun'})
        self.assertEqual(response.status_code, 200, response.content)
        response = post({'action': 'update_table_row', 'table_name': 'ClassGuideSyncRun',
            'row_id': run.pk, 'update_data': {'status': 'failed'}})
        self.assertEqual(response.status_code, 403)

    def test_monitor_table_preserves_flag_and_rejects_oversized_value(self):
        flag = '批次 1 · completed · 70 篇'
        MonitorTask.objects.filter(pk=self.task.pk).update(flag=flag)
        response = self.client.post('/dashboard/', json.dumps({
            'action': 'update_table_row', 'table_name': 'MonitorTask',
            'row_id': self.task.pk, 'update_data': {'is_active': True, 'flag': flag},
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200, response.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.flag, flag)
        response = self.client.post('/dashboard/', json.dumps({
            'action': 'update_table_row', 'table_name': 'MonitorTask',
            'row_id': self.task.pk, 'update_data': {'is_active': False, 'flag': 'x' * 2001},
        }), content_type='application/json')
        self.assertEqual(response.status_code, 400)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_active)
        self.assertEqual(self.task.flag, flag)

    def test_one_backend_scan_updates_candidates_and_unchanged_source_skips_translation(self):
        self.task.notes = '测试授权'; self.task.save(update_fields=['notes'])
        now = timezone.now()
        MonitorTask.objects.filter(pk=self.task.pk).update(is_active=True, last_scan_time=now - timedelta(days=2))
        task = claim_next_monitor_task(now=now, lease_owner='guide-test')
        self.assertEqual(task.pk, self.task.pk)
        req = Mock()
        plugin = MaxrollClassGuideMonitor(req, task)
        with patch('botend.services.class_guide_service.MaxrollClient') as source, patch(
            'botend.services.class_guide_service.translate_blocks',
            return_value=([{'id': 'intro', 'type': 'html', 'html': '<p>中文候选攻略</p>'}], []),
        ) as translate:
            source.return_value.discover.return_value = ['https://maxroll.gg/wow/class-guides/arcane-mage-raid-guide']
            source.return_value.article.return_value = source_post()
            self.assertTrue(plugin.scan(task.target))
            translate.assert_called_once()
            source.assert_called_once_with(request_client=req)
            translate.reset_mock()
            self.assertTrue(plugin.scan(task.target))
            translate.assert_not_called()
        guide = ClassGuide.objects.get()
        self.assertTrue(guide.content_markdown)
        self.assertFalse(hasattr(guide, 'published_revision_id'))
        self.assertEqual(ClassGuideSyncRun.objects.first().results[0]['status'], 'unchanged')
        self.assertTrue(MonitorTaskLease.objects.filter(task=task, owner='guide-test').exists())
        self.assertTrue(complete_monitor_task_lease(task.pk, 'guide-test', task_updates={'flag': task.flag}, now=now))
        self.assertIsNone(claim_next_monitor_task(now=now, lease_owner='guide-second'))

    def test_failure_returns_to_backend_and_records_batch(self):
        self.task.notes = '测试授权'; self.task.save(update_fields=['notes'])
        plugin = MaxrollClassGuideMonitor(Mock(), self.task)
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            source.return_value.discover.side_effect = ValueError('模拟目录请求失败')
            self.assertFalse(plugin.scan(self.task.target))
        self.assertIn('模拟目录请求失败', plugin.last_error_detail)
        self.assertEqual(ClassGuideSyncRun.objects.get().status, 'failed')
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())

    def test_manual_sync_and_backend_share_one_lease(self):
        self.task.notes = '测试授权'; self.task.save(update_fields=['notes'])
        MonitorTask.objects.filter(pk=self.task.pk).update(is_active=True, last_scan_time=timezone.now() - timedelta(days=2))
        backend = claim_next_monitor_task(lease_owner='后台执行者')
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            with self.assertRaisesMessage(MonitorTaskLeaseLost, '已有攻略同步任务执行中'):
                sync_guides()
            source.assert_not_called()
        self.assertTrue(complete_monitor_task_lease(backend.pk, '后台执行者'))

        def discover():
            lease = MonitorTaskLease.objects.get(task=self.task)
            self.assertTrue(lease.owner)
            self.assertIsNone(claim_monitor_task(self.task.pk))
            MonitorTask.objects.filter(pk=self.task.pk).update(last_scan_time=timezone.now() - timedelta(days=2))
            self.assertIsNone(claim_next_monitor_task(now=timezone.now() + timedelta(seconds=1)))
            return []
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            source.return_value.discover.side_effect = discover
            call_command('sync_class_guides', stdout=StringIO())
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())
        self.task.refresh_from_db()
        self.assertIn('completed', self.task.flag)

    def test_expired_backend_lease_cannot_fall_back_to_manual_claim(self):
        self.task.notes = '测试授权'; self.task.save(update_fields=['notes'])
        task = claim_monitor_task(self.task.pk, lease_owner='旧执行者')
        MonitorTaskLease.objects.filter(task=task).update(expires_at=timezone.now() - timedelta(seconds=1))
        newer = claim_monitor_task(task.pk, lease_owner='新执行者')
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            with self.assertRaises(MonitorTaskLeaseLost):
                sync_guides(monitor_task=task)
            source.assert_not_called()
        self.assertEqual(MonitorTaskLease.objects.get(task=task).owner, '新执行者')
        self.assertIsNotNone(newer)

    def test_lease_loss_during_fetch_stops_article_writes(self):
        self.task.notes = '测试授权'; self.task.save(update_fields=['notes'])
        def article(_):
            MonitorTaskLease.objects.filter(task=self.task).update(owner='接管执行者')
            return source_post()
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            source.return_value.discover.return_value = ['https://maxroll.gg/wow/class-guides/arcane-mage-raid-guide']
            source.return_value.article.side_effect = article
            with self.assertRaises(MonitorTaskLeaseLost):
                sync_guides()
        self.assertFalse(ClassGuide.objects.exists())
        self.assertEqual(ClassGuideSyncRun.objects.get().status, 'failed')
        self.assertEqual(MonitorTaskLease.objects.get(task=self.task).owner, '接管执行者')

    def test_manual_authorization_note_and_failure_release(self):
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            source.side_effect = ValueError('客户端初始化失败')
            with self.assertRaisesMessage(CommandError, '客户端初始化失败'):
                call_command('sync_class_guides', authorization_note='命令行登记授权', stdout=StringIO())
        self.task.refresh_from_db()
        self.assertEqual(self.task.notes, '命令行登记授权')
        self.assertFalse(self.task.is_active)
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())
        self.assertEqual(ClassGuideSyncRun.objects.get().status, 'failed')

    def test_zip_import_is_repeatable_without_translation_or_monitor_execution(self):
        original, _ = import_post(source_post(), 'https://maxroll.gg/wow/class-guides/arcane-mage-raid-guide')
        folder = Path('tmp/django-tests/guide-zip-' + uuid.uuid4().hex)
        folder.mkdir(parents=True)
        call_command('audit_class_guides', output_dir=str(folder), game_version='12.1', stdout=StringIO())
        archive = folder / 'drafts.zip'
        with ZipFile(archive, 'w') as bundle:
            for entry in folder.glob('*.json'):
                bundle.write(entry, entry.name)
        original.slug = 'original-' + original.slug
        original.save(update_fields=['slug'])
        with patch('botend.services.class_guide_service.build_translation_service') as translate, patch.object(MaxrollClassGuideMonitor, 'scan') as monitor:
            call_command('import_class_guide_drafts', input_zip=str(archive), dry_run=True, stdout=StringIO())
            self.assertEqual(ClassGuide.objects.count(), 1)
            call_command('import_class_guide_drafts', input_zip=str(archive), stdout=StringIO())
            call_command('import_class_guide_drafts', input_zip=str(archive), stdout=StringIO())
            translate.assert_not_called()
            monitor.assert_not_called()
        self.assertEqual(ClassGuide.objects.count(), 2)
        self.assertTrue(ClassGuide.objects.get(slug='arcane-mage-raid-guide').content_markdown)


class GuideMonitorCommandTests(SimpleTestCase):
    def test_retired_watch_fails_before_database_access(self):
        with self.assertRaisesMessage(CommandError, '--watch 已停用'):
            call_command('sync_class_guides', watch=True)

    def test_backend_request_client_is_reused(self):
        url = 'https://maxroll.gg/wow/class-guides'
        req = Mock()
        req.get.return_value = Mock(status_code=200, url=url, content='中文页面'.encode())
        with patch('botend.services.class_guide_maxroll.requests.Session') as session:
            self.assertEqual(MaxrollClient(request_client=req).fetch(url), '中文页面')
            session.assert_not_called()
        req.get.assert_called_once()
        req.get.return_value = False
        with self.assertRaisesMessage(ValueError, '来源请求失败'):
            MaxrollClient(request_client=req).fetch(url)
