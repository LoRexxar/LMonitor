import copy
from unittest import mock
from django.test import SimpleTestCase
from botend.constants.simc_specs import SIMC_KNOWN_SPECS
from botend.services.simc_skill_damage import collect_skill_damage_unresolved
from botend.services.simc_skill_damage import build_single_talent_actor_input
from botend.services.simc_skill_damage import classify_global_skill_effects
from botend.services.simc_skill_damage import flatten_single_talent_damage_variants
from botend.tests.test_simc_skill_damage_expectation import project
from botend.tests.test_simc_skill_damage_expectation import skill, with_target_crit_evidence
from unittest import TestCase
from scripts.generate_simc_reviewed_talent_probes import talent_probe_catalog


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
        # Rebuild this synthetic fixture's source evidence after changing a layer.
        with_target_crit_evidence({'actors': [{'actions': [action]}]})
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


"""验证排除表不会退化为主动技能和局部天赋的保留白名单。"""


class TalentProbeCatalogTests(TestCase):
    def test_unlisted_active_and_passive_remain_and_mixed_global_is_preserved(self):
        catalog = {'talent_catalog': [
            {'trait_entry_id': i, 'name': f'测试天赋{i}', 'passive': i != 4}
            for i in range(1, 6)
        ]}
        review = {'条目': [
            {'类型': '天赋', '节点': 2, '名称': '全局被动', '分量': [{'处理结论': '应剔除'}]},
            {'类型': '天赋', '节点': 3, '名称': '混合被动', '分量': [{'处理结论': '应剔除'}, {'处理结论': '保留'}]},
            {'类型': '天赋', '节点': 4, '名称': '带全局效果的主动技能', '分量': [{'处理结论': '应剔除'}]},
        ]}
        selected = talent_probe_catalog(catalog, review)
        self.assertEqual([r['trait_entry_id'] for r in selected], [1, 3, 4, 5])
        self.assertEqual(selected[0]['display_name'], '测试天赋1')
        self.assertEqual(selected[1]['display_name'], '混合被动')

    def test_empty_exclusion_table_retains_entire_catalog(self):
        rows = [{'trait_entry_id': 1, 'name': '未复核主动技能', 'passive': False}]
        self.assertEqual(len(talent_probe_catalog({'talent_catalog': rows}, {'条目': []})), 1)

    def test_non_player_tree_and_empty_dbc_placeholder_are_not_skills(self):
        rows = [{'trait_entry_id': 1, 'name': '非职业条目', 'class_id': 0},
                {'trait_entry_id': 2, 'name': 'PvP条目', 'tree_index': 4},
                {'trait_entry_id': 3, 'name': '空占位', 'spell_id': 0}]
        self.assertEqual(talent_probe_catalog({'talent_catalog': rows}, {'条目': []}), [])

    def test_runtime_damage_missing_dbc_formula_is_reported(self):
        action = {'token': '测试伤害', 'spell_id': 1, 'supported': True, 'harmful': True,
                  'baseline': {'direct': {'hit': 20}}, 'dbc_scaling': {'direct': None}}
        payload = {'actors': [{'class': 'warrior', 'spec': 'fury', 'actions': [action]}]}
        self.assertEqual(collect_skill_damage_unresolved(payload)[0]['reason'], 'dbc_damage_effect_unresolved')
        action['dbc_scaling']['direct'] = {'attack_power_coefficient': 0.2}
        self.assertEqual(collect_skill_damage_unresolved(payload), [])
