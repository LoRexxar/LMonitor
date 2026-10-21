import copy
from django.test import SimpleTestCase
from botend.services.simc_skill_damage import flatten_single_talent_damage_variants
from botend.services.simc_skill_damage import project_skill_damage_product_payload
import unittest
from scripts.build_simc_damage_condition_review import build_rows


"""A health-dependent baseline is not an effect of a no-op talent or Buff."""




TARGETS = (1, 2, 5, 10, 20)
BUFF = {'token': 'buff.test_condition', 'scope': 'self', 'spell_id': 902, 'stacks': 1}
TALENT = {'id': 900, 'node_id': 901, 'name': 'Measured talent', 'tree_type': 'spec'}


def amount(hit):
    component = {
        'hit': hit, 'crit': hit * 2, 'expected': hit * 1.2,
        'crit_multiplier': 2, 'crit_chance': 0.2, 'can_crit': True,
        'damage_equivalent_count': 1, 'native_base_damage': 100,
        'base_damage_layers': {'base_multiplier': 1, 'component_multiplier': 1},
        'runtime_layers': {'da_multiplier': 1, 'target_da_multiplier': hit / 100},
    }
    for field, value in (
        ('hit', hit), ('crit', hit * 2), ('expected', hit * 1.2),
        ('noncrit_contribution', hit * 0.8), ('crit_contribution', hit * 0.4),
    ):
        component['target_' + field] = {str(n): value * n for n in TARGETS}
    return {'direct': component, 'tick': None, 'unresolved_reason': None}


def actor(hit, *, buff_hit=None):
    action = {
        'token': 'measured', 'name': 'Measured', 'spell_id': 903,
        'harmful': True, 'supported': True, 'player_skill': True,
        'reporting_root_token': 'measured', 'reporting_root_spell_id': 903,
        'reporting_root_component': True,
        'dbc_scaling': {'direct': {
            'normalized_base': 100, 'attack_power_coefficient': 1,
            'spell_power_coefficient': 0,
        }},
        'baseline': amount(hit), 'scenarios': [],
    }
    if buff_hit is not None:
        action['scenarios'] = [{'buffs': [copy.deepcopy(BUFF)], 'values': amount(buff_hit)}]
    return {'actions': [action]}


def variant(reference_high, reference_low, selected_high, selected_low):
    return {
        'talent': TALENT, 'reference_high': reference_high, 'reference_low': reference_low,
        'high': selected_high, 'low': selected_low,
    }


class ZeroMarginalConditionTests(SimpleTestCase):
    def product_rows(self, high, low, variants=()):
        frozen = copy.deepcopy((high, low, variants))
        flat = flatten_single_talent_damage_variants(high, low, variants, global_effects=[])
        rows = project_skill_damage_product_payload({'actors': [{'actions': flat}]})['actors'][0]['actions']
        self.assertEqual((high, low, variants), frozen)
        return rows

    def test_health_change_does_not_become_a_noop_talent_effect(self):
        high, low = actor(100), actor(150)
        rows = self.product_rows(high, low, [variant(high, low, copy.deepcopy(high), copy.deepcopy(low))])
        self.assertEqual([row['variant']['trait_entry_id'] for row in rows], [None, None])
        self.assertEqual([row['product']['noncrit_damage'] for row in rows], [100, 150])
        self.assertEqual(rows[1]['variant']['runtime_condition'], '血量低于35%')

    def test_health_change_does_not_become_a_noop_buff_effect(self):
        rows = self.product_rows(actor(100, buff_hit=100), actor(150, buff_hit=150))
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(not row['variant']['runtime_conditions'] for row in rows))
        self.assertEqual([row['product']['noncrit_damage'] for row in rows], [100, 150])

    def test_existing_buff_damage_does_not_get_attributed_to_noop_talent(self):
        high, low = actor(100, buff_hit=120), actor(150, buff_hit=180)
        rows = self.product_rows(high, low, [variant(high, low, copy.deepcopy(high), copy.deepcopy(low))])
        self.assertTrue(all(row['variant']['trait_entry_id'] is None for row in rows))
        buff_rows = [row for row in rows if row['variant']['scenario_tokens']]
        self.assertEqual(len(buff_rows), 2)
        self.assertEqual([row['product']['noncrit_damage'] for row in buff_rows], [120, 180])

    def test_real_low_health_talent_effect_survives(self):
        high, low = actor(100), actor(150)
        rows = self.product_rows(high, low, [variant(high, low, actor(100), actor(180))])
        attributed = [r for r in rows if r['variant']['trait_entry_id'] == TALENT['node_id']]
        self.assertEqual(len(attributed), 1)
        self.assertEqual(attributed[0]['variant']['runtime_condition'], '血量低于35%')
        self.assertEqual(attributed[0]['product']['noncrit_damage'], 180)

    def test_real_low_health_buff_effect_survives(self):
        rows = self.product_rows(actor(100, buff_hit=100), actor(150, buff_hit=180))
        conditions = [r for r in rows if r['variant']['scenario_tokens']]
        self.assertEqual(len(conditions), 1)
        self.assertEqual(conditions[0]['variant']['scenario_tokens'], [BUFF['token']])
        self.assertEqual(conditions[0]['product']['noncrit_damage'], 180)

    def test_aoe_only_low_health_effect_is_not_removed(self):
        high, low, changed = actor(100), actor(150), actor(150)
        component = changed['actions'][0]['baseline']['direct']
        for field, value in (('hit', 360), ('crit', 720), ('expected', 432),
                             ('noncrit_contribution', 288), ('crit_contribution', 144)):
            component['target_' + field]['2'] = value
        rows = self.product_rows(high, low, [variant(high, low, actor(100), changed)])
        attributed = [r for r in rows if r['variant']['trait_entry_id'] == TALENT['node_id']]
        self.assertEqual(len(attributed), 1)
        self.assertEqual(attributed[0]['product']['noncrit_damage'], 150)
        self.assertEqual(attributed[0]['product']['final_normalized_damage_by_target']['2'], 432)

    def test_high_health_effect_disappearing_at_low_health_is_still_a_change(self):
        high, low = actor(100), actor(150)
        rows = self.product_rows(high, low, [variant(high, low, actor(120), actor(150))])
        attributed = [r for r in rows if r['variant']['trait_entry_id'] == TALENT['node_id']]
        self.assertEqual(len(attributed), 2)
        self.assertEqual([r['product']['noncrit_damage'] for r in attributed], [120, 150])
        self.assertEqual(attributed[1]['variant']['runtime_condition'], '血量低于35%')


"""核对表必须以实际伤害差异筛选状态，不能按候选名称或可用性收录。"""



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
