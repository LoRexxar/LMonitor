"""验证同次施法补齐、条件隔离和完整全局目录，避免只测页面能加载。"""
import copy
from django.test import SimpleTestCase
from botend.services.simc_skill_damage import complete_cast_damage_components, reviewed_global_display_effects, project_skill_damage_product_payload
from botend.services.simc_skill_damage import _validate_global_scope_catalog, classify_global_skill_effects, _amount_change_only_global_projections
from botend.services.simc_skill_damage import flatten_single_talent_damage_variants
from botend.services.simc_skill_damage import _validate_native_action_coverage


class CastProjectionTests(SimpleTestCase):
    def test_native_coverage_detects_silently_missing_cast_component(self):
        part = self.part('主手', 30)
        ledger = {'token': '主手', 'spell_id': 1, 'root_token': '施法', 'root_spell_id': 100,
                  'status': 'exported_damage'}
        _validate_native_action_coverage({'actions': [part], 'action_coverage': [ledger]})
        with self.assertRaisesRegex(ValueError, '未进入结果'):
            _validate_native_action_coverage({'actions': [], 'action_coverage': [ledger]})
        with self.assertRaisesRegex(ValueError, '缺少原生处理记录'):
            _validate_native_action_coverage({'actions': [part], 'action_coverage': []})

    def test_same_child_spell_in_distinct_casts_is_not_dropped(self):
        first = self.part('相同流血', 30)
        second = copy.deepcopy(first)
        second.update(reporting_root_token='另一施法', reporting_root_spell_id=200)
        actor = {'actions': [first, second]}
        rows = flatten_single_talent_damage_variants(actor, actor, [])
        self.assertEqual(len(rows), 2)
        self.assertEqual({row['reporting_root_spell_id'] for row in rows}, {100, 200})

    def test_activation_condition_survives_unchanged_damage(self):
        action = self.part('替换技能', 30)
        required = {'token': 'buff.required', 'scope': 'self', 'spell_id': 500,
                    'name': '施法前提', 'stacks': 1}
        action['activation_conditions'] = [required]
        actor = {'actions': [action]}
        rows = flatten_single_talent_damage_variants(actor, actor, [])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['variant']['activation_conditions'], [required])
        self.assertEqual(rows[0]['variant']['runtime_conditions'], [])

    def global_state_actor(self, multiplier):
        return {'actions':[], 'global_damage_policy':'exclude_before_probe', 'global_damage_states':[{
            'token':'buff.example','scope':'self','spell_id':123,'name':'测试增伤',
            'available':True,'evidence':'precomputed_global_damage_scope',
            'scope_basis':'reviewed_dbc_native_effect_scope','excluded_before_probe':True,
            'dbc_base_multiplier':multiplier}]}

    def test_global_multiplier_range_preserves_verified_endpoints(self):
        rows=classify_global_skill_effects(self.global_state_actor(1.1),self.global_state_actor(1.3),[])
        self.assertEqual(len(rows),1)
        projection=rows[0]['projections'][0]
        self.assertEqual(projection['kind'],'damage_multiplier_range')
        self.assertEqual(projection['operation'],'display_only')
        self.assertEqual((projection['minimum'],projection['maximum']),(1.1,1.3))
        self.assertNotIn('value',projection)
        amount={'direct':{'hit':100.0,'crit':200.0,'expected':120.0},'tick':None}
        self.assertFalse(_amount_change_only_global_projections(amount,amount,rows[0]))

    def test_incomplete_global_evidence_does_not_claim_complete_range(self):
        rows=classify_global_skill_effects(self.global_state_actor(1.1),self.global_state_actor(None),[])
        self.assertEqual(rows[0]['projections'],[])
        self.assertEqual(rows[0]['value_status'],'configuration_dependent_or_unresolved')

    def test_fixed_global_multiplier_remains_scalar(self):
        rows=classify_global_skill_effects(self.global_state_actor(1.2),self.global_state_actor(1.2),[])
        self.assertEqual(rows[0]['projections'][0]['kind'],'damage_multiplier')
        self.assertEqual(rows[0]['projections'][0]['value'],1.2)

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

    def test_cast_completion_preserves_native_target_scope(self):
        main, off = self.part('主手',30), self.part('副手',20)
        changed = copy.deepcopy(main)
        changed['affected_target_counts'] = [2,5,10,20]
        rows = complete_cast_damage_components([changed], {id(changed):{'actions':[main,off]}})
        self.assertEqual(len(rows),2)
        self.assertTrue(all(row['affected_target_counts'] == [2,5,10,20] for row in rows))

    def test_flatten_keeps_multi_target_only_runtime_evidence(self):
        action = self.part('主手',100)
        action['player_skill'] = True
        condition = {'token':'buff.sweeping_strikes','scope':'self','spell_id':260708,'stacks':1}
        action['baseline']['direct'].update(expected=120, damage_equivalent_count=1,
            target_expected={str(n):120 for n in (1,2,5,10,20)})
        changed = copy.deepcopy(action['baseline'])
        changed['direct']['target_expected'].update({'2':180,'5':180,'10':180,'20':180})
        action['scenarios'] = [{'buffs':[condition],'values':changed,'affected_target_counts':[2,5,10,20]}]
        actor = {'actions':[action]}
        rows = flatten_single_talent_damage_variants(actor,actor,[])
        states = [row for row in rows if row['variant']['runtime_conditions']]
        self.assertEqual(len(states),1)
        self.assertEqual(states[0]['affected_target_counts'],[2,5,10,20])

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

    def test_catalog_preserves_runtime_multiplier_and_stack_conditions(self):
        detail = {'label':'直接伤害','base_value':10,'source_spell_id':1,'effect_index':1}
        fact = {'source_spell_ids':[1],'global_components':[{'spell_id':1,'effect_index':1,'effect_id':10}],
                'effect_details':[detail], 'projections':[]}
        runtime = {'source_spell_ids':[1], 'source_type':'runtime_state',
                   'runtime_conditions':[{'token':'buff.test','scope':'self','spell_id':1,'stacks':2}],
                   'projections':[{'kind':'damage_multiplier','value':1.2}], 'runtime_condition':'自身效果存在时'}
        actor = {'spec':'arms','reviewed_global_effects':[fact], 'global_skill_effects':[runtime]}
        before = copy.deepcopy(actor)
        rows = reviewed_global_display_effects(actor)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['projections'], runtime['projections'])
        self.assertEqual(rows[0]['runtime_conditions'], runtime['runtime_conditions'])
        self.assertEqual(rows[0]['effect_details'],[detail])
        self.assertEqual(actor,before)

    def test_global_runtime_state_absent_from_review_is_not_discarded(self):
        effect = {'source_type':'runtime_state','source_spell_ids':[184362],
                  'projections':[{'kind':'damage_multiplier','value':1.5}]}
        rows = reviewed_global_display_effects({'reviewed_global_effects':[], 'global_skill_effects':[effect]})
        self.assertEqual(rows,[effect])

    def test_shared_class_buff_does_not_leak_into_other_specialization(self):
        fact={'source_spell_ids':[184362],'specializations':['fury'],
              'global_components':[{'spell_id':76856,'effect_index':1,'effect_id':68045}]}
        runtime={'source_spell_ids':[184362],'source_type':'runtime_state','projections':[]}
        self.assertEqual(reviewed_global_display_effects({'spec':'arms','reviewed_global_effects':[fact],
                                                         'global_skill_effects':[runtime]}),[])

    def test_zero_base_value_cannot_hide_residual_global_mastery(self):
        component={'spell_id':76856,'effect_index':1,'effect_id':68045}
        actor={'global_scope_candidates':[],'global_damage_states':[],'scope_contract_sha256':'a'*64,
               'normalized_scope_effects':[{**component,'actual_base_value':0,'actual_mastery_coefficient':0.014}],
               'reviewed_global_effects':[{'global_components':[component]}]}
        with self.assertRaisesRegex(ValueError,'没有归零'):
            _validate_global_scope_catalog(actor)

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

        # 施法前提必须在最终列表呈现，但不能把未变化的状态伪装成额外增伤行。
        required = {'token': 'buff.required', 'scope': 'self', 'spell_id': 500, 'stacks': 1}
        for action in raw['actors'][0]['actions']:
            action['variant'] = {'activation_conditions': [required], 'runtime_conditions': []}
        rows = project_skill_damage_product_payload(raw)['actors'][0]['actions']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['variant']['runtime_conditions'], [required])
        self.assertEqual(rows[0]['product']['final_normalized_damage'], 120.0)
