"""Contracts for unchanged samples and one cross-dungeon collection phase."""
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.controller.plugins.portal.SpecDetailAggregationMonitor import SpecDetailAggregationMonitor
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import (MonitorTask, SeasonMeta, SpecDungeonRanking, SpecRaidRanking,
                           WclCombatantSnapshot, WclCollectionCycle)
from botend.services.spec_stats_service import _select_dungeon_sample_records
from botend.tests.test_wcl_combatant_snapshot import event


class DungeonCollectionPipelineTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='pipeline-')
        self.addCleanup(directory.cleanup)
        self.media = Path(directory.name)
        settings = self.settings(MEDIA_ROOT=directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.now = datetime(2026, 10, 12, 8, tzinfo=ZoneInfo('Asia/Shanghai'))
        self.season = SeasonMeta.objects.create(
            season_key='pipeline', season_name='Pipeline', is_active=True,
            mplus_encounters=[{'id': 1, 'name': 'One'}, {'id': 2, 'name': 'Two'}],
            mplus_zone_id=1, raid_zone_id=2,
        )
        self.task = MonitorTask.objects.create(name='SpecDetailRankingMonitor', target='',
                                               type=881, flag='last-good')
        self.monitor = SpecDetailRankingMonitor(None, self.task)

    @staticmethod
    def ranking(fight, actor=232):
        return {
            'name': f'Player{actor}', 'server': {'name': 'BurningLegion', 'region': 'eu'},
            'report': {'code': 'SharedReport', 'fightID': fight},
            'amount': 100, 'hardModeLevel': 20,
        }

    def run_monitor(self, fetch_rankings, fetch_combatants):
        path = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'
        with patch(path + '.CLASS_SPEC_MAP', {'DeathKnight': ['Frost']}), \
             patch(path + '.time.sleep'), \
             patch(path + '.timezone.now', return_value=self.now), \
             patch.object(self.monitor, 'fetch_wcl_rankings', side_effect=fetch_rankings), \
             patch.object(self.monitor, 'fetch_wcl_combatant_info', side_effect=fetch_combatants):
            return self.monitor.scan('')

    def test_all_rankings_precede_one_shared_report_fetch_and_resume_uses_cache(self):
        self.season.raid_encounters = [{'id': 3, 'name': 'Boss'}]
        self.season.save(update_fields=['raid_encounters'])
        trace = []
        def ranking(enc, *args, **kwargs):
            trace.append(('ranking', enc, kwargs.get('difficulty')))
            row = self.ranking(enc)
            if kwargs.get('difficulty'):
                row['talents'] = [{'talentID': 1234, 'points': 1}]
            return {'rankings': [row], 'hasMorePages': False}
        def combatants(code, fights):
            trace.append(('combatants', code, fights))
            self.assertEqual(SpecDungeonRanking.objects.count(), 2)
            self.assertEqual(set(SpecRaidRanking.objects.values_list('difficulty', flat=True)), {4, 5})
            cycle = WclCollectionCycle.objects.get()
            self.assertEqual(cycle.phase, 'combatants')
            self.assertEqual(cycle.completed_scopes,
                             ['dungeon:1', 'dungeon:2', 'raid:5:3', 'raid:4:3'])
            self.assertEqual(cycle.selected_samples['DeathKnight/Frost'], {
                str(row.dungeon_id): [row.pk] for row in SpecDungeonRanking.objects.all()})
            return [event(fight) for fight in fights]
        self.assertTrue(self.run_monitor(ranking, combatants))
        rankings_trace = [('ranking', 1, None), ('ranking', 2, None),
                          ('ranking', 3, 5), ('ranking', 3, 4)]
        self.assertEqual(trace, rankings_trace + [
                                 ('combatants', 'SharedReport', [1, 2])])
        self.assertEqual(WclCombatantSnapshot.objects.count(), 2)
        cycle = WclCollectionCycle.objects.get()
        self.assertEqual(cycle.phase, 'ready')
        frozen_ids = cycle.selected_samples
        # A ready cohort cannot be recollected before publication.
        trace.clear()
        self.assertFalse(self.run_monitor(ranking, combatants))
        self.assertEqual(trace, [])
        aggregation = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'
        with patch(aggregation + '.CLASS_SPEC_MAP', {'DeathKnight': ['Frost']}), \
             patch(aggregation + '.timezone.now', return_value=self.now):
            self.assertTrue(SpecDetailAggregationMonitor(None, None).scan(''))
        cycle.refresh_from_db()
        self.assertEqual(cycle.phase, 'published')
        self.assertIsNotNone(cycle.published_at)
        # A new real cycle still refreshes rankings, but reuses central facts.
        self.now += timedelta(days=1)
        self.assertTrue(self.run_monitor(ranking, combatants))
        self.assertEqual(trace, rankings_trace)
        next_cycle = WclCollectionCycle.objects.exclude(pk=cycle.pk).get()
        self.assertEqual(next_cycle.phase, 'ready')
        self.assertEqual(next_cycle.selected_samples, frozen_ids)
        self.assertEqual(next_cycle.counts['cached'], 2)
        self.assertEqual(next_cycle.counts['reports_requested'], 0)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 2)

    def test_bad_rankings_preserve_old_scope_and_never_enter_collection_union(self):
        invalid_pages = [
            {}, {'rankings': [], 'hasMorePages': True},
            {'rankings': [self.ranking(1), 'invalid-row'], 'hasMorePages': False},
            {'rankings': [self.ranking(1), {**self.ranking(1, 233), 'gear': 'invalid'}],
             'hasMorePages': False},
        ]
        # Commit the valid scope first, then prove the bad scope blocks the
        # whole cohort without rolling back that valid checkpoint.
        self.season.mplus_encounters.reverse()
        self.season.save(update_fields=['mplus_encounters'])
        target = self.media / 'aggregated' / str(self.season.pk) / 'DeathKnight/Frost/dungeon.json'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'{"last_good": true}')
        for payload in invalid_pages:
            with self.subTest(payload=payload):
                WclCollectionCycle.objects.all().delete()
                SpecDungeonRanking.objects.all().delete()
                WclCombatantSnapshot.objects.all().delete()
                old = SpecDungeonRanking.objects.create(
                    season_id=self.season.id, dungeon_id=1, dungeon_name='One',
                    class_name='DeathKnight', spec_name='Frost', character_name='Previous',
                    realm='BurningLegion', region='eu', report_code='OldReport',
                    fight_id=10, dps=55, keystone_level=20,
                )
                old_values = SpecDungeonRanking.objects.filter(pk=old.pk).values().get()
                WclCombatantSnapshot.objects.create(report_code='OldReport', fight_id=10,
                                                     actor_id=232, payload_json=event(10))
                calls = []
                def ranking(enc, *args, **kwargs):
                    return payload if enc == 1 else {
                        'rankings': [self.ranking(2)], 'hasMorePages': False}
                def combatants(code, fights):
                    calls.append((code, fights))
                    return [event(fight) for fight in fights]
                self.assertFalse(self.run_monitor(ranking, combatants))
                old.refresh_from_db()
                self.assertEqual(old.character_name, 'Previous')
                self.assertEqual(SpecDungeonRanking.objects.filter(pk=old.pk).values().get(), old_values)
                self.assertEqual(list(SpecDungeonRanking.objects.filter(dungeon_id=1)
                                      .values_list('pk', flat=True)), [old.pk])
                self.assertEqual(calls, [])
                self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, event(10))
                self.assertEqual(SpecDungeonRanking.objects.get(dungeon_id=2).character_name, 'Player232')
                cycle = WclCollectionCycle.objects.get()
                self.assertEqual(cycle.phase, 'rankings')
                self.assertEqual(cycle.completed_scopes, ['dungeon:2'])
                self.assertEqual(cycle.selected_samples, {})
                self.assertIsNone(cycle.collected_at)
                self.assertIsNone(cycle.published_at)
                self.assertIsNotNone(cycle.next_retry_at)
                self.task.refresh_from_db()
                self.assertEqual(self.task.flag, 'last-good')
                aggregation = 'botend.controller.plugins.portal.SpecDetailAggregationMonitor'
                with patch(aggregation + '.CLASS_SPEC_MAP', {'DeathKnight': ['Frost']}), \
                     patch(aggregation + '.timezone.now', return_value=self.now):
                    # The independent leaderboard may refresh, never WCL.
                    self.assertTrue(SpecDetailAggregationMonitor(None, None).scan(''))
                cycle.refresh_from_db()
                self.assertEqual(cycle.phase, 'rankings')
                self.assertIsNone(cycle.published_at)
                for allow_legacy in (False, True):
                    with self.assertRaises(CommandError):
                        call_command('aggregate_spec_stats', season_id=self.season.pk,
                                     class_name='DeathKnight', spec_name='Frost',
                                     dungeon_only=True, allow_legacy=allow_legacy)
                self.assertEqual(target.read_bytes(), b'{"last_good": true}')

    def test_identity_projection_retains_exact_selection_order_without_large_json(self):
        SpecDungeonRanking.objects.bulk_create([
            SpecDungeonRanking(
                season_id=self.season.id, dungeon_id=1, dungeon_name='One',
                class_name='DeathKnight', spec_name='Frost', character_name=f'Player{i}',
                realm='BurningLegion', region='eu', report_code='SharedReport', fight_id=i,
                dps=i, keystone_level=20, gear_json=[{'id': 123, 'name': 'gear'}],
                talents_json=[{'talentID': 456}],
            ) for i in range(1, 301)
        ])
        qs = SpecDungeonRanking.objects.filter(season_id=self.season.id, dungeon_id=1)
        expected = _select_dungeon_sample_records(qs)
        fields = ('id', 'report_code', 'fight_id', 'character_name', 'realm', 'region')
        with CaptureQueriesContext(connection) as queries:
            got = _select_dungeon_sample_records(qs, hydrate_fields=fields)
        self.assertEqual([r['id'] for r in got], [r['id'] for r in expected])
        self.assertEqual(len(got), 100)
        self.assertTrue(all(set(r) == set(fields) for r in got))
        self.assertEqual(len(queries), 2)
        for query in queries:
            for field in ('gear_json', 'talents_json', 'talent_build_code'):
                self.assertNotIn(field, query['sql'])
