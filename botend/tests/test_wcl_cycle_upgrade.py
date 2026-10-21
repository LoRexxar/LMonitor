"""0239 only bridges a live, unfinished legacy ranking claim into control state."""
from datetime import datetime, timedelta
from importlib import import_module
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection, migrations
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings

from botend.models import MonitorTask, MonitorTaskLease, WclApiBudget, WclCollectionCycle
from botend.plugin_sync import claim_next_monitor_task
from botend.services.wcl_collection_cycle import begin_collection


from botend.tests.migration_test_helpers import restore_migration_schema


class WclCycleUpgradeTests(TransactionTestCase):
    before = [('botend', '0238_wclcombatantfetchstate')]
    after = [('botend', '0239_wcl_budget_collection_cycle')]

    def setUp(self):
        self.addCleanup(restore_migration_schema)
        self.executor = MigrationExecutor(connection)
        self.executor.migrate(self.before)
        self.old_apps = self.executor.loader.project_state(self.before).apps
        self.Season = self.old_apps.get_model('botend', 'SeasonMeta')
        self.Task = self.old_apps.get_model('botend', 'MonitorTask')
        self.Lease = self.old_apps.get_model('botend', 'MonitorTaskLease')
        self.Task.objects.update(is_active=False)
        self.Season.objects.update(is_active=False)
        self.claimed_at = datetime(2026, 10, 12, 3, 13, 12, 345678,
                                   tzinfo=ZoneInfo('Asia/Shanghai'))
        self.now = self.claimed_at + timedelta(minutes=4)
        self.season = self.Season.objects.create(
            season_key='upgrade', season_name='Upgrade', mplus_zone_id=1, raid_zone_id=2)
        self.task = self.Task.objects.create(
            name='SpecDetailRankingMonitor', target='', type=891, is_active=True,
            last_scan_time=self.claimed_at,
            flag=f'upgrade@rankings@{int((self.claimed_at - timedelta(days=1)).timestamp())}')


    def lease(self, **kwargs):
        values = dict(task_id=self.task.pk, owner='legacy-worker',
                      claimed_at=self.claimed_at, expires_at=self.now + timedelta(minutes=5))
        values.update(kwargs)
        return self.Lease.objects.create(**values)

    def upgrade(self):
        with patch('django.utils.timezone.now', return_value=self.now):
            self.executor = MigrationExecutor(connection)
            self.executor.migrate(self.after)
        self.apps = self.executor.loader.project_state(self.after).apps

    def replay_bridge(self):
        migration = import_module('botend.migrations.0239_wcl_budget_collection_cycle')
        operations = [op for op in migration.Migration.operations if isinstance(op, migrations.RunPython)]
        self.assertEqual(len(operations), 1, '0239 must have one bounded rollout bridge')
        with patch('django.utils.timezone.now', return_value=self.now):
            with connection.schema_editor() as editor:
                operations[0].code(self.apps, editor)

    def test_live_unfinished_claim_recovers_via_scheduler_without_changing_legacy_state(self):
        lease = self.lease()
        task_before = self.Task.objects.get(pk=self.task.pk).__dict__.copy()
        task_before.pop('_state')
        lease_before = self.Lease.objects.values().get(pk=self.task.pk)
        self.upgrade()
        self.assertEqual(WclCollectionCycle.objects.count(), 1,
                         'Deploy must not lose a legacy ranking claim just because last_scan was refreshed')
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.season_id, self.season.pk)
        self.assertEqual(cycle.slot, self.claimed_at.replace(hour=3, minute=0, second=0, microsecond=0))
        self.assertEqual(cycle.phase, 'rankings')
        self.assertEqual(cycle.completed_scopes, [])
        self.assertEqual(cycle.selected_samples, {})
        self.assertEqual(cycle.counts, {})
        self.assertEqual(cycle.owner, '')
        self.assertEqual(cycle.error, '')
        self.assertEqual(cycle.started_at, self.claimed_at)
        self.assertEqual(cycle.last_attempt_at, self.claimed_at)
        self.assertIsNone(cycle.next_retry_at)
        self.assertIsNone(cycle.collected_at)
        self.assertIsNone(cycle.published_at)
        self.assertFalse(WclApiBudget.objects.exists())
        self.assertEqual(MonitorTask.objects.values().get(pk=self.task.pk), task_before)
        self.assertEqual(MonitorTaskLease.objects.values().get(pk=self.task.pk), lease_before)
        self.replay_bridge()
        self.assertEqual(WclCollectionCycle.objects.get().pk, cycle.pk)
        with patch('django.utils.timezone.now', return_value=self.now):
            self.assertIsNone(claim_next_monitor_task(now=self.now))
        retry = lease.expires_at + timedelta(seconds=1)
        with patch('django.utils.timezone.now', return_value=retry):
            claimed = claim_next_monitor_task(now=retry, lease_owner='replacement')
            self.assertIsNotNone(claimed, 'The rankings phase, not tomorrow\'s fixed slot, must drive recovery')
            self.assertEqual(claimed.pk, self.task.pk)
            resumed = begin_collection(self.season, claimed, now=retry)
        self.assertEqual(resumed.pk, cycle.pk)
        self.assertEqual(WclCollectionCycle.objects.count(), 1)

    def test_no_lease_does_not_invent_recovery(self):
        self.upgrade()
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_expired_lease_does_not_invent_recovery(self):
        self.lease(expires_at=self.now)
        self.upgrade()
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_no_active_season_does_not_invent_recovery(self):
        self.lease()
        self.Season.objects.update(is_active=False)
        self.upgrade()
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_inactive_task_does_not_invent_recovery(self):
        self.lease()
        self.Task.objects.filter(pk=self.task.pk).update(is_active=False)
        self.upgrade()
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_other_monitor_lease_does_not_invent_recovery(self):
        self.lease()
        self.Task.objects.filter(pk=self.task.pk).update(name='SpecDetailAggregationMonitor')
        self.upgrade()
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_success_flag_before_lease_release_does_not_reopen_completed_claim(self):
        self.lease()
        self.Task.objects.filter(pk=self.task.pk).update(
            flag=f'upgrade@rankings@{int((self.claimed_at + timedelta(minutes=1)).timestamp())}')
        self.upgrade()
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_only_current_season_valid_success_timestamp_suppresses_recovery(self):
        self.upgrade()
        self.lease()
        recent = int((self.claimed_at + timedelta(minutes=1)).timestamp())
        flags = (None, '', 'upgrade@rankings@invalid', f'other-season@rankings@{recent}',
                 f'upgrade@aggregation@{recent}', 'upgrade@rankings@1',
                 f'upgrade@rankings@{int((self.now + timedelta(days=1)).timestamp())}')
        for flag in flags:
            with self.subTest(flag=flag):
                self.Task.objects.filter(pk=self.task.pk).update(flag=flag)
                self.replay_bridge()
                self.assertEqual(WclCollectionCycle.objects.count(), 1)
                WclCollectionCycle.objects.all().delete()

    def test_existing_same_slot_or_unfinished_cycle_is_untouched(self):
        self.upgrade()
        self.lease()
        slot = self.claimed_at.replace(hour=3, minute=0, second=0, microsecond=0)
        for phase, cycle_slot in [('published', slot), ('rankings', slot - timedelta(days=1)),
                                  ('combatants', slot - timedelta(days=1)),
                                  ('ready', slot - timedelta(days=1)),
                                  ('aggregating', slot - timedelta(days=1))]:
            with self.subTest(phase=phase):
                WclCollectionCycle.objects.all().delete()
                cycle = WclCollectionCycle.objects.create(
                    season_id=self.season.pk, slot=cycle_slot, phase=phase,
                    owner='existing-worker', completed_scopes=['existing-scope'],
                    selected_samples={'existing': {}}, error='preserve')
                before = WclCollectionCycle.objects.values().get(pk=cycle.pk)
                self.replay_bridge()
                self.replay_bridge()
                self.assertEqual(list(WclCollectionCycle.objects.values()), [before])
                cycle.delete()

    @override_settings(TIME_ZONE='UTC')
    def test_midnight_upgrade_uses_claim_slot_not_migration_wall_clock(self):
        self.now = self.claimed_at + timedelta(days=2, hours=21)
        self.lease()
        self.upgrade()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
        self.assertEqual(WclCollectionCycle.objects.get().slot,
                         self.claimed_at.replace(hour=3, minute=0, second=0, microsecond=0))

    def test_pre_three_am_claim_belongs_to_previous_fixed_slot(self):
        self.claimed_at = self.claimed_at.replace(hour=1)
        self.Task.objects.filter(pk=self.task.pk).update(last_scan_time=self.claimed_at)
        self.lease()
        self.upgrade()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
        self.assertEqual(WclCollectionCycle.objects.get().slot,
                         (self.claimed_at - timedelta(days=1)).replace(hour=3, minute=0, second=0, microsecond=0))

    def test_reverse_only_removes_new_tables_and_preserves_legacy_rows(self):
        self.lease()
        task_before = self.Task.objects.values().get(pk=self.task.pk)
        lease_before = self.Lease.objects.values().get(pk=self.task.pk)
        self.upgrade()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
        MigrationExecutor(connection).migrate(self.before)
        self.assertEqual(self.Task.objects.values().get(pk=self.task.pk), task_before)
        self.assertEqual(self.Lease.objects.values().get(pk=self.task.pk), lease_before)
        tables = connection.introspection.table_names()
        self.assertNotIn('wow_wcl_collection_cycle', tables)
        self.assertNotIn('wow_wcl_api_budget', tables)
        self.upgrade()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
