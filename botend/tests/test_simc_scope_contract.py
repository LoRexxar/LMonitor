"""作用域契约必须拒绝冲突，并按分量保留混合状态。"""
import copy
import unittest
from scripts.build_simc_scope_contract import compile_contract, render_contract


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
