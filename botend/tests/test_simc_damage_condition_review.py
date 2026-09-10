"""核对表必须以实际伤害差异筛选状态，不能按候选名称或可用性收录。"""
import unittest

from scripts.build_simc_damage_condition_review import build_rows


def action(*, state=False, expected=120, targets=None):
    return {'token':'test_spell', 'spell_id':100, 'name':'测试技能', 'supported':True,
            'variant':{'runtime_conditions':[{'token':'buff.test', 'spell_id':200, 'scope':'self'}] if state else [],
                       'runtime_condition':''},
            'product':{'normalized_base_damage':100, 'noncrit_damage':100, 'crit_damage':200,
                       'final_normalized_damage':expected,
                       'final_normalized_damage_by_target':targets or {'1':expected,'5':expected*5}},
            'components':[{'spell_id':100,'token':'test_spell','component':'direct',
                           'noncrit_damage':100,'crit_damage':200,'crit_chance':.2}]}


class DamageConditionReviewTests(unittest.TestCase):
    def rows(self, actions, **kwargs):
        return build_rows({'class':'mage','actions':actions}, config='测试配置', label='火焰', source='实测', **kwargs)

    def test_unchanged_buff_is_not_a_damage_row(self):
        self.assertEqual(len(self.rows([action(),action(state=True)])),1)

    def test_multi_target_only_difference_is_preserved(self):
        rows = self.rows([action(),action(state=True,targets={'1':120,'5':720})])
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['前']['final_normalized_damage_by_target']['5'],600)
        self.assertEqual(rows[1]['后']['final_normalized_damage_by_target']['5'],720)

    def test_equal_expectation_different_crit_is_preserved(self):
        changed = action(state=True)
        changed['product']['noncrit_damage']=80
        changed['product']['crit_damage']=280
        self.assertEqual(len(self.rows([action(),changed])),2)

    def test_same_total_different_components_is_preserved(self):
        changed = action(state=True)
        changed['components'][0]['component']='tick'
        self.assertEqual(len(self.rows([action(),changed])),2)

    def test_talent_no_effect_does_not_emit_rows(self):
        before = {'actions':[action()]}
        self.assertEqual(self.rows([action()],reference_actor=before,talent={'名称':'测试天赋'}),[])

    def test_missing_reference_is_not_zero_or_unchanged(self):
        rows = self.rows([action(state=True)])
        self.assertEqual(len(rows),1)
        self.assertIsNone(rows[0]['前'])
        self.assertEqual(rows[0]['关系'],'缺少同配置对照')

    def test_zero_damage_not_presented_as_damage_skill(self):
        self.assertEqual(self.rows([action(expected=0)]),[])

    def test_health_comparison_uses_same_skill_baseline(self):
        changed = action(expected=150)
        changed['variant']['runtime_condition']='血量低于35%'
        rows = self.rows([action(),changed])
        self.assertEqual(rows[1]['目标血量'],34)
        self.assertEqual(rows[1]['前']['final_normalized_damage'],120)
        self.assertEqual(rows[1]['关系'],'目标血量对照')


if __name__=='__main__':
    unittest.main()
