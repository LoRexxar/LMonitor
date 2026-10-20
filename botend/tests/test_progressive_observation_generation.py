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
from botend.tests import test_player_observation_investigation as inquiry_fixtures


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
                with patch('botend.services.player_observation.PlayerObservationGLMClient', side_effect=AssertionError('no client')):
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
        self.assertEqual(engine.send_message.call_args.kwargs['max_tokens'], 3600)
        self.assertIn('metrics.investigation', engine.system_prompt)
        self.assertTrue(json.loads(engine.send_message.call_args.args[0])['metrics']['investigation']['triggered'])
        self.assertFalse(cached['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        previous = copy.deepcopy(PlayerObservationAnalysis.objects.get().result_json)
        bad = Mock(send_message=Mock(return_value=json.dumps({'observations': [], 'conclusion_id': 'public_sample'})))
        self.assertEqual(generate_observation(self.player, force=True, engine=bad, skip_uninformative=True)['status'], 'missing_investigation')
        self.assertEqual(bad.send_message.call_count, 1)
        self.assertEqual(PlayerObservationAnalysis.objects.get().result_json, previous)

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

    def test_single_substitution_exposes_sorted_comparison_group_not_rotation(self):
        roster = RaiderIORunRoster.objects.get(run_id=125)
        raw = roster.payload_json
        raw['roster'][4]['character'].update(id=105, name='Member5', path='/characters/eu/test-realm/Member5')
        roster.payload_json = raw
        roster.save()
        data = build_evidence(self.player)
        structure = data['metrics']['behavior_review']['structure']
        self.assertEqual(data['metrics']['team_pattern']['kind'], 'fixed_five')
        self.assertIn('occasional_substitution', structure['codes'])
        self.assertNotIn('rotating_core', structure['codes'])
        expected = sorted(RaiderIOCharacterActivity.objects.filter(
            character_name__in=['member0', 'member1', 'member2', 'member3']
        ).values_list('identity_digest', flat=True))
        group = next(g for g in structure['comparison_groups'] if g['members'] == expected)
        self.assertEqual(len(group['members']), 4)
        self.assertEqual(group['members'], sorted(group['members']))
        self.assertTrue(all(isinstance(key, str) and len(key) == 64 for key in group['members']))
        self.assertEqual(set(group['run_ids']), {123, 124, 125})
        self.assertEqual(group['non_dominant_appearances'], 1)
        self.assertEqual(group['observed_config_switches'], 1)

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
        with patch('botend.services.player_observation.PlayerObservationGLMClient', side_effect=AssertionError('no client')):
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
        from botend.services.player_observation import _hash, _same_publication
        roster = RaiderIORunRoster.objects.get(run_id=125)
        raw = roster.payload_json
        raw['roster'][4]['character'].update(id=105, name='Member5', path='/characters/eu/test-realm/Member5')
        roster.payload_json = raw
        roster.save()
        generate_observation(self.player, engine=self.engine())
        row = PlayerObservationAnalysis.objects.get()
        previous_result = copy.deepcopy(row.result_json)
        previous_evidence = copy.deepcopy(row.evidence_json)
        # The live builder no longer emits v5 core_members. Exercise that
        # additive legacy compatibility explicitly instead of deleting a
        # nonexistent live field (or making deletion a vacuous no-op).
        enriched = copy.deepcopy(row.evidence_json)
        group = next(g for g in enriched['metrics']['behavior_review']['structure']['comparison_groups']
                     if len(g['members']) == 4 and set(g['run_ids']) == {123, 124, 125})
        enriched['metrics']['team_pattern']['core_members'] = group['members']
        enriched['evidence_hash'] = _hash({key: value for key, value in enriched.items()
                                         if key not in ('evidence_hash', 'source_failed')})
        self.assertNotEqual(row.evidence_hash, enriched['evidence_hash'])
        self.assertTrue(_same_publication(row, enriched))
        changed = copy.deepcopy(enriched)
        changed['metrics']['highest_timed_level'] = 30
        changed['evidence_hash'] = _hash({key: value for key, value in changed.items()
                                       if key not in ('evidence_hash', 'source_failed')})
        self.assertFalse(_same_publication(row, changed))
        # Actual newly collected but unchanged facts must also reuse the
        # persisted publication without rewriting the frozen evidence/result.
        RaiderIORunRoster.objects.filter(run_id=125).update(fetched_at=timezone.now())
        self.assertNotEqual(build_evidence(self.player)['evidence_hash'], row.evidence_hash)
        self.assertEqual(project_player_observation(self.player)['status'], 'ready')
        engine = self.engine()
        self.assertEqual(generate_observation(self.player, engine=engine, skip_uninformative=True)['status'], 'fresh')
        engine.send_message.assert_not_called()
        row.refresh_from_db()
        self.assertEqual(row.result_json, previous_result)
        self.assertEqual(row.evidence_json, previous_evidence)
        RaiderIORunRoster.objects.filter(run_id=124).update(roster_visible=False)
        self.assertEqual(project_player_observation(self.player)['status'], 'pending')


class ProgressiveCoreGenerationTests(TestCase):
    setUp = progression_fixtures.ObservationProgressionTests.setUp
    engine = inquiry_fixtures.InvestigationIntegrationTests.engine

    def test_lower_equal_and_missing_fifths_each_require_explanation(self):
        engine = self.engine()
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertTrue(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 1)
        for row in RaiderIOCharacterActivity.objects.filter(character_name__in=['member4', 'member5', 'member6']):
            row.scored_runs_json['dungeons'][0].update(mythic_level=26, score=550)
            row.save()
        equal = build_evidence(self.player)
        self.assertTrue(equal['metrics']['investigation']['triggered'])
        self.assertTrue(all(e['relative_comparisons']['lower_peers'] == []
                            for e in equal['metrics']['behavior_review']['resilience']['events']))
        self.assertEqual({e['id'] for e in equal['evidence'] if e['id'] in
                          {f'investigation:near:{rid}' for rid in range(200, 208)}},
                         {f'investigation:near:{rid}' for rid in range(200, 208)})
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 2)
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4', 'member5', 'member6']).update(scored_runs_fetched_at=None)
        missing = build_evidence(self.player)
        self.assertEqual(len(missing['metrics']['investigation']['questions']), 3)
        self.assertEqual({e['id'] for e in missing['evidence'] if e['id'].startswith('investigation:unknown:')},
                         {f'investigation:unknown:{rid}' for rid in range(200, 208)})
        self.assertNotIn('carrying', missing['metrics']['behavior_review']['supported'])
        result = generate_observation(self.player, engine=engine, skip_uninformative=True)
        self.assertEqual(result['status'], 'ready')
        self.assertTrue(result['ai_called'])
        self.assertEqual(engine.send_message.call_count, 3)
        self.assertTrue(project_player_observation(self.player)['has_investigation'])
