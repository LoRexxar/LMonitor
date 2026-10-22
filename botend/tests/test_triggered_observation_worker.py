"""Real investigation -> collection -> fenced worker integration, no live AI/network."""
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import PlayerObservationAnalysis, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services import player_analysis_recheck as recheck
from botend.services import player_observation as observation
from botend.services.player_observation_sources import refresh_observation_sources
from botend.services.player_raiderio_activity import MODULES
from botend.tests import test_player_observation_progression as fixtures
from botend.tests import test_player_observation_investigation as inquiry_fixtures
from botend.tests.test_player_raiderio_activity import payload


class TriggeredWorkerTests(TestCase):
    engine = inquiry_fixtures.InvestigationIntegrationTests.engine

    def setUp(self):
        fixtures.ObservationProgressionTests.setUp(self)
        self.activity.refresh_from_db()
        now = timezone.now()
        self.activity.next_attempt_at = now+timedelta(hours=24)
        self.activity.module_fetched_at = {key: now.isoformat() for key in MODULES.values()}
        self.activity.payload_json['raid_first_kills'] = {
            'version': 1, 'status': 'ready', 'visible': True, 'last_error': '',
            'identity_digest': self.activity.identity_digest, 'rio_season': self.season.rio_season,
            'fetched_at': now.isoformat(), 'next_attempt_at': (now+timedelta(hours=24)).isoformat(),
            'source': {'character_id': 100}, 'raids': []}
        self.activity.save()
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4', 'member5', 'member6']).update(scored_runs_fetched_at=None)
        self.calls = []
        self.http_status = 200

    def transport(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if url.endswith('/profile'):
            data = payload('Member0')
            data.update(mythic_plus_best_runs=self.runs, mythic_plus_recent_runs=self.runs)
            return Mock(status_code=200, headers={}, json=Mock(return_value=data))
        self.assertTrue(url.endswith('/mythic-plus-runs'), url)
        scope = kwargs['params']
        self.assertEqual(scope['season'], self.season.rio_season)
        self.assertEqual(scope['dungeonId'], 1)
        self.assertIn(scope['characterId'], (104, 105, 106))
        # Real history wire shape, not a current scored-runs snapshot. Member5
        # has a visible gain; Member4 already had a higher score before the run.
        # Member6 remains unqueried when the two-request budget is exhausted.
        rid = {104: 204, 105: 206, 106: 207}[scope['characterId']]
        run = next(run for run in self.runs if run['keystone_run_id'] == rid)
        summary = {'season': scope['season'], 'dungeon': {'id': scope['dungeonId']},
                   'keystone_run_id': rid, 'mythic_level': run['mythic_level'],
                   'num_chests': 1, 'completed_at': run['completed_at']}
        prior = dict(summary, keystone_run_id=2000 + scope['characterId'],
                     mythic_level=30 if scope['characterId'] == 104 else 18,
                     completed_at=(self.now-timedelta(days=2)).isoformat())
        data = {'ui': dict(scope), 'runs': [
            {'score': run['score'], 'summary': summary},
            {'score': 600 if scope['characterId'] == 104 else 400, 'summary': prior}]}
        return Mock(status_code=self.http_status, headers={}, json=Mock(return_value=data))

    def collect(self):
        with patch('requests.Session.get', side_effect=self.transport), \
                patch('botend.services.player_observation_sources._pace', return_value=True):
            return refresh_observation_sources(self.player, progressive=True, force=True)

    def test_real_builder_directs_two_missing_peripheral_members_and_rebuilds(self):
        before = observation.build_evidence(self.player)
        questions = before['metrics']['investigation']['questions']
        self.assertEqual(len(questions), 3)
        self.assertTrue(all(q['kind'] == 'scored_history' and q['purpose'] == 'verify_guest_gain'
                            for q in questions))
        previous_scores = dict(RaiderIOCharacterActivity.objects.values_list('pk', 'scored_runs_json'))
        result = self.collect()
        self.assertEqual(result['followup_api_requests'], 2)
        self.assertEqual({s['identity_digest'] for s in result['investigation']['requests']},
                         {q['identity_digest'] for q in questions[:2]})
        self.assertEqual(sum(s['api_requests'] for s in result['investigation']['requests']), 2)
        after = observation.build_evidence(self.player)
        self.assertEqual(result['investigation']['evidence_hash'], after['evidence_hash'])
        self.assertNotEqual(before['evidence_hash'], after['evidence_hash'])
        self.assertEqual(len(after['metrics']['investigation']['questions']), 1)
        self.assertEqual(after['metrics']['investigation']['questions'][0]['run_ids'], [207])
        self.assertEqual(dict(RaiderIOCharacterActivity.objects.values_list('pk', 'scored_runs_json')),
                         previous_scores)  # history is never a replacement current score
        for request in result['investigation']['requests']:
            history = RaiderIOCharacterActivity.objects.get(
                identity_digest=request['identity_digest']).payload_json['mythic_plus_scored_history']['1']
            self.assertEqual(history['status'], 'ready')
            self.assertEqual(history['coverage'], 'observed_partial')
            self.assertEqual(len(history['runs']), 2)
        self.assertEqual(result['followup_budget_reason'], 'budget_completed')
        recheck.require_sources(result)
        engine = self.engine()
        generated = observation.generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(generated['status'], 'ready')
        self.assertTrue(generated['ai_called'])
        sent = json.loads(engine.send_message.call_args.args[0])
        self.assertEqual(len(sent['metrics']['investigation']['questions']), 1)
        self.assertNotIn('carrying', sent['metrics']['behavior_review']['supported'])
        self.assertTrue(any(':unknown:' in e['id'] for e in sent['evidence']))
        saved = PlayerObservationAnalysis.objects.get(activity=self.activity)
        self.assertEqual(len(saved.result_json['investigation']['next_question_ids']), 1)

    def test_real_fixed_five_seed_not_suppressed_and_zero_seed_stays_readonly(self):
        for row in RaiderIORunRoster.objects.filter(run_id__gte=200):
            row.payload_json['roster'][4]['character'].update(id=104, name='Member4', path='/characters/eu/test-realm/Member4')
            row.save()
        RaiderIOCharacterActivity.objects.exclude(pk=self.activity.pk).update(scored_runs_fetched_at=None)
        before = observation.build_evidence(self.player)
        self.assertEqual(before['metrics']['team_pattern']['kind'], 'fixed_five')
        self.assertTrue(before['metrics']['investigation']['triggered'])
        # Fixed five is still a lead, but it has no rotating guest-gain question.
        # Missing teammate snapshots alone must not spend the follow-up budget.
        self.assertEqual(before['metrics']['investigation']['questions'], [])
        fixed = self.collect()
        self.assertEqual(fixed['followup_api_requests'], 0)
        self.assertTrue(fixed['followup_triggered'])  # audit retains the investigation trigger
        self.assertEqual(fixed['followup_budget_reason'], 'completed')
        self.assertEqual(fixed['investigation']['requests'], [])
        self.assertEqual(self.calls, [])
        recheck.require_sources(fixed)
        # Two repeated lower runs are now a legitimate behavior lead,
        # independent of the older three-run inquiry threshold.
        self.activity.payload_json['mythic_plus_recent_runs'] = self.runs[:2]
        self.activity.save(update_fields=['payload_json'])
        two = observation.build_evidence(self.player)
        self.assertTrue(two['metrics']['behavior_review']['triggered'])
        self.assertEqual(two['metrics']['investigation']['seed_run_ids'], [200, 201])
        self.assertTrue(two['metrics']['investigation']['triggered'])
        self.assertEqual(two['metrics']['investigation']['questions'], [])
        # Remove all recent activity for the zero-seed/read-only case; retain
        # best records and the exact fixed-five roster facts as background.
        self.activity.payload_json['mythic_plus_recent_runs'] = []
        self.activity.save(update_fields=['payload_json'])
        no_seed = observation.build_evidence(self.player)
        self.assertFalse(no_seed['metrics']['behavior_review']['triggered'])
        self.assertFalse(no_seed['metrics'].get('investigation', {}).get('triggered'))
        self.calls.clear()
        self.assertEqual(self.collect()['followup_api_requests'], 0)
        self.assertEqual(self.calls, [])

    def worker(self):
        row = PlayerObservationAnalysis.objects.create(player=self.player, activity=self.activity,
            manual_json={'conclusion': 'retain'}, result_json={'last': 'good'},
            is_published=False, review_status='rejected')
        token = recheck.enqueue(row.pk, {'revision': row.revision})['token']
        engine = self.engine()
        with patch('requests.Session.get', side_effect=self.transport), \
                patch('botend.services.player_observation_sources._pace', return_value=True), \
                patch('botend.services.player_observation.PlayerObservationGLMClient', return_value=engine):
            result = recheck.run(row.pk, token)
        row.refresh_from_db()
        return row, token, engine, result

    def test_real_worker_finishes_with_remaining_unknown_and_persists_literal_budget(self):
        row, token, engine, result = self.worker()
        self.assertEqual(result['state'], 'succeeded', result)
        self.assertIn('仍有待验证假设', result['message'])
        self.assertEqual(len(self.calls), 3)  # one profile + two targeted history HTTPs
        self.assertTrue(self.calls[0][0].endswith('/profile'))
        self.assertTrue(all(url.endswith('/mythic-plus-runs') for url, _ in self.calls[1:]))
        self.assertEqual(result['collection']['basic_api_requests'], 1)
        self.assertEqual(result['collection']['followup_api_requests'], 2)
        self.assertEqual(result['collection']['followup_api_budget'], 2)
        self.assertEqual(row.recheck_json['sources']['investigation'], row.recheck_json['investigation'])
        trace = row.recheck_json['investigation']
        # Audit retains partial source answers as unknown, even when the active
        # planner no longer needs to query those guests. Only Member6 is pending.
        self.assertEqual({tuple(q['run_ids']): (q['status'], q['request_status'])
                          for q in trace['outcomes']}, {
            (206,): ('observed_partial', 'ready'),
            (204,): ('observed_partial', 'ready'),
            (207,): ('budget_pending', 'not_requested'),
        })
        self.assertCountEqual(trace['remaining_question_ids'], [q['id'] for q in trace['outcomes']])
        self.assertEqual([q['run_ids'] for q in trace['questions']], [[207]])
        self.assertEqual(trace['stop_reason'], 'budget_completed')
        self.assertTrue(trace['bounded_complete'])
        self.assertEqual(row.manual_json, {'conclusion': 'retain'})
        self.assertEqual(row.review_status, 'rejected')
        self.assertFalse(row.is_published)
        self.assertEqual(row.evidence_json, observation.build_evidence(self.player))
        self.assertEqual(engine.send_message.call_count, 1)
        with patch('requests.Session.get', side_effect=AssertionError('GET must be read only')), \
                patch('botend.services.player_observation.PlayerObservationGLMClient', side_effect=AssertionError('GET must not generate')):
            self.assertEqual(recheck.get_state(row.pk)['collection'], result['collection'])
        with self.assertRaises(recheck.Blocked):
            recheck.record_sources(row.pk, 'different-token', {'status': 'ready'})
        row.refresh_from_db()
        self.assertEqual(row.recheck_json['token'], token)

    def test_real_worker_429_preserves_previous_result_and_audits_failed_request(self):
        self.http_status = 429
        row, _, engine, result = self.worker()
        self.assertEqual(result['state'], 'blocked', result)
        self.assertEqual(row.result_json, {'last': 'good'})
        self.assertEqual(row.recheck_json['collection']['followup_api_requests'], 1)
        self.assertEqual(row.recheck_json['investigation']['stop_reason'], 'rate_limited')
        self.assertEqual(len(self.calls), 2)  # profile then the first history 429; no second request
        requests = row.recheck_json['investigation']['requests']
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]['status'], 'rate_limited')
        self.assertEqual(requests[0]['api_requests'], 1)
        self.assertEqual(row.manual_json, {'conclusion': 'retain'})
        self.assertEqual(row.review_status, 'rejected')
        self.assertFalse(row.is_published)
        engine.send_message.assert_not_called()
