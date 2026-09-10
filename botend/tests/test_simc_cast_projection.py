"""验证同次施法补齐、条件隔离和完整全局目录，避免只测页面能加载。"""
import copy
from django.test import SimpleTestCase
from botend.services.simc_skill_damage import complete_cast_damage_components, reviewed_global_display_effects, project_skill_damage_product_payload


class CastProjectionTests(SimpleTestCase):
    def part(self, token, value, *, condition=''):
        return {'token':token,'spell_id':1,'supported':True,'reporting_root_component':True,
                'reporting_root_token':'施法','reporting_root_spell_id':100,
                'baseline':{'direct':{'hit':value}},'scenarios':[],
                'variant':{'talent_id':5,'runtime_condition':condition,'runtime_conditions':[], 'reference_available':True}}

    def test_one_changed_hand_includes_unchanged_other_hand(self):
        main, off = self.part('主手',30), self.part('副手',20)
        actor={'actions':[main,off]}
        changed=copy.deepcopy(main)
        rows=complete_cast_damage_components([changed],{id(changed):actor})
        self.assertEqual({r['token'] for r in rows},{'主手','副手'})
        self.assertEqual(sum(r['baseline']['direct']['hit'] for r in rows),50)

    def test_changed_middle_hit_restores_first_and_last(self):
        parts=[self.part(str(i),v) for i,v in enumerate([10,40,30])]
        changed=copy.deepcopy(parts[1])
        rows=complete_cast_damage_components([changed],{id(changed):{'actions':parts}})
        self.assertEqual(len(rows),3)
        self.assertEqual(sum(r['baseline']['direct']['hit'] for r in rows),80)

    def test_different_conditions_never_merge(self):
        parts=[self.part('主手',10),self.part('副手',20)]
        a,b=self.part('主手',30,condition='条件甲'),self.part('主手',40,condition='条件乙')
        rows=complete_cast_damage_components([a,b],{id(a):{'actions':parts},id(b):{'actions':parts}})
        self.assertEqual(len(rows),4)
        self.assertEqual(sum(r['baseline']['direct']['hit'] for r in rows if r['variant']['runtime_condition']=='条件甲'),50)
        self.assertEqual(sum(r['baseline']['direct']['hit'] for r in rows if r['variant']['runtime_condition']=='条件乙'),60)

    def test_global_catalog_does_not_depend_on_selected_traits(self):
        def fact(spell,spec):
            return {'source_spell_ids':[spell],'specializations':[spec],'global_components':[{'spell_id':spell,'effect_index':1,'effect_id':spell*10}]}
        actor={'spec':'arms','selected_trait_ids':[], 'reviewed_global_effects':[fact(1,'arms'),fact(2,'arms'),fact(3,'fury')]}
        rows=reviewed_global_display_effects(actor)
        self.assertEqual([r['source_spell_ids'] for r in rows],[[1],[2]])

    def test_talent_and_buff_same_effect_are_deduplicated(self):
        fact={'source_spell_ids':[1],'global_components':[{'spell_id':1,'effect_index':1,'effect_id':10}]}
        rows=reviewed_global_display_effects({'spec':'arms','reviewed_global_effects':[fact,copy.deepcopy(fact)]})
        self.assertEqual(len(rows),1)
        self.assertEqual(len(rows[0]['global_components']),1)

    def test_secondary_target_component_does_not_inflate_single_target_total(self):
        def part(token, secondary):
            values = {str(n):120.0 * (n-1 if secondary else 1) for n in (1,2,5,10,20)}
            return {'token':token,'spell_id':1,'supported':True,'reporting_root_component':True,
                'reporting_root_token':'blade_dance','reporting_root_spell_id':188499,
                'dbc_scaling':{'direct':{'attack_power_coefficient':1.0,'spell_power_coefficient':0.0}},
                'baseline':{'direct':{'single_target_eligible':not secondary,'damage_equivalent_count':1.0,
                    'native_base_damage':100.0,'product':{'dbc_base_damage_min':100.0,'dbc_base_damage_max':100.0,
                    'current_talent_damage':100.0,'crit_damage':200.0,'crit_multiplier':2.0,
                    'actual_crit_chance':0.2,'normalized_expected':120.0,'normalized_expected_by_target':values}}}}
        raw={'actors':[{'actions':[part('主目标',False),part('副目标',True)]}]}
        row=project_skill_damage_product_payload(raw)['actors'][0]['actions'][0]
        self.assertEqual(row['component_count'],2)
        self.assertEqual(row['product']['final_normalized_damage'],120.0)
        self.assertEqual(row['product']['final_normalized_damage_by_target']['2'],240.0)
        self.assertEqual(row['components'][1]['final_normalized_damage'],0.0)
        self.assertTrue(all(f.get('status') != 'incomplete' for f in row['product']['formula_components']))
