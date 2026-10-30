"""逐行关联回归：整套配点、同树、选择器命中都不能直接成为展示理由。"""
import copy
from unittest import TestCase
from botend.services.simc_skill_talent_links import collect_skill_talent_links
from botend.services.simc_skill_damage import project_skill_damage_product_payload, attach_runtime_product_metrics
from botend.tests.test_simc_reference_condition_context import actor


def fixture():
    action = actor(1.35)['actions'][0]
    action['variant'] = {'reference_context': {'source': 'native_reference_selection',
        'trait_entry_ids': [30, 40], 'traits': [
            {'trait_entry_id': 30, 'name_zh': '当前技能强化'},
            {'trait_entry_id': 40, 'name_zh': '无关前置'}]}}
    action['selected_trait_effects'] = [{'trait_entry_id': 30, 'source_spell_id': 300, 'effect_index': 0},
                                        {'trait_entry_id': 40, 'source_spell_id': 400, 'effect_index': 0}]
    action['baseline']['direct']['runtime_factor_sources'] = [{
        'source_spell_id': 300, 'effect_index': 1, 'factor': 1.35,
        'source_name': '当前技能强化',
        'evidence': 'native_multiplier_dataflow_v1', 'layer': 'da_multiplier'}]
    attach_runtime_product_metrics({'actions': [action]})
    return action


class SkillTalentLinksTests(TestCase):
    def test_only_actual_effect_source_is_related(self):
        action = fixture()
        frozen = copy.deepcopy(action)
        self.assertEqual([r['trait_entry_id'] for r in collect_skill_talent_links(action)], [30])
        row = project_skill_damage_product_payload({'actors': [{'actions': [action]}]})['actors'][0]['actions'][0]
        self.assertEqual([r['trait_entry_id'] for r in row['variant']['related_talents']], [30])
        self.assertNotIn('reference_context', row['variant'])
        self.assertNotIn('无关前置', str(row['variant']))
        self.assertAlmostEqual(row['product']['noncrit_damage'], 135)
        self.assertEqual(action, frozen)

    def test_selector_without_active_effect_or_other_effect_index_is_hidden(self):
        for updates in ({'factor': 1}, {'effect_index': 2}, {'source_spell_id': 400, 'effect_index': 2},
                        {'evidence': 'unverified'}, {'source_spell_id': 0}):
            with self.subTest(updates=updates):
                action = fixture()
                action['baseline']['direct']['runtime_factor_sources'][0].update(updates)
                self.assertEqual(collect_skill_talent_links(action), [])

    def test_unlock_context_is_filtered_per_root_not_per_build(self):
        action = fixture()
        action['variant']['activation_context'] = {'trait_entry_ids': [50, 60, 70],
            'traits': [{'trait_entry_id': e, 'name_zh': f'前置{e}'} for e in (50,60,70)],
            'action_spell_ids': [500, 600], 'description': '一大堆旧说明',
            'evidence': [{'trait_entry_id': 50, 'replacement_spell_id': 500},
                         {'trait_entry_id': 60, 'replacement_spell_id': 600}]}
        links = collect_skill_talent_links(action)
        self.assertEqual([r['trait_entry_id'] for r in links], [30,50])
        row = project_skill_damage_product_payload({'actors': [{'actions': [action]}]})['actors'][0]['actions'][0]
        self.assertEqual([r['trait_entry_id'] for r in row['variant']['related_talents']], [30,50])
        self.assertNotIn('activation_context', row['variant'])
        self.assertNotIn('旧说明', str(row['variant']))

    def test_unchanged_cast_child_contributes_its_own_links_without_splitting_cast(self):
        first, second = fixture(), fixture()
        first['selected_trait_effects'] = []
        second.update(token='child', spell_id=501)
        rows = project_skill_damage_product_payload({'actors': [{'actions': [first,second]}]})['actors'][0]['actions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['component_count'], 2)
        self.assertEqual(rows[0]['variant']['related_talents'][0]['evidence'][0]['skill_spell_id'], 501)
        self.assertAlmostEqual(rows[0]['product']['noncrit_damage'], 270)

    def test_component_without_a_displayable_formula_cannot_add_a_prerequisite(self):
        action = fixture()
        tick = copy.deepcopy(action['baseline']['direct'])
        tick['runtime_factor_sources'][0]['source_spell_id'] = 400
        tick['runtime_layers'] = {'ta_multiplier': 1.35}
        action['baseline']['tick'] = tick
        # 该周期分量没有 DBC 公式，不会进入这一行展示的伤害组成。
        row = project_skill_damage_product_payload({'actors': [{'actions': [action]}]})['actors'][0]['actions'][0]
        self.assertEqual([r['trait_entry_id'] for r in row['variant']['related_talents']], [30])
        self.assertEqual(row['component_count'], 1)
