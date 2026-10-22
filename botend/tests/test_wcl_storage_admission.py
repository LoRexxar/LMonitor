"""Real disk/ORM admission, durable pause and resume; no external fetches."""
import shutil
from datetime import timedelta

from django.conf import settings
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import (MonitorTask, SeasonMeta, SpecRaidRanking, WclCollectionCycle,
                           WclCombatantSnapshot, WclCombatantFetchState)
from botend.services.wcl_storage_guard import WCLStorageError, check_wcl_storage
from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
from botend.tests.aggregation_cache_helpers import isolate_work_cache


class WCLStorageAdmissionTests(TestCase):
    def setUp(self):
        isolate_work_cache(self)
        self.season = SeasonMeta.objects.create(
            season_key='storage-admission', season_name='Storage admission', is_active=True,
            mplus_encounters=[], raid_encounters=[], mplus_zone_id=1, raid_zone_id=2)
        self.task = MonitorTask.objects.create(name='SpecDetailRankingMonitor', target='', flag='last-good')
        self.monitor = SpecDetailRankingMonitor(None, self.task)
        self.cycle = WclCollectionCycle.objects.create(
            season=self.season, slot=timezone.now(), phase='rankings', owner='test-owner',
            completed_scopes=['dungeon:completed-before-pause'])
        self.old = SpecRaidRanking.objects.create(
            season_id=self.season.pk, boss_id=123, boss_name='Boss', difficulty=5,
            class_name='DeathKnight', spec_name='Frost', character_name='GuardPlayer',
            realm='KnownRealm', region='eu', report_code='GuardReport', fight_id=1,
            dps=100, talents_json=[{'talentID': 1, 'points': 1}], gear_json=[{'id': 2}],
            last_updated=timezone.now())
        self.high = shutil.disk_usage(settings.BASE_DIR).total * 2

    def test_scan_defers_without_fact_writes_then_resumes_same_cycle(self):
        before = SpecRaidRanking.objects.filter(pk=self.old.pk).values().get()
        with override_settings(WCL_MIN_FREE_DISK_BYTES=self.high):
            self.assertFalse(self.monitor.scan(''))
        self.cycle.refresh_from_db()
        self.task.refresh_from_db()
        self.assertEqual(self.cycle.phase, 'rankings')
        self.assertEqual(self.cycle.completed_scopes, ['dungeon:completed-before-pause'])
        self.assertIn('insufficient_space', self.cycle.error)
        self.assertGreater(self.cycle.next_retry_at, timezone.now())
        self.assertEqual(self.task.flag, 'last-good')
        self.assertEqual(SpecRaidRanking.objects.filter(pk=self.old.pk).values().get(), before)
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        # Make the already-recorded retry due; no new daily cycle or forced source fetch.
        WclCollectionCycle.objects.filter(pk=self.cycle.pk).update(
            next_retry_at=timezone.now() - timedelta(seconds=1))
        with override_settings(WCL_MIN_FREE_DISK_BYTES=1):
            self.assertTrue(self.monitor.scan(''))
        self.cycle.refresh_from_db()
        self.assertEqual(WclCollectionCycle.objects.count(), 1)
        self.assertEqual(self.cycle.phase, 'ready')
        self.assertEqual(self.cycle.completed_scopes, ['dungeon:completed-before-pause'])
        self.assertEqual(SpecRaidRanking.objects.filter(pk=self.old.pk).values().get(), before)

    def test_scope_admission_rechecks_before_transaction_and_checkpoint(self):
        self.monitor._cycle = self.cycle
        with override_settings(WCL_MIN_FREE_DISK_BYTES=self.high):
            with self.assertRaises(WCLStorageError):
                with self.monitor._scope_transaction():
                    self.monitor._checkpoint('raid:5:123')
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.completed_scopes, ['dungeon:completed-before-pause'])

    def test_diff_writer_rechecks_before_any_dml(self):
        incoming = SpecRaidRanking.objects.get(pk=self.old.pk)
        incoming.dps = 101
        with override_settings(WCL_MIN_FREE_DISK_BYTES=self.high), CaptureQueriesContext(connection) as queries:
            with self.assertRaises(WCLStorageError):
                SpecDetailRankingMonitor._sync_ranking_records(
                    SpecRaidRanking, {'season_id': self.season.pk, 'boss_id': 123, 'difficulty': 5},
                    [incoming], SpecDetailRankingMonitor.RAID_KEY_FIELDS,
                    SpecDetailRankingMonitor.RAID_UPDATE_FIELDS)
        self.assertFalse([q for q in queries if q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE'))])
        self.old.refresh_from_db()
        self.assertEqual(self.old.dps, 100)

    def test_combatant_admission_blocks_before_fetcher_or_negative_cache(self):
        records = [{'report_code': 'GuardReport', 'fight_id': 1, 'character_name': 'GuardPlayer',
                    'realm': 'KnownRealm', 'region': 'eu', 'class_name': 'DeathKnight', 'spec_name': 'Frost'}]
        with override_settings(WCL_MIN_FREE_DISK_BYTES=self.high):
            with self.assertRaises(WCLStorageError):
                collect_dungeon_combatants(records, apply=True, admission_check=check_wcl_storage)
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertFalse(WclCombatantFetchState.objects.exists())
