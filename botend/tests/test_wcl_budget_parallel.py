"""Parallel budget invariants; HTTP is controlled, the ledger is real."""
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone

from botend.models import WclApiBudget
from botend.services import wcl_api_budget as budget
from botend.tests.test_wcl_api_budget import DATA, QUERY, Response, quota


class ParallelBudgetTests(TransactionTestCase):
    def setUp(self):
        self.now = timezone.now()
        self.key = budget._query_info(QUERY, {'code': 'Report'})[0]
        self.row = WclApiBudget.objects.create(
            account_key=budget.account_key('parallel'), limit_per_hour=1000,
            spent=100, reset_at=self.now + timedelta(seconds=500),
            observed_at=self.now, costs={self.key: 40})

    def request(self, post):
        return budget.graphql_request('parallel', 'token', QUERY, {'code': 'Report'}, post=post)

    def test_fresh_window_measures_every_success_without_extra_http(self):
        calls = []
        def post(*args, **kwargs):
            calls.append(kwargs['json']['query'])
            self.assertIn('_wclBudget', calls[-1])
            return Response({'data': {**DATA, '_wclBudget': quota(
                spent=100 + len(calls), cost=1, limit=1000)}})
        for _ in range(25):
            self.assertEqual(self.request(post).error, '')
        self.row.refresh_from_db()
        self.assertEqual(len(calls), 25)
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (126, 25, 25))
        self.assertEqual(self.row.costs[self.key], 40)

    def test_measured_success_releases_only_its_proven_overestimate(self):
        self.row.spent = 750
        self.row.save()
        response = Response({'data': {**DATA, '_wclBudget': quota(spent=751, cost=1, limit=1000)}})
        self.assertEqual(self.request(lambda *a, **k: response).error, '')
        self.row.refresh_from_db()
        # The inline quota may omit the current request: keep its actual cost.
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (752, 1, 1))
        self.assertEqual(self.row.costs[self.key], 40)  # Do not lower learned safety margins.
        sent = Mock(return_value=Response({'data': DATA}))
        self.assertEqual(self.request(sent).error, '')
        sent.assert_called_once()
        self.row.refresh_from_db()
        self.assertEqual(self.row.spent, 792)

    def test_reverse_refunds_preserve_external_usage_and_completed_baselines(self):
        early, _, _ = budget._reserve(self.row.account_key, 'early', self.key, False)
        late, _, _ = budget._reserve(self.row.account_key, 'late', self.key, False)
        self.assertEqual((early.baseline, late.baseline), (0, 0))
        budget._settle(self.row.account_key, 'late', self.key,
                       {'data': {**DATA, '_wclBudget': quota(spent=600, cost=2, limit=1000)}})
        newest, _, deferred = budget._reserve(self.row.account_key, 'newest', self.key, False)
        self.assertIsNone(deferred)
        self.assertEqual(newest.baseline, 2)
        budget._settle(self.row.account_key, 'early', self.key,
                       {'data': {**DATA, '_wclBudget': quota(spent=101, cost=1, limit=1000)}})
        self.row.refresh_from_db()
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (643, 43, 3))
        budget._settle(self.row.account_key, 'newest', self.key,
                       {'data': {**DATA, '_wclBudget': quota(spent=602, cost=3, limit=1000)}})
        self.row.refresh_from_db()
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (606, 6, 6))
        self.assertEqual(self.row.reservations.count(), 0)

    def test_graphql_error_with_quota_does_not_refund(self):
        result = self.request(lambda *a, **k: Response({
            'data': {'_wclBudget': quota(spent=101, cost=1, limit=1000)},
            'errors': [{'message': 'failed'}]}))
        self.assertEqual(result.error, 'graphql')
        self.row.refresh_from_db()
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (141, 40, 40))

    def test_missing_or_invalid_actual_cost_never_releases_reservation(self):
        for actual in (None, -1, float('nan'), float('inf'), 'invalid'):
            with self.subTest(actual=actual):
                self.row.spent = 100
                self.row.charged = self.row.completed = 0
                self.row.save()
                result = self.request(lambda *a, **k: Response({'data': {
                    **DATA, '_wclBudget': quota(spent=0, cost=actual, limit=1000)}}))
                self.assertEqual(result.error, '')
                self.row.refresh_from_db()
                self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (140, 40, 40))

    def test_zero_actual_cost_refunds_once_without_erasing_existing_spent(self):
        reservation, _, _ = budget._reserve(self.row.account_key, 'zero', self.key, False)
        payload = {'data': {**DATA, '_wclBudget': quota(spent=0, cost=0, limit=1000)}}
        self.assertIsNone(budget._settle(self.row.account_key, reservation.request_id, self.key, payload))
        self.assertEqual(budget._settle(self.row.account_key, reservation.request_id, self.key, payload).error,
                         'budget_wait')
        self.row.refresh_from_db()
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (100, 0, 0))

    def test_event_workloads_learn_independent_costs(self):
        from botend.services.raid_burst_events import PAGE
        learned = {}
        for kind, actual in [('Casts', 8), ('Buffs', 2), ('CombatantInfo', 40)]:
            with self.subTest(kind=kind):
                variables = {'code': 'Report', 'ids': [1], 'actor': 2,
                             'start': 0.0, 'end': 1000.0, 'kind': kind}
                sent = Mock(return_value=Response({'data': {
                    **DATA, '_wclBudget': quota(spent=100, cost=actual, limit=1000)}}))
                self.assertEqual(budget.graphql_request('parallel', 'token', PAGE, variables, post=sent).error, '')
                self.assertIn('_wclBudget', sent.call_args.kwargs['json']['query'])
                learned[budget._query_info(PAGE, variables)[0]] = actual * 1.25
        self.assertEqual(len(learned), 3)
        self.row.refresh_from_db()
        for key, cost in learned.items():
            self.assertEqual(self.row.costs[key], cost)

    def test_known_requests_overlap_and_late_quota_preserves_spent(self):
        def first(*args, **kwargs):
            self.assertFalse(connection.in_atomic_block)
            result = self.request(lambda *a, **k: Response({'data': {**DATA, '_wclBudget': quota(spent=200, cost=40, limit=1000)}}))
            self.assertEqual(result.error, '')
            self.row.refresh_from_db()
            high = self.row.spent
            self.assertGreaterEqual(high, 240)
            return Response({'data': {**DATA, '_wclBudget': quota(spent=101, cost=40, limit=1000)}})
        self.assertEqual(self.request(first).error, '')
        self.row.refresh_from_db()
        self.assertGreaterEqual(self.row.spent, 240)

    def test_late_success_does_not_clear_429(self):
        def first(*args, **kwargs):
            limited = self.request(lambda *a, **k: Response(status=429, headers={'Retry-After': '900'}))
            self.assertEqual(limited.error, 'rate_limit')
            return Response({'data': {**DATA, '_wclBudget': quota(limit=1000)}})
        self.assertEqual(self.request(first).error, '')
        denied = Mock()
        self.assertEqual(self.request(denied).error, 'rate_limit')
        denied.assert_not_called()

    def test_worker_limit_is_live_per_account_and_clamped(self):
        self.assertEqual(budget.collection_worker_limit('new-account'), 10)
        for stored, expected in [(1, 1), (8, 8), (99, 16), (0, 1)]:
            WclApiBudget.objects.filter(pk=self.row.pk).update(collection_workers=stored)
            self.assertEqual(budget.collection_worker_limit('parallel'), expected)

    def test_atomic_reservations_do_not_overspend(self):
        self.row.spent = 750
        self.row.save()
        def first(*args, **kwargs):
            denied = Mock()
            self.assertEqual(self.request(denied).error, 'budget_wait')
            denied.assert_not_called()
            return Response({'data': DATA})
        self.assertEqual(self.request(first).error, '')
        self.row.refresh_from_db()
        self.assertEqual(self.row.spent, 790)

    def test_discovery_failure_cannot_spin_unmetered_probes(self):
        self.row.reset_at = None
        self.row.limit_per_hour = 0
        self.row.save()
        self.assertEqual(self.request(lambda *a, **k: Response({'data': {}})).error, 'invalid_quota')
        denied = Mock()
        self.assertEqual(self.request(denied).error, 'admission_busy')
        denied.assert_not_called()
        self.row.refresh_from_db()
        self.assertGreater(self.row.spent, 100)

    def test_bootstrap_is_singleflight_and_low_real_limit_blocks_business(self):
        self.row.delete()
        calls = []
        def probe(*args, **kwargs):
            calls.append(kwargs)
            denied = Mock()
            self.assertEqual(self.request(denied).error, 'admission_busy')
            denied.assert_not_called()
            return Response({'data': {'rateLimitData': quota(spent=6, limit=10)}})
        result = self.request(probe)
        self.assertEqual(result.error, 'budget_wait')
        self.assertEqual(len(calls), 1)
        self.row = WclApiBudget.objects.get(account_key=budget.account_key('parallel'))
        self.assertEqual(self.row.limit_per_hour, 10)

    def test_crash_charge_is_retained_and_expired_request_cannot_settle(self):
        reservation, _, deferred = budget._reserve(self.row.account_key, 'crashed', self.key, False)
        self.assertIsNone(deferred)
        later = reservation.expires_at + timedelta(seconds=1)
        with patch.object(budget.timezone, 'now', return_value=later):
            # An unrelated later success must not wash out the lost request.
            self.assertEqual(self.request(lambda *a, **k: Response({'data': {**DATA, '_wclBudget': quota(spent=1, cost=1, limit=1000)}})).error, '')
            self.row.refresh_from_db()
            before = (self.row.spent, self.row.charged, self.row.completed)
            result = budget._settle(self.row.account_key, 'crashed', self.key,
                                   {'data': {'rateLimitData': quota(spent=0)}})
        self.assertEqual(result.error, 'budget_wait')
        self.row.refresh_from_db()
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), before)
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (141, 41, 1))
        self.assertEqual(self.row.reservations.get(request_id='crashed').cost, 40)

    def test_reset_generation_fences_late_old_window_response(self):
        old, _, _ = budget._reserve(self.row.account_key, 'old-window', self.key, False)
        later = self.row.reset_at + timedelta(seconds=1)
        calls = []
        def post(*args, **kwargs):
            calls.append(kwargs)
            return Response({'data': {**DATA, 'rateLimitData': quota(spent=1, reset=1234, cost=1, limit=777)}})
        with patch.object(budget.timezone, 'now', return_value=later):
            self.assertEqual(self.request(post).error, '')
            self.row.refresh_from_db()
            before = (self.row.spent, self.row.reset_at, self.row.limit_per_hour)
            self.assertGreater(self.row.window_generation, old.generation)
            late = budget._settle(self.row.account_key, old.request_id, self.key,
                                  {'data': {'rateLimitData': quota(spent=9999, reset=9999)}})
            self.assertEqual(late.error, 'budget_wait')
        self.row.refresh_from_db()
        self.assertEqual((self.row.spent, self.row.reset_at, self.row.limit_per_hour), before)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.row.reset_at, later + timedelta(seconds=1234))

    def test_reset_waits_for_live_old_request_before_calibration(self):
        self.row.reset_at = self.now + timedelta(seconds=1)
        self.row.save()
        old, _, _ = budget._reserve(self.row.account_key, 'live-old', self.key, False)
        with patch.object(budget.timezone, 'now', return_value=self.now + timedelta(seconds=2)):
            denied = Mock()
            result = self.request(denied)
            self.assertEqual((result.error, result.retry_at), ('admission_busy', self.now + timedelta(seconds=3)))
            denied.assert_not_called()

    def test_late_snapshot_keeps_calls_completed_after_dispatch(self):
        def early_snapshot(*args, **kwargs):
            self.assertEqual(self.request(lambda *a, **k: Response({'data': DATA})).error, '')
            return Response({'data': {**DATA, '_wclBudget': quota(spent=600, cost=40, limit=1000)}})
        self.assertEqual(self.request(early_snapshot).error, '')
        self.row.refresh_from_db()
        self.assertGreaterEqual(self.row.spent, 680)

    def test_actual_cost_overrun_and_older_smaller_limit_are_conservative(self):
        def first(*args, **kwargs):
            self.assertEqual(self.request(lambda *a, **k: Response({'data': {**DATA, '_wclBudget': quota(spent=600, cost=80, limit=900)}})).error, '')
            return Response({'data': {**DATA, '_wclBudget': quota(spent=5, cost=5, limit=1000)}})
        self.assertEqual(self.request(first).error, '')
        self.row.refresh_from_db()
        self.assertEqual(self.row.limit_per_hour, 900)
        # Keep the 600-point upstream observation plus both actual costs,
        # not the early request's disproven 40-point reservation.
        self.assertEqual((self.row.spent, self.row.charged, self.row.completed), (685, 85, 85))
        self.assertEqual(self.row.costs[self.key], 100)
        denied = Mock()
        self.assertEqual(self.request(denied).error, 'budget_wait')
        denied.assert_not_called()

    def test_unknown_cost_is_singleflight(self):
        self.row.costs = {}
        self.row.save()
        def first(*args, **kwargs):
            denied = Mock()
            self.assertEqual(self.request(denied).error, 'admission_busy')
            denied.assert_not_called()
            return Response({'data': {**DATA, '_wclBudget': quota(limit=1000)}})
        self.assertEqual(self.request(first).error, '')
