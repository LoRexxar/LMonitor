"""Progressive generation uses the public display authority, not trivial labels."""
import copy
import io
import json
import tempfile
from datetime import timedelta
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from botend.models import PlayerObservationAnalysis, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.services.player_observation import build_evidence, generate_observation, project_player_observation
from botend.tests import test_player_observation as fixtures
from botend.tests import test_player_observation_progression as progression_fixtures


class ProgressiveObservationGenerationTests(TestCase):
    setUp = fixtures.PlayerObservationTests.setUp
    engine = fixtures.PlayerObservationTests.engine

    def trivial(self, empty=False):
        RaiderIORunRoster.objects.all().delete()
        if empty:
            self.activity.payload_json = {}
            self.activity.save()

    def test_trivial_and_empty_publish_locally_without_client_or_ai(self):
        for empty in (False, True):
            with self.subTest(empty=empty):
                PlayerObservationAnalysis.objects.all().delete()
                self.trivial(empty)
                engine = self.engine()
                with patch('core.glm.GLMClient', side_effect=AssertionError('no client')):
                    result = generate_observation(self.player, engine=engine, skip_uninformative=True)
                self.assertEqual(result['status'], 'ready')
                self.assertFalse(result['ai_called'])
                self.assertEqual(result['skip_reason'], 'no_supported_judgement')
                engine.send_message.assert_not_called()
                row = PlayerObservationAnalysis.objects.get()
                self.assertEqual(row.status, 'ready')
                self.assertEqual(row.result_json['observations'], [])
                self.assertEqual(row.result_json['metadata']['source_kind'], 'local_deterministic')
                self.assertNotIn('response', row.result_json['metadata'])
                view = project_player_observation(self.player)
                self.assertEqual(view['status'], 'ready')
                self.assertFalse(view['has_supported_judgements'])
                self.assertEqual(view['observations'], [])
                self.assertEqual(view['model_label'], '')
                cached = generate_observation(self.player, engine=engine, skip_uninformative=True)
                self.assertEqual(cached['status'], 'fresh')
                self.assertFalse(cached['ai_called'])

    def test_supported_calls_ai_once_and_real_empty_response_remains_invalid(self):
        engine = self.engine()
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        cached = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertFalse(cached['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        bad = Mock(send_message=Mock(return_value=json.dumps({'observations': [], 'conclusion_id': 'public_sample'})))
        self.assertEqual(generate_observation(self.player, force=True, engine=bad, skip_uninformative=True)['status'], 'invalid_schema')
        self.assertEqual(bad.send_message.call_count, 1)

    def test_local_commit_rechecks_hash_and_preserves_previous_good(self):
        generate_observation(self.player, engine=self.engine())
        old = copy.deepcopy(PlayerObservationAnalysis.objects.get().result_json)
        self.trivial()
        evidence = build_evidence(self.player)
        changed = copy.deepcopy(evidence)
        changed['evidence_hash'] = 'changed'
        with patch('botend.services.player_observation.build_evidence', side_effect=[evidence, changed]):
            result = generate_observation(self.player, force=True, skip_uninformative=True)
        self.assertEqual(result['status'], 'evidence_changed')
        self.assertEqual(PlayerObservationAnalysis.objects.get().result_json, old)
        self.assertEqual(generate_observation(self.player, force=True, skip_uninformative=True)['status'], 'backoff')
        PlayerObservationAnalysis.objects.update(lease_token='other', lease_expires_at=timezone.now()+timedelta(minutes=1))
        self.assertEqual(generate_observation(self.player, force=True, skip_uninformative=True)['status'], 'busy')

    def test_invalid_context_is_unavailable_and_zero_ai(self):
        RaiderIOCharacterActivity.objects.all().delete()
        engine = self.engine()
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'unavailable')
        self.assertFalse(result['ai_called'])
        engine.send_message.assert_not_called()
        self.assertFalse(PlayerObservationAnalysis.objects.exists())

    def test_qualified_core_exposes_sorted_identity_references(self):
        roster = RaiderIORunRoster.objects.get(run_id=125)
        raw = roster.payload_json
        raw['roster'][4]['character'].update(id=105, name='Member5', path='/characters/eu/test-realm/Member5')
        roster.payload_json = raw
        roster.save()
        pattern = build_evidence(self.player)['metrics']['team_pattern']
        self.assertEqual(pattern['kind'], 'rotating_core')
        self.assertEqual(len(pattern['core_members']), 4)
        self.assertEqual(pattern['core_members'], sorted(pattern['core_members']))
        self.assertTrue(all(isinstance(key, str) and len(key) == 64 for key in pattern['core_members']))

    def test_cli_refresh_selects_progressive_budgets_and_local_skip(self):
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
                patch('botend.services.player_observation_sources.refresh_observation_sources', return_value={'status': 'ready', 'basic_api_requests': 1, 'followup_api_requests': 0}) as sources, \
                patch('botend.services.player_observation.generate_observation', return_value={'status': 'ready', 'ai_called': False}) as generate:
            output = io.StringIO()
            call_command('generate_player_observation', player_id=self.player.pk, apply=True, refresh_sources=True, stdout=output)
        self.assertTrue(sources.call_args.kwargs['progressive'])
        self.assertEqual(sources.call_args.kwargs['max_followup_requests'], 2)
        self.assertEqual(sources.call_args.kwargs['max_basic_roster_requests'], 5)
        self.assertTrue(generate.call_args.kwargs['skip_uninformative'])
        self.assertIn('basic_api_requests', output.getvalue())

    def test_local_path_does_not_construct_client_and_source_change_retracts(self):
        self.trivial()
        with patch('core.glm.GLMClient', side_effect=AssertionError('no client')):
            self.assertEqual(generate_observation(self.player, skip_uninformative=True)['status'], 'ready')
        self.activity.payload_json = {}
        self.activity.save()
        self.assertEqual(project_player_observation(self.player)['status'], 'pending')

    def test_superseded_lease_preserves_good_and_counts_attempted_ai(self):
        generate_observation(self.player, engine=self.engine())
        old = copy.deepcopy(PlayerObservationAnalysis.objects.get().result_json)
        def supersede(result):
            PlayerObservationAnalysis.objects.update(lease_token='new-owner')
        engine = self.engine(supersede)
        result = generate_observation(self.player, engine=engine, force=True, skip_uninformative=True)
        self.assertEqual(result['status'], 'superseded')
        self.assertTrue(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        self.assertEqual(PlayerObservationAnalysis.objects.get().result_json, old)

    def test_legacy_core_reference_addition_preserves_good_without_recall(self):
        from botend.services.player_observation import _hash
        roster = RaiderIORunRoster.objects.get(run_id=125)
        raw = roster.payload_json
        raw['roster'][4]['character'].update(id=105, name='Member5', path='/characters/eu/test-realm/Member5')
        roster.payload_json = raw
        roster.save()
        generate_observation(self.player, engine=self.engine())
        row = PlayerObservationAnalysis.objects.get()
        previous = row.evidence_json
        previous['metrics']['team_pattern'].pop('core_members')
        previous['evidence_hash'] = _hash({key: value for key, value in previous.items() if key not in ('evidence_hash', 'source_failed')})
        PlayerObservationAnalysis.objects.filter(pk=row.pk).update(evidence_json=previous, evidence_hash=previous['evidence_hash'])
        self.assertEqual(project_player_observation(self.player)['status'], 'ready')
        engine = self.engine()
        self.assertEqual(generate_observation(self.player, engine=engine, skip_uninformative=True)['status'], 'fresh')
        engine.send_message.assert_not_called()
        RaiderIORunRoster.objects.filter(run_id=124).update(roster_visible=False)
        self.assertEqual(project_player_observation(self.player)['status'], 'pending')


class ProgressiveCoreGenerationTests(TestCase):
    setUp = progression_fixtures.ObservationProgressionTests.setUp
    engine = fixtures.PlayerObservationTests.engine

    def test_supported_four_core_calls_once_then_weakened_and_missing_skip(self):
        engine = self.engine()
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertTrue(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        for row in RaiderIOCharacterActivity.objects.filter(character_name__in=['member4', 'member5', 'member6']):
            row.scored_runs_json['dungeons'][0].update(mythic_level=26, score=550)
            row.save()
        self.assertEqual(build_evidence(self.player)['metrics']['progression']['status'], 'weakened')
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4', 'member5', 'member6']).update(scored_runs_fetched_at=None)
        self.assertEqual(build_evidence(self.player)['metrics']['progression']['status'], 'insufficient')
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['ai_called'])
        self.assertFalse(project_player_observation(self.player)['has_supported_judgements'])
