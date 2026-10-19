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
        self.assertNotIn('resilience',result['metrics']['behavior_review'])
        self.assertEqual(result['metrics']['behavior_review']['witnesses'],{'carrying':[1,2]})
        self.assertEqual(data,frozen)
        result['evidence'][0]['text']='modified'
        self.assertEqual(data,frozen)

    def test_legacy_absent_behavior_keeps_eligible_facts(self):
        data={'evidence':[{'id':'timed_experience'}],'eligible_labels':[{'code':'timed_experience'}],'metrics':{}}
        self.assertEqual(compact_ai_input(data)['evidence'],data['evidence'])
