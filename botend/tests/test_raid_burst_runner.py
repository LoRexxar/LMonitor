from datetime import timedelta
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone

from botend.models import MonitorTask, MonitorTaskLease, SeasonMeta, WclCollectionCycle
from botend.plugin_sync import claim_monitor_task, monitor_task_due_at, sync_monitortasks_from_plugin_list
from botend.services.raid_burst_runner import burst_source_guard, run_raid_burst
from botend.services.wcl_collection_cycle import CycleOwnershipLost
from botend.controller.plugins.portal.RaidBurstAggregationMonitor import RaidBurstAggregationMonitor


class RaidBurstRunnerTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='burst-runner', season_name='Burst',
            is_active=True, mplus_zone_id=1, raid_zone_id=2,
            raid_encounters=[{'id': 3, 'name': 'Boss'}, {'id': 4, 'name': 'New boss'}])
        self.cycle = WclCollectionCycle.objects.create(season=self.season, slot=timezone.now(),
            started_at=timezone.now(), last_attempt_at=timezone.now(), phase='aggregating',
            owner='ordinary-owner', collected_at=timezone.now(),
            completed_scopes=['raid:5:3', 'raid:4:3'])
        self.task = MonitorTask.objects.create(name='RaidBurstAggregationMonitor', type=990,
            target='', is_active=False)

    def test_manual_uses_dedicated_lease_and_frozen_source_without_cycle_mutation(self):
        before = WclCollectionCycle.objects.values().get(pk=self.cycle.pk)
        task = claim_monitor_task(self.task.pk)
        def aggregate(season, cls, spec, *, guard, source_cutoff):
            self.assertEqual([row['id'] for row in season.raid_encounters], [3])
            self.assertEqual(source_cutoff, self.cycle.collected_at)
            with guard():
                self.assertTrue(MonitorTaskLease.objects.filter(task=self.task).exists())
            return {'scopes': []}
        with patch('botend.services.raid_burst_aggregation.aggregate_raid_burst', side_effect=aggregate):
            result = run_raid_burst(task, class_name='Warrior', spec_name='Arms')
        self.assertEqual(result['specs'], ['Warrior/Arms'])
        self.assertEqual(before, WclCollectionCycle.objects.values().get(pk=self.cycle.pk))

    def test_source_fence_rejects_new_cycle_cutoff_change_and_wrong_lease(self):
        task = claim_monitor_task(self.task.pk)
        with burst_source_guard(task, self.cycle):
            pass
        WclCollectionCycle.objects.filter(pk=self.cycle.pk).update(collected_at=timezone.now())
        with self.assertRaises(CycleOwnershipLost), burst_source_guard(task, self.cycle):
            pass
        self.cycle.refresh_from_db()
        WclCollectionCycle.objects.create(season=self.season, slot=self.cycle.slot + timedelta(days=1),
            started_at=timezone.now(), last_attempt_at=timezone.now(), phase='rankings', owner='new')
        with self.assertRaises(CycleOwnershipLost), burst_source_guard(task, self.cycle):
            pass
        task.name = 'SpecDetailAggregationMonitor'
        with self.assertRaises(CycleOwnershipLost), burst_source_guard(task, self.cycle):
            pass

    def test_sync_disabled_and_never_due_even_if_enabled(self):
        self.task.delete()
        sync_monitortasks_from_plugin_list([RaidBurstAggregationMonitor], default_is_active=True)
        task = MonitorTask.objects.get(name='RaidBurstAggregationMonitor')
        self.assertFalse(task.is_active)
        sync_monitortasks_from_plugin_list([RaidBurstAggregationMonitor], default_is_active=True)
        task.refresh_from_db()
        self.assertFalse(task.is_active)
        task.is_active = True
        self.assertIsNone(monitor_task_due_at(task))

    def test_newer_scope_facts_are_rejected_before_any_event_request(self):
        from botend.models import SpecRaidRanking
        SpecRaidRanking.objects.create(season_id=self.season.pk, boss_id=3, boss_name='Boss',
            class_name='Warrior', spec_name='Arms', difficulty=5, character_name='Player',
            realm='Realm', region='US', report_code='Report', fight_id=1, dps=100,
            last_updated=self.cycle.collected_at + timedelta(seconds=1))
        task = claim_monitor_task(self.task.pk)
        with patch('botend.services.raid_burst_aggregation.fetch_events') as fetch:
            with self.assertRaises(CycleOwnershipLost):
                run_raid_burst(task, class_name='Warrior', spec_name='Arms')
        fetch.assert_not_called()

    def test_page_guard_never_loads_dungeon_payload_or_scans_all_rankings(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        task = claim_monitor_task(self.task.pk)
        with CaptureQueriesContext(connection) as queries:
            with burst_source_guard(task, self.cycle):
                pass
        sql = '\n'.join(q['sql'].lower() for q in queries)
        self.assertNotIn('selected_samples', sql)
        from botend.models import SpecRaidRanking
        self.assertNotIn(SpecRaidRanking._meta.db_table.lower(), sql)

    def test_registered_plugin_uses_standard_executor_with_disabled_task(self):
        from LMonitor.config import Monitor_Type_BaseObject_List
        from botend.views import LMonitorCore
        self.task.type = Monitor_Type_BaseObject_List.index(RaidBurstAggregationMonitor)
        self.task.save(update_fields=['type'])
        task = claim_monitor_task(self.task.pk)
        before = WclCollectionCycle.objects.values().get(pk=self.cycle.pk)
        with patch('botend.views.LReq'), \
                patch('botend.views._record_monitor_task_alert') as alert, \
                patch('botend.services.raid_burst_aggregation.aggregate_raid_burst',
                      return_value={'scopes': []}) as aggregate:
            LMonitorCore()._execute_claimed_task(task, task._monitor_task_lease_owner)
        self.assertTrue(aggregate.called)
        alert.assert_not_called()
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())
        self.assertEqual(before, WclCollectionCycle.objects.values().get(pk=self.cycle.pk))

    def test_missing_and_expired_lease_rejected_and_ready_published_accepted(self):
        with self.assertRaises(CycleOwnershipLost), burst_source_guard(self.task, self.cycle):
            pass
        task = claim_monitor_task(self.task.pk)
        for phase in ('ready', 'published'):
            WclCollectionCycle.objects.filter(pk=self.cycle.pk).update(phase=phase)
            with burst_source_guard(task, self.cycle):
                pass
        MonitorTaskLease.objects.filter(task=self.task).update(expires_at=timezone.now())
        with self.assertRaises(CycleOwnershipLost), burst_source_guard(task, self.cycle):
            pass

    def test_command_failure_is_nonzero_and_releases_lease(self):
        WclCollectionCycle.objects.filter(pk=self.cycle.pk).update(phase='combatants')
        with self.assertRaises(CommandError):
            call_command('aggregate_raid_burst', class_name='Warrior', spec_name='Arms', stdout=StringIO())
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())

    def test_command_success_uses_real_heartbeat_and_releases_lease(self):
        with patch('botend.services.raid_burst_aggregation.aggregate_raid_burst', return_value={'scopes': []}):
            out = StringIO()
            call_command('aggregate_raid_burst', class_name='Warrior', spec_name='Arms', stdout=out)
        self.assertIn('completed', out.getvalue())
        self.assertFalse(MonitorTaskLease.objects.filter(task=self.task).exists())
