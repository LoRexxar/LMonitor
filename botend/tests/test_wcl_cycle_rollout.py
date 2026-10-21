"""The first deployment preserves the daily slot and makes claims durable."""
from datetime import datetime, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.test import TestCase

from botend.models import MonitorTask, SeasonMeta, WclCollectionCycle
from botend.plugin_sync import claim_monitor_task, claim_next_monitor_task, release_monitor_task_lease
from botend.services.wcl_collection_cycle import begin_collection


class WclCycleRolloutTests(TestCase):
    def setUp(self):
        MonitorTask.objects.update(is_active=False)
        self.slot = datetime(2026, 10, 12, 0, 0, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.before = self.slot - timedelta(seconds=1)
        self.season = SeasonMeta.objects.create(
            season_key='rollout', season_name='Rollout', mplus_zone_id=1, raid_zone_id=2)
        self.task = MonitorTask.objects.create(
            name='SpecDetailRankingMonitor', target='', type=881, is_active=True,
            last_scan_time=(self.slot - timedelta(days=1)).replace(minute=37))

    def test_first_deployment_does_not_invent_a_yesterday_catchup(self):
        with patch('django.utils.timezone.now', return_value=self.before):
            self.assertIsNone(claim_next_monitor_task(now=self.before))
        self.assertFalse(WclCollectionCycle.objects.exists())

    def test_first_due_claim_creates_checkpoint_before_worker_start(self):
        now = self.slot
        # The supplied claim time, not wall time, defines the persisted slot.
        with patch('django.utils.timezone.now', return_value=self.before):
            first = claim_next_monitor_task(now=now, lease_owner='rollout-first', lease_seconds=60)
        self.assertIsNotNone(first)
        cycle = WclCollectionCycle.objects.filter(season=self.season).first()
        self.assertIsNotNone(cycle, 'A crash between claim and scan must not lose the daily run')
        self.assertEqual(cycle.slot, now)
        self.assertEqual(cycle.phase, 'rankings')
        self.assertEqual(cycle.started_at, now)
        self.assertEqual(cycle.last_attempt_at, now)
        self.assertEqual(cycle.owner, first._wcl_cycle_owner)
        retry = now + timedelta(seconds=61)
        with patch('django.utils.timezone.now', return_value=retry):
            resumed = claim_next_monitor_task(now=retry, lease_owner='rollout-replacement')
        self.assertEqual(resumed.pk, self.task.pk)
        self.assertEqual(WclCollectionCycle.objects.get().pk, cycle.pk)
        with patch('django.utils.timezone.now', return_value=retry):
            started = begin_collection(self.season, resumed, now=retry)
        self.assertEqual(started.pk, cycle.pk)
        self.assertEqual(started.owner, resumed._wcl_cycle_owner)
        self.assertNotEqual(started.owner, cycle.owner)
        self.assertEqual(WclCollectionCycle.objects.count(), 1)

    def test_manual_first_claim_uses_explicit_claim_time(self):
        now = self.slot
        with patch('django.utils.timezone.now', return_value=self.before):
            task = claim_monitor_task(self.task.pk, now=now)
        cycle = WclCollectionCycle.objects.get(season=self.season)
        self.assertEqual(cycle.slot, now)
        self.assertEqual(cycle.started_at, now)
        self.assertEqual(cycle.last_attempt_at, now)
        self.assertEqual(cycle.owner, task._wcl_cycle_owner)

    def test_manual_ranking_claim_cannot_reopen_publication_phases(self):
        now = self.slot
        cycle = WclCollectionCycle.objects.create(
            season=self.season, slot=now, phase='ready', owner='publication-owner')
        for phase in ('ready', 'aggregating', 'published'):
            with self.subTest(phase=phase):
                WclCollectionCycle.objects.filter(pk=cycle.pk).update(phase=phase)
                with patch('django.utils.timezone.now', return_value=now):
                    task = claim_monitor_task(self.task.pk, now=now)
                    self.assertIsNotNone(task)
                    self.assertIsNone(begin_collection(self.season, task, now=now))
                cycle.refresh_from_db()
                self.assertEqual(cycle.phase, phase)
                self.assertEqual(cycle.owner, 'publication-owner')
                self.assertEqual(WclCollectionCycle.objects.count(), 1)
                self.assertTrue(release_monitor_task_lease(task.pk, task._monitor_task_lease_owner))

    def test_published_history_uses_daily_slot_not_last_scan(self):
        now = self.slot
        WclCollectionCycle.objects.create(
            season=self.season, slot=now - timedelta(days=1), phase='published', owner='old')
        self.task.last_scan_time = now
        self.task.save(update_fields=['last_scan_time'])
        self.assertIsNone(claim_next_monitor_task(now=self.before))
        task = claim_next_monitor_task(now=now)
        self.assertIsNotNone(task)
        cycle = WclCollectionCycle.objects.get(season=self.season, slot=now)
        self.assertEqual(cycle.phase, 'rankings')
        self.assertEqual(cycle.owner, task._wcl_cycle_owner)
