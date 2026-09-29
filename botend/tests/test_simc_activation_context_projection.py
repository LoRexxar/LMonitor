import copy
from django.test import SimpleTestCase
from botend.services.simc_skill_damage import (
    classify_global_skill_effects, flatten_single_talent_damage_variants,
)
from botend.tests.test_simc_partial_state_local_damage import REAL_ACTOR


class ActivationContextProjectionTests(SimpleTestCase):
    def test_fixed_context_is_kept_separate_from_measured_talent(self):
        reference = copy.deepcopy(REAL_ACTOR)
        reference['actions'][0]['scenarios'] = []
        selected = copy.deepcopy(REAL_ACTOR)
        context = {'trait_entry_ids': [119139], 'action_spell_ids': [335096],
                   'traits': [{'trait_entry_id': 119139, 'name': 'Reckless Abandon', 'name_zh': '肆意放纵'}],
                   'evidence': [{'trait_entry_id': 119139, 'replacement_spell_id': 335096}]}
        item = {'talent': {'id': 123, 'node_id': 112299, 'name': 'Cruelty', 'name_zh': '残忍', 'tree_type': 'spec'},
                'reference_high': reference, 'reference_low': reference,
                'high': selected, 'low': selected, 'activation_context': context}
        base = {'class': 'warrior', 'spec': 'fury', 'actions': []}
        effects = classify_global_skill_effects(selected, selected, [])
        rows = flatten_single_talent_damage_variants(base, base, [item], global_effects=effects)
        enrage_rows = [row for row in rows if row['variant']['scenario_tokens'] == ['buff.enrage']]
        self.assertEqual(len(enrage_rows), 1)
        variant = enrage_rows[0]['variant']
        self.assertEqual(variant['trait_entry_id'], 112299)
        self.assertEqual(variant['activation_context']['trait_entry_ids'], [119139])
        self.assertIn('肆意放纵', variant['activation_context']['display_label'])
        self.assertNotIn('残忍', variant['activation_context']['display_label'])
        self.assertEqual(enrage_rows[0]['baseline']['direct']['hit'], 615.67657338)
        self.assertEqual(context, item['activation_context'])
        self.assertNotIn('display_label', context)
