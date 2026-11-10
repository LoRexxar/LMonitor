"""恢复按战斗归组的查询放大、内存边界与缓存语义回归。"""
from botend.tests.test_wcl_raw_staging import RawStagingIsolationMixin, persisted_event
from botend.models import WclCombatantFact
from botend.services.wcl_combatant_store import canonical_event

from collections import Counter
from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import WclCombatantFetchState, WclCombatantSnapshot
from botend.services import wcl_combatant_snapshot as service
from botend.tests.test_wcl_combatant_snapshot import event, sample


class GroupedFactReadTests(RawStagingIsolationMixin, TestCase):
    def store(self, code, actors, fight=11):
        WclCombatantSnapshot.objects.bulk_create([
            WclCombatantSnapshot(report_code=code, fight_id=fight, actor_id=actor,
                                 payload_json=event(fight, actor)) for actor in actors])

    def test_scattered_targets_load_each_fight_once_and_never_fetch(self):
        for i in range(120):
            self.store(f'private-report-{i}', range(1, 4))
        rows = [sample(actor=actor, code=f'private-report-{i}')
                for actor in range(1, 4) for i in range(120)]
        original = deepcopy(rows)
        fetcher = Mock()
        logs = []
        with patch.object(service, '_load_facts', wraps=service._load_facts) as load:
            counts = service.collect_dungeon_combatants(rows, fetcher, apply=True, log=logs.append)
        seen = Counter(service._fight_key(row) for call in load.call_args_list for row in call.args[0])
        self.assertEqual(len(seen), 120)
        self.assertEqual(set(seen.values()), {1})
        self.assertTrue(all(len(call.args[0]) <= 100 for call in load.call_args_list))
        self.assertEqual(counts['cached'], 360)
        self.assertEqual(counts['preflight_fights'], 120)
        self.assertEqual(counts['preflight_fact_rows'], 360)
        self.assertEqual(counts['preflight_batches'], 4)
        fetcher.fetch_wcl_combatant_info.assert_not_called()
        self.assertEqual(rows, original)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 360)
        self.assertEqual(WclCombatantFact.objects.count(), 0)
        self.assertFalse(WclCombatantFetchState.objects.exists())
        self.assertIn('preflight_fact_rows=360', logs[-1])
        self.assertFalse(any('private-report-' in line or 'Player' in line for line in logs))

    def test_one_fight_over_target_limit_is_not_hydrated_repeatedly(self):
        self.store('Large', range(1, 202))
        rows = [sample(actor=actor, code='Large') for actor in range(1, 202)]
        rows.extend(sample(fight=fight, code='Other') for fight in range(1, 102))
        with patch.object(service, '_load_facts', wraps=service._load_facts) as load:
            counts = service.collect_dungeon_combatants(rows)
        self.assertEqual([len(call.args[0]) for call in load.call_args_list], [1, 100, 1])
        self.assertEqual(counts['cached'], 201)
        self.assertEqual(counts['pending'], 101)
        self.assertEqual(counts['preflight_fact_rows'], 201)
        self.assertEqual(counts['preflight_fights'], 102)

    def test_positive_facts_override_negatives_and_expired_negatives_remain_pending(self):
        now = timezone.now()
        self.store('A', [1])
        WclCombatantFetchState.objects.bulk_create([
            WclCombatantFetchState(report_code=code, fight_id=fight, status=status,
                checked_at=now-timedelta(hours=8), retry_after=deadline)
            for code, fight, status, deadline in (
                ('A', 11, 'missing', now+timedelta(hours=1)),
                ('B', 0, 'permission_denied', now+timedelta(hours=1)),
                ('C', 11, 'missing', now-timedelta(seconds=1)))])
        rows = [sample(actor=1, code='A'), sample(code='B'), sample(code='C'),
                sample(actor=2, code='A'), {**sample(code='D'), 'realm': ''},
                {**sample(), 'report_code': ''}]
        before = list(WclCombatantFetchState.objects.order_by('pk').values())
        counts = service.collect_dungeon_combatants(rows)
        self.assertEqual({key: counts[key] for key in ('cached', 'missing', 'permission_denied',
                                                      'negative_cached', 'pending')},
                         dict(cached=1, missing=3, permission_denied=1, negative_cached=2, pending=1))
        self.assertEqual(before, list(WclCombatantFetchState.objects.order_by('pk').values()))

    def test_empty_input_reports_zero_preflight_without_queries(self):
        logs = []
        with self.assertNumQueries(0):
            counts = service.collect_dungeon_combatants([], apply=True, log=logs.append)
        self.assertEqual(counts['preflight_fact_rows'], 0)
        self.assertEqual(counts['preflight_batches'], 0)
        self.assertEqual(counts['selected'], 0)
        self.assertIn('preflight_fights=0', logs[-1])
