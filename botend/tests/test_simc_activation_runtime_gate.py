from unittest import TestCase
from botend.services.simc_skill_activation_context import materialize_activation_pair, plan_activation_context_pairs
from botend.tests.test_simc_skill_activation_context import trait


class ActivationRuntimeGateTests(TestCase):
    def pair(self):
        unlock, target = trait(10), trait(30)
        plan = plan_activation_context_pairs(
            [unlock, target], candidates=[{'trait_entry_id':10, 'replacement_spell_id':300}],
            contexts={10:{'selected_talents':[unlock], 'actor':{
                'selected_trait_ids':[10], 'actions':[{'spell_id':300}]}}},
        )
        return plan['pairs'][0]

    def materialize(self, ref_high, selected_high, ref_low=None, selected_low=None):
        sets = {(100,'reference'):ref_high, (100,'selected'):selected_high,
                (34,'reference'):ref_high if ref_low is None else ref_low,
                (34,'selected'):selected_high if selected_low is None else selected_low}
        pair = self.pair()
        def load(health, name):
            kind='reference' if name==pair['reference_name'] else 'selected'
            return {'selected_trait_ids':sets[health,kind], 'actions':[{'spell_id':300}]}
        return materialize_activation_pair(pair,load)

    def test_missing_fixed_context_on_both_sides_is_not_a_valid_pair(self):
        with self.assertRaisesRegex(ValueError, 'context'):
            self.materialize([99], [99,30])

    def test_unplanned_shared_runtime_traits_are_not_silently_accepted(self):
        with self.assertRaisesRegex(ValueError, 'selection'):
            self.materialize([10,99], [10,99,30])

    def test_target_health_does_not_change_fixed_talent_selection(self):
        with self.assertRaisesRegex(ValueError, 'selection'):
            self.materialize([10], [10,30], [10,99], [10,99,30])

    def test_exact_pair_and_declared_implicit_baseline_traits_remain_valid(self):
        self.assertEqual(self.materialize([10],[10,30])['high']['selected_trait_ids'],[10,30])
        unlock, target=trait(10),trait(30)
        plan=plan_activation_context_pairs([unlock,target],
            candidates=[{'trait_entry_id':10,'replacement_spell_id':300}],
            contexts={10:{'selected_talents':[unlock], 'actor':{
                'selected_trait_ids':[10,99], 'actions':[{'spell_id':300}]}}},
            implicit_trait_entry_ids=[99])
        pair=plan['pairs'][0]
        result=materialize_activation_pair(pair, lambda health,name:{
            'selected_trait_ids':[10,99]+([30] if name==pair['selected_name'] else []),
            'actions':[{'spell_id':300}]})
        self.assertEqual(result['reference_low']['selected_trait_ids'],[10,99])
