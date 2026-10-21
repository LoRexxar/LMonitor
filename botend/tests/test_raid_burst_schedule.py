"""部署后补建及每日调度与已完成周期的联动。"""
import tempfile
import json
from io import StringIO
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
from django.test import TestCase, override_settings
from django.core.management import call_command
from botend.models import MonitorTask, SeasonMeta, WclCollectionCycle
from botend.plugin_sync import claim_next_monitor_task, release_monitor_task_lease
from botend.services.wcl_collection_cycle import begin_aggregation, scheduled_cycle_due_at
from botend.services.raid_burst_snapshot import needs_raid_burst_refresh
from botend.services.raid_burst_skills import SUPPORTED
from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor


class RaidBurstScheduleTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 17, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.root = Path(tempfile.mkdtemp(prefix='raid-burst-schedule-'))
        override = override_settings(RAID_BURST_SNAPSHOT_ROOT=self.root, MEDIA_ROOT=self.root.parent / 'raid-public-tests')
        override.enable()
        self.addCleanup(override.disable)
        MonitorTask.objects.update(is_active=False)
        self.task = MonitorTask.objects.create(name='SpecDetailAggregationMonitor', target='', type=882,
                                               is_active=True, last_scan_time=self.now)
        self.season = SeasonMeta.objects.create(season_key='burst-schedule', season_name='补建测试',
            mplus_zone_id=1, raid_zone_id=2, is_active=True,
            raid_encounters=[{'id': 3470, 'name': '测试首领'}])
        self.cycle = WclCollectionCycle.objects.create(season=self.season, phase='published', owner='old',
            slot=self.now.replace(hour=3), collected_at=self.now.replace(hour=5),
            published_at=self.now.replace(hour=6), completed_scopes=['raid:5:3470', 'raid:4:3470'])

    def test_published_before_deployment_is_claimed_and_backfilled_once(self):
        module = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'
        profiles = {('Warrior', 'Arms'): SUPPORTED[('Warrior', 'Arms')]}
        with patch('django.utils.timezone.now', return_value=self.now), \
                patch('botend.services.raid_burst_aggregation.SUPPORTED', profiles), \
                patch(f'{module}.CLASS_SPEC_MAP', {'Warrior': ['Arms']}), \
                patch(f'{module}.publish_stats'), \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_leaderboard'), \
                patch('botend.services.spec_stats_service.SpecStatsService._compute_raid_stats', return_value={}), \
                patch('botend.services.raid_burst_events.fetch_events', side_effect=AssertionError('空样本不联网')):
            self.assertTrue(needs_raid_burst_refresh(self.cycle))
            claimed = claim_next_monitor_task(now=self.now, lease_owner='补建')
            self.assertEqual(claimed.pk, self.task.pk)
            self.assertTrue(SpecDetailAggregationMonitor(None, claimed).scan(''))
            release_monitor_task_lease(claimed.pk, '补建')
            self.cycle.refresh_from_db()
            self.assertEqual(self.cycle.phase, 'published')
            self.assertFalse(needs_raid_burst_refresh(self.cycle))
            self.assertIsNone(claim_next_monitor_task(now=self.now))
            self.assertIsNone(begin_aggregation(self.season, self.task, now=self.now))
            with patch('botend.services.raid_burst_aggregation.ANALYSIS_VERSION', 999):
                self.assertTrue(needs_raid_burst_refresh(self.cycle))
                self.assertLessEqual(scheduled_cycle_due_at(self.task, self.now), self.now)

    def test_incomplete_current_cycle_is_never_bypassed_by_old_published_cycle(self):
        self.cycle.slot -= timedelta(days=1)
        self.cycle.save(update_fields=['slot'])
        pending = WclCollectionCycle.objects.create(season=self.season, phase='combatants', owner='collecting',
                                                    slot=self.now.replace(hour=3))
        with patch('botend.services.raid_burst_snapshot.needs_raid_burst_refresh', side_effect=AssertionError('越过当前采集周期')):
            self.assertIsNone(begin_aggregation(self.season, self.task, now=self.now))
            self.assertIsNone(claim_next_monitor_task(now=self.now))
        pending.refresh_from_db()
        self.assertEqual(pending.phase, 'combatants')

    def test_today_ready_cycle_generates_burst_after_six_without_backfill(self):
        """昨日已部署、今日采集完成的正常路径，不能依赖已发布周期补建。"""
        self.cycle.phase = 'ready'
        self.cycle.published_at = None
        self.cycle.save(update_fields=['phase', 'published_at'])
        self.task.last_scan_time = self.now - timedelta(days=1)
        self.task.save(update_fields=['last_scan_time'])
        module = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'
        profiles = {('Warrior', 'Arms'): SUPPORTED[('Warrior', 'Arms')]}
        with patch('django.utils.timezone.now', return_value=self.now.replace(hour=6)), \
                patch('botend.services.raid_burst_aggregation.SUPPORTED', profiles), \
                patch(f'{module}.CLASS_SPEC_MAP', {'Warrior': ['Arms']}), \
                patch(f'{module}.publish_stats'), \
                patch.object(SpecDetailAggregationMonitor, '_aggregate_leaderboard'), \
                patch('botend.services.spec_stats_service.SpecStatsService._compute_raid_stats', return_value={}), \
                patch('botend.services.raid_burst_aggregation.fetch_events', side_effect=AssertionError('空样本不联网')):
            claimed = claim_next_monitor_task(now=self.now.replace(hour=6), lease_owner='今日聚合')
            self.assertEqual(claimed.pk, self.task.pk)
            self.assertTrue(SpecDetailAggregationMonitor(None, claimed).scan(''))
            release_monitor_task_lease(claimed.pk, '今日聚合')
            self.cycle.refresh_from_db()
            self.assertEqual(self.cycle.phase, 'published')
            self.assertFalse(needs_raid_burst_refresh(self.cycle))

    def test_diagnostics_do_not_change_task_or_cycle(self):
        output = StringIO()
        before = list(WclCollectionCycle.objects.values())
        call_command('raid_burst_status', stdout=output)
        result = json.loads(output.getvalue())
        self.assertTrue(result['需要补建战斗统计'])
        self.assertEqual(result['最近周期'][0]['阶段'], 'published')
        self.assertEqual(list(WclCollectionCycle.objects.values()), before)
        self.assertFalse((self.root / str(self.season.pk)).exists())

    def test_dungeon_only_published_cycle_does_not_trigger_backfill(self):
        self.cycle.completed_scopes = ['dungeon:101']
        self.cycle.save(update_fields=['completed_scopes'])
        self.assertFalse(needs_raid_burst_refresh(self.cycle))
        self.assertIsNone(claim_next_monitor_task(now=self.now))
