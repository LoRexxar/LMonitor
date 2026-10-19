"""Transport-contract regressions; no external model or publication calls."""
import copy
from django.test import SimpleTestCase
from botend.services import player_observation as service
from botend.services.player_observation_ai import compact_ai_input

class PlayerAIContractTests(SimpleTestCase):
    def test_unknown_list_is_not_a_transaction_assertion(self):
        plan={'triggered':True,'hypotheses':[
            {'id':name,'status':'unknown','support_ids':[],'unknown_ids':['unknown:'+name],'counter_ids':[]}
            for name in ('assistance','routine_group')],'questions':[]}
        value={'summary':'钥匙归属、收费与组队动机未知。','confidence':'低',
            'hypotheses':[{'id':h['id'],'assessment':'possible','evidence_ids':h['unknown_ids'],
                'counter_evidence_ids':[],'explanation':'协作解释仍可能，动机未知。'} for h in plan['hypotheses']],
            'next_question_ids':[]}
        for text in ('钥匙归属、收费与组队动机未知。', '钥匙归属、收费与完整历史未知。'):
            value['summary'] = text
            service._validate_investigation(value,plan,bounded=True)
        bad=copy.deepcopy(value);bad['summary']='收费已经确认。'
        with self.assertRaises(service.InvalidResult):service._validate_investigation(bad,plan,bounded=True)

    def test_model_menu_is_bounded_with_local_reference_templates(self):
        hypotheses=[{'id':key,'status':'supported' if key=='assistance' else 'unknown',
                     'support_ids':['s:'+key] if key=='assistance' else [],
                     'unknown_ids':['u:'+key],'counter_ids':['c:'+key] if key=='assistance' else []}
                    for key in ('assistance','routine_group','key_access','later_progress','external_assistance','being_carried','joint_progress')]
        facts=[{'id':ref,'text':ref} for h in hypotheses for k in ('support_ids','unknown_ids','counter_ids') for ref in h[k]]
        full={'evidence':facts,'evidence_hash':'unchanged','eligible_labels':[
            {'code':'timed_experience'},{'code':'carrying'},{'code':'recent_inquiry'}],
            'conclusion_options':{'public_sample':{'required_codes':[]},'timed_sample':{'required_codes':['timed_experience']}},
            'metrics':{'behavior_review':{'supported':{'carrying':['s:assistance']},'structure':{}},
                       'investigation':{'triggered':True,'hypotheses':hypotheses,'questions':[]}}}
        original=copy.deepcopy(full);packed=compact_ai_input(full)
        menu=packed['metrics']['investigation']['hypotheses']
        self.assertLessEqual(len(menu),4)
        self.assertIn('assistance',{h['id'] for h in menu});self.assertIn('key_access',{h['id'] for h in menu})
        self.assertEqual({x['code'] for x in packed['eligible_labels']},{'carrying','recent_inquiry'})
        self.assertEqual(set(packed['conclusion_options']),{'public_sample'})
        templates=packed['metrics']['investigation']['response_templates']
        for h,t in zip(menu,templates):
            self.assertEqual(t['id'],h['id']);self.assertTrue(set(t['evidence_ids'])<=set(h['support_ids']+h['unknown_ids']))
            self.assertEqual(t['counter_evidence_ids'],h['counter_ids']);self.assertLessEqual(len(t['evidence_ids']),3)
        self.assertEqual(packed['evidence'],facts);self.assertEqual(full,original)
