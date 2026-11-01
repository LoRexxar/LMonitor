"""Source-level ORM proof for the synthetic five/six-event boundary.

These are invented test inputs, not six captured online runs. Only transport,
clock/pacing and the paid-model boundary are injected; evidence remains real.
"""
import copy
import json
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from django.test import TestCase

from botend.models import PlayerObservationAnalysis, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.raiderio_identity import activity_identity_digest
from botend.services import player_observation as service
from botend.services.player_observation_badges import attach_badge_snapshots, public_badge_players
from botend.services.player_observation_history import validate_history, scope
from botend.services.player_observation_sources import validate_scored_runs
from botend.services.raiderio_run_roster import validate_details
from botend.tests.player_carrying_pipeline_fixtures import (
    CarryingPipelineFixture, SEASON, synthetic_model_response,
)


class CarryingSourcePipelineTests(TestCase):
    transport = CarryingPipelineFixture.transport
    refresh = CarryingPipelineFixture.refresh

    def collect(self, count=6, **kwargs):
        CarryingPipelineFixture.setUp(self, count, **kwargs)
        self.refresh()
        return service.build_evidence(self.player)

    def publish(self):
        # Selecting a label is not evidence: the real current-version validator
        # must accept this deterministic test model's choices before publication.
        engine = Mock(send_message=Mock(side_effect=synthetic_model_response))
        result = service.generate_observation(self.player, engine=engine)
        self.assertEqual(result['status'], 'ready', result)
        engine.send_message.assert_called_once()
        return PlayerObservationAnalysis.objects.get(activity=self.activity)

    def badges(self):
        players = [dict(id=self.player.pk, rank=1, region=self.player.region,
            realm=self.player.realm, character_name=self.player.character_name,
            profile_url=self.player.profile_url)]
        attach_badge_snapshots(players)
        return public_badge_players(players, self.player.class_name, self.player.spec_name)[0].get('observation_badges', [])

    def test_six_independent_events_cross_all_source_validators_and_publish_same_strong_tag(self):
        evidence = self.collect()
        expected = set(range(920001, 920007))
        review = evidence['metrics']['behavior_review']
        grade = review['evidence_strengths']['carrying']
        self.assertEqual((grade['level'], grade['confidence'], grade['verified_runs']), ('strong', '高', 6))
        self.assertEqual(grade['distinct_beneficiaries'], 2)
        self.assertEqual(set(review['witnesses']['carrying']), expected)
        self.assertIn('carrying', review['supported'])
        self.assertEqual(evidence['prompt_version'], 10)
        self.assertEqual(evidence['coverage']['roster_runs'], 6)
        self.assertEqual(evidence['coverage']['score_characters'], 6)
        self.assertEqual({ref['run_id'] for ref in evidence['source_stamps']['rosters']}, expected)
        self.assertEqual(len(evidence['source_stamps']['scores']), 6)
        self.assertEqual(len(evidence['source_stamps']['histories']), 2)
        # The complete APIs were really requested, not raw derived facts written
        # to a mock builder. Inspect outbound identity and response scope too.
        for suffix, count in (('/characters/profile', 1), ('/run-details', 6),
                              ('/mythic-plus-scored-runs', 6), ('/mythic-plus-runs', 2)):
            self.assertEqual(sum(url.endswith(suffix) for url, _ in self.calls), count, suffix)
        source_rows = {row.identity_digest: row for row in RaiderIOCharacterActivity.objects.all()}
        known_ids = {c['id'] for c in self.data['characters']}
        for raw in self.data['rosters'].values():
            run = next(r for r in self.data['profile']['mythic_plus_recent_runs']
                if r['url'].endswith('/' + str(raw['keystone_run_id'])))
            clean = validate_details(raw, run, SEASON)
            self.assertEqual(clean, RaiderIORunRoster.objects.get(run_id=raw['keystone_run_id']).payload_json)
            for member in clean['roster']:
                char = member['character']
                self.assertIn(char['id'], known_ids)
                digest = activity_identity_digest(region=char['region']['slug'],
                    realm_slug=char['realm']['slug'], character_name=char['name'])
                row = source_rows[digest]
                self.assertEqual(validate_scored_runs(row.scored_runs_json, SEASON, char['id']), row.scored_runs_json)
        for ref in evidence['source_stamps']['histories']:
            query = parse_qs(urlsplit(ref['source_url']).query)
            cid = int(query['characterId'][0])
            self.assertEqual(query['season'], [SEASON])
            self.assertEqual(query['dungeonId'], ['1'])
            row = RaiderIOCharacterActivity.objects.get(pk=ref['activity_id'])
            entry = row.payload_json['mythic_plus_scored_history']['1']
            clean = validate_history(self.data['histories'][cid], SEASON, cid, 1)
            self.assertEqual(entry['runs'], clean['runs'])
            self.assertEqual(entry['ui'], scope(SEASON, cid, 1))
            self.assertEqual(ref['source_url'], clean['source_url'])
        model_input = service._generation_input(evidence)
        self.assertEqual(model_input['metrics']['investigation']['response_contract']['schema_version'], 9)
        raw = synthetic_model_response(json.dumps(model_input))
        self.assertLess(len(raw), 16000)
        service._validate_publication(raw, evidence, publication_version=10)
        row = self.publish()
        self.assertEqual(row.prompt_version, 10)
        self.assertEqual(row.evidence_hash, evidence['evidence_hash'])
        self.assertEqual(row.result_json['observations'][0]['code'], 'carrying')
        frozen = copy.deepcopy((row.evidence_hash, row.evidence_json, row.result_json, row.generated_at))
        with patch('requests.sessions.Session.request', side_effect=AssertionError('read must not fetch')), \
                patch.object(service, 'PlayerObservationGLMClient', side_effect=AssertionError('read must not call AI')):
            detail = service.project_player_observation(self.player)
            badge = next(b for b in self.badges() if b['code'] == 'carrying')
        judgement = next(j for j in detail['display_judgements'] if j['code'] == 'carrying')
        self.assertTrue(judgement['supported'])
        self.assertEqual(judgement['evidence_strength']['level'], 'strong')
        self.assertEqual(judgement['evidence_strength']['confidence'], '高')
        self.assertEqual(badge['confidence'], '高')
        self.assertEqual(detail['status'], 'ready')
        row.refresh_from_db()
        self.assertEqual((row.evidence_hash, row.evidence_json, row.result_json, row.generated_at), frozen)

    def test_five_same_source_events_are_partial_and_model_cannot_promote_them(self):
        evidence = self.collect(5)
        review = evidence['metrics']['behavior_review']
        self.assertEqual(review['evidence_strengths']['carrying']['verified_runs'], 5)
        self.assertEqual(review['evidence_strengths']['carrying']['level'], 'partial')
        self.assertNotIn('carrying', review['supported'])
        self.assertNotIn('carrying', [label['code'] for label in evidence['eligible_labels']])
        valid = json.loads(synthetic_model_response(json.dumps(service._generation_input(evidence))))
        valid['observations'] = [dict(code='carrying', confidence='高', reason_id='repeated_witnesses',
            limitation_ids=['sample_scope'], evidence_ids=['behavior:carrying:920001'])]
        with self.assertRaisesRegex(service.InvalidResult, 'invalid_label'):
            service._validate_publication(json.dumps(valid), evidence, publication_version=10)
        self.publish()
        detail = service.project_player_observation(self.player)
        self.assertNotIn('carrying', [item['code'] for item in detail['observations']])
        self.assertNotIn('carrying', [item['code'] for item in self.badges()])

    def test_duplicate_protocol_rows_do_not_turn_five_into_six_events(self):
        evidence = self.collect(5, duplicate=True)
        self.assertEqual(len(self.data['profile']['mythic_plus_recent_runs']), 10)
        review = evidence['metrics']['behavior_review']
        self.assertEqual(review['evidence_strengths']['carrying']['verified_runs'], 5)
        self.assertEqual(len(set(review['witnesses']['carrying'])), 5)
        self.assertEqual(review['evidence_strengths']['carrying']['level'], 'partial')
        self.assertNotIn('carrying', review['supported'])
        self.publish()
        self.assertNotIn('carrying', [b['code'] for b in self.badges()])

    def test_six_runs_with_one_guest_are_not_a_rotating_assistance_mode(self):
        evidence = self.collect(6, one_guest=True)
        review = evidence['metrics']['behavior_review']
        self.assertNotEqual(review['evidence_strengths']['carrying']['level'], 'strong')
        self.assertNotIn('carrying', review['supported'])
        self.publish()
        self.assertNotIn('carrying', [b['code'] for b in self.badges()])
