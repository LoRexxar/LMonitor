import copy
from unittest import mock

from django.test import SimpleTestCase

from botend.constants.simc_specs import SIMC_KNOWN_SPECS
from botend.services.simc_skill_damage import (
    collect_skill_damage_unresolved, build_single_talent_actor_input, classify_global_skill_effects,
    flatten_single_talent_damage_variants,
)
from botend.tests.test_simc_skill_damage_expectation import project, skill


class SkillDamageAuditTests(SimpleTestCase):
    def test_flatten_does_not_copy_the_discarded_scenario_graph(self):
        class Scenarios(list):
            def __deepcopy__(self, memo):
                raise AssertionError('单行生成不应复制完整场景图')

        action = skill()
        action['scenarios'] = Scenarios()
        actor = {'actions': [action]}
        rows = flatten_single_talent_damage_variants(actor, actor, [])
        self.assertTrue(rows)
        self.assertEqual(rows[0]['scenarios'], [])
        self.assertIsInstance(action['scenarios'], Scenarios)

    def test_preclassified_export_never_reinfers_globals_from_skill_samples(self):
        actor = {'class': 'mage', 'spec': 'frost', 'global_damage_policy': 'exclude_before_probe',
                 'global_damage_states': [], 'global_scope_candidates': [], 'actions': [skill()]}
        with mock.patch('botend.services.simc_skill_damage._scenario_token_universe',
                        side_effect=AssertionError('不应重新扫描状态样本')):
            self.assertEqual(classify_global_skill_effects(actor, copy.deepcopy(actor), []), [])

    def test_every_physical_actor_uses_the_same_dbc_catalog_entry(self):
        profile = 'mage="原模板"\nspec=frost\nactions=unknown\nactions.burst+=/unknown\nuse_apl=burst\n'
        result = build_single_talent_actor_input(profile, 'mage', [], actor_plan=[
            {'name': 'first', 'selected_talents': []}, {'name': 'second', 'selected_talents': []},
        ])
        self.assertEqual(result.count('actions=wait'), 2)
        self.assertNotIn('unknown', result)
        self.assertNotIn('use_apl=', result)

    def test_audit_inventory_includes_healers_support_and_devourer(self):
        identities = {(name, spec) for name, specs in SIMC_KNOWN_SPECS.items() for spec in specs}
        self.assertEqual(len(identities), 40)
        self.assertTrue({('monk', 'mistweaver'), ('paladin', 'holy'),
                         ('priest', 'holy'), ('evoker', 'augmentation'),
                         ('demonhunter', 'devourer')} <= identities)

    def test_single_target_native_aoe_hook_is_part_of_formula(self):
        action = skill(hit=106, crit=212)
        amount = action['baseline']['direct']
        amount['native_base_damage'] = 100
        amount['runtime_layers']['aoe_multiplier'] = 1.06
        formula = project(action)['product']['formula_components'][0]
        self.assertEqual(formula['base_damage'], 100)
        self.assertEqual(formula['runtime_factors'], [1.06])
        self.assertAlmostEqual(formula['final_damage'], 127.2)
        self.assertNotIn('status', formula)

    def test_unresolved_damage_survives_projection_without_claiming_zero(self):
        action = skill()
        action.update(harmful=True, name='触发伤害')
        action['baseline']['unresolved_reason'] = 'runtime_damage_context_unavailable'
        action['scenarios'] = [{'values': copy.deepcopy(action['baseline'])}]
        harmless = copy.deepcopy(action)
        harmless.update(token='absorb', harmful=False)
        actor = {'class': 'monk', 'spec': 'windwalker', 'actions': [action, harmless]}
        payload = {'actors': [actor, copy.deepcopy(actor)]}
        rows = collect_skill_damage_unresolved(payload, target_health=34)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['action']['name'], '触发伤害')
        self.assertEqual(rows[0]['target_health_percentage'], 34)
        self.assertEqual(rows[0]['reason'], 'runtime_damage_context_unavailable')
        self.assertNotIn('damage', rows[0])
