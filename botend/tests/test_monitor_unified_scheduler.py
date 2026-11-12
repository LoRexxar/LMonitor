"""Unified three-worker admission contract; no collectors or external work run.

SQLite covers deterministic admission/state semantics. The separate InnoDB
cases require independent connections and are not concurrency evidence on SQLite.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from threading import Barrier
from types import ModuleType
from unittest import skipUnless
from unittest.mock import Mock, patch
from zoneinfo import ZoneInfo

from django.db import connection, connections
from django.test import TestCase, TransactionTestCase, override_settings

from botend import plugin_sync as scheduler
from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle


BJT = ZoneInfo('Asia/Shanghai')
NOW = datetime(2026, 11, 11, 1, 0, tzinfo=BJT)
# Frozen cadence contract, intentionally NOT derived from LONG_MONITOR_TASKS.
DAILY_TASKS = {
    'SpecDetailSeasonMonitor', 'WowDataVersionMonitor',
    'MythicDungeonToolsMonitor', 'RioStatisticsMonitor',
    'MaxrollClassGuideMonitor', 'SpecDetailPlayerMonitor',
    'SpecDetailRankingMonitor', 'SpecDetailAggregationMonitor',
    'WowDailyReportMonitor',
}
SHORT_TASKS = (
    'wowheadMonitor', 'PortalPostMonitor', 'PortalArticleTranslateMonitor',
    'WowDailyReportMonitor', 'PortalPeakSpecRankMonitor',
    'PlayerRaiderIOActivityMonitor', 'SpecDungeonDpsRankingMonitor',
    'SpecDetailSeasonMonitor',
)


class SchedulerFixtures:
    def setUp(self):
        super().setUp()
        MonitorTask.objects.all().delete()
        SeasonMeta.objects.all().delete()
        self.now = NOW
        # Freeze all shared Django timezone users, including WCL fencing.
        self.clock = self.enterContext(patch('botend.plugin_sync.timezone.now', return_value=self.now))

    def task(self, name, **overrides):
        values = dict(type=900 + MonitorTask.objects.count(), target='', env_limit=0,
                      is_active=True, wait_time=scheduler.monitor_default_wait_time(name),
                      last_scan_time=self.now - timedelta(days=2))
        values.update(overrides)
        return MonitorTask.objects.create(name=name, **values)

    def lease(self, task, *, manual=False, owner=None, expires=None):
        return MonitorTaskLease.objects.create(
            task=task, owner=owner or 'running-{}'.format(task.pk), is_manual=manual,
            claimed_at=self.now - timedelta(minutes=30),
            expires_at=expires or self.now + timedelta(hours=12))

    def claim(self, *, now=None, owner='ordinary-worker'):
        return scheduler.claim_next_monitor_task(now=now or self.now, lease_owner=owner,
                                                lease_seconds=12 * 3600)

    def assert_claim(self, expected, **kwargs):
        got = self.claim(**kwargs)
        self.assertIsNotNone(got, 'ordinary worker must admit {}'.format(expected.name))
        self.assertEqual(got.pk, expected.pk)
        lease = MonitorTaskLease.objects.get(task=expected)
        self.assertFalse(lease.is_manual)
        self.assertEqual(lease.owner, kwargs.get('owner', 'ordinary-worker'))
        return got


class UnifiedSchedulerAdmissionTests(SchedulerFixtures, TestCase):
    def test_three_shorts_fill_shared_pool_without_separate_two_short_cap(self):
        tasks = [self.task(name) for name in ('ngaMonitor', 'PortalPostMonitor', 'wowheadMonitor')]
        claimed = [self.claim(owner='worker-{}'.format(i)) for i in range(3)]
        self.assertTrue(all(claimed), 'all three ordinary workers may run short tasks')
        self.assertCountEqual([task.pk for task in claimed], [task.pk for task in tasks])
        fourth = self.task('PortalArticleTranslateMonitor')
        self.assertIsNone(self.claim(owner='fourth'))
        self.assertEqual(MonitorTaskLease.objects.filter(is_manual=False).count(), 3)
        self.assertTrue(scheduler.release_monitor_task_lease(claimed[0].pk, 'worker-0'))
        self.assert_claim(fourth, owner='replacement')

    def test_total_three_also_blocks_a_long_when_three_shorts_are_running(self):
        for name in ('ngaMonitor', 'PortalPostMonitor', 'wowheadMonitor'):
            self.lease(self.task(name))
        self.task('MaxrollClassGuideMonitor')
        self.assertIsNone(self.claim())
        self.assertEqual(MonitorTaskLease.objects.filter(is_manual=False).count(), 3)

    def test_two_unrelated_long_tasks_can_run_at_night_not_only_one_rio_chain(self):
        self.lease(self.task('MaxrollClassGuideMonitor'))
        player = self.task('SpecDetailPlayerMonitor')
        self.assertTrue(self.assert_claim(player)._monitor_task_is_long)
        self.task('MythicDungeonToolsMonitor')
        self.assertIsNone(self.claim())
        short = self.task('PortalPostMonitor')
        self.assertFalse(self.assert_claim(short)._monitor_task_is_long)
        self.task('wowheadMonitor')
        self.assertIsNone(self.claim())

    def test_former_version_and_rio_dedicated_tasks_are_claimable_by_any_normal_worker(self):
        for name in ('WowDataVersionMonitor', 'RioStatisticsMonitor'):
            with self.subTest(name=name):
                MonitorTask.objects.all().delete()
                task = self.task(name)
                got = self.assert_claim(task, owner='ordinary-worker-any-index')
                self.assertTrue(got._monitor_task_is_long)

    def test_season_version_and_mdt_preserve_source_preparation_barrier(self):
        for name in ('SpecDetailSeasonMonitor', 'WowDataVersionMonitor', 'MythicDungeonToolsMonitor'):
            with self.subTest(running=name):
                MonitorTask.objects.all().delete()
                self.lease(self.task(name))
                player = self.task('SpecDetailPlayerMonitor')
                self.assertIsNone(self.claim())
                self.assertIsNone(scheduler.claim_monitor_task(player.pk, now=self.now))
                short = self.task('PortalPostMonitor')
                self.assert_claim(short)
                held = MonitorTaskLease.objects.get(task__name=name)
                MonitorTask.objects.filter(pk=held.task_id).update(last_scan_time=self.now)
                self.assertTrue(scheduler.release_monitor_task_lease(held.task_id, held.owner))
                self.assert_claim(player)

    def test_preparation_waits_for_running_heavy_work_even_when_season_is_short(self):
        for name in ('SpecDetailSeasonMonitor', 'WowDataVersionMonitor', 'MythicDungeonToolsMonitor'):
            with self.subTest(preparation=name):
                MonitorTask.objects.all().delete()
                running = self.lease(self.task('SpecDetailPlayerMonitor'))
                preparation = self.task(name)
                self.assertIsNone(self.claim())
                self.assertIsNone(scheduler.claim_monitor_task(preparation.pk, now=self.now))
                MonitorTask.objects.filter(pk=running.task_id).update(last_scan_time=self.now)
                self.assertTrue(scheduler.release_monitor_task_lease(running.task_id, running.owner))
                self.assert_claim(preparation)

    def test_daytime_has_one_long_and_two_remaining_shared_slots(self):
        self.now = NOW.replace(hour=12)
        self.lease(self.task('MaxrollClassGuideMonitor'))
        self.task('SpecDetailPlayerMonitor')
        first = self.task('PortalPostMonitor')
        second = self.task('wowheadMonitor')
        claimed = [self.claim(owner='short-{}'.format(i)) for i in range(2)]
        self.assertTrue(all(claimed))
        self.assertCountEqual([task.pk for task in claimed], [first.pk, second.pk])
        self.assertIsNone(self.claim())

    def test_bjt_night_boundary_uses_aware_source_time_not_utc_hour(self):
        for hour, minute, admits_long in ((0, 0, True), (5, 59, True), (6, 0, False), (23, 59, False)):
            with self.subTest(hour=hour, minute=minute):
                MonitorTask.objects.all().delete()
                self.now = NOW.replace(hour=hour, minute=minute)
                self.clock.return_value = self.now
                self.lease(self.task('MaxrollClassGuideMonitor'))
                player = self.task('SpecDetailPlayerMonitor')
                now = self.now.astimezone(ZoneInfo('UTC'))
                got = self.claim(now=now)
                if admits_long:
                    self.assertIsNotNone(got)
                    self.assertEqual(got.pk, player.pk)
                else:
                    self.assertIsNone(got)

    def test_two_longs_cross_six_am_are_not_cancelled_or_replenished(self):
        self.now = NOW.replace(hour=5, minute=59)
        first = self.lease(self.task('MaxrollClassGuideMonitor'))
        second = self.lease(self.task('SpecDetailPlayerMonitor'))
        self.task('MythicDungeonToolsMonitor')
        short = self.task('PortalPostMonitor')
        before = list(MonitorTaskLease.objects.order_by('pk').values())
        daytime = self.now.replace(hour=6, minute=0)
        self.assert_claim(short, now=daytime)
        self.assertEqual(list(MonitorTaskLease.objects.filter(pk__in=[first.pk, second.pk])
                              .order_by('pk').values()), before)
        self.assertTrue(scheduler.release_monitor_task_lease(short.pk, 'ordinary-worker'))
        self.assertIsNone(self.claim(now=daytime))
        self.assertTrue(scheduler.release_monitor_task_lease(first.task_id, first.owner))
        self.assertIsNone(self.claim(now=daytime), 'one surviving long still fills the daytime long quota')
        self.assertEqual(MonitorTaskLease.objects.get(pk=second.pk).owner, second.owner)

    def test_disabled_alias_lease_preserves_scope_mutex_and_total_capacity(self):
        canonical = self.task('WowDataVersionMonitor', target='retail')
        alias = self.task('disabled legacy version alias', type=canonical.type,
                          target=canonical.target, is_active=False)
        held = self.lease(alias, owner='old-worker')
        self.assertIsNone(self.claim())
        self.assertIsNone(scheduler.claim_monitor_task(canonical.pk, now=self.now))
        self.lease(self.task('PortalPostMonitor'))
        other = self.task('wowheadMonitor')
        self.assert_claim(other)
        self.task('PortalArticleTranslateMonitor')
        self.assertIsNone(self.claim())
        self.assertEqual(MonitorTaskLease.objects.get(pk=held.pk).owner, 'old-worker')

    def test_manual_lease_guards_scope_and_consumes_one_of_three_shared_slots(self):
        canonical = self.task('WowDataVersionMonitor')
        alias = self.task('manual disabled alias', type=canonical.type, is_active=False)
        self.assertIsNotNone(scheduler.claim_monitor_task(alias.pk, now=self.now,
                                                        lease_owner='manual-owner'))
        self.assertIsNone(scheduler.claim_monitor_task(canonical.pk, now=self.now))
        shorts = [self.task(name) for name in ('ngaMonitor', 'PortalPostMonitor', 'wowheadMonitor')]
        claimed = [self.claim(owner='ordinary-{}'.format(i)) for i in range(2)]
        self.assertTrue(all(claimed))
        self.assertTrue({task.pk for task in claimed}.issubset({task.pk for task in shorts}))
        self.assertIsNone(self.claim())
        pending = next(task for task in shorts if task.pk not in {row.pk for row in claimed})
        self.assertIsNone(scheduler.claim_monitor_task(pending.pk, now=self.now))
        self.assertEqual(MonitorTaskLease.objects.count(), 3)
        self.assertEqual(MonitorTaskLease.objects.filter(is_manual=False).count(), 2)
        self.assertTrue(MonitorTaskLease.objects.get(task=alias).is_manual)

    def test_different_targets_are_different_scopes_not_a_task_name_mutex(self):
        one = self.task('MaxrollClassGuideMonitor', target='one')
        two = self.task(one.name, type=one.type, target='two')
        first = self.assert_claim(one, owner='one')
        second = self.assert_claim(two, owner='two')
        self.assertNotEqual(scheduler.monitor_scope_key(first), scheduler.monitor_scope_key(second))

    def test_expired_lease_does_not_keep_scope_or_capacity_occupied(self):
        task = self.task('WowDataVersionMonitor')
        self.lease(task, owner='expired', expires=self.now - timedelta(seconds=1))
        self.assert_claim(task, owner='replacement')
        self.assertEqual(MonitorTaskLease.objects.count(), 1)

    def test_required_news_daily_activity_and_local_dps_tasks_remain_short(self):
        for name in SHORT_TASKS:
            with self.subTest(name=name):
                MonitorTask.objects.all().delete()
                self.lease(self.task('ngaMonitor'))
                self.lease(self.task('PortalPostMonitor' if name != 'PortalPostMonitor' else 'wowheadMonitor'))
                task = self.task(name)
                got = self.assert_claim(task, now=self.now.replace(hour=12))
                self.assertFalse(got._monitor_task_is_long)
                self.assertNotIn(name, scheduler.LONG_MONITOR_TASKS)

    def test_manual_requests_share_night_and_day_long_limits(self):
        for hour, limit in ((1, 2), (12, 1)):
            with self.subTest(hour=hour):
                MonitorTask.objects.all().delete()
                now = self.now.replace(hour=hour)
                for index in range(limit):
                    task = self.task('MaxrollClassGuideMonitor', target=str(index), is_active=False)
                    self.assertIsNotNone(scheduler.claim_monitor_task(task.pk, now=now))
                blocked = self.task('WagoSkillDiffMonitor', is_active=False)
                self.assertIsNone(scheduler.claim_monitor_task(blocked.pk, now=now))
                self.assertIsNone(self.claim(now=now))
                self.assertEqual(MonitorTaskLease.objects.count(), limit)

    def test_long_classification_does_not_redefine_frozen_daily_schedule(self):
        required_long = DAILY_TASKS - {'SpecDetailSeasonMonitor', 'WowDailyReportMonitor'}
        self.assertTrue(required_long.issubset(set(scheduler.LONG_MONITOR_TASKS)))
        self.assertIn('WagoSkillDiffMonitor', scheduler.LONG_MONITOR_TASKS)
        self.assertSetEqual(set(scheduler.DAILY_MONITOR_TASKS), DAILY_TASKS)
        self.assertEqual(scheduler.PORTAL_DATA_SCHEDULE_HOURS_BY_TASK['WowTodayMonitor'], (4, 8, 10))
        for name in DAILY_TASKS:
            with self.subTest(name=name):
                self.assertEqual(scheduler.monitor_default_wait_time(name), 86400)
                self.assertEqual(scheduler.PORTAL_DATA_SCHEDULE_HOURS_BY_TASK[name], (0,))
        for name, seconds in (('WagoSkillDiffMonitor', 3600), ('PortalPeakSpecRankMonitor', 600),
                              ('PlayerRaiderIOActivityMonitor', 600), ('SpecDungeonDpsRankingMonitor', 3600)):
            with self.subTest(name=name):
                self.assertNotIn(name, scheduler.DAILY_MONITOR_TASKS)
                self.assertNotIn(name, scheduler.PORTAL_DATA_SCHEDULED_TASKS)
                self.assertEqual(scheduler.monitor_default_wait_time(name), seconds)

    def test_player_daily_claim_and_wcl_ready_cycle_keep_existing_identity(self):
        from botend.services.wcl_collection_cycle import ranking_slot, scheduled_cycle_due_at
        season = SeasonMeta.objects.create(season_key='unified', season_name='Unified',
                                          is_active=True, mplus_zone_id=1, raid_zone_id=2)
        player = self.task('SpecDetailPlayerMonitor')
        self.assert_claim(player)
        player.refresh_from_db()
        self.assertEqual(json.loads(player.flag), {'kind': 'player_daily',
                                                  'day': '2026-11-11', 'state': 'claimed'})
        slot = ranking_slot(self.now)
        self.assertEqual(slot, self.now.replace(hour=0))
        cycle = WclCollectionCycle.objects.create(season=season, slot=slot, owner='ready',
                                                  phase='ready', collected_at=self.now)
        aggregate = self.task('SpecDetailAggregationMonitor')
        self.assertIsNotNone(scheduled_cycle_due_at(aggregate, self.now))
        self.assert_claim(aggregate)
        cycle.refresh_from_db()
        self.assertEqual(cycle.slot, slot)
        self.assertEqual(WclCollectionCycle.objects.count(), 1)


class UnifiedWorkerStartupTests(TestCase):
    @override_settings(MONITOR_TASK_AUTO_SYNC_PLUGINS=False)
    def test_backend_starts_exactly_three_unfiltered_ordinary_execution_workers(self):
        registry = ModuleType('LMonitor.config')
        registry.Monitor_Type_BaseObject_List = []
        with patch.dict('sys.modules', {'LMonitor.config': registry}):
            from botend import views
        submitted = []
        lifecycle = Mock(retiring=False)

        class RecordingPool:
            max_thread_num = None
            alive_thread_list = []

            def get_free_num(self):
                return self.max_thread_num - len(submitted)

            def new(self, target, *args, **kwargs):
                submitted.append((target, args, kwargs))

        waits = []

        def bounded_wait(seconds):
            waits.append(seconds)
            self.assertLess(len(waits), 12, 'startup must not stall before three workers exist')
            if len(submitted) >= 3:
                lifecycle.retiring = True

        lifecycle.wait.side_effect = bounded_wait
        isolated = Mock()
        pool = RecordingPool()
        with ExitStack() as stack:
            stack.enter_context(patch.object(views, 'ThreadPool', return_value=pool))
            # Also catches the old startup path without creating real daemons.
            stack.enter_context(patch.object(views, 'start_isolated_monitor_worker', isolated, create=True))
            for module, function in (
                ('gear_catalog_snapshot', 'start_catalog_snapshot_worker'),
                ('journal_loot_snapshot', 'start_journal_loot_snapshot_worker'),
                ('news_snapshot', 'start_news_snapshot_worker'),
                ('class_guide_snapshot', 'start_guide_snapshot_worker'),
                ('journal_snapshot', 'start_journal_snapshot_worker'),
                ('snapshot_cache_maintenance', 'start_snapshot_cache_maintenance_worker'),
            ):
                stack.enter_context(patch('botend.services.{}.{}'.format(module, function), return_value=None))
            views.LMonitorCoreBackend(lifecycle=lifecycle)
        self.assertEqual(pool.max_thread_num, 3)
        self.assertEqual(len(submitted), 3)
        self.assertEqual(len({id(target.__self__) for target, _, _ in submitted}), 3)
        for target, args, kwargs in submitted:
            self.assertIs(target.__func__, views.LMonitorCore.scan)
            self.assertEqual(args, ())
            self.assertEqual(kwargs, {}, 'ordinary workers must not have per-class filters')
        isolated.assert_not_called()
        lifecycle.drain_threads.assert_called_once()


@skipUnless(connection.vendor == 'mysql', 'Requires real InnoDB row locks; SQLite is not concurrency evidence')
class UnifiedSchedulerConcurrentClaimsTests(SchedulerFixtures, TransactionTestCase):
    """Run with the parent's isolated InnoDB probe; never create a DB here."""

    def race_claims(self, count):
        barrier = Barrier(count)

        def claim(index):
            connections.close_all()
            try:
                barrier.wait(timeout=10)
                got = scheduler.claim_next_monitor_task(now=self.now, lease_owner='racer-{}'.format(index),
                                                       lease_seconds=3600)
                return got.pk if got else None
            finally:
                connections.close_all()

        with ThreadPoolExecutor(max_workers=count) as pool:
            return list(pool.map(claim, range(count)))

    def test_atomic_shared_total_allows_exactly_three_of_five_short_claimants(self):
        tasks = [self.task('PortalPostMonitor', target=str(i)) for i in range(5)]
        results = [pk for pk in self.race_claims(5) if pk is not None]
        self.assertEqual(len(results), 3)
        self.assertEqual(len(set(results)), 3)
        self.assertTrue(set(results).issubset({task.pk for task in tasks}))
        self.assertEqual(MonitorTaskLease.objects.filter(is_manual=False).count(), 3)

    def test_atomic_night_long_limit_allows_exactly_two_non_rio_scopes(self):
        tasks = [self.task('MaxrollClassGuideMonitor', target=str(i)) for i in range(4)]
        results = [pk for pk in self.race_claims(4) if pk is not None]
        self.assertEqual(len(results), 2)
        self.assertEqual(len(set(results)), 2)
        self.assertTrue(set(results).issubset({task.pk for task in tasks}))
        self.assertEqual(MonitorTaskLease.objects.filter(is_manual=False).count(), 2)

    def test_atomic_scope_alias_claims_create_only_one_lease(self):
        task = self.task('PortalPostMonitor')
        self.task('renamed alias', type=task.type, target=task.target)
        results = [pk for pk in self.race_claims(3) if pk is not None]
        self.assertEqual(results, [task.pk])
        self.assertEqual(MonitorTaskLease.objects.count(), 1)
