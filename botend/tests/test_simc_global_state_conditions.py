"""全局状态、原生触发前提及真正局部组合必须在完整投影中分离。"""
import copy

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import flatten_single_talent_damage_variants, project_skill_damage_product_payload
from botend.services.simc_skill_state_projection import global_damage_state_identities, uses_global_damage_scenario
from botend.tests.test_simc_skill_damage_semantics import damage_action


class GlobalStateConditionsTests(SimpleTestCase):
    def actor(self):
        action = damage_action(scenarios=(
            ('buff.shared', 'self', 91001, 200),
            ('buff.local_a', 'self', 91002, 110),
            ('buff.local_b', 'self', 91003, 130),
        ), native_base=100)
        combined = copy.deepcopy(action['scenarios'][1])
        combined['buffs'] += copy.deepcopy(action['scenarios'][2]['buffs'])
        combined['values'] = copy.deepcopy(damage_action(scenarios=(('buff.x', 'self', 9, 143),))['scenarios'][0]['values'])
        action['scenarios'].append(combined)
        polluted = copy.deepcopy(combined)
        polluted['buffs'] += copy.deepcopy(action['scenarios'][0]['buffs'])
        polluted['values'] = copy.deepcopy(damage_action(scenarios=(('buff.x', 'self', 9, 286),))['scenarios'][0]['values'])
        action['scenarios'].append(polluted)
        return {'class': 'warrior', 'spec': 'arms', 'actions': [action], 'global_damage_states': [
            {'token': 'buff.shared', 'scope': 'self', 'spell_id': 91001,
             'partial_state': True, 'available': True, 'excluded_before_probe': True}]}

    def test_drop_global_scenarios_without_losing_local_combination_or_changing_values(self):
        actor = self.actor()
        actor['actions'] = flatten_single_talent_damage_variants(actor, actor, [])
        product = project_skill_damage_product_payload({'actors': [actor]})['actors'][0]
        self.assertEqual(len(product['actions']), 4)
        self.assertFalse(any('buff.shared' in r['variant']['scenario_tokens'] for r in product['actions']))
        combo = next(r for r in product['actions'] if len(r['variant']['scenario_tokens']) == 2)
        self.assertAlmostEqual(combo['product']['noncrit_damage'], 143)
        self.assertAlmostEqual(combo['product']['final_normalized_damage'], 171.6)
        self.assertEqual({c['token'] for c in combo['variant']['runtime_conditions']}, {'buff.local_a', 'buff.local_b'})

    def test_global_activation_is_retained_only_for_the_native_replacement_action(self):
        actor = self.actor()
        required = {'token': 'buff.shared', 'scope': 'self', 'spell_id': 91001, 'stacks': 1}
        replacement = copy.deepcopy(actor['actions'][0])
        replacement.update(token='replacement', spell_id=92000, reporting_root_token='replacement',
                           reporting_root_spell_id=92000, activation_conditions=[required])
        actor['actions'].append(replacement)
        actor['actions'] = flatten_single_talent_damage_variants(actor, actor, [])
        product = project_skill_damage_product_payload({'actors': [actor]})['actors'][0]
        for row in product['actions']:
            shared = [c for c in row['variant']['runtime_conditions'] if c['spell_id'] == 91001]
            self.assertEqual(bool(shared), row['token'] == 'replacement')
        self.assertFalse(any(uses_global_damage_scenario(row, global_damage_state_identities(actor))
                             for row in product['actions']))

    def test_direct_projector_does_not_publish_preflattened_global_scenario_rows(self):
        actor = self.actor()
        rows = flatten_single_talent_damage_variants({**actor, 'global_damage_states': []},
                                                    {**actor, 'global_damage_states': []}, [])
        actor['actions'] = rows
        product = project_skill_damage_product_payload({'actors': [actor]})['actors'][0]
        self.assertEqual(len(product['actions']), 4)
        self.assertFalse(any('buff.shared' in r['variant']['scenario_tokens'] for r in product['actions']))

    def test_variant_uses_its_own_global_declarations_without_shared_base_catalog(self):
        selected = self.actor()
        reference = copy.deepcopy(selected)
        reference['actions'][0]['scenarios'] = []
        item = {'talent': {'id': 1, 'node_id': 1, 'tree_type': 'spec'},
                'high': selected, 'low': selected, 'reference_high': reference, 'reference_low': reference}
        rows = flatten_single_talent_damage_variants({'actions': []}, {'actions': []}, [item])
        self.assertFalse(any('buff.shared' in row['variant']['scenario_tokens'] for row in rows))
        self.assertTrue(any(len(row['variant']['scenario_tokens']) == 2 for row in rows))

    def test_same_token_on_another_owner_scope_is_not_borrowed_as_global(self):
        actor = self.actor()
        other_scope = copy.deepcopy(actor['actions'][0]['scenarios'][0])
        other_scope['buffs'][0]['scope'] = 'target'
        actor['actions'][0]['scenarios'].append(other_scope)
        actor['actions'] = flatten_single_talent_damage_variants(actor, actor, [])
        rows = project_skill_damage_product_payload({'actors': [actor]})['actors'][0]['actions']
        self.assertEqual(len(rows), 5)
        retained = [c for row in rows for c in row['variant']['runtime_conditions'] if c['spell_id'] == 91001]
        self.assertEqual({c['scope'] for c in retained}, {'target'})
