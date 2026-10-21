import copy
from unittest import TestCase
from types import SimpleNamespace
from botend.services.simc_skill_damage import _SpoolBackedTalentVariants
from botend.services.simc_skill_damage import flatten_single_talent_damage_variants
from botend.services.simc_skill_damage import project_skill_damage_product_payload
from django.test import SimpleTestCase
from botend.services.simc_skill_damage import single_talent_reference_entry


"""Absolute formulas must disclose the native reference talent configuration."""


def actor(multiplier, scenario=None):
    def amount(factor):
        return {'direct': {'hit': 100 * factor, 'crit': 200 * factor,
            'expected': 120 * factor, 'crit_multiplier': 2, 'crit_chance': .2,
            'damage_equivalent_count': 1, 'native_base_damage': 100,
            'runtime_layers': {'da_multiplier': factor}}, 'tick': None, 'unresolved_reason': None}
    action = {'token': 'measured', 'name': 'Measured', 'spell_id': 500,
        'supported': True, 'harmful': True, 'reporting_root_token': 'measured',
        'reporting_root_spell_id': 500, 'reporting_root_component': True,
        'dbc_scaling': {'direct': {'normalized_base': 100,
            'attack_power_coefficient': 1, 'spell_power_coefficient': 0}},
        'baseline': amount(multiplier), 'scenarios': []}
    if scenario is not None:
        action['scenarios'] = [{'buffs': [{'token': 'buff.measured', 'spell_id': 600,
            'scope': 'self', 'stacks': 1}], 'values': amount(scenario)}]
    return {'class': 'test', 'specialization': 'test', 'actions': [action]}


class ReferenceConditionContextTests(TestCase):
    def test_native_reference_selection_survives_flatten_and_product(self):
        reference = actor(1.35)
        selected = actor(1.35, scenario=1.485)
        reference['selected_trait_ids'] = [30, 40]
        selected['selected_trait_ids'] = [20, 30, 40]
        item = {'talent': {'id': 10, 'node_id': 20, 'name': 'Target', 'tree_type': 'spec'},
                'reference_traits': [{'trait_entry_id': 30, 'name': 'Prerequisite', 'name_zh': '固定前置'}],
                'reference_high': reference, 'reference_low': reference,
                'high': selected, 'low': selected}
        frozen = copy.deepcopy(item)
        rows = flatten_single_talent_damage_variants(reference, reference, [item])
        row = next(r for r in rows if r['variant']['trait_entry_id'] == 20)
        context = row['variant']['reference_context']
        self.assertEqual(context['trait_entry_ids'], [30, 40])
        self.assertEqual(context['source'], 'native_reference_selection')
        self.assertEqual(context['traits'][0]['name_zh'], '固定前置')
        self.assertEqual(context['traits'][1], {'trait_entry_id': 40})
        result = project_skill_damage_product_payload({'actors': [{'actions': [row]}]})
        output = result['actors'][0]['actions'][0]
        self.assertEqual(output['variant']['reference_context'], context)
        self.assertAlmostEqual(output['product']['noncrit_damage'], 148.5)
        self.assertEqual(item, frozen)

    def test_spool_freezes_metadata_from_the_planned_reference_config(self):
        def talent(pk, node_id, name):
            return SimpleNamespace(pk=pk, node_id=node_id, name=name, name_zh=name,
                description='', description_zh='', tree_type='spec', db2_subtree_id=None)
        target, parent = talent(10, 20, '目标'), talent(11, 30, '前置')
        aliases = {f'skill_damage_{kind}_10_trait_20': {'canonical_name': name,
                    'talent_effectiveness': state}
                   for kind, name, state in [('reference', 'reference', 'inactive'), ('talent', 'selected', 'active')]}
        plan = {'aliases': aliases, 'actors': [{'name': 'reference', 'selected_talents': [parent]},
                                               {'name': 'selected', 'selected_talents': [parent, target]}]}
        raw = {'reference': {'selected_trait_ids': [30], 'actions': []},
               'selected': {'selected_trait_ids': [30, 20], 'actions': []}}
        spool = SimpleNamespace(load=lambda health, name: copy.deepcopy(raw[name]))
        item = next(iter(_SpoolBackedTalentVariants(profile=SimpleNamespace(spec='fixture'),
            talents=[target], actor_plan=plan, actor_spool=spool)))
        self.assertIn({'trait_entry_id': 30, 'name': '前置', 'name_zh': '前置'}, item['reference_traits'])


"""天赋前置配置与伤害归因回归，避免共同启用的技能被误记成增伤。"""


class ConditionAttributionTests(SimpleTestCase):
    def action(self, damage=100):
        return {'token':'derived_strike','spell_id':101,'supported':True,'player_skill':True,
                'baseline':{'direct':{'hit':damage,'crit':damage*2,'expected':damage}},'scenarios':[]}

    def variant(self, selected, reference):
        return {'talent':{'id':2,'node_id':2,'tree_type':'hero','hero_subtree_id':60},
                'high':selected,'low':selected,'reference_high':reference,'reference_low':reference}

    def test_reference_retains_hero_selection_node(self):
        # 基础 actor 虽列出免费天赋，但未选英雄树，尚未创建该英雄树的伤害动作。
        selected={None:[10,11], 1:[10,11,90], 2:[10,11,90,2]}
        self.assertEqual(single_talent_reference_entry(2,selected),1)

    def test_same_subtree_is_not_enough_for_reference(self):
        self.assertIsNone(single_talent_reference_entry(2,{2:[10,90,2],3:[10,90,3]}))

    def test_shared_hero_action_not_attributed_to_unrelated_talent(self):
        base={'actions':[]}
        reference={'actions':[self.action()]}
        rows=flatten_single_talent_damage_variants(base,base,[self.variant(copy.deepcopy(reference),reference)])
        self.assertEqual(rows,[])

    def test_actual_derived_damage_change_is_preserved(self):
        base={'actions':[]}
        reference={'actions':[self.action()]}
        selected={'actions':[self.action(125)]}
        reference['actions'][0]['player_skill']=False
        selected['actions'][0]['player_skill']=False
        rows=flatten_single_talent_damage_variants(base,base,[self.variant(selected,reference)])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['baseline']['direct']['hit'],125)

    def test_uninitialized_cast_root_is_not_a_learned_skill(self):
        base={'actions':[]}
        reference={'actions':[self.action()]}
        selected={'actions':[self.action(125)]}
        for actor in (reference,selected):
            actor['actions'][0].update(player_skill=False,reporting_root_spell_id=0,reporting_root_token='未启用的父技能')
        self.assertEqual(flatten_single_talent_damage_variants(base,base,[self.variant(selected,reference)]),[])
