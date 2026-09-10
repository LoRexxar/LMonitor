"""天赋前置配置与伤害归因回归，避免共同启用的技能被误记成增伤。"""
import copy
from django.test import SimpleTestCase
from botend.services.simc_skill_damage import (
    single_talent_reference_entry, flatten_single_talent_damage_variants,
)


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
