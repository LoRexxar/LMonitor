"""作用域结论不能由名称、集合数量或缺少运行记录决定。"""
from collections import defaultdict
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock
import hashlib

module_path = Path(__file__).resolve().parents[2] / 'scripts/simc_scope_resolution.py'
spec = importlib.util.spec_from_file_location('scope_resolution', module_path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ScopeResolutionTests(unittest.TestCase):
    def setUp(self):
        self.r = module.ScopeResolver.__new__(module.ScopeResolver)
        self.r.revision = module.REVIEWED_REVISION
        self.r.spells = {1: {'_id':1, '_class_flags_family':4, '_class_flags':[512,0,0,0]},
                         2: {'_id':2, '_class_flags_family':4, '_class_flags':[512,0,0,0]}}
        self.r.local_groups = []
        self.r.flag_members = defaultdict(set)
        self.r.colors = {}
        self.r.damage = {1,2}
        self.r.skill_sets = []

    def effect(self, **kwargs):
        return {'type':6, 'subtype':108, 'misc1':0, 'misc2':0, 'flags':[512,0,0,0],
                'class_family':4, 'method':'dbc.effect_affects_spells',
                'affected_spells':[{'spell_id':1},{'spell_id':2}], **kwargs}

    def test_common_selector_precedes_concrete_group(self):
        self.r.local_groups=[(1,{1,2})]
        for name in ('任意天赋','只有一个技能','全部伤害'):
            e=self.effect(name=name,description=name)
            self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'应剔除')

    def test_target_debuff_uses_same_common_selector(self):
        e=self.effect(subtype=271,method='spell_data_t.affected_by_all')
        self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'应剔除')

    def test_target_debuff_bleed_category_is_global(self):
        self.r.flag_members[(4,3,16777216)]={1,2}
        e=self.effect(subtype=271,flags=[0,0,0,16777216],method='spell_data_t.affected_by_all')
        self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'应剔除')

    def test_target_debuff_specific_skill_set_is_local(self):
        self.r.damage_families={4:{1,2,3}}
        e=self.effect(subtype=271,flags=[1,0,0,0],method='spell_data_t.affected_by_all')
        self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'保留')

    def test_same_spell_periodic_damage_is_a_retained_local_component(self):
        self.r.by_spell={1:[{'_id':101,'_index':0,'_type':6,'_subtype':3,
            '_misc_value':0,'_misc_value_2':0,'_base_value':0,'_class_flags':[0,0,0,0]}]}
        own=self.r.own_damage_relations(1)
        self.assertEqual((own[0]['effect_id'],own[0]['effect_index']),(101,1))
        self.assertEqual(self.r.resolve(own[0],[],'技能本体伤害')[0],'保留')

    def test_incomplete_dbc_membership_not_certified_global(self):
        result=self.r.resolve(self.effect(affected_spells=[{'spell_id':1}]),[],'已解析技能应用关系')
        self.assertEqual(result[0],'待确认')

    def test_version_change_invalidates_reviewed_mask(self):
        self.r.revision='different'
        self.assertIsNone(self.r.resolve(self.effect(),[],'已解析技能应用关系'))

    def test_large_set_alone_does_not_prove_global(self):
        e=self.effect(class_family=100,flags=[1,0,0,0],affected_spells=[{'spell_id':i} for i in range(100)])
        self.assertIsNone(self.r.resolve(e,[],'已解析技能应用关系'))

    def test_union_of_explicit_skill_selectors_retained(self):
        self.r.flag_members[(100,0,1)]={1}
        self.r.flag_members[(100,0,2)]={2}
        result=self.r.resolve(self.effect(class_family=100,flags=[3,0,0,0]),[],'已解析技能应用关系')
        self.assertEqual(result[0],'保留')
        self.assertEqual(result[2]['完整DBC集合'],[1,2])

    def test_native_expansion_blocks_local_verdict(self):
        e=self.effect(class_family=100,flags=[1,0,0,0],affected_spells=[{'spell_id':1}])
        self.assertIsNone(self.r.resolve(e,[{'传递到的伤害技能':[1,3]}],'已解析技能应用关系'))

    def test_mechanic_and_guardian_auras_are_categories(self):
        for subtype in (163,276,303,531):
            e=self.effect(subtype=subtype,flags=[0,0,0,0],affected_spells=[],method='no_static_spell_selector')
            self.assertEqual(self.r.resolve(e,[],'需追踪手写逻辑')[0],'应剔除')

    def test_non_aura_numeric_subtype_not_global(self):
        e=self.effect(type=3,subtype=531,flags=[0,0,0,0],affected_spells=[],method='no_static_spell_selector')
        self.assertIsNone(self.r.resolve(e,[],'需追踪手写逻辑'))

    def certificate(self):
        text='struct first_skill {};\nstruct second_skill {};\n'
        self.r.source=Mock()
        self.r.source.__truediv__=Mock(return_value=Mock(read_text=Mock(return_value=text)))
        self.r.flag_members[(100,0,1)]={1,2}
        self.r.skill_sets=[{'职业族':100,'选择位':[1,0,0,0],
                           '技能集合':'两个独立技能','完整DBC集合':[1,2],
                           '源码身份':[{'文件':'技能.cpp','定位代码':'struct first_skill',
                                      '文本摘要':hashlib.sha256(text.encode()).hexdigest()}]}]

    def test_multiple_unrelated_skills_in_one_selector_retained(self):
        self.certificate()
        self.assertEqual(self.r.local_groups,[])
        result=self.r.resolve(self.effect(class_family=100,flags=[1,0,0,0]),[],
                              'DBC 范围已解析，原生应用未覆盖')
        self.assertEqual(result[0],'保留')
        self.assertEqual(result[2]['判定路径'],'完整具名技能集合')

    def test_certificate_does_not_ignore_extra_dbc_member(self):
        self.certificate()
        self.r.flag_members[(100,0,1)].add(3)
        e=self.effect(class_family=100,flags=[1,0,0,0],affected_spells=[{'spell_id':i} for i in (1,2,3)])
        self.assertIsNone(self.r.resolve(e,[],'已解析技能应用关系'))

    def test_changed_source_invalidates_skill_set_certificate(self):
        self.certificate()
        self.r.skill_sets[0]['源码身份'][0]['文本摘要']='过期'
        self.assertIsNone(self.r.resolve(self.effect(class_family=100,flags=[1,0,0,0]),[],'已解析技能应用关系'))

    def test_single_spell_label_does_not_require_current_configuration(self):
        e=self.effect(class_family=100,subtype=218,flags=[0,0,0,0],misc2=99,
                      method='dbc.spells_by_label',affected_spells=[{'spell_id':1}])
        self.assertEqual(self.r.resolve(e,[],'DBC 范围已解析，原生应用未覆盖')[0],'保留')

    def test_common_registry_precedes_multi_skill_certificate(self):
        self.certificate()
        self.assertEqual(self.r.resolve(self.effect(class_family=100,flags=[1,0,0,0]),[],
                                        '公共伤害乘区')[0],'应剔除')

    def test_partial_dbc_set_needs_no_manual_certificate_or_shared_root(self):
        self.r.damage={1,2,3}
        self.r.damage_families={100:{1,2,3}}
        e=self.effect(class_family=100,flags=[1,0,0,0])
        result=self.r.resolve(e,[],'DBC 范围已解析，原生应用未覆盖')
        self.assertEqual(result[0],'保留')
        self.assertEqual(result[2]['判定路径'],'DBC 部分技能选择器')
        self.assertEqual(result[2]['未命中的伤害技能'],[3])

    def test_partial_label_set_uses_same_rule(self):
        self.r.damage={1,2,3}
        self.r.damage_families={100:{1,2,3}}
        e=self.effect(class_family=100,subtype=218,flags=[0,0,0,0],misc2=80,method='dbc.spells_by_label')
        self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'保留')

    def test_complete_damage_set_is_global(self):
        self.r.damage_families={100:{1,2}}
        e=self.effect(class_family=100,flags=[1,0,0,0])
        self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'应剔除')

    def test_known_bleed_category_precedes_partial_set(self):
        self.r.damage_families={4:{1,2,3}}
        self.r.flag_members[(4,3,16777216)]={1,2}
        e=self.effect(flags=[0,0,0,16777216])
        self.assertEqual(self.r.resolve(e,[],'已解析技能应用关系')[0],'应剔除')

    def test_native_addition_that_remains_partial_is_retained(self):
        self.r.damage={1,2,3,4}
        self.r.damage_families={100:{1,2,3,4}}
        e=self.effect(class_family=100,flags=[1,0,0,0])
        result=self.r.resolve(e,[{'传递到的伤害技能':[1,2,3]}],'已解析技能应用关系')
        self.assertEqual(result[0],'保留')
        self.assertEqual(result[2]['原生附加技能'],[3])

    def test_native_expansion_to_all_damage_is_global(self):
        self.r.damage={1,2,3}
        self.r.damage_families={100:{1,2,3}}
        e=self.effect(class_family=100,flags=[1,0,0,0])
        self.assertEqual(self.r.resolve(e,[{'传递到的伤害技能':[1,2,3]}],'已解析技能应用关系')[0],'应剔除')
