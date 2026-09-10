"""原生关系审计必须依赖 DBC 和实际登记字段，描述与名称不改变结论。"""
import copy
import importlib.util
from pathlib import Path
import unittest

path=Path(__file__).resolve().parents[2]/'scripts/simc_native_scope_evidence.py'
spec=importlib.util.spec_from_file_location('native_scope_evidence',path)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class NativeScopeEvidenceTests(unittest.TestCase):
    def test_numeric_spell_registration_is_traced_to_damage_read(self):
        from unittest.mock import patch
        code='''
spell.target_debuff = find_spell( 208086 );
double composite_target_multiplier(player_t* t) const override {
 return 1 + p()->spell.target_debuff->effectN( 1 ).percent();
}
'''
        with patch.object(Path,'rglob',return_value=[Path('engine/class_modules/sc_warrior.cpp')]), patch.object(Path,'read_text',return_value=code):
            reads=module.index_native_reads(Path('.'),{'talent_catalog':[]})
            self.assertEqual(reads[(208086,1)][0]['伤害函数'],'composite_target_multiplier')

    def relation(self):
        return {'effect_id':1,'effect_index':1,'source_spell_id':10,'type':6,'subtype':108,
                'misc1':0,'base_value':20,'method':'dbc.effect_affects_spells',
                'affected_spells':[{'spell_id':100,'dbc_direct_damage':True,'dbc_periodic_damage':False}]}

    def binding(self):
        return {'effect_id':1,'effect_index':1,'source_spell_id':10,'target_spell_id':100,
                'layer':'passive_action','field':'direct_damage'}

    def test_description_and_name_do_not_affect_scope(self):
        relation=self.relation()
        expected=module.structural_scope(relation,[self.binding()])
        for description in ('Your damage increased by 20%.','Fireball damage increased by 20%.','完全没有描述'):
            r={**relation,'description':description,'name':'任意名字'}
            self.assertEqual(module.structural_scope(r,[self.binding()]),expected)

    def test_damage_uses_native_field_and_target(self):
        self.assertTrue(module.binding_has_damage(self.binding(),{1:self.relation()},set()))
        for field in ('cost','healing','cooldown','effect_1'):
            self.assertFalse(module.binding_has_damage({**self.binding(),'field':field},{1:self.relation()},{100}))

    def test_generic_modifier_on_healing_spell_is_not_damage(self):
        relation=self.relation()
        relation['affected_spells'][0]['dbc_direct_damage']=False
        self.assertFalse(module.relation_has_damage(relation))
        self.assertFalse(module.binding_has_damage(self.binding(),{1:relation},set()))

    def test_small_or_large_dbc_set_does_not_prove_global(self):
        for size in (1,2,100):
            r=self.relation()
            r['affected_spells']*=size
            self.assertEqual(module.structural_scope(r,[])[0],'DBC 范围已解析，原生应用未覆盖')

    def test_native_public_multiplier_has_structural_scope(self):
        b={**self.binding(),'target_spell_id':0,'layer':'player_registry','field':'player_multiplier'}
        self.assertEqual(module.structural_scope(self.relation(),[b])[0],'公共伤害乘区')

    def test_target_public_and_pet_multipliers_need_no_buff_object(self):
        for field in ('target_multiplier','pet_multiplier'):
            b={**self.binding(),'target_spell_id':0,'layer':'target_player_registry','field':field,'buff_spell_id':0}
            self.assertTrue(module.binding_has_damage(b,{1:self.relation()},set()))
            self.assertEqual(module.structural_scope(self.relation(),[b])[0],'公共伤害乘区')

    def test_reject_normalized_or_broken_source_link(self):
        data={'evidence_schema':1,'normalized':False,'actors':[{'source_effects':[self.relation()],'bindings':[self.binding()]}]}
        module.validate_native_payload(data)
        for changed in ({**data,'normalized':True},{**data,'evidence_schema':0}):
            with self.assertRaises(ValueError):module.validate_native_payload(changed)
        data['actors'][0]['bindings'][0]['effect_id']=999
        with self.assertRaises(ValueError):module.validate_native_payload(data)

    def test_handwritten_function_boundaries_ignore_comments_and_other_methods(self):
        text='''// double fake::action_multiplier() { }
double pet::composite_player_critical_damage_multiplier(int school) const {
  if (condition) { result += talent.effectN(2).percent(); }
  return result;
}
double pet::resource_regen() const { return talent.effectN(1).percent(); }
'''
        spans=module.damage_function_spans(text)
        self.assertEqual(len(spans),1)
        self.assertEqual(spans[0][2],'pet::composite_player_critical_damage_multiplier')
        self.assertNotIn('resource_regen',text[spans[0][0]:spans[0][1]])

    def test_follow_actual_effect_rewrite_to_damage_and_ignore_unconnected_effect(self):
        modifier={**self.relation(),'effect_id':2,'source_spell_id':20}
        unrelated={**modifier,'effect_id':3,'source_spell_id':30}
        link={**self.binding(),'effect_id':2,'source_spell_id':20,'target_spell_id':10,
              'layer':'passive_effect','field':'effect_1'}
        actor={'source_effects':[self.relation(),modifier,unrelated],'damage_actions':[],
               'bindings':[self.binding(),link,{**link,'effect_id':3,'source_spell_id':30,'target_spell_id':999}]}
        found=module.native_damage_bindings(actor)
        self.assertEqual({b['effect_id'] for b in found},{1,2})
        self.assertEqual(next(b for b in found if b['effect_id']==2)['传递到的伤害技能'],[100])


    def test_target_count_modifier_is_damage_relevant_only_for_damage_spells(self):
        for prop in (17,40):
            relation={**self.relation(),'subtype':107,'misc1':prop,'base_value':1}
            self.assertTrue(module.relation_has_damage(relation))
            relation['affected_spells']=[]
            self.assertFalse(module.relation_has_damage(relation))

    def test_integer_target_count_hook_is_scanned(self):
        spans=module.damage_function_spans('int n_targets() const override { return 2; }')
        self.assertEqual(len(spans),1)
        self.assertEqual(spans[0][2],'n_targets')


if __name__=='__main__':unittest.main()
