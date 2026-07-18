from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import SpecRaidRanking


class SpecDetailRankingMonitorDifferentialUpdateTests(TestCase):
    def _record(self, *, report_code, fight_id, dps, last_updated):
        return SpecRaidRanking(
            season_id=1,
            boss_id=100,
            boss_name='Boss',
            raid_zone_id=10,
            raid_zone_name='Raid',
            class_name='Warrior',
            spec_name='Fury',
            character_name='Player',
            realm='Kazzak',
            region='eu',
            dps=dps,
            kill_time=100000,
            talents_json=[{'node_id': 1}],
            talent_build_code='build',
            gear_json=[{'item_id': 1}],
            faction=1,
            guild_name='Guild',
            report_code=report_code,
            fight_id=fight_id,
            last_updated=last_updated,
        )

    def test_sync_ranking_records_only_creates_updates_and_deletes_differences(self):
        old_time = timezone.now() - timedelta(days=1)
        new_time = timezone.now()
        unchanged = self._record(report_code='same', fight_id=1, dps=100, last_updated=old_time)
        changed = self._record(report_code='changed', fight_id=2, dps=200, last_updated=old_time)
        stale = self._record(report_code='stale', fight_id=3, dps=300, last_updated=old_time)
        SpecRaidRanking.objects.bulk_create([unchanged, changed, stale])

        incoming = [
            self._record(report_code='same', fight_id=1, dps=100, last_updated=new_time),
            self._record(report_code='changed', fight_id=2, dps=250, last_updated=new_time),
            self._record(report_code='new', fight_id=4, dps=400, last_updated=new_time),
        ]

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=incoming,
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {'created': 1, 'updated': 1, 'deleted': 1, 'unchanged': 1})
        unchanged.refresh_from_db()
        changed.refresh_from_db()
        self.assertEqual(unchanged.last_updated, old_time)
        self.assertEqual(changed.dps, 250)
        self.assertEqual(changed.last_updated, new_time)
        self.assertFalse(SpecRaidRanking.objects.filter(report_code='stale').exists())
        self.assertTrue(SpecRaidRanking.objects.filter(report_code='new').exists())

    def test_sync_ranking_records_merges_duplicate_named_player_from_wcl_pages(self):
        now = timezone.now()
        duplicate_records = [
            self._record(report_code='same', fight_id=1, dps=100, last_updated=now),
            self._record(report_code='same', fight_id=1, dps=200, last_updated=now),
        ]

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=duplicate_records,
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {
            'created': 1,
            'updated': 0,
            'deleted': 0,
            'unchanged': 0,
        })
        self.assertEqual(SpecRaidRanking.objects.count(), 1)
        self.assertEqual(SpecRaidRanking.objects.get().dps, 100)

    def test_sync_ranking_records_keeps_distinct_anonymous_players_in_same_fight(self):
        now = timezone.now()
        first = self._record(report_code='a:report', fight_id=4, dps=100, last_updated=now)
        second = self._record(report_code='a:report', fight_id=4, dps=200, last_updated=now)
        for row in (first, second):
            row.character_name = 'Anonymous'
            row.realm = ''
            row.region = ''

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=[first, second],
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {
            'created': 2,
            'updated': 0,
            'deleted': 0,
            'unchanged': 0,
        })
        self.assertEqual(
            list(SpecRaidRanking.objects.order_by('dps').values_list('dps', flat=True)),
            [100.0, 200.0],
        )

    def test_sync_ranking_records_removes_existing_duplicate_business_keys(self):
        now = timezone.now()
        first = self._record(report_code='same', fight_id=1, dps=100, last_updated=now)
        duplicate = self._record(report_code='same', fight_id=1, dps=100, last_updated=now)
        SpecRaidRanking.objects.bulk_create([first, duplicate])

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=[self._record(report_code='same', fight_id=1, dps=100, last_updated=now)],
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {
            'created': 0,
            'updated': 0,
            'deleted': 1,
            'unchanged': 1,
        })
        self.assertEqual(SpecRaidRanking.objects.count(), 1)
