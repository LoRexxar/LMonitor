"""Previously validated AI priorities only reorder current, bounded questions."""
import copy
from datetime import timedelta
from django.test import TestCase
from django.utils import timezone
from botend.models import PlayerObservationAnalysis, RaiderIOCharacterActivity
from botend.services import player_observation as observation
from botend.services import player_observation_sources as sources
from botend.tests import test_player_observation_progression as fixtures


class InvestigationPriorityTests(TestCase):
    def setUp(self):
        fixtures.ObservationProgressionTests.setUp(self)
        RaiderIOCharacterActivity.objects.filter(character_name__in=['member4','member5','member6']).update(scored_runs_fetched_at=None)

    def test_only_validated_same_fact_ai_priorities_are_reused(self):
        evidence = observation.build_evidence(self.player)
        plan = evidence['metrics']['investigation']
        self.assertGreaterEqual(len(plan['questions']), 2)
        selected = plan['questions'][-1]['id']
        result = {'observations': [], 'conclusion_id': 'public_sample', 'investigation': {
            'summary': '已有协作线索，但仍需要区分不同解释。', 'confidence': '低',
            'hypotheses': [{'id': h['id'], 'assessment': h['status'],
                'evidence_ids': h['support_ids'], 'counter_evidence_ids': h['counter_ids'],
                'explanation': '仅为候选解释，缺失资料不能当作反证。'} for h in plan['hypotheses']],
            'next_question_ids': [selected]}}
        row = PlayerObservationAnalysis.objects.create(activity=self.activity, player=self.player,
            status='ready', prompt_version=observation.PROMPT_VERSION, evidence_json=evidence,
            evidence_hash=evidence['evidence_hash'], result_json=result)
        self.assertEqual(sources._preferred_question_ids(self.activity, evidence), [selected])
        malformed = copy.deepcopy(result); malformed['investigation']['next_question_ids'] = ['unrelated']
        row.result_json = malformed; row.save(update_fields=['result_json'])
        self.assertEqual(sources._preferred_question_ids(self.activity, evidence), [])
        row.result_json = result; row.save(update_fields=['result_json'])
        RaiderIOCharacterActivity.objects.all().update(scored_runs_fetched_at=timezone.now()-timedelta(days=3))
        changed = observation.build_evidence(self.player)
        self.assertEqual(sources._preferred_question_ids(self.activity, changed), [])
