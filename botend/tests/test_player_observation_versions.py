"""Trusted publication versions, actual ORM generation and frozen reads."""
import copy
import json
from unittest.mock import Mock

from django.test import TestCase
from botend.tests import test_player_observation as fixtures
from botend.services import player_observation as service
from botend.models import PlayerObservationAnalysis


class PublicationVersionTests(TestCase):
    setUp = fixtures.PlayerObservationTests.setUp
    evidence = fixtures.PlayerObservationTests.evidence

    def legacy(self):
        evidence = self.evidence()
        evidence['prompt_version'] = 7
        evidence['evidence_hash'] = service._hash({k: v for k, v in evidence.items()
            if k not in ('evidence_hash', 'source_failed')})
        result = fixtures.observation_response(evidence)
        from django.utils import timezone
        return PlayerObservationAnalysis.objects.create(activity=self.activity, player=self.player,
            prompt_version=7, evidence_json=evidence, evidence_hash=evidence['evidence_hash'],
            result_json=result, status='ready', revision=3, generated_at=timezone.now())

    def test_valid_v7_reused_without_model_or_rewrite(self):
        row = self.legacy()
        before = PlayerObservationAnalysis.objects.values().get(pk=row.pk)
        engine = Mock()
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'fresh')
        engine.send_message.assert_not_called()
        self.assertEqual(PlayerObservationAnalysis.objects.values().get(pk=row.pk), before)
        self.assertEqual(service.project_player_observation(self.player)['status'], 'ready')
        self.assertEqual(service._project_frozen_investigation(row)['summary'], row.result_json['investigation']['summary'])

    def test_invalid_v7_and_corrupt_hash_cannot_reuse(self):
        row = self.legacy()
        row.result_json['investigation']['summary'] = '确认收费带人'
        row.save()
        with self.assertRaises(service.InvalidResult):
            service._validate_frozen_publication(row)
        self.assertEqual(service.project_player_observation(self.player)['status'], 'error')
        engine = Mock(send_message=Mock(return_value='{}'))
        self.assertNotEqual(service.generate_observation(self.player, engine=engine)['status'], 'fresh')
        engine.send_message.assert_called_once()
        row.evidence_json['metrics']['injected'] = True
        self.assertFalse(service._same_publication(row, row.evidence_json))

    def test_version_binding_and_narrow_cross_version_equivalence(self):
        row = self.legacy()
        current = self.evidence()
        self.assertEqual(current['prompt_version'], 8)
        self.assertTrue(service._same_publication(row, current))
        for mutation in ({'version': 9}, {'prompt_version': 9}, {'unknown': True}):
            changed = copy.deepcopy(current)
            changed['metrics'].update(mutation)
            self.assertFalse(service._same_publication(row, changed))
        for version in (True, '7', 9, 8):
            row.prompt_version = version
            with self.assertRaises(service.InvalidResult):
                service._validate_frozen_publication(row)

    def test_priority_reuse_is_validated_and_failures_keep_backoff(self):
        from datetime import timedelta
        from django.utils import timezone
        from botend.services.player_observation_sources import _preferred_question_ids
        row = self.legacy()
        current = self.evidence()
        expected = row.result_json['investigation']['next_question_ids']
        self.assertTrue(expected)
        self.assertEqual(_preferred_question_ids(self.activity, current), expected)
        row.result_json['investigation']['summary'] = '确认收费带人'
        row.next_attempt_at = timezone.now() + timedelta(minutes=30)
        row.save()
        self.assertEqual(_preferred_question_ids(self.activity, current), [])
        engine = Mock()
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'backoff')
        engine.send_message.assert_not_called()

    def test_frozen_hash_and_source_failure_cannot_be_normalized_away(self):
        row = self.legacy()
        current = self.evidence()
        current['source_failed'] = True
        self.assertFalse(service._same_publication(row, current))
        row.evidence_json['metrics']['untrusted_change'] = True
        current = copy.deepcopy(row.evidence_json)
        current['prompt_version'] = 8
        # Matching row/current hashes do not prove the frozen content is intact.
        self.assertEqual(current['evidence_hash'], row.evidence_hash)
        self.assertFalse(service._same_publication(row, current))
        with self.assertRaisesRegex(service.InvalidResult, 'invalid_frozen_hash'):
            service._project_frozen_investigation(row)

    def test_local_empty_is_server_only_and_cannot_hide_a_trigger(self):
        row = self.legacy()
        row.result_json = {'observations': [], 'conclusion_id': 'public_sample',
                           'metadata': copy.deepcopy(service.LOCAL_EMPTY_METADATA)}
        with self.assertRaises(service.InvalidResult):
            service._validate_frozen_publication(row)
        row.evidence_json['metrics']['investigation']['triggered'] = False
        row.evidence_json['metrics']['behavior_review']['supported'] = {}
        row.evidence_hash = service._hash({k: v for k, v in row.evidence_json.items()
            if k not in ('evidence_hash', 'source_failed')})
        row.evidence_json['evidence_hash'] = row.evidence_hash
        self.assertEqual(service._validate_frozen_publication(row), row.result_json)
        self.assertIsNone(service._project_frozen_investigation(row))
        row.result_json['metadata']['ai']['called'] = 0
        with self.assertRaises(service.InvalidResult):
            service._validate_frozen_publication(row)

    def test_v8_real_generator_transport_and_frozen_projection(self):
        def respond(prompt, **kwargs):
            data = json.loads(prompt)
            plan = data['metrics']['investigation']
            self.assertNotIn('response_templates', plan)
            menu = plan['response_contract']
            result = fixtures.observation_response(data)
            result['investigation'] = {'schema_version': 8, 'confidence': '低',
                'hypotheses': [{'id': h['id'], 'assessment': h['assessment_options'][0],
                    'support_ids': h['support_ids'][:3], 'counter_ids': h['counter_ids'],
                    'unknown_ids': h['unknown_ids'][:3]} for h in menu['hypotheses']],
                'next_question_ids': menu['next_question_ids'][:3]}
            return json.dumps(result)
        engine = Mock(send_message=Mock(side_effect=respond))
        self.assertEqual(service.generate_observation(self.player, engine=engine)['status'], 'ready')
        row = PlayerObservationAnalysis.objects.get()
        self.assertEqual(row.prompt_version, 8)
        self.assertNotIn('response_contract', row.evidence_json['metrics']['investigation'])
        raw = copy.deepcopy(row.result_json)
        projected = service._project_frozen_investigation(row)
        self.assertEqual(projected['structured_investigation'], raw['investigation'])
        self.assertEqual(service.project_player_observation(self.player)['investigation'], projected)
        row.refresh_from_db()
        self.assertEqual(row.result_json, raw)
        for investigation in (fixtures.observation_response(row.evidence_json)['investigation'],
                              {**raw['investigation'], 'summary': '任意文字'}):
            changed = {**raw, 'investigation': investigation}
            with self.assertRaises(service.InvalidResult):
                service._validate_publication(json.dumps(changed), row.evidence_json,
                    publication_version=8, require_investigation=True)
