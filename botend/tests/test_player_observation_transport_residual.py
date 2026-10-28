"""Transport structure regressions, not evidence of a successful model call."""
import copy
import gzip
import json
from pathlib import Path
from unittest import TestCase

from botend.services.player_observation import _generation_input, _validate_publication, InvalidResult
from botend.services.player_observation_ai import compact_ai_input
from botend.services.player_observation_contract import build_contract, validate_investigation, InvalidContract


class ResidualTransportTests(TestCase):
    @classmethod
    def setUpClass(cls):
        cls.captures = json.loads(gzip.decompress((Path(__file__).parent / 'fixtures/player_observation_transport_residual.json.gz').read_bytes()))

    def test_background_is_separate_and_losslessly_recoverable(self):
        for capture in self.captures:
            with self.subTest(player_id=capture['player_id']):
                evidence = copy.deepcopy(capture['sent'])
                before = copy.deepcopy(evidence)
                old = compact_ai_input(evidence)
                data = _generation_input(evidence)
                for key, fields in [('investigation', ('hypotheses', 'questions')),
                                    ('behavior_review', ('questions', 'label_reviews'))]:
                    block = data['metrics'][key]
                    for field in fields:
                        self.assertNotIn(field, block)
                        self.assertEqual(block['non_output_background'][field], old['metrics'][key][field])
                self.assertEqual(data['evidence'], evidence['evidence'])
                self.assertEqual(evidence, before)
                plan = data['metrics']['investigation']
                self.assertEqual(plan['response_contract'], evidence['metrics']['investigation']['response_contract'])
                self.assertEqual(plan['response_contract'], build_contract(evidence))
                self.assertEqual(plan['selection_guide'], evidence['metrics']['investigation']['selection_guide'])
                self.assertEqual(plan['next_question_guide']['candidates'], evidence['metrics']['investigation']['next_question_guide']['candidates'])
                # Restore relocated fields to prove every pre-existing compact fact survives.
                restored = copy.deepcopy(data)
                for key in ('investigation', 'behavior_review'):
                    block = restored['metrics'][key]
                    block.update(block.pop('non_output_background'))
                old['metrics']['investigation'].pop('response_templates', None)
                for key in ('response_contract', 'selection_guide', 'schema_version_guide', 'next_question_guide'):
                    restored['metrics']['investigation'].pop(key, None)
                    old['metrics']['investigation'].pop(key, None)
                self.assertEqual(restored, old)
                data['evidence'][0]['text'] = 'test-only mutation'
                self.assertEqual(evidence, before)

    def test_historical_raw_stays_rejected_and_test_only_choices_obey_original_validator(self):
        for capture in self.captures:
            with self.subTest(player_id=capture['player_id']):
                evidence = capture['sent']
                before = copy.deepcopy(evidence)
                with self.assertRaisesRegex(InvalidResult, capture['error']):
                    _validate_publication(capture['raw'], evidence, publication_version=9, require_investigation=True)
                contract = _generation_input(evidence)['metrics']['investigation']['response_contract']
                value = {'schema_version': contract['schema_version'], 'confidence': contract['confidence'],
                         'next_question_ids': contract['next_question_ids'][:3], 'hypotheses': [
                             {'id': h['id'], 'assessment': h['assessment_options'][0],
                              'support_ids': h['support_ids'][:3], 'counter_ids': h['counter_ids'][:],
                              'unknown_ids': h['unknown_ids'][:3]} for h in contract['hypotheses']]}
                validate_investigation(value, evidence)
                # Preserve the model's legal assessment/ordering freedom; these
                # are deliberately test-built choices, never repaired raw output.
                for index, h in enumerate(contract['hypotheses']):
                    for assessment in h['assessment_options']:
                        alternate = copy.deepcopy(value)
                        alternate['hypotheses'][index]['assessment'] = assessment
                        alternate['hypotheses'].reverse()
                        validate_investigation(alternate, evidence)
                # Expose each real violation separately from an otherwise legal
                # test response, including errors masked by too_many_ids.
                historical = json.loads(capture['raw'])['investigation']
                for historic in historical['hypotheses']:
                    for index, h in enumerate(contract['hypotheses']):
                        if h['id'] != historic['id']:
                            continue
                        for field in ('support_ids', 'counter_ids', 'unknown_ids'):
                            refs = historic[field]
                            if not set(refs) <= set(h[field]) or (field != 'counter_ids' and len(refs) > 3):
                                bad = copy.deepcopy(value)
                                bad['hypotheses'][index][field] = refs[:]
                                with self.assertRaises(InvalidContract):
                                    validate_investigation(bad, evidence)
                if not set(historical['next_question_ids']) <= set(contract['next_question_ids']):
                    bad = copy.deepcopy(value)
                    bad['next_question_ids'] = historical['next_question_ids'][:]
                    with self.assertRaisesRegex(InvalidContract, 'invalid_polarity_or_id'):
                        validate_investigation(bad, evidence)
                bad = copy.deepcopy(value)
                bad['next_question_ids'] = ['not-an-authorized-question']
                with self.assertRaisesRegex(InvalidContract, 'invalid_polarity_or_id'):
                    validate_investigation(bad, evidence)
                for index, h in enumerate(contract['hypotheses']):
                    for field in ('support_ids', 'unknown_ids', 'counter_ids'):
                        bad = copy.deepcopy(value)
                        bad['hypotheses'][index][field] = ['not-an-authorized-fact']
                        with self.assertRaisesRegex(InvalidContract, 'invalid_polarity_or_id'):
                            validate_investigation(bad, evidence)
                    if len(h['unknown_ids']) > 3:
                        bad = copy.deepcopy(value)
                        bad['hypotheses'][index]['unknown_ids'] = h['unknown_ids'][:4]
                        with self.assertRaisesRegex(InvalidContract, 'too_many_ids'):
                            validate_investigation(bad, evidence)
                    if h['counter_ids']:
                        bad = copy.deepcopy(value)
                        bad['hypotheses'][index]['counter_ids'] = []
                        with self.assertRaisesRegex(InvalidContract, 'missing_counter_ids'):
                            validate_investigation(bad, evidence)
                self.assertEqual(evidence, before)
