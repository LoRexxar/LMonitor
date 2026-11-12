"""Remaining manual CLIs share scheduler admission; no external requests.

SQLite checks deterministic state, not InnoDB concurrent lock semantics.
"""
from datetime import timedelta
from io import StringIO
import json
from pathlib import Path
import tempfile
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from LMonitor.config import Monitor_Type_BaseObject_List
from botend.models import MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost, SeasonMeta, WowArticle


class RemainingCommandAdmissionTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.now = timezone.now()
        self.season = SeasonMeta.objects.create(season_key='remaining-cli', season_name='CLI',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.daily = self.task('WowDailyReportMonitor')
        self.activity = self.task('PlayerRaiderIOActivityMonitor')
        self.forum = self.task('PortalPostMonitor')
        self.article = WowArticle.objects.create(title='Preserve', content='Original',
            source='blizzard_tracker', category='bluepost',
            url='https://us.forums.blizzard.com/en/wow/t/tuning/2404092/1')
        self.enterContext(patch('botend.management.commands.sync_player_raiderio_activity.top20_candidates',
            return_value=[{'player_ids': [123], 'identity': True, 'player': object()}]))
        self.enterContext(patch('botend.management.commands.sync_player_raiderio_rosters.top20_run_candidates',
            return_value=([{'run_id': 456, 'run': {}, 'conflict': False}], 0)))

    def task(self, name):
        task_type = next(i for i, plugin in enumerate(Monitor_Type_BaseObject_List) if plugin.__name__ == name)
        return MonitorTask.objects.create(name=name, type=task_type, target='', env_limit=0,
            is_active=False, last_scan_time=self.now - timedelta(days=2), flag='preserve')

    def lease(self, task, owner='existing-owner'):
        return MonitorTaskLease.objects.create(task=task, owner=owner, claimed_at=self.now,
            expires_at=self.now + timedelta(hours=1))

    def cases(self):
        return (
            ('GenerateWowDailyReport', {'date': '2026-11-03', 'no_llm': True}, self.daily,
             'botend.management.commands.GenerateWowDailyReport.generate_wow_daily_report', {'md_path': 'report', 'full_path': 'full'}),
            ('sync_player_raiderio_activity', {'apply': True, 'force': True}, self.activity,
             'botend.management.commands.sync_player_raiderio_activity.ActivityCollector.refresh', {'status': 'ready'}),
            ('sync_player_raiderio_rosters', {'apply': True, 'force': True, 'workers': 3, 'season': self.season.pk}, self.activity,
             'botend.management.commands.sync_player_raiderio_rosters.collect_candidates', [{'run_id': 456, 'status': 'ready'}]),
            ('refresh_blizzard_forum_metadata', {'apply': True, 'ids': [self.article.pk], 'limit': 1}, self.forum,
             'botend.management.commands.refresh_blizzard_forum_metadata.Command.build_collector', None),
        )

    def test_all_four_reject_full_pool_without_executing(self):
        for index in range(3):
            task = MonitorTask.objects.create(name=f'occupied-{index}', type=1000 + index,
                target='', last_scan_time=self.now)
            self.lease(task)
        for name, options, task, target, result in self.cases():
            with self.subTest(command=name), patch(target, return_value=result) as execute:
                with self.assertRaises(CommandError):
                    call_command(name, stdout=StringIO(), **options)
                execute.assert_not_called()
        self.assertEqual(MonitorTaskLease.objects.count(), 3)

    def test_apply_busy_preserves_existing_result_json(self):
        self.lease(self.activity)
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'result.json'
            original = b'{"prior": "checkpoint"}\n'
            for name in ('sync_player_raiderio_activity', 'sync_player_raiderio_rosters'):
                path.write_bytes(original)
                with self.subTest(command=name), self.assertRaises(CommandError):
                    call_command(name, apply=True, season=self.season.pk, result_json=str(path), stdout=StringIO())
                self.assertEqual(path.read_bytes(), original)

    def test_dry_runs_do_not_claim_even_when_full(self):
        for task in (self.daily, self.activity, self.forum):
            self.lease(task)
        before = list(MonitorTaskLease.objects.order_by('pk').values())
        for name in ('sync_player_raiderio_activity', 'sync_player_raiderio_rosters', 'refresh_blizzard_forum_metadata'):
            options = {'season': self.season.pk} if name != 'refresh_blizzard_forum_metadata' else {}
            with self.subTest(command=name):
                out = StringIO()
                call_command(name, stdout=out, **options)
                self.assertTrue(json.loads(out.getvalue()))
        self.assertEqual(list(MonitorTaskLease.objects.order_by('pk').values()), before)

    def test_execution_holds_manual_lease_and_preserves_control_state_and_options(self):
        from datetime import date
        for name, options, task, target, result in self.cases():
            def execute(*args, **kwargs):
                lease = MonitorTaskLease.objects.get(task=task)
                self.assertTrue(lease.is_manual)
                self.assertGreater(lease.expires_at, timezone.now())
                if name == 'GenerateWowDailyReport':
                    self.assertEqual(kwargs, {'report_date': date(2026, 11, 3), 'use_llm': False})
                elif name == 'sync_player_raiderio_activity':
                    self.assertTrue(kwargs['force'])
                elif name == 'sync_player_raiderio_rosters':
                    self.assertEqual(kwargs, {'workers': 3, 'force': True})
                else:
                    self.assertEqual(args[0]._monitor_task_lease_owner, lease.owner)
                    collector = MagicMock()
                    collector.ensure.return_value = True
                    collector.last_error = None
                    return collector
                return result
            original_time = task.last_scan_time
            with self.subTest(command=name), patch(target, side_effect=execute) as executor:
                call_command(name, stdout=StringIO(), **options)
                executor.assert_called_once()
            task.refresh_from_db()
            self.assertEqual((task.last_scan_time, task.flag), (original_time, 'preserve'))
            self.assertFalse(MonitorTaskLease.objects.exists())

    def test_all_four_release_on_executor_error(self):
        for name, options, task, target, result in self.cases():
            with self.subTest(command=name), patch(target, side_effect=RuntimeError('executor failed')):
                with self.assertRaisesRegex(RuntimeError, 'executor failed'):
                    call_command(name, stdout=StringIO(), **options)
            self.assertFalse(MonitorTaskLease.objects.exists())

    def test_all_four_reject_environment_and_missing_configuration(self):
        for name, options, task, target, result in self.cases():
            for invalid in ('environment', 'type'):
                with self.subTest(command=name, invalid=invalid), patch(target) as execute:
                    original_type = task.type
                    if invalid == 'environment':
                        task.env_limit = 1
                    else:
                        task.type = 1000
                    task.save(update_fields=['type', 'env_limit'])
                    with self.assertRaises(CommandError):
                        call_command(name, stdout=StringIO(), **options)
                    execute.assert_not_called()
                    task.type, task.env_limit = original_type, 0
                    task.save(update_fields=['type', 'env_limit'])
            self.assertFalse(MonitorTaskLease.objects.exists())

    def test_forum_borrowed_lease_is_validated_and_not_reclaimed_or_released(self):
        self.lease(self.forum, 'exact-owner')
        self.lease(self.daily)
        self.lease(self.activity)
        before = list(MonitorTaskLease.objects.order_by('pk').values())
        collector = MagicMock()
        collector.ensure.return_value = True
        collector.last_error = None
        with patch('botend.management.commands.refresh_blizzard_forum_metadata.Command.build_collector', return_value=collector) as build, \
                patch('botend.services.monitor_task_execution.claim_monitor_task', side_effect=AssertionError('no duplicate claim')):
            call_command('refresh_blizzard_forum_metadata', apply=True, task_id=self.forum.pk,
                lease_owner='exact-owner', stdout=StringIO())
        self.assertEqual(build.call_args.args[0]._monitor_task_lease_owner, 'exact-owner')
        self.assertEqual(list(MonitorTaskLease.objects.order_by('pk').values()), before)

    def test_forum_borrowed_lease_rejects_forged_type_name_owner_expiry_and_environment(self):
        lease = self.lease(self.forum, 'exact-owner')
        original_type = self.forum.type
        for invalid in ('type', 'name', 'owner', 'expiry', 'environment'):
            with self.subTest(invalid=invalid):
                self.forum.type = 1000 if invalid == 'type' else original_type
                self.forum.name = 'not-portal-post' if invalid == 'name' else 'PortalPostMonitor'
                self.forum.env_limit = 1 if invalid == 'environment' else 0
                self.forum.save(update_fields=['type', 'name', 'env_limit'])
                lease.expires_at = self.now - timedelta(seconds=1) if invalid == 'expiry' else self.now + timedelta(hours=1)
                lease.save(update_fields=['expires_at'])
                with patch('botend.management.commands.refresh_blizzard_forum_metadata.Command.build_collector') as build:
                    with self.assertRaises((CommandError, MonitorTaskLeaseLost)):
                        call_command('refresh_blizzard_forum_metadata', apply=True, task_id=self.forum.pk,
                            lease_owner='wrong-owner' if invalid == 'owner' else 'exact-owner', stdout=StringIO())
                    build.assert_not_called()
                self.assertEqual(MonitorTaskLease.objects.get(task=self.forum).owner, 'exact-owner')
