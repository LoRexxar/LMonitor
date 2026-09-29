from unittest import TestCase
from botend.services.simc_skill_activation_context import discover_replacement_candidates, plan_activation_context_pairs
from botend.tests.test_simc_skill_activation_context import trait


class MaskReplacementSelectorTests(TestCase):
    def test_unspecified_original_does_not_erase_valid_replacement_or_fake_readiness(self):
        unlock=trait(10)
        catalog={'talent_catalog':[{'trait_entry_id':10,'spell_id':20,'effects':[
            {'type':6,'subtype':332,'misc1':0,'misc2':3,'value':30,'flags':[0,0,16,0],'id':50,'index':7}]}]}
        candidates=discover_replacement_candidates(catalog,[unlock])
        self.assertEqual(len(candidates),1)
        self.assertEqual(candidates[0]['original_spell_id'],0)
        self.assertEqual(candidates[0]['replacement_spell_id'],30)
        plan=plan_activation_context_pairs([unlock],candidates=candidates,contexts={10:{
            'selected_talents':[unlock],'actor':{'selected_trait_ids':[10],'actions':[]}}})
        self.assertEqual(plan['context_count'],0)
        self.assertEqual(plan['pairs'],[])

    def test_malformed_or_negative_original_is_still_rejected(self):
        for original in (-1, None, '0', True):
            with self.subTest(original=original),self.assertRaisesRegex(ValueError,'Invalid DBC'):
                discover_replacement_candidates({'talent_catalog':[{
                    'trait_entry_id':10,'spell_id':20,'effects':[{
                        'type':6,'subtype':332,'misc1':original,'value':30}]}]},[trait(10)])
