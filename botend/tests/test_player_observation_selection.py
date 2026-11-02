"""Actual 129 capture + explicitly TEST SYNTHESIS wire, never a real AI pass."""
from copy import deepcopy
import gzip
import hashlib
import json
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from core.glm import GLMClient
from botend.services.player_observation import (
    PlayerObservationGLMClient, _generation_input, _validate_publication, InvalidResult)
from botend.services.player_observation_contract import build_contract, render_investigation
from botend.services.player_observation_selection import (
    WIRE_PROTOCOL, build_selection_input, decode_selection, InvalidSelection)


class PlayerObservationSelectionTests(TestCase):
    def test_actual_capture_selection_transport(self):
        fixture = Path(__file__).parent / 'fixtures/player_observation_selection_129.json.gz'
        self.assertEqual(hashlib.sha256(fixture.read_bytes()).hexdigest(), FIXTURE_SHA256)
        capture = json.loads(gzip.decompress(fixture.read_bytes()))
        evidence, old_raw = capture['evidence'], capture['raw']
        before = deepcopy(capture)
        # Real RED remains: the original provider returned four support IDs.
        with self.assertRaisesRegex(InvalidResult, 'too_many_ids'):
            _validate_publication(old_raw, evidence, publication_version=10)
        generated = _generation_input(evidence)
        self.assertEqual(generated, capture['sent'])
        contract = build_contract(evidence)
        self.assertEqual(contract, generated['metrics']['investigation']['response_contract'])
        request = build_selection_input(generated)
        self.assertEqual(request['non_output_background'], generated)
        self.assertNotIn('response_contract', request['selection_menu'])
        # TEST SYNTHESIS: reuse legal captured assessments, independently choose
        # up to three local focus slots, not an edited/truncated legacy raw.
        historical = json.loads(old_raw)
        wire = {'wire_protocol': WIRE_PROTOCOL,
                'observations': [{k: deepcopy(v) for k, v in row.items() if k != 'evidence_ids'}
                                 for row in historical['observations']],
                'conclusion_id': historical['conclusion_id'], 'hypotheses': [],
                'next_question_1': 0, 'next_question_2': 0, 'next_question_3': 0}
        h_by_id = {h['id']: h for h in contract['hypotheses']}
        for old in historical['investigation']['hypotheses']:
            h = h_by_id[old['id']]
            self.assertIn(old['assessment'], h['assessment_options'])
            row = {'id': old['id'], 'assessment': old['assessment']}
            for prefix in ('support', 'unknown'):
                for n in (1, 2, 3):
                    row[f'{prefix}_{n}'] = n if n <= len(h[prefix + '_ids']) else 0
            wire['hypotheses'].append(row)
        raw_wire = json.dumps(wire, ensure_ascii=False)
        # Real public client boundary + real underlying send_message; only the
        # provider I/O/config boundary is stubbed. No validator/builder mocks.
        client = PlayerObservationGLMClient.__new__(PlayerObservationGLMClient)
        client.system_prompt = 'original canonical prompt'
        with patch.object(GLMClient, '_begin_call'), patch.object(
                GLMClient, '_create_completion', return_value={
                    'choices': [{'message': {'content': raw_wire}}]}) as provider:
            canonical = client.send_message(json.dumps(generated, ensure_ascii=False),
                                            max_tokens=3600, thinking_type='disabled')
        kwargs = provider.call_args.kwargs
        self.assertEqual(kwargs['max_tokens'], 3600)
        self.assertEqual(kwargs['thinking'], {'type': 'disabled'})
        self.assertEqual(kwargs['response_format'], {'type': 'json_object'})
        self.assertEqual(json.loads(kwargs['messages'][1]['content']), request)
        self.assertEqual(client.last_raw_response, raw_wire)
        self.assertFalse(client.last_raw_response_truncated)
        self.assertEqual(client.last_canonical_response, canonical)
        self.assertEqual(client.system_prompt, 'original canonical prompt')
        result = _validate_publication(canonical, evidence, publication_version=10)
        rendered = render_investigation(result['investigation'], evidence)
        for selected, public in zip(result['investigation']['hypotheses'], rendered['hypotheses']):
            h = h_by_id[selected['id']]
            self.assertLessEqual(len(selected['support_ids']), 3)
            self.assertEqual(selected['counter_ids'], h['counter_ids'])
            self.assertEqual([r['id'] for r in public['counter_evidence']], h['counter_ids'])
            self.assertEqual([r['id'] for r in public['unknown_evidence']], h['unknown_ids'])
            self.assertTrue(set(selected['support_ids']) <= set(h['support_ids']))
        def reject(bad):
            with self.assertRaises(InvalidSelection):
                decode_selection(json.dumps(bad), request)
        reject(historical)  # Never reinterpret a legacy canonical response.
        for value in (-1, 100000, True, 1.0, '1', [], None, 'behavior:carrying_excluded'):
            with self.subTest(invalid_index=repr(value)):
                bad = deepcopy(wire)
                bad['hypotheses'][0]['support_1'] = value
                reject(bad)
        for extra in ('support_4', 'counter_ids', 'unknown_4', 'evidence_ids', 'subject'):
            with self.subTest(extra=extra):
                bad = deepcopy(wire)
                bad['hypotheses'][0][extra] = 0
                reject(bad)
        bad = deepcopy(wire)
        bad['hypotheses'][0]['id'] = 'not_this_hypothesis'
        reject(bad)
        bad = deepcopy(wire)
        bad['next_question_4'] = 0
        reject(bad)
        for index in (-1, True, '1', 100000):
            bad = deepcopy(wire)
            bad['next_question_1'] = index
            reject(bad)
        if contract['next_question_ids']:
            bad = deepcopy(wire)
            bad['next_question_1'] = bad['next_question_2'] = 1
            reject(bad)
        for prefix in ('support', 'unknown'):
            row_index = next(i for i, row in enumerate(wire['hypotheses'])
                             if h_by_id[row['id']][prefix + '_ids'])
            bad = deepcopy(wire)
            bad['hypotheses'][row_index][prefix + '_1'] = 1
            bad['hypotheses'][row_index][prefix + '_2'] = 1
            reject(bad)
        bad = deepcopy(wire)
        bad['hypotheses'] = bad['hypotheses'] * 2
        reject(bad)
        for hid in contract['required_hypothesis_ids']:
            bad = deepcopy(wire)
            bad['hypotheses'] = [r for r in bad['hypotheses'] if r['id'] != hid]
            reject(bad)
        # A foreign-polarity string never becomes a reference; integer 1 always
        # resolves ONLY to this hypothesis's same-field local authority.
        for row in wire['hypotheses']:
            if not h_by_id[row['id']]['support_ids']:
                bad = deepcopy(wire)
                next(r for r in bad['hypotheses'] if r['id'] == row['id'])['support_1'] = 1
                reject(bad)
        with self.assertRaises(InvalidSelection):
            decode_selection(raw_wire.replace('"support_1":', '"support_1": 0, "support_1":', 1), request)
        with self.assertRaises(InvalidSelection):
            decode_selection(raw_wire, {**request, 'wire_protocol': 'old'})
        # No fallback even for otherwise legal legacy canonical output.
        with patch.object(GLMClient, '_begin_call'), patch.object(GLMClient, '_create_completion',
                return_value={'choices': [{'message': {'content': canonical}}]}):
            rejected = client.send_message(json.dumps(generated), max_tokens=3600)
        self.assertEqual(client.last_raw_response, canonical)
        self.assertEqual(client.last_canonical_response, '')
        with self.assertRaises(InvalidResult):
            _validate_publication(rejected, evidence, publication_version=10)
        # Strict original canonical maximum is untouched as well.
        bad = deepcopy(result)
        routine = next(h for h in bad['investigation']['hypotheses'] if h['id'] == 'routine_group')
        routine['support_ids'] = deepcopy(h_by_id['routine_group']['support_ids'])
        self.assertGreater(len(routine['support_ids']), 3)
        with self.assertRaisesRegex(InvalidResult, 'too_many_ids'):
            _validate_publication(json.dumps(bad), evidence, publication_version=10)
        self.assertEqual(capture, before)


FIXTURE_SHA256 = '77f35305159459a89f235b428240bdc458b7de418c0d802070486d52ba7b8464'
