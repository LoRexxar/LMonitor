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
from botend.guide_models import ClassGuide, ClassGuideFeed, ClassGuideSyncRun
from botend.models import MonitorTask
from botend.plugin_sync import claim_next_monitor_task, complete_monitor_task_lease, sync_monitortasks_from_plugin_list
from botend.services.class_guide_maxroll import MaxrollClient
from botend.services.class_guide_monitor import get_guide_monitor_task
from botend.services.class_guide_service import import_post
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
        url = '/api/dashboard/class-guides/feed/'
        response = self.client.patch(url, json.dumps({'enabled': True, 'interval_minutes': 90, 'authorization_note': '测试授权'}), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.task.refresh_from_db()
        self.assertTrue(self.task.is_active)
        self.assertEqual(self.task.wait_time, 5400)
        sync_monitortasks_from_plugin_list(Monitor_Type_BaseObject_List)
        self.task.refresh_from_db()
        self.assertEqual(self.task.wait_time, 5400)
        MonitorTask.objects.filter(pk=self.task.pk).update(is_active=False, wait_time=7200)
        data = self.client.get(url).json()
        self.assertFalse(data['enabled'])
        self.assertEqual(data['interval_minutes'], 120)
        self.assertEqual(data['monitor_task_id'], self.task.pk)
        self.assertIsNone(data['next_check_at'])

    def test_one_backend_scan_updates_candidates_and_unchanged_source_skips_translation(self):
        ClassGuideFeed.objects.create(authorization_note='测试授权')
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
        self.assertEqual(guide.revisions.count(), 1)
        self.assertIsNone(guide.published_revision_id)
        self.assertEqual(ClassGuideSyncRun.objects.first().results[0]['status'], 'unchanged')
        self.assertIsNone(ClassGuideFeed.objects.get().lease_until)
        self.assertTrue(complete_monitor_task_lease(task.pk, 'guide-test', task_updates={'flag': task.flag}, now=now))
        self.assertIsNone(claim_next_monitor_task(now=now, lease_owner='guide-second'))

    def test_failure_returns_to_backend_and_records_batch(self):
        ClassGuideFeed.objects.create(authorization_note='测试授权')
        plugin = MaxrollClassGuideMonitor(Mock(), self.task)
        with patch('botend.services.class_guide_service.MaxrollClient') as source:
            source.return_value.discover.side_effect = ValueError('模拟目录请求失败')
            self.assertFalse(plugin.scan(self.task.target))
        self.assertIn('模拟目录请求失败', plugin.last_error_detail)
        self.assertEqual(ClassGuideSyncRun.objects.get().status, 'failed')
        self.assertIsNone(ClassGuideFeed.objects.get().lease_until)

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
        self.assertEqual(ClassGuide.objects.get(slug='arcane-mage-raid-guide').revisions.count(), 1)


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
