"""Persistent negative-cache contracts; ORM is real, only WCL is controlled."""
from datetime import timedelta
from unittest.mock import Mock, patch

from django.apps import apps
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import WclCombatantSnapshot
from botend.services import wcl_combatant_snapshot as service


def sample(fight=11, actor=232, code='Report'):
    return dict(report_code=code, fight_id=fight, character_name=f'Player{actor}',
                realm='Burning-Legion', region='eu', class_name='DeathKnight', spec_name='Frost')


def event(fight=11, actor=232):
    return dict(type='combatantinfo', fight=fight, sourceID=actor, specID=251,
                source={'id': actor, 'name': f'Player{actor}', 'server': 'BurningLegion'},
                critMelee=1410)


class CombatantNegativeCacheTests(TestCase):
    @property
    def states(self):
        return apps.get_model('botend', 'WclCombatantFetchState').objects

    def collect(self, rows, fetcher, **kwargs):
        result = service.collect_dungeon_combatants(rows, fetcher, **kwargs)
        self.assertEqual(result['selected'], sum(result[key] for key in (
            'cached', 'matched', 'missing', 'permission_denied', 'failed', 'pending')))
        return result

    def fetcher(self, events=None, error=''):
        fetcher = Mock(_wcl_last_error=error)
        fetcher.fetch_wcl_combatant_info.return_value = events
        return fetcher

    def state(self, fight=11, status='missing', code='Report', expired=False):
        now = timezone.now()
        return self.states.create(report_code=code, fight_id=fight, status=status,
                                  checked_at=now - timedelta(hours=8),
                                  retry_after=now + timedelta(hours=-1 if expired else 1))

    def test_empty_result_reused_by_independent_collector_until_expiry(self):
        first = self.fetcher([])
        result = self.collect([sample()], first, apply=True)
        self.assertEqual(result['missing'], 1)
        second = self.fetcher([event()])
        repeated = self.collect([sample()], second, apply=True)
        second.fetch_wcl_combatant_info.assert_not_called()
        self.assertEqual((repeated['missing'], repeated['negative_cached']), (1, 1))
        state = self.states.get(report_code='Report', fight_id=11)
        self.assertEqual(state.status, 'missing')
        self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=6))
        with patch('django.utils.timezone.now', return_value=state.retry_after):
            recovered = self.collect([sample()], second, apply=True)
        second.fetch_wcl_combatant_info.assert_called_once_with('Report', [11])
        self.assertEqual(recovered['matched'], 1)
        self.assertFalse(self.states.exists())
        self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, event())

    def test_real_collector_instances_share_persisted_empty_result(self):
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        empty_page = {'reportData': {'report': {'masterData': {'actors': []},
                      'events': {'data': [], 'nextPageTimestamp': None}}}}
        first, second = SpecDetailBase(None, None), SpecDetailBase(None, None)
        with patch.object(first, '_wcl_graphql', return_value=empty_page) as first_api:
            self.assertEqual(self.collect([sample()], first, apply=True)['missing'], 1)
        first_api.assert_called_once()
        persisted = self.states.get()
        with patch.object(second, '_wcl_graphql') as second_api:
            result = self.collect([sample()], second, apply=True)
        second_api.assert_not_called()
        self.assertEqual((result['missing'], result['negative_cached']), (1, 1))
        persisted.refresh_from_db()
        self.assertEqual(self.states.count(), 1)

    def test_missing_pagination_field_fails_without_persisting_facts_or_negative_cache(self):
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        incomplete_page = {'reportData': {'report': {'masterData': {'actors': []},
                           'events': {'data': []}}}}
        collector = SpecDetailBase(None, None)
        with patch.object(collector, '_wcl_graphql', return_value=incomplete_page) as api:
            result = self.collect([sample()], collector, apply=True)
        api.assert_called_once()
        self.assertEqual(result['failed'], 1)
        self.assertEqual((result['missing'], result['matched'], result['snapshots_written'],
                          result['negative_cached']), (0, 0, 0, 0))
        self.assertIn(collector._wcl_last_error, ('invalid_payload', 'invalid_pagination'))
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        self.assertFalse(self.states.exists())

    def test_permission_is_report_wide_and_skips_unrequested_batches(self):
        rows = [sample(fight) for fight in range(1, 42)] + [sample(code='Public')]
        first = self.fetcher(error='permission')
        first.fetch_wcl_combatant_info.side_effect = [None, [event()]]
        result = self.collect(rows, first, apply=True)
        self.assertEqual((result['permission_denied'], result['matched']), (41, 1))
        self.assertEqual((result['reports_requested'], result['fights_requested']), (2, 21))
        self.assertEqual(first.fetch_wcl_combatant_info.call_count, 2)
        state = self.states.get()
        self.assertEqual((state.report_code, state.fight_id, state.status),
                         ('Report', 0, 'permission_denied'))
        self.assertEqual(state.retry_after - state.checked_at, timedelta(hours=1))
        second = self.fetcher([])
        result = self.collect([sample(999)], second, apply=True)
        self.assertEqual((result['permission_denied'], result['negative_cached']), (1, 1))
        second.fetch_wcl_combatant_info.assert_not_called()
        with patch('django.utils.timezone.now', return_value=state.retry_after):
            result = self.collect([sample(999)], second, apply=True)
        self.assertEqual(result['missing'], 1)
        self.assertFalse(self.states.filter(fight_id=0).exists())
        self.assertEqual(self.states.get().fight_id, 999)

    def test_dry_run_is_read_only_including_expired_states(self):
        self.state()
        self.state(0, 'permission_denied', code='Private')
        self.state(12, expired=True)
        before = list(self.states.order_by('id').values())
        rows = [sample(), sample(code='Private'), sample(12), sample(13)]
        fetcher = self.fetcher([event()])
        with CaptureQueriesContext(connection) as queries:
            result = self.collect(rows, fetcher)
        self.assertEqual((result['missing'], result['permission_denied'],
                          result['pending'], result['negative_cached']), (1, 1, 2, 2))
        fetcher.fetch_wcl_combatant_info.assert_not_called()
        self.assertTrue(all(q['sql'].lstrip().upper().startswith('SELECT') for q in queries))
        self.assertEqual(list(self.states.order_by('id').values()), before)
        self.assertEqual(WclCombatantSnapshot.objects.count(), 0)

    def test_new_positive_facts_override_both_negative_scopes_without_mutation(self):
        self.state()
        self.state(0, 'permission_denied')
        payload = event()
        WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=232,
                                           payload_json=payload)
        fetcher = self.fetcher([])
        for apply in (False, True):
            result = self.collect([sample()], fetcher, apply=apply)
            self.assertEqual((result['cached'], result['negative_cached']), (1, 0))
        fetcher.fetch_wcl_combatant_info.assert_not_called()
        self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, payload)

    def test_matching_actor_and_missing_actor_share_fight_state_not_raw_copy(self):
        fetcher = self.fetcher([event()])
        result = self.collect([sample(), sample(actor=233)], fetcher, apply=True)
        self.assertEqual((result['matched'], result['missing']), (1, 1))
        self.assertEqual((self.states.get().fight_id, self.states.get().status), (11, 'missing'))
        fetcher = self.fetcher([])
        repeated = self.collect([sample(), sample(actor=233)], fetcher, apply=True)
        self.assertEqual((repeated['cached'], repeated['missing'], repeated['negative_cached']), (1, 1, 1))
        fetcher.fetch_wcl_combatant_info.assert_not_called()
        self.assertEqual({f.name for f in self.states.model._meta.fields},
                         {'id', 'report_code', 'fight_id', 'status', 'checked_at', 'retry_after'})

    def test_missing_requires_persisted_exact_identity_and_keeps_old_actor_immutable(self):
        original = event(actor=233)
        WclCombatantSnapshot.objects.create(report_code='Report', fight_id=11, actor_id=233,
                                           payload_json=original)
        # The returned actor changed names; immutable persisted fact cannot match Player232.
        changed = {**event(), 'sourceID': 233, 'source': {**event()['source'], 'id': 233}}
        result = self.collect([sample()], self.fetcher([changed]), apply=True)
        self.assertEqual((result['missing'], result['matched'], result['snapshots_written']), (1, 0, 0))
        self.assertEqual(WclCombatantSnapshot.objects.get().payload_json, original)
        self.assertEqual(self.states.get().status, 'missing')

    def test_missing_is_scoped_by_report_and_fight_and_positive_spec_is_exact(self):
        self.state()
        wrong_spec = {**event(12), 'specID': 250}
        fetcher = self.fetcher()
        fetcher.fetch_wcl_combatant_info.side_effect = [[wrong_spec], [event()]]
        result = self.collect([sample(), sample(12), sample(code='Other')], fetcher, apply=True)
        self.assertEqual((result['missing'], result['negative_cached'], result['matched']), (2, 1, 1))
        self.assertEqual([call.args for call in fetcher.fetch_wcl_combatant_info.call_args_list],
                         [('Report', [12]), ('Other', [11])])

    def test_transient_and_incomplete_failures_never_create_negative_states(self):
        for error in ('network', 'authentication', 'rate_limit', 'http_500', 'graphql',
                      'request_failed', 'report_unavailable', 'invalid_payload',
                      'invalid_pagination', 'pagination_limit', ''):
            with self.subTest(error=error):
                result = self.collect([sample()], self.fetcher(error=error), apply=True)
                self.assertEqual(result['failed'], 1)
                self.assertFalse(self.states.exists())
        invalid = ({}, [None], [{'fight': 11}], [event(12)],
                   [{**event(), 'sourceID': 999}], [event(), {**event(), 'critMelee': 1}])
        for payload in invalid:
            with self.subTest(payload=payload):
                result = self.collect([sample()], self.fetcher(payload), apply=True)
                self.assertEqual(result['failed'], 1)
                self.assertFalse(self.states.exists())
        self.assertEqual(WclCombatantSnapshot.objects.count(), 0)

    def test_full_pagination_required_before_persisting_missing(self):
        from botend.controller.plugins.portal.SpecDetailBase import SpecDetailBase
        def page(cursor):
            raw = event(actor=233)
            actor = raw.pop('source')
            return {'reportData': {'report': {'masterData': {'actors': [actor]},
                    'events': {'data': [raw], 'nextPageTimestamp': cursor}}}}
        collector = SpecDetailBase(None, None)
        with patch.object(collector, '_wcl_graphql', side_effect=[page(200), None]):
            self.assertEqual(self.collect([sample()], collector, apply=True)['failed'], 1)
        self.assertFalse(self.states.exists())
        self.assertFalse(WclCombatantSnapshot.objects.exists())
        with patch.object(collector, '_wcl_graphql', side_effect=[page(200), page(None)]) as api:
            self.assertEqual(self.collect([sample()], collector, apply=True)['missing'], 1)
        self.assertEqual(api.call_count, 2)
        self.assertEqual(self.states.get().status, 'missing')
        self.assertEqual(WclCombatantSnapshot.objects.count(), 1)

    def test_expired_missing_refreshes_only_after_authoritative_result(self):
        old = self.state(expired=True)
        original = (old.checked_at, old.retry_after)
        self.assertEqual(self.collect([sample()], self.fetcher(error='network'), apply=True)['failed'], 1)
        old.refresh_from_db()
        self.assertEqual((old.checked_at, old.retry_after), original)
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now):
            self.assertEqual(self.collect([sample()], self.fetcher([]), apply=True)['missing'], 1)
        old.refresh_from_db()
        self.assertEqual((old.checked_at, old.retry_after), (now, now + timedelta(hours=6)))

    def test_unique_scope_is_enforced_by_database(self):
        self.state()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.state()

    def test_fact_and_state_preflight_is_bounded_to_100_observations(self):
        rows = [sample(fight) for fight in range(1, 202)]
        self.states.bulk_create([self.states.model(
            report_code='Report', fight_id=row['fight_id'], status='missing',
            checked_at=timezone.now(), retry_after=timezone.now() + timedelta(hours=6)) for row in rows])
        with patch.object(service, '_load_facts', wraps=service._load_facts) as facts, \
             CaptureQueriesContext(connection) as queries:
            result = self.collect(rows, self.fetcher([]))
        self.assertEqual(result['negative_cached'], 201)
        self.assertEqual([len(call.args[0]) for call in facts.call_args_list], [100, 100, 1])
        state_queries = [q['sql'] for q in queries if 'wow_wcl_combatant_fetch_state' in q['sql']]
        self.assertEqual(len(state_queries), 3)
        self.assertFalse(any('payload_json' in sql for sql in state_queries))

    def test_readback_hydration_is_bounded_even_with_many_actors_in_one_fight(self):
        rows = [sample(actor=actor) for actor in range(1, 202)]
        fetcher = self.fetcher([event(actor=actor) for actor in range(1, 202)])
        with patch.object(service, '_load_facts', wraps=service._load_facts) as facts:
            result = self.collect(rows, fetcher, apply=True)
        self.assertEqual(result['matched'], 201)
        self.assertTrue(all(len(call.args[0]) <= 100 for call in facts.call_args_list))
        self.assertFalse(self.states.exists())

    def test_request_batches_remain_at_most_twenty_fights(self):
        rows = [sample(fight) for fight in range(1, 42)]
        fetcher = self.fetcher([])
        result = self.collect(rows, fetcher, apply=True)
        self.assertEqual(result['missing'], 41)
        self.assertEqual([len(call.args[1]) for call in fetcher.fetch_wcl_combatant_info.call_args_list], [20, 20, 1])
        self.assertEqual(self.states.count(), 41)
        with self.assertRaises(ValueError):
            self.collect(rows, fetcher, apply=True, batch_size=21)
