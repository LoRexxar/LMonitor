"""真实队列、来源缓存、租约及HTTP边界的补采回归。"""
from datetime import timedelta
import json
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import CharacterEnrichmentTask, RaiderIOCharacterActivity, RaiderIOProfileCache, RaiderIORunRoster
from botend.services.character_enrichment import (enqueue_enrichment, consume_enrichment, EnrichmentBudget,
    _claim, _finish, guarded_get, source_backoff)
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.tests.test_player_raiderio_activity import PlayerActivityTests, payload
from botend.tests.test_raiderio_run_roster import details


class CharacterEnrichmentTests(TestCase):
    setUp = PlayerActivityTests.setUp
    make_player = PlayerActivityTests.make_player

    def activity(self, runs=None, **fields):
        from botend.services.player_raiderio_activity import player_identity
        return RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(self.player), payload_json={
                'mythic_plus_best_runs': runs if runs is not None else payload()['mythic_plus_best_runs'], **fields})

    def response(self, data=None, status=200):
        return Mock(status_code=status, headers={'Retry-After': '900'}, json=Mock(return_value=data or details()))

    def test_profile_is_saved_before_roster_http_and_cache_hit_has_no_http(self):
        data = payload()
        data.update(gear={'items': {}}, talents=[])
        http = Mock(return_value=self.response(data))
        monitor = SpecDetailPlayerMonitor(None, None)
        with patch('requests.get', http):
            self.assertEqual(monitor._fetch_profile_with_activity(self.player, self.season), data)
            self.assertEqual(monitor._fetch_profile_with_activity(self.player, self.season), data)
        self.assertEqual(http.call_count, 1)
        self.assertEqual(RaiderIOProfileCache.objects.get().payload, data)
        self.assertEqual(CharacterEnrichmentTask.objects.count(), 1)
        self.assertEqual(RaiderIORunRoster.objects.count(), 0)
        roster_http = Mock(return_value=self.response())
        result = consume_enrichment(self.season, EnrichmentBudget(roster_http))
        self.assertEqual((result['attempted'], result['ready']), (1, 1))
        self.assertEqual(roster_http.call_count, 1)
        self.assertEqual(RaiderIORunRoster.objects.get().status, 'ready')

    def test_shared_run_deduplication_does_not_reset_success_or_retry_clock(self):
        row = self.activity()
        enqueue_enrichment(row, self.season, self.player)
        http = Mock(return_value=self.response())
        consume_enrichment(self.season, EnrichmentBudget(http))
        before = CharacterEnrichmentTask.objects.values().get()
        row.payload_json['mythic_plus_best_runs'][0]['role'] = 'dps'
        enqueue_enrichment(row, self.season, self.player)
        self.assertEqual(CharacterEnrichmentTask.objects.values().get(), before)
        self.assertEqual(consume_enrichment(self.season, EnrichmentBudget(http))['attempted'], 0)
        self.assertEqual(http.call_count, 1)

    def test_same_run_across_ranked_and_bound_identity_has_one_queue_entry(self):
        from botend.services.player_raiderio_activity import player_identity
        row = self.activity()
        enqueue_enrichment(row, self.season, self.player)
        other = self.make_player('Shadow', name='Bound', rank=None)
        second = RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(other), payload_json=row.payload_json)
        enqueue_enrichment(second, self.season, other)
        self.assertEqual(CharacterEnrichmentTask.objects.count(), 1)

    def test_one_remaining_request_can_run_roster_but_never_partially_start_raid(self):
        row = self.activity(raid_first_kills={})
        enqueue_enrichment(row, self.season, self.player)
        http = Mock(return_value=self.response())
        result = consume_enrichment(self.season, EnrichmentBudget(http, max_requests=1))
        self.assertEqual((result['ready'], result['remaining_due']), (1, 1))
        self.assertEqual(CharacterEnrichmentTask.objects.get(kind='raid').state, 'pending')
        self.assertEqual(http.call_count, 1)

    def test_nonranking_identity_still_queues_rosters_without_enrolling_raid(self):
        self.player.rank = None
        self.player.save(update_fields=['rank'])
        row = self.activity()
        enqueue_enrichment(row, self.season, self.player)
        self.assertEqual(list(CharacterEnrichmentTask.objects.values_list('kind', flat=True)), ['roster'])

    def test_request_budget_leaves_remaining_work_for_new_instance(self):
        runs = payload()['mythic_plus_best_runs']
        second = {**runs[0], 'url': runs[0]['url'].replace('/123-', '/124-')}
        row = self.activity(runs + [second])
        enqueue_enrichment(row, self.season, self.player)
        http = Mock(side_effect=lambda url, **kwargs: self.response(details(kwargs['params']['id'])))
        first = consume_enrichment(self.season, EnrichmentBudget(http, max_requests=1))
        self.assertEqual((first['attempted'], first['remaining_due']), (1, 1))
        second = consume_enrichment(self.season, EnrichmentBudget(http, max_requests=1))
        self.assertEqual((second['attempted'], second['remaining_due']), (1, 0))
        self.assertEqual([c.kwargs['params']['id'] for c in http.call_args_list], [123, 124])

    def test_time_budget_stops_before_claiming_and_does_not_consume_progress(self):
        enqueue_enrichment(self.activity(), self.season, self.player)
        http = Mock()
        budget = EnrichmentBudget(http)
        budget.started -= 100
        result = consume_enrichment(self.season, budget)
        self.assertEqual(result['attempted'], 0)
        self.assertEqual(CharacterEnrichmentTask.objects.get().state, 'pending')
        http.assert_not_called()

    def test_failed_coordinate_keeps_source_backoff_and_only_it_is_retried(self):
        enqueue_enrichment(self.activity(), self.season, self.player)
        http = Mock(return_value=self.response(status=503))
        first = consume_enrichment(self.season, EnrichmentBudget(http))
        self.assertEqual(first['error'], 1)
        task = CharacterEnrichmentTask.objects.get()
        self.assertEqual(task.due_at, RaiderIORunRoster.objects.get().next_attempt_at)
        enqueue_enrichment(RaiderIOCharacterActivity.objects.get(), self.season, self.player)
        self.assertEqual(consume_enrichment(self.season, EnrichmentBudget(http))['attempted'], 0)
        http.return_value = self.response()
        with patch('django.utils.timezone.now', return_value=task.due_at + timedelta(seconds=1)):
            self.assertEqual(consume_enrichment(self.season, EnrichmentBudget(http))['ready'], 1)
        self.assertEqual(http.call_count, 2)

    def test_live_lease_and_expired_owner_cannot_publish_queue_progress(self):
        enqueue_enrichment(self.activity(), self.season, self.player)
        first = _claim(self.season)
        self.assertIsNone(_claim(self.season))
        with patch('django.utils.timezone.now', return_value=first.lease_expires_at + timedelta(seconds=1)):
            second = _claim(self.season)
        _finish(first, 'waiting', timezone.now() + timedelta(days=7))
        self.assertEqual(CharacterEnrichmentTask.objects.get().lease_owner, second.lease_owner)

    def test_new_payload_during_execution_remains_pending(self):
        row = self.activity()
        enqueue_enrichment(row, self.season, self.player)
        first = _claim(self.season)
        row.payload_json['mythic_plus_best_runs'][0]['mythic_level'] += 1
        enqueue_enrichment(row, self.season, self.player)
        _finish(first, 'waiting', timezone.now() + timedelta(days=7))
        task = CharacterEnrichmentTask.objects.get()
        self.assertEqual(task.state, 'pending')
        self.assertEqual(task.lease_owner, '')
        self.assertLessEqual(task.due_at, timezone.now())

    def test_expired_candidate_is_not_scanned_until_requested_again(self):
        row = self.activity()
        enqueue_enrichment(row, self.season, self.player)
        CharacterEnrichmentTask.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
        self.assertIsNone(_claim(self.season))
        consume_enrichment(self.season, EnrichmentBudget(Mock()))
        self.assertEqual(CharacterEnrichmentTask.objects.get().state, 'expired')
        enqueue_enrichment(row, self.season, self.player)
        self.assertIsNotNone(_claim(self.season))

    def test_rate_limit_is_shared_with_daily_transport_and_survives_new_batch(self):
        enqueue_enrichment(self.activity(), self.season, self.player)
        http = Mock(return_value=self.response(status=429))
        consume_enrichment(self.season, EnrichmentBudget(http))
        self.assertGreater(source_backoff(), timezone.now())
        self.assertEqual(consume_enrichment(self.season, EnrichmentBudget(http))['stop'], 'rate_limited')
        response = guarded_get(http, 'https://raider.io/api/v1/characters/profile')
        self.assertEqual(response.status_code, 429)
        self.assertEqual(http.call_count, 1)
        http.return_value = self.response()
        with patch('django.utils.timezone.now', return_value=source_backoff() + timedelta(seconds=1)):
            self.assertEqual(consume_enrichment(self.season, EnrichmentBudget(http))['ready'], 1)

    def test_inactive_season_never_fetches(self):
        enqueue_enrichment(self.activity(), self.season, self.player)
        self.season.is_active = False
        self.season.save(update_fields=['is_active'])
        http = Mock()
        consume_enrichment(self.season, EnrichmentBudget(http))
        http.assert_not_called()

    def test_backlog_shortens_schedule_but_source_cooldown_has_priority(self):
        from types import SimpleNamespace
        from botend.plugin_sync import monitor_task_due_at
        now = timezone.now()
        task = SimpleNamespace(name='PlayerRaiderIOActivityMonitor', wait_time=600,
            last_scan_time=now, flag=json.dumps({'enrichment': {'remaining_due': 5}}))
        self.assertIsNone(monitor_task_due_at(task, now=now + timedelta(seconds=59)))
        self.assertEqual(monitor_task_due_at(task, now=now + timedelta(seconds=60)), now + timedelta(seconds=60))
        task.flag = json.dumps({'enrichment': {'remaining_due': 5},
                               'rate_limited_until': (now + timedelta(hours=1)).isoformat()})
        self.assertIsNone(monitor_task_due_at(task, now=now + timedelta(minutes=20)))
        task.flag = '{}'
        self.assertIsNone(monitor_task_due_at(task, now=now + timedelta(seconds=60)))
        self.assertIsNotNone(monitor_task_due_at(task, now=now + timedelta(minutes=10)))

    def test_small_time_limit_is_not_overridden_by_queue_phase_limit(self):
        budget = EnrichmentBudget(max_seconds=1)
        budget.started -= 2
        self.assertFalse(budget.can_start(seconds=45))
