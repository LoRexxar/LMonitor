import copy

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import (
    _compact_equivalent_damage_states,
    classify_global_skill_effects,
    flatten_single_talent_damage_variants,
    project_skill_damage_product_payload,
)


def damage_action(*, scenarios=(), native_base=None):
    def amount(hit):
        component = {
            'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
            'crit_chance': 0.2, 'expected': hit * 1.2,
            'damage_equivalent_count': 1.0,
            'runtime_layers': {'da_multiplier': hit / 100},
            'target_hit': {str(n): hit * n for n in (1, 2, 5, 10, 20)},
        }
        if native_base is not None:
            component['native_base_damage'] = native_base
        return {'direct': component, 'tick': None, 'unresolved_reason': None}
    return {
        'token': 'colossus_smash', 'spell_id': 167105,
        'supported': True, 'player_skill': True, 'harmful': True,
        'reporting_root_token': 'colossus_smash', 'reporting_root_spell_id': 167105,
        'reporting_root_component': True,
        'dbc_scaling': {'direct': {
            'attack_power_coefficient': 1.0, 'spell_power_coefficient': 0.0,
            'normalized_base': 100.0,
        }},
        'baseline': amount(100),
        'scenarios': [{'buffs': [{
            'token': token, 'scope': scope, 'spell_id': spell_id, 'stacks': 1,
        }], 'values': amount(hit)} for token, scope, spell_id, hit in scenarios],
    }


class SkillDamageSemanticRegressionTests(SimpleTestCase):
    def test_global_states_do_not_depend_on_root_count_low_health_or_translation(self):
        states = (
            ('buff.avatar', 'self', 107574, 120),
            ('debuff.colossus_smash', 'target', 208086, 130),
            ('buff.enrage', 'self', 184362, 150),
        )
        high = {'class': 'warrior', 'actions': [damage_action(scenarios=states)]}
        low = {'class': 'warrior', 'actions': []}
        effects = classify_global_skill_effects(high, low, [])
        self.assertEqual({effect['source_spell_ids'][0] for effect in effects}, {107574, 208086, 184362})
        rows = flatten_single_talent_damage_variants(high, low, [], global_effects=effects)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['spell_id'], 167105)
        self.assertEqual(rows[0]['variant']['scenario_tokens'], [])
        for count in (1, 3):
            varied = copy.deepcopy(high)
            varied['actions'] *= count
            self.assertEqual(
                {effect['effect_id'] for effect in classify_global_skill_effects(varied, low, [])},
                {effect['effect_id'] for effect in effects},
            )

    def test_scope_uses_actual_state_identity_and_preserves_local_modifiers(self):
        high = {'class': 'warrior', 'actions': [damage_action(scenarios=(
            ('buff.local', 'self', 999, 125),
            ('buff.avatar', 'target', 107574, 130),
        ))]}
        rows = flatten_single_talent_damage_variants(high, high, [], global_effects=[])
        self.assertEqual(len(rows), 3)
        other_class = {'class': 'mage', 'actions': [damage_action(scenarios=(
            ('buff.avatar', 'self', 107574, 120),
        ))]}
        self.assertEqual(len(flatten_single_talent_damage_variants(other_class, other_class, [])), 2)

    def test_variable_global_percent_never_uses_first_actor_as_universal_value(self):
        high = {'class': 'warrior', 'actions': [damage_action(scenarios=(
            ('buff.enrage', 'self', 184362, 150),
        ))]}
        low = {'class': 'warrior', 'actions': [damage_action(scenarios=(
            ('buff.enrage', 'self', 184362, 170),
        ))]}
        effects = classify_global_skill_effects(high, low, [])
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['projections'], [])
        self.assertEqual(effects[0]['value_status'], 'configuration_dependent_or_unresolved')
        self.assertFalse(any(row['variant']['scenario_tokens'] for row in
                             flatten_single_talent_damage_variants(high, low, [], global_effects=effects)))

    def test_equal_stack_damage_merges_only_with_same_multi_target_curve(self):
        def row(stacks, *, targets=200, talent=1):
            return {
                'token': 'bloodthirst', 'spell_id': 23881,
                'variant': {'talent_id': talent, 'scenario_tokens': ['buff.whirlwind'],
                            'runtime_conditions': [{'token': 'buff.whirlwind', 'scope': 'self',
                                                    'spell_id': 85739, 'stacks': stacks}]},
                'product': {'final_normalized_damage': 100,
                            'final_normalized_damage_by_target': {'1': 100, '2': targets}},
            }
        result = _compact_equivalent_damage_states([row(n) for n in (4, 3, 2, 1)])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['variant']['runtime_conditions'][0]['stack_values'], [1, 2, 3, 4])
        self.assertEqual(len(_compact_equivalent_damage_states([row(1), row(2, targets=220)])), 2)
        self.assertEqual(len(_compact_equivalent_damage_states([row(1), row(2, talent=2)])), 2)

    def test_formula_uses_independent_native_base_and_never_divides_by_zero(self):
        action = damage_action(native_base=50)
        direct = action['baseline']['direct']
        direct['runtime_layers']['da_multiplier'] = 2
        direct['product'] = {
            'dbc_base_damage_min': 100, 'dbc_base_damage_max': 100,
            'current_talent_damage': 100, 'crit_damage': 200,
            'crit_multiplier': 2, 'actual_crit_chance': .2, 'normalized_expected': 120,
        }
        formula = project_skill_damage_product_payload({'actors': [{'actions': [action]}]})['actors'][0]['actions'][0]['product']['formula_components'][0]
        self.assertEqual(formula['base_damage'], 50)
        self.assertEqual(formula['base_evidence'], 'native_action_coefficients')
        self.assertNotIn('status', formula)
        direct.pop('native_base_damage')
        direct['runtime_layers']['da_multiplier'] = 0
        formula = project_skill_damage_product_payload({'actors': [{'actions': [action]}]})['actors'][0]['actions'][0]['product']['formula_components'][0]
        self.assertEqual(formula['base_damage'], 100)
        self.assertEqual(formula['status'], 'incomplete')

    def test_dbc_declared_scope_is_available_without_any_successful_damage_probe(self):
        actor = {'class': 'mage', 'actions': [], 'global_damage_states': [{
            'token': 'buff.sample_power', 'scope': 'self', 'spell_id': 777777,
            'name': '示例全局增伤', 'evidence': 'dbc_all_school_damage_aura',
        }]}
        effects = classify_global_skill_effects(actor, actor, [])
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['source_spell_ids'], [777777])
        self.assertEqual(effects[0]['projections'], [])
        selected = copy.deepcopy(actor)
        selected['actions'] = [damage_action(scenarios=(
            ('buff.sample_power', 'self', 777777, 150),
        ))]
        rows = flatten_single_talent_damage_variants(selected, selected, [], global_effects=effects)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['variant']['scenario_tokens'], [])

    def test_dbc_scope_discovered_only_in_selected_talent_still_filters_stably(self):
        base = {'class': 'mage', 'actions': [damage_action()]}
        selected = {'class': 'mage', 'global_damage_states': [{
            'token': 'buff.sample_power', 'scope': 'self', 'spell_id': 777777,
            'name': '示例全局增伤', 'evidence': 'dbc_all_school_damage_aura',
        }], 'actions': [damage_action(scenarios=(('buff.sample_power', 'self', 777777, 150),))]}
        variants = [{'talent': {'id': 1}, 'high': selected, 'low': selected,
                     'reference_high': base, 'reference_low': base}]
        effects = classify_global_skill_effects(base, base, variants)
        rows = flatten_single_talent_damage_variants(base, base, variants, global_effects=effects)
        self.assertTrue(effects)
        self.assertFalse(any(row['variant']['scenario_tokens'] for row in rows))
