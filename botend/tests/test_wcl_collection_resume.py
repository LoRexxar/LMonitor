"""验证恢复时缩小读取范围，以及最终核验、事务和所有权边界。"""
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import transaction
from django.test import TransactionTestCase
from django.utils import timezone

from botend.models import (SeasonMeta, SpecDungeonRanking, SpecRaidRanking, WclCollectionCycle,
                           WclCombatantFact, WclCombatantFetchState)
from botend.services.wcl_collection_cycle import (
    CycleOwnershipLost, finish_collection, frozen_combatant_records,
    begin_aggregation, finish_publication, collection_incomplete,
)
from botend.services.wcl_collection_resume import (
    CHECKPOINT_KEY, CombatantResume, collect_cycle_combatants,
)
from botend.services.wcl_combatant_snapshot import collect_dungeon_combatants
from botend.services.wcl_combatant_store import persist_events
from botend.tests.test_wcl_combatant_snapshot import event
from botend.tests.test_wcl_raw_staging import RawStagingIsolationMixin


RESUME = 'botend.services.wcl_collection_resume'
SNAPSHOT = 'botend.services.wcl_combatant_snapshot'


class WclCollectionResumeTests(RawStagingIsolationMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        self.season = SeasonMeta.objects.create(season_key='resume', season_name='恢复测试',
                                               mplus_zone_id=1, raid_zone_id=2)
        self.rows = [SpecDungeonRanking.objects.create(
            season_id=self.season.pk, dungeon_id=1, dungeon_name='测试',
            class_name='DeathKnight', spec_name='Frost', character_name='Player232',
            realm='BurningLegion', region='eu', report_code=f'Report{i}', fight_id=1,
            dps=100, keystone_level=20) for i in range(3)]
        self.cycle = WclCollectionCycle.objects.create(
            season=self.season, slot=timezone.now(), phase='combatants', owner='current',
            selected_samples={'DeathKnight/Frost': {'1': [row.pk for row in self.rows]}})
        self.fetcher = Mock(_wcl_last_error='rate_limit', _wcl_retry_at=None)
        self.fetcher.fetch_wcl_combatant_info.return_value = None

    def store(self, *indices):
        for index in indices:
            persist_events(self.rows[index].report_code, {1: [event(1)]}, fetched_at=timezone.now())

    def run_collection(self):
        self.cycle.refresh_from_db()
        counts = collect_cycle_combatants(self.cycle, self.fetcher)
        self.assertTrue(finish_collection(self.cycle, counts))
        complete = not collection_incomplete(counts)
        if not complete:
            self.cycle = begin_aggregation(self.season)
            finish_publication(self.cycle)
        return counts, complete

    def test_retry_reads_only_remaining_then_verifies_all_without_http_replay(self):
        self.store(0, 1)
        counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertEqual(counts['cached'], 2)
        self.assertEqual(counts[CHECKPOINT_KEY]['remaining'], [f'd:{self.rows[2].pk}'])
        with patch(RESUME + '.frozen_combatant_records', wraps=frozen_combatant_records) as read:
            counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.kwargs['references'], {f'd:{self.rows[2].pk}'})
        self.assertEqual((counts['selected'], counts['cached'], counts['preflight_fights']), (3, 2, 1))
        self.fetcher.fetch_wcl_combatant_info.reset_mock()
        self.fetcher.fetch_wcl_combatant_info.return_value = [event(1)]
        with patch(RESUME + '.frozen_combatant_records', wraps=frozen_combatant_records) as read:
            counts, finished = self.run_collection()
        self.assertTrue(finished)
        self.assertEqual(read.call_count, 2)
        self.assertEqual(read.call_args.kwargs, {})
        self.fetcher.fetch_wcl_combatant_info.assert_called_once_with('Report2', [1])
        self.assertEqual((counts['selected'], counts['cached'], counts['matched']), (3, 2, 1))
        self.assertEqual(counts['verification_fights'], 3)
        self.assertNotIn(CHECKPOINT_KEY, counts)

    def test_missing_and_permission_states_remain_eligible_after_expiry(self):
        self.store(0)
        for status, fight in [('missing', 1), ('permission_denied', 0)]:
            with self.subTest(status=status):
                WclCombatantFetchState.objects.update_or_create(
                    report_code='Report1', fight_id=fight,
                    defaults={'status': status, 'checked_at': timezone.now(),
                              'retry_after': timezone.now() + timedelta(hours=1)})
                counts, finished = self.run_collection()
                self.assertFalse(finished)
                self.assertEqual(set(counts[CHECKPOINT_KEY]['remaining']),
                                 {f'd:{row.pk}' for row in self.rows[1:]})
                WclCombatantFetchState.objects.filter(report_code='Report1').update(
                    retry_after=timezone.now() - timedelta(seconds=1))
                self.fetcher.fetch_wcl_combatant_info.reset_mock()
                self.run_collection()
                self.assertEqual(self.fetcher.fetch_wcl_combatant_info.call_args.args[0], 'Report1')

    def test_positive_fact_wins_over_active_report_denial(self):
        self.store(0, 1)
        self.run_collection()
        self.store(2)
        WclCombatantFetchState.objects.create(report_code='Report2', fight_id=0,
            status='permission_denied', checked_at=timezone.now(),
            retry_after=timezone.now() + timedelta(hours=1))
        self.fetcher.fetch_wcl_combatant_info.reset_mock()
        counts, finished = self.run_collection()
        self.assertTrue(finished)
        self.assertEqual((counts['cached'], counts['permission_denied']), (3, 0))
        self.fetcher.fetch_wcl_combatant_info.assert_not_called()

    def test_invalid_or_changed_cohort_checkpoint_falls_back_to_full_read(self):
        self.store(0, 1)
        counts, _ = self.run_collection()
        saved = counts[CHECKPOINT_KEY]
        invalid = [None, 'broken', {**saved, 'version': 99},
                   {**saved, 'remaining': []}, {**saved, 'cohort': 'old'}]
        for checkpoint in invalid:
            with self.subTest(checkpoint=checkpoint):
                self.cycle.counts = {CHECKPOINT_KEY: checkpoint}
                self.assertEqual(len(CombatantResume(self.cycle).remaining), 3)
        self.cycle.counts = counts
        self.cycle.selected_samples['DeathKnight/Frost']['1'].pop()
        self.assertEqual(len(CombatantResume(self.cycle).remaining), 2)

    def test_final_verification_catches_disappeared_positive_without_fetching_it(self):
        self.store(0, 1)
        self.run_collection()
        WclCombatantFact.objects.filter(report_code='Report0').delete()
        self.fetcher.fetch_wcl_combatant_info.return_value = [event(1)]
        self.fetcher.fetch_wcl_combatant_info.reset_mock()
        counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertEqual((counts['pending'], counts['cached'], counts['matched']), (1, 1, 1))
        self.assertEqual(counts[CHECKPOINT_KEY]['remaining'], [f'd:{self.rows[0].pk}'])
        self.fetcher.fetch_wcl_combatant_info.assert_called_once_with('Report2', [1])
        self.assertEqual(self.cycle.phase, 'combatants')

    def test_final_verification_rejects_missing_frozen_ranking_reference(self):
        self.store(0, 1)
        self.run_collection()
        self.rows[0].delete()
        self.fetcher.fetch_wcl_combatant_info.return_value = [event(1)]
        with self.assertRaisesMessage(ValueError, '冻结的日志样本引用已丢失'):
            self.run_collection()
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.phase, 'combatants')

    def test_expiry_during_collection_is_caught_before_ready(self):
        self.store(0)
        state = WclCombatantFetchState.objects.create(report_code='Report1', fight_id=1,
            status='missing', checked_at=timezone.now(), retry_after=timezone.now() + timedelta(hours=1))
        def fetch(*args):
            WclCombatantFetchState.objects.filter(pk=state.pk).update(
                retry_after=timezone.now() - timedelta(seconds=1))
            return [event(1)]
        self.fetcher.fetch_wcl_combatant_info.side_effect = fetch
        counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertEqual((counts['pending'], counts['missing']), (1, 0))

    def test_crash_after_fact_commit_before_progress_reuses_fact_on_restart(self):
        records = frozen_combatant_records(self.cycle)
        self.fetcher.fetch_wcl_combatant_info.return_value = [event(1)]
        resume = CombatantResume(self.cycle)
        original = resume.positive
        def interrupted(rows):
            original(rows)
            raise KeyboardInterrupt('模拟入库后进程退出')
        with self.assertRaises(KeyboardInterrupt):
            collect_dungeon_combatants(records, self.fetcher, apply=True, on_positive=interrupted)
        self.assertEqual(WclCombatantFact.objects.count(), 1)
        self.cycle.refresh_from_db()
        self.assertEqual(self.cycle.counts, {})
        self.fetcher.fetch_wcl_combatant_info.reset_mock()
        counts, finished = self.run_collection()
        self.assertTrue(finished)
        self.assertEqual(counts['cached'], 1)
        self.assertEqual(self.fetcher.fetch_wcl_combatant_info.call_count, 2)

    def test_rolled_back_fact_does_not_advance_checkpoint(self):
        self.fetcher.fetch_wcl_combatant_info.return_value = [event(1)]
        resume = CombatantResume(self.cycle)
        with self.assertRaises(RuntimeError), transaction.atomic():
            collect_dungeon_combatants(frozen_combatant_records(self.cycle), self.fetcher,
                                      apply=True, on_positive=resume.positive)
            self.assertEqual(resume.remaining, resume.references)
            raise RuntimeError('模拟事务回滚')
        self.assertEqual(WclCombatantFact.objects.count(), 0)
        self.assertEqual(resume.remaining, resume.references)

    def test_lost_owner_cannot_save_resume_or_final_result(self):
        self.store(0, 1, 2)
        WclCollectionCycle.objects.filter(pk=self.cycle.pk).update(owner='replacement')
        with self.assertRaises(CycleOwnershipLost):
            collect_cycle_combatants(self.cycle, self.fetcher)
        self.cycle.refresh_from_db()
        self.assertEqual((self.cycle.phase, self.cycle.counts), ('combatants', {}))

    def test_without_positive_progress_no_duplicate_reference_list_is_saved(self):
        counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertNotIn(CHECKPOINT_KEY, counts)

    def test_large_completed_cohort_is_not_scanned_on_each_failed_retry(self):
        extras = SpecDungeonRanking.objects.bulk_create([SpecDungeonRanking(
            season_id=self.season.pk, dungeon_id=1, dungeon_name='测试',
            class_name='DeathKnight', spec_name='Frost', character_name='Player232',
            realm='BurningLegion', region='eu', report_code='LargeReport', fight_id=fight,
            dps=100, keystone_level=20) for fight in range(1, 102)])
        self.cycle.selected_samples['DeathKnight/Frost']['1'].extend(row.pk for row in extras)
        self.cycle.save(update_fields=['selected_samples'])
        self.store(0, 1)
        persist_events('LargeReport', {fight: [event(fight)] for fight in range(1, 102)},
                       fetched_at=timezone.now())
        first, _ = self.run_collection()
        second, _ = self.run_collection()
        self.assertEqual((first['selected'], first['preflight_fights']), (104, 104))
        self.assertEqual((second['selected'], second['cached'], second['preflight_fights']), (104, 103, 1))
        self.assertNotIn('verification_fights', second)

    def test_newly_committed_positive_survives_another_reports_failure(self):
        self.fetcher.fetch_wcl_combatant_info.side_effect = [[event(1)], None]
        counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertEqual(counts['matched'], 1)
        self.assertEqual(set(counts[CHECKPOINT_KEY]['remaining']),
                         {f'd:{row.pk}' for row in self.rows[1:]})
        self.assertEqual(WclCombatantFact.objects.count(), 1)

    def test_dungeon_and_raid_primary_keys_do_not_collide(self):
        raid = SpecRaidRanking.objects.create(pk=self.rows[0].pk,
            season_id=self.season.pk, boss_id=1, boss_name='测试', difficulty=5,
            class_name='DeathKnight', spec_name='Frost', character_name='Player232',
            realm='BurningLegion', region='eu', report_code='RaidReport', fight_id=1, dps=100)
        self.cycle.selected_samples['raid:DeathKnight/Frost'] = {'5:1': [raid.pk]}
        self.cycle.save(update_fields=['selected_samples'])
        self.store(0, 1, 2)
        counts, finished = self.run_collection()
        self.assertFalse(finished)
        self.assertEqual((counts['selected'], counts['cached']), (4, 3))
        self.assertEqual(counts[CHECKPOINT_KEY]['remaining'], [f'r:{raid.pk}'])
        self.fetcher.fetch_wcl_combatant_info.return_value = [event(1)]
        self.assertTrue(self.run_collection()[1])
