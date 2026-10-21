"""Question-directed collection through real ORM and the HTTP transport boundary."""
import copy
from datetime import timedelta
from unittest.mock import patch

import requests
from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity, PlayerObservationAnalysis
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_analysis_recheck import Blocked, require_sources
from botend.services.player_observation_sources import _characters
from botend.tests import test_progressive_observation_sources as progressive_fixtures
from botend.tests.test_player_observation_sources import scored


class TriggeredSourcesTests(TestCase):
    setUp = progressive_fixtures.ProgressiveSourcesTests.setUp
    def transport(self, url, **kwargs):
        response = progressive_fixtures.ProgressiveSourcesTests.transport(self, url, **kwargs)
        if url.endswith('/mythic-plus-scored-runs'):
            response.json.return_value['dungeons'][0]['num_chests'] = 1
        if url.endswith('/mythic-plus-runs'):
            scope=kwargs['params']
            data={'ui':dict(scope),'runs':[{'score':100,'summary':dict(season=scope['season'],
                dungeon={'id':scope['dungeonId']},keystone_run_id=321,mythic_level=10,
                num_chests=1,completed_at=self.run['completed_at'])}]}
            if self.bad_scored:
                data['ui']['characterId']+=1
            response.json.return_value=data
        return response

    refresh = progressive_fixtures.ProgressiveSourcesTests.refresh

    def contract(self, *, triggered=True, count=4):
        chars, _ = _characters([self.row.payload_json], self.activity.identity_digest)
        self.chars = chars
        questions = [{'id': f'scores_{c["character_id"]}', 'kind': 'scored_history', 'dungeon_id':self.row.payload_json['dungeon']['id'],
                      'purpose':'verify_guest_gain', 'expected_change':'核验第五人的可见提升',
                      'core_members':[activity_identity_digest(**x['identity']) for x in chars if x!=c],
                      'prior_run_ids':{activity_identity_digest(**x['identity']):999 for x in chars if x!=c},
                      'identity_digest': activity_identity_digest(**c['identity']),
                      'run_ids': [123], 'hypothesis_ids': ['key_access'],
                      'priority': count-i, 'reason': 'compare_core_and_guest'}
                     for i, c in enumerate(chars[:count])]
        return {'evidence_hash': 'fixture-facts', 'metrics': {
            'team_pattern': {'kind': 'fixed_five'}, 'progression': {'status': 'supported'},
            'investigation': {'version': 1, 'triggered': triggered, 'seed_run_ids': [123],
                             'core_members': [q['identity_digest'] for q in questions],
                             'hypotheses': [{'id': 'key_access', 'status': 'unknown'}],
                             'evidence': [], 'questions': questions,
                             'stop_reason': 'questions_pending'}}}

    def collect(self, contract, **kwargs):
        with patch('botend.services.player_observation.build_evidence', return_value=contract), \
                patch('botend.services.player_observation_sources._pace', return_value=True):
            return self.refresh(progressive=True, force=True, **kwargs)

    def test_explicit_questions_respect_two_request_budget(self):
        data = self.contract()
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual([c[1]['params']['characterId'] for c in self.calls],
                         [c['character_id'] for c in self.chars[:2]])
        self.assertEqual(result['followup_budget_reason'], 'budget_completed')
        trace = result['investigation']
        self.assertTrue(trace['bounded_complete'])
        self.assertEqual(trace['stop_reason'], 'budget_completed')
        self.assertEqual(len(trace['remaining_question_ids']), 4)
        self.assertEqual([q['request_status'] for q in trace['outcomes']],
                         ['ready', 'ready', 'not_requested', 'not_requested'])
        self.assertEqual(trace['hypotheses'][0]['status'], 'unknown')
        require_sources(result)

    def test_explicit_no_seed_never_falls_back_to_legacy_rotation(self):
        data = self.contract(triggered=False)
        data['metrics']['team_pattern'].update(kind='suspected_four')
        data['metrics']['progression']['status'] = 'insufficient'
        result = self.collect(data)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertFalse(result['followup_triggered'])

    def test_fresh_missing_dungeon_is_source_missing_not_pending_or_refetched(self):
        data = self.contract(count=1)
        char = self.chars[0]
        from botend.services.player_observation_history import scope,URL
        from urllib.parse import urlencode
        expected=scope(self.season.rio_season,char['character_id'],self.row.payload_json['dungeon']['id'])
        self.activity.payload_json['mythic_plus_scored_history']={str(expected['dungeonId']):{
            'ui':expected,'source_url':URL+'?'+urlencode(expected),'coverage':'observed_partial',
            'runs':[],'status':'ready','fetched_at':timezone.now().isoformat()}}
        self.activity.save()
        for _ in range(2):
            result=self.collect(data)
            self.assertEqual(result['followup_api_requests'],0)
            self.assertEqual(result['investigation']['outcomes'][0]['status'],'observed_partial')
            require_sources(result)
        self.assertEqual(self.calls,[])

    def test_unverified_or_unrelated_identity_never_becomes_a_request(self):
        data = self.contract(count=1)
        q = data['metrics']['investigation']['questions'][0]
        q['identity_digest'] = 'unverified'
        result = self.collect(data)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['investigation']['outcomes'][0]['status'], 'identity_unresolved')
        q['identity_digest'] = self.activity.identity_digest
        q['run_ids'] = [999999]
        result = self.collect(data)
        self.assertEqual(self.calls, [])
        self.assertEqual(result['investigation']['outcomes'][0]['status'], 'invalid_question')

    def test_new_reverse_gap_question_is_not_suppressed_by_resolved_progression(self):
        data = self.contract(count=1)
        data['metrics']['progression']['status'] = 'weakened'
        data['metrics']['investigation']['questions'][0]['reason'] = 'guest_higher_than_core'
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 1)
        self.assertEqual(result['investigation']['outcomes'][0]['reason'], 'guest_higher_than_core')

    def test_ai_priority_reorders_only_verified_pending_questions(self):
        data = self.contract(count=4)
        selected = data['metrics']['investigation']['questions'][-1]['id']
        with patch('botend.services.player_observation_sources._preferred_question_ids', return_value=[selected, 'unrelated']):
            result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual([c[1]['params']['characterId'] for c in self.calls],
                         [self.chars[3]['character_id'], self.chars[0]['character_id']])
        self.assertEqual(result['followup_preferred_question_ids'], [selected])
        require_sources(result)

    def test_repeated_input_reuses_successful_responses(self):
        data = self.contract(count=2)
        self.collect(data)
        self.calls.clear()
        result = self.collect(data)
        self.assertEqual(result['followup_api_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_failed_requests_never_become_budget_success_and_preserve_last_good(self):
        data = self.contract(count=3)
        old = scored(104)
        RaiderIOCharacterActivity.objects.filter(pk=self.activity.pk).update(
            scored_runs_json=old, scored_runs_fetched_at=timezone.now()-timedelta(days=3))
        for code in (429, 403, 500, 'timeout', 'invalid'):
            self.calls.clear()
            for cache in RaiderIOCharacterActivity.objects.all():
                cache.payload_json.pop('mythic_plus_scored_history',None)
                cache.save(update_fields=['payload_json'])
            self.http_status = code if isinstance(code, int) else 200
            self.bad_scored = code == 'invalid'
            original = self.transport
            def transport(url, **kwargs):
                response = original(url, **kwargs)
                if code == 'timeout':
                    raise requests.Timeout()
                return response
            with patch.object(self, 'transport', side_effect=transport):
                result = self.collect(data)
            self.assertEqual(len(self.calls), 1 if code == 429 else 2)
            self.assertFalse(result['investigation']['bounded_complete'])
            with self.assertRaises(Blocked):
                require_sources(result)
            self.activity.refresh_from_db()
            self.assertEqual(self.activity.scored_runs_json, old)

    def test_force_never_bypasses_question_identity_lease_or_backoff(self):
        data = self.contract(count=2)
        char = self.chars[1]
        for status in ('fetching', 'error'):
            RaiderIOCharacterActivity.objects.update_or_create(season=self.season, **char['identity'],
                defaults={'status': status, 'next_attempt_at': timezone.now()+timedelta(hours=1)})
            result = self.collect(data)
            self.assertFalse(result['investigation']['bounded_complete'])
            with self.assertRaises(Blocked):
                require_sources(result)
        self.assertTrue(all(c[1]['params']['characterId'] == 104 for c in self.calls))

    def test_unneeded_rosters_do_not_hide_actual_source_failure(self):
        data = self.contract(count=1)
        runs = [self.run]
        for rid in range(124, 131):
            run = copy.deepcopy(self.run)
            run['url'] = run['url'].replace('/123-', f'/{rid}-')
            runs.append(run)
        self.activity.payload_json = {'mythic_plus_best_runs': runs, 'mythic_plus_recent_runs': runs}
        self.activity.save()
        result = self.collect(data)
        self.assertEqual(result['basic_api_requests'], 0)
        self.assertEqual(sum(s['status'] == 'not_needed' for s in result['runs']), 7)
        self.assertTrue(result['investigation']['bounded_complete'])
        self.assertEqual(len(result['investigation']['remaining_run_ids']), 0)
        require_sources(result)
        for status in ('transport_error', 'backoff', 'busy', 'invalid_payload', 'http_403'):
            with self.subTest(status=status), self.assertRaises(Blocked):
                require_sources({**result, 'runs': [{'status': status}]})
        result['investigation']['triggered'] = False
        with self.assertRaises(Blocked):
            require_sources(result)

    def test_budget_marker_cannot_hide_source_failure(self):
        result = self.collect(self.contract())
        for changes in ({'source_failed': True}, {'characters': [{'status': 'transport_error'}]},
                        {'activity': {'status': 'partial'}}, {'runs': [{'status': 'invalid_payload'}]}):
            with self.subTest(changes=changes), self.assertRaises(Blocked):
                require_sources({**result, **changes})
