import copy
import unittest
from botend.services.player_observation_ai import compact_ai_input

class CompactInputTests(unittest.TestCase):
    def test_retains_all_facts_contract_and_inquiry_without_duplicate_matrices(self):
        facts=[{'id':'behavior:carrying:1','text':'dated fact','run_url':'https://raider.io/'}]
        data={'evidence':facts,'eligible_labels':[{'code':'carrying','evidence_ids':[facts[0]['id']]}],
              'conclusion_options':{'public_sample':{}},'limitations':['partial'],'source_stamps':{'time':'original'},
              'evidence_hash':'frozen-hash','metrics':{'team_pattern':{'kind':'fixed_five'},
              'behavior_review':{'version':1,'triggered':True,'evidence':facts,'supported':{'carrying':[facts[0]['id']]},
                'witnesses':{'carrying':[1,2]},'resilience':{'events':[{'large':'proof matrix'}]},
                'structure':{'codes':['fixed_five'],'recent_runs':2,'roster_runs':2,'untimed_runs':0,
                    'observed_config_switches':0,'lineups':[{'members':['a','b'],'count':2,'run_ids':[1,2]}],
                    'comparison_groups':[{'members':['a','b'],'run_ids':[1,2]}]}},
              'investigation':{'triggered':True,'evidence':facts,'hypotheses':[{'id':'assistance','support_ids':[facts[0]['id']]}],
                               'questions':[{'id':'history:one','run_ids':[1]}]}}}
        frozen=copy.deepcopy(data);result=compact_ai_input(data)
        for key in ('evidence','eligible_labels','conclusion_options','limitations','source_stamps','evidence_hash'):
            self.assertEqual(result[key],data[key])
        self.assertEqual(result['metrics']['investigation']['hypotheses'],data['metrics']['investigation']['hypotheses'])
        self.assertEqual(result['metrics']['investigation']['questions'],data['metrics']['investigation']['questions'])
        self.assertNotIn('evidence',result['metrics']['investigation'])
        self.assertNotIn('large',result['metrics']['behavior_review']['resilience']['events'][0])
        self.assertEqual(result['metrics']['behavior_review']['witnesses'],{'carrying':[1,2]})
        self.assertEqual(data,frozen)
        result['evidence'][0]['text']='modified'
        self.assertEqual(data,frozen)

    def test_generation_guide_binds_facts_to_local_polarity_and_full_counters(self):
        import json
        from pathlib import Path
        from botend.services.player_observation import _generation_input
        from botend.services.player_observation_contract import build_contract, validate_investigation, InvalidContract

        captures = json.loads((Path(__file__).parent / 'fixtures/player_observation_contract_v8.json').read_text())
        for capture in captures:
            evidence = copy.deepcopy(capture['evidence'])
            before = copy.deepcopy(evidence)
            data = _generation_input(evidence)
            plan = data['metrics']['investigation']
            contract = build_contract(evidence)
            self.assertEqual(plan['response_contract'], contract)
            guides = plan['selection_guide']
            self.assertEqual([g['id'] for g in guides], [h['id'] for h in contract['hypotheses']])
            facts = {f['id']: f['text'] for f in evidence['evidence']}
            for guide, menu in zip(guides, contract['hypotheses']):
                for field in ('support_ids', 'counter_ids', 'unknown_ids'):
                    self.assertEqual(guide[field]['operation'], 'copy_all' if field == 'counter_ids' else 'select_up_to_3')
                    self.assertEqual(guide[field]['candidates'], [{'id': ref, 'text': facts[ref]} for ref in menu[field]])
            self.assertEqual(data['evidence'], evidence['evidence'])
            self.assertEqual(evidence, before)
            # Test-only selections, not model outputs: the same unchanged validator
            # must still reject moving an unknown into support or dropping a counter.
            value = {'schema_version': 8, 'confidence': '低', 'next_question_ids': [],
                     'hypotheses': [{'id': h['id'], 'assessment': 'possible',
                         'support_ids': h['support_ids'][:3], 'counter_ids': h['counter_ids'][:],
                         'unknown_ids': h['unknown_ids'][:3]} for h in contract['hypotheses']]}
            validate_investigation(value, evidence)
            for row in value['hypotheses']:
                if row['unknown_ids']:
                    bad = copy.deepcopy(value)
                    next(h for h in bad['hypotheses'] if h['id'] == row['id'])['support_ids'] = row['unknown_ids'][:1]
                    with self.assertRaisesRegex(InvalidContract, 'invalid_polarity_or_id'):
                        validate_investigation(bad, evidence)
                if row['counter_ids']:
                    bad = copy.deepcopy(value)
                    next(h for h in bad['hypotheses'] if h['id'] == row['id'])['counter_ids'] = []
                    with self.assertRaisesRegex(InvalidContract, 'missing_counter_ids'):
                        validate_investigation(bad, evidence)

    def test_legacy_absent_behavior_keeps_eligible_facts(self):
        data={'evidence':[{'id':'timed_experience'}],'eligible_labels':[{'code':'timed_experience'}],'metrics':{}}
        self.assertEqual(compact_ai_input(data)['evidence'],data['evidence'])

    def test_per_run_protection_and_structure_authority_survive_compaction(self):
        events = [dict(run_id=rid, started_at=f'2026-10-18T0{rid}:00:00Z', level=20,
                       members={'subject': dict(state=state, required_level=21, per_dungeon={'1': {'level': 21}}),
                                'peer': dict(state='unknown', required_level=21, per_dungeon={})})
                  for rid, state in [(1, 'confirmed_before_start'), (2, 'unknown')]]
        data = {'evidence': [{'id': str(i), 'text': 'canonical'} for i in range(30)],
                'metrics': {'team_pattern': {'kind': 'fixed_five'}, 'behavior_review': {
                    'structure': {'codes': ['occasional_substitution'], 'recent_runs': 8},
                    'resilience': {'subject_key': 'subject', 'events': events}}}}
        frozen = copy.deepcopy(data)
        result = compact_ai_input(data)
        review = result['metrics']['behavior_review']
        self.assertNotIn('team_pattern', result['metrics'])
        self.assertEqual(review['structure']['codes'], ['occasional_substitution'])
        self.assertEqual(review['resilience']['subject_key'], 'subject')
        for original, projected in zip(events, review['resilience']['events']):
            self.assertEqual({k: projected[k] for k in ('run_id', 'started_at', 'level')},
                             {k: original[k] for k in ('run_id', 'started_at', 'level')})
            for member, proof in original['members'].items():
                self.assertEqual(projected['members'][member],
                                 {k: proof[k] for k in ('state', 'required_level')})
        self.assertEqual(result['evidence'], data['evidence'])
        self.assertEqual(data, frozen)
        del data['metrics']['behavior_review']['resilience']['subject_key']
        legacy = compact_ai_input(data)['metrics']['behavior_review']['resilience']
        self.assertNotIn('subject_key', legacy)
        self.assertEqual(legacy['events'], review['resilience']['events'])
