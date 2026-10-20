"""Management's real frozen validator/projection and Portal rendering boundary."""
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from botend.services.player_analysis_management import frozen_ai
from botend.services.player_observation import _hash
from botend.services.player_observation_contract import build_contract

TEXT = 'A & B < C <img src=x onerror=alert(1)> 中文三场'


def publication(version=8):
    evidence = deepcopy(json.loads((Path(__file__).parent / 'fixtures/player_observation_contract_v8.json').read_text())[0]['evidence'])
    evidence['prompt_version'] = version
    evidence['eligible_labels'] = []
    evidence['conclusion_options'] = {'public_sample': {'text': '公开样本'}}
    for item in evidence['evidence']:
        item['text'] += ' ' + TEXT
    contract = build_contract(evidence)
    if version == 8:
        investigation = {'schema_version': 8, 'confidence': '低', 'hypotheses': [
            {'id': h['id'], 'assessment': 'possible', 'support_ids': h['support_ids'][:2],
             'counter_ids': h['counter_ids'], 'unknown_ids': h['unknown_ids'][:1]}
            for h in contract['hypotheses']], 'next_question_ids': contract['next_question_ids'][:3]}
    else:
        investigation = {'summary': '旧版冻结解释仍需核查', 'confidence': '低', 'hypotheses': [
            {'id': h['id'], 'assessment': 'possible', 'evidence_ids': h['support_ids'][:2],
             'counter_evidence_ids': h['counter_ids'], 'explanation': '仍需区分朋友互助等可能性'}
            for h in contract['hypotheses']], 'next_question_ids': contract['next_question_ids'][:3]}
    result = {'observations': [], 'conclusion_id': 'public_sample', 'investigation': investigation}
    digest = _hash(evidence)
    evidence['evidence_hash'] = digest
    return SimpleNamespace(evidence_json=evidence, result_json=result, evidence_hash=digest, prompt_version=version)


def portal(view):
    return render_to_string('portal/spec_detail/_player_observation.html', {'observation': {
        'status': 'ready', 'source_kind': 'ai', 'investigation': view}})


class FrozenManagementProjectionTests(SimpleTestCase):
    def test_real_versions_preserve_audit_and_render_full_unknowns(self):
        for version in (7, 8):
            row = publication(version)
            before = deepcopy(vars(row))
            ai = frozen_ai(row)
            self.assertEqual(ai['validation_status'], 'valid')
            self.assertEqual(vars(row), before)
            self.assertIs(ai['result_json'], row.result_json)
            self.assertIs(ai['evidence_json'], row.evidence_json)
            view = ai['rendered_investigation']
            self.assertTrue(view['hypotheses'])
            if version == 7:
                self.assertEqual(view['summary'], row.result_json['investigation']['summary'])
            for hypothesis in view['hypotheses']:
                frozen = next(h for h in row.evidence_json['metrics']['investigation']['hypotheses'] if h['id'] == hypothesis['id'])
                self.assertEqual([e['id'] for e in hypothesis['unknown_evidence']], frozen['unknown_ids'])
            html = portal(view)
            self.assertIn('A &amp; B &lt; C', html)
            self.assertNotIn('&amp;amp;', html)
            self.assertNotIn('<img', html)

    def test_v8_limitations_are_independent_paragraph_lists(self):
        row = publication()
        first = frozen_ai(row)['rendered_investigation']['limitations']
        second = frozen_ai(row)['rendered_investigation']['limitations']
        self.assertIsInstance(first, list)
        self.assertTrue(all(isinstance(text, str) and len(text) > 1 for text in first))
        self.assertIn('团队通关不是独立完成的证明。', ''.join(first))
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        first.clear()
        self.assertTrue(second)
        self.assertEqual(frozen_ai(row)['rendered_investigation']['limitations'], second)

    def test_tampered_hash_and_mixed_schema_are_audit_only(self):
        for mutation in ('hash', 'mixed', 'version'):
            row = publication()
            if mutation == 'hash':
                row.evidence_json['evidence'][0]['text'] = 'tampered'
            elif mutation == 'mixed':
                row.result_json['investigation']['summary'] = 'untrusted prose'
            else:
                row.prompt_version = 7
            ai = frozen_ai(row)
            self.assertEqual(ai['validation_status'], 'invalid')
            self.assertIsNone(ai['rendered_investigation'])
            self.assertEqual(ai['tags'], [])
            self.assertEqual(ai['conclusion'], '')
            self.assertIs(ai['result_json'], row.result_json)
