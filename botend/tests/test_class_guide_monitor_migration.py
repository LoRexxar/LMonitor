"""验证来源表合并保留授权和历史，并拒绝迁移正在运行的同步。"""
from datetime import timedelta

from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone


class GuideMonitorMigrationTests(TransactionTestCase):
    before = [('botend', '0217_allow_blank_monitor_task_flag')]
    after = [('botend', '0218_consolidate_class_guide_monitor')]

    def setUp(self):
        executor = MigrationExecutor(connection)
        executor.migrate(self.before)
        self.apps = executor.loader.project_state(self.before).apps

    def tearDown(self):
        # 失败用例也恢复最新结构，避免影响其它测试。
        Feed = self.apps.get_model('botend', 'ClassGuideFeed')
        if Feed._meta.db_table in connection.introspection.table_names():
            Feed.objects.update(lease_until=None, lease_token='')
        self.apps.get_model('botend', 'MonitorTaskLease').objects.all().delete()
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

    def test_merge_preserves_authorization_task_configuration_and_history(self):
        Task = self.apps.get_model('botend', 'MonitorTask')
        Feed = self.apps.get_model('botend', 'ClassGuideFeed')
        Run = self.apps.get_model('botend', 'ClassGuideSyncRun')
        task, _ = Task.objects.update_or_create(name='MaxrollClassGuideMonitor', defaults={'type': 34,
            'target': 'https://maxroll.gg/wow/class-guides', 'is_active': True, 'wait_time': 7200})
        Feed.objects.update_or_create(key='maxroll', defaults={'authorization_note': '已获得原作者翻译转载授权',
            'enabled': False, 'interval_minutes': 15})
        run = Run.objects.create(status='partial', results=[{'url': '原文地址', 'error': '历史错误'}])
        executor = MigrationExecutor(connection); executor.migrate(self.after)
        apps = executor.loader.project_state(self.after).apps
        migrated = apps.get_model('botend', 'MonitorTask').objects.get(pk=task.pk)
        self.assertEqual(migrated.notes, '已获得原作者翻译转载授权')
        self.assertTrue(migrated.is_active)
        self.assertEqual(migrated.wait_time, 7200)
        self.assertEqual(apps.get_model('botend', 'ClassGuideSyncRun').objects.get(pk=run.pk).results, run.results)
        self.assertNotIn(Feed._meta.db_table, connection.introspection.table_names())
        MigrationExecutor(connection).migrate(self.before)
        self.assertEqual(Feed.objects.get(key='maxroll').authorization_note, migrated.notes)

    def test_active_source_lease_blocks_table_removal(self):
        Feed = self.apps.get_model('botend', 'ClassGuideFeed')
        Feed.objects.update_or_create(key='maxroll', defaults={'authorization_note': '保留的授权说明',
            'lease_until': timezone.now() + timedelta(minutes=10), 'lease_token': '旧执行者'})
        with self.assertRaisesMessage(RuntimeError, '攻略同步仍持有执行锁'):
            MigrationExecutor(connection).migrate(self.after)
        self.assertIn(Feed._meta.db_table, connection.introspection.table_names())
        self.assertEqual(Feed.objects.get(key='maxroll').authorization_note, '保留的授权说明')
