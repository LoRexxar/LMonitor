"""验证排除表不会退化为主动技能和局部天赋的保留白名单。"""
from unittest import TestCase
from scripts.generate_simc_reviewed_talent_probes import talent_probe_catalog
from botend.services.simc_skill_damage import collect_skill_damage_unresolved


class TalentProbeCatalogTests(TestCase):
    def test_unlisted_active_and_passive_remain_and_mixed_global_is_preserved(self):
        catalog = {'talent_catalog': [
            {'trait_entry_id': i, 'name': f'测试天赋{i}', 'passive': i != 4}
            for i in range(1, 6)
        ]}
        review = {'条目': [
            {'类型': '天赋', '节点': 2, '名称': '全局被动', '分量': [{'处理结论': '应剔除'}]},
            {'类型': '天赋', '节点': 3, '名称': '混合被动', '分量': [{'处理结论': '应剔除'}, {'处理结论': '保留'}]},
            {'类型': '天赋', '节点': 4, '名称': '带全局效果的主动技能', '分量': [{'处理结论': '应剔除'}]},
        ]}
        selected = talent_probe_catalog(catalog, review)
        self.assertEqual([r['trait_entry_id'] for r in selected], [1, 3, 4, 5])
        self.assertEqual(selected[0]['display_name'], '测试天赋1')
        self.assertEqual(selected[1]['display_name'], '混合被动')

    def test_empty_exclusion_table_retains_entire_catalog(self):
        rows = [{'trait_entry_id': 1, 'name': '未复核主动技能', 'passive': False}]
        self.assertEqual(len(talent_probe_catalog({'talent_catalog': rows}, {'条目': []})), 1)

    def test_non_player_tree_and_empty_dbc_placeholder_are_not_skills(self):
        rows = [{'trait_entry_id': 1, 'name': '非职业条目', 'class_id': 0},
                {'trait_entry_id': 2, 'name': 'PvP条目', 'tree_index': 4},
                {'trait_entry_id': 3, 'name': '空占位', 'spell_id': 0}]
        self.assertEqual(talent_probe_catalog({'talent_catalog': rows}, {'条目': []}), [])

    def test_runtime_damage_missing_dbc_formula_is_reported(self):
        action = {'token': '测试伤害', 'spell_id': 1, 'supported': True, 'harmful': True,
                  'baseline': {'direct': {'hit': 20}}, 'dbc_scaling': {'direct': None}}
        payload = {'actors': [{'class': 'warrior', 'spec': 'fury', 'actions': [action]}]}
        self.assertEqual(collect_skill_damage_unresolved(payload)[0]['reason'], 'dbc_damage_effect_unresolved')
        action['dbc_scaling']['direct'] = {'attack_power_coefficient': 0.2}
        self.assertEqual(collect_skill_damage_unresolved(payload), [])
