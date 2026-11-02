"""TEST SYNTHESIS: cached privacy facts through public sources and the real gate.

No production observations are replayed here. Only the HTTP boundary is mocked;
canonical discovery, domain sync, evidence construction and admission stay real.
"""
import copy
from datetime import timedelta
from unittest.mock import patch

from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services.player_analysis_recheck import Blocked, require_sources
from botend.services.player_observation import build_evidence
from botend.services.player_observation_sources import refresh_observation_sources
from botend.tests import test_player_observation_sources as fixtures
from botend.tests.test_player_raiderio_activity import payload


class UnavailableExecutionDomainTests(TestCase):
    def setUp(self):
        fixtures.ObservationSourcesTests.setUp(self)
        now = timezone.now()
        runs = []
        for offset, rid in enumerate((123, 124, 125)):
            run = copy.deepcopy(self.run)
            run.update(url=run['url'].replace('/123-', f'/{rid}-'),
                       completed_at=(now - timedelta(hours=4-offset)).isoformat(),
                       num_keystone_upgrades=1)
            runs.append(run)
        self.runs = runs
        facts = payload('Member4')
        facts.update(mythic_plus_best_runs=runs, mythic_plus_recent_runs=runs)
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(
            payload_json=facts, status='ready', last_error='', fetched_at=now,
            last_attempt_at=now, next_attempt_at=now+timedelta(hours=24),
            module_fetched_at={field: now.isoformat() for field in (
                'mythic_plus_best_runs', 'mythic_plus_recent_runs',
                'mythic_plus_dungeon_run_counts', 'raid_progression')})
        for rid in (123, 124, 125):
            RaiderIORunRoster.objects.update_or_create(
                rio_season=self.season.rio_season, run_id=rid,
                defaults={'status': 'unavailable', 'roster_visible': False,
                          'payload_json': {}, 'last_error': '', 'failure_count': 0,
                          'lease_token': '', 'fetched_at': now, 'last_attempt_at': now,
                          'next_attempt_at': now+timedelta(hours=24)})

    def collect_without_http_or_writes(self):
        # Full concrete rows, including clocks, leases, failures and hidden payload.
        before = list(RaiderIORunRoster.objects.order_by('pk').values())
        activity_before = list(RaiderIOCharacterActivity.objects.order_by('pk').values())
        with patch('requests.Session.get', side_effect=AssertionError('unexpected HTTP')) as http:
            result = refresh_observation_sources(
                self.player, progressive=True, force=False,
                max_runs=18, max_characters=40,
                max_basic_roster_requests=5, max_followup_requests=2)
        http.assert_not_called()
        self.assertEqual(result['api_requests'], 0)
        self.assertEqual(result['basic_api_requests'], 0)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(list(RaiderIORunRoster.objects.order_by('pk').values()), before)
        self.assertEqual(list(RaiderIOCharacterActivity.objects.order_by('pk').values()), activity_before)
        return result

    def test_confirmed_unavailable_negative_cache_is_not_failure_backoff(self):
        result = self.collect_without_http_or_writes()
        self.assertEqual(result['activity']['status'], 'fresh')
        self.assertEqual({s['run_id']: s['status'] for s in result['runs']},
                         {123: 'unavailable', 124: 'unavailable', 125: 'unavailable'})
        # Real structural questions reached batch.roster, not the not_needed path.
        self.assertTrue(all(s['question']['kind'] == 'roster' for s in result['runs']))
        self.assertEqual(result['rosters_visible'], 0)
        self.assertEqual(result['characters'], [])
        self.assertEqual(result['character_admissions'], {})
        evidence = build_evidence(self.player)
        self.assertIsNotNone(evidence)
        self.assertFalse(evidence['source_failed'])
        self.assertEqual(evidence['source_stamps']['rosters'], [])
        self.assertEqual(evidence['metrics']['timed_roster_runs'], 0)
        require_sources(result)

    def test_real_errors_and_active_fetching_still_block_hidden_rosters(self):
        for status, error, expected in (
                ('unavailable', 'transport_error', 'backoff'),
                ('error', '', 'backoff'),
                ('fetching', '', 'busy')):
            with self.subTest(status=status, error=error):
                RaiderIORunRoster.objects.update(
                    status=status, last_error=error,
                    lease_token='active-fetch-lease' if status == 'fetching' else '')
                result = self.collect_without_http_or_writes()
                self.assertEqual({s['status'] for s in result['runs']}, {expected})
                with self.assertRaises(Blocked):
                    require_sources(result)

    def test_ready_future_ttl_and_invalid_payload_keep_their_contracts(self):
        # A single visible reference has no structural follow-up authorization.
        self.activity.refresh_from_db()
        facts = self.activity.payload_json
        facts.update(mythic_plus_best_runs=self.runs[:1], mythic_plus_recent_runs=self.runs[:1])
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(payload_json=facts)
        valid = fixtures.roster()
        valid.update(completed_at=self.runs[0]['completed_at'], num_chests=1)
        for details, expected in ((valid, 'fresh'), ({}, 'invalid_payload')):
            with self.subTest(expected=expected):
                # Invalid facts must be in an authorized roster question, rather
                # than an intentionally unrequested single-run not_needed gap.
                facts.update(mythic_plus_best_runs=self.runs if expected == 'invalid_payload' else self.runs[:1],
                             mythic_plus_recent_runs=self.runs if expected == 'invalid_payload' else self.runs[:1])
                RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(payload_json=facts)
                RaiderIORunRoster.objects.update(
                    status='ready', roster_visible=True, payload_json=details)
                result = self.collect_without_http_or_writes()
                self.assertEqual(result['runs'][0]['status'], expected)
                if expected == 'fresh':
                    require_sources(result)
                else:
                    with self.assertRaises(Blocked):
                        require_sources(result)
