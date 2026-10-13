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
        self.assertGreaterEqual(self.row.spent, 180)
        self.assertTrue(self.row.reservations.filter(request_id='crashed').exists())

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
        self.assertGreaterEqual(self.row.spent, 720)
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
