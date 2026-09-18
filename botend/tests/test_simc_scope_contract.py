"""作用域契约必须拒绝冲突，并按分量保留混合状态。"""
import copy
import unittest
from scripts.build_simc_scope_contract import compile_contract, render_contract, display_details


class ScopeContractTests(unittest.TestCase):
    def review(self):
        def part(index, decision):
            return {'源法术ID':31884, '效果编号':index, '效果ID':100+index, '处理结论':decision,
                    'DBC':{'source_spell_id':31884,'effect_index':index,'effect_id':100+index,'class_family':10}}
        return {'源码提交':'a'*40,'客户端版本':'12.1.0.69587','条目':[
            {'类型':'自身Buff','法术ID':31884,'分量':[part(1,'应剔除'),part(12,'保留')]}]}

    def test_mixed_buff_keeps_local_component(self):
        effects, _, buffs = compile_contract(self.review())
        self.assertTrue(effects[(31884,1)][2])
        self.assertFalse(effects[(31884,12)][2])
        self.assertEqual(buffs[31884], [True,True])

    def test_talent_and_target_state_share_all_self_components(self):
        review=self.review()
        talent=copy.deepcopy(review['条目'][0])
        talent['类型']='天赋'
        talent['分量']=talent['分量'][1:]
        review['条目'].append(talent)
        _,parents,_=compile_contract(review)
        self.assertIn((31884,31884,1),parents)
        self.assertNotIn((31884,31884,12),parents)

    def test_conflicting_component_is_rejected(self):
        review = self.review()
        duplicate = copy.deepcopy(review['条目'][0])
        duplicate['分量'][0]['处理结论'] = '保留'
        review['条目'].append(duplicate)
        with self.assertRaisesRegex(ValueError,'冲突'):
            compile_contract(review)

    def test_unresolved_and_wrong_dbc_identity_are_rejected(self):
        for field, value in [('处理结论','待确认'),('效果ID',999)]:
            review = self.review()
            review['条目'][0]['分量'][0][field] = value
            with self.assertRaises(ValueError):
                compile_contract(review)

    def test_description_cannot_change_contract(self):
        review = self.review()
        original = render_contract(review, 'b'*64)
        review['条目'][0]['描述仅供参考'] = '提高所有技能伤害'
        self.assertEqual(render_contract(review, 'b'*64), original)

    def test_display_contract_carries_numeric_specialization_scope(self):
        review = self.review()
        review['条目'][0].update({'职业': '战士', '专精': '狂怒', '名称':'测试'})
        rendered = render_contract(review, 'b'*64)
        self.assertIn('specializations', rendered)
        self.assertIn('fury', rendered)
        self.assertIn('"fury",', rendered)
        self.assertNotIn('arms', rendered)

    def test_display_contract_narrows_precreated_target_state_to_talent_owner(self):
        review = self.review()
        review['条目'][0].update({'类型': '目标减益', '职业': '法师', '专精': '冰霜、奥术、火焰',
                                  '法术ID': 210824, '名称': '大法师之触'})
        review['条目'][0]['分量'][0]['源法术ID'] = 210824
        review['条目'][0]['分量'][0]['DBC'].update(
            source_spell_id=210824, class_family=3,
        )
        rendered = render_contract(review, 'b' * 64)
        self.assertIn('{3, "arcane"', rendered)
        self.assertNotIn('{3, "fire"', rendered)
        self.assertNotIn('{3, "frost"', rendered)

    def test_conditional_mastery_keeps_buff_switch_and_quantitative_source(self):
        review=self.review()
        row=review['条目'][0]
        row['分量']=row['分量'][:1]
        part=row['分量'][0]
        part['DBC'].update(subtype=108,misc1=0,base_value=0,mastery_scaled=True,mastery_coefficient=0.014)
        part['原生实际应用']=[{'layer':'conditional_action_registry','conditional':True}]
        _,_,buffs=compile_contract(review)
        self.assertEqual(buffs[31884],[True,True])
        detail=display_details(row)[0]
        self.assertEqual(detail['value_kind'],'mastery')
        self.assertEqual(detail['coefficient'],0.014)

    def test_critical_chance_and_damage_components_keep_distinct_units(self):
        row=self.review()['条目'][0]
        for part,subtype,prop,value in zip(row['分量'],[107,108],[7,0],[20,15]):
            part['处理结论']='应剔除'
            part['DBC'].update(subtype=subtype,misc1=prop,base_value=value)
        details=display_details(row)
        self.assertEqual([d['value_kind'] for d in details],['percentage_points','percent'])
        self.assertEqual([d['base_value'] for d in details],[20,15])
