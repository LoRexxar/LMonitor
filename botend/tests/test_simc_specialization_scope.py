"""核对专精光环分量边界、版本漂移和上表与归一化使用同一事实。"""
import copy
import json
from pathlib import Path
import re
import unittest

from scripts.build_simc_specialization_scope import classify_component, extend_contract


class SpecializationScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review = json.loads((Path(__file__).resolve().parents[2] /
                                 'scripts/simc_specialization_scope_1d3897.json').read_text(encoding='utf-8'))

    def base(self):
        return (f'constexpr auto revision = "{self.review["源码提交"]}";\n'
                f'constexpr auto build = "{self.review["客户端版本"]}";\n'
                f'constexpr const char* skill_damage_scope_sha256 = "{"a"*64}";\n'
                'constexpr auto skill_damage_scope_effects[] = {\n};\n'
                'constexpr auto skill_damage_scope_display[] = {\n};\n')

    def test_public_part_removed_local_same_spell_part_kept(self):
        rows = {r['法术ID']:r for r in self.review['条目']}
        for sid, public, local in [(137025,1,12), (137033,1,15), (137050,1,10)]:
            parts = {p['效果编号']:p for p in rows[sid]['分量']}
            self.assertEqual(parts[public]['处理'], '剔除公共分量')
            self.assertEqual(parts[local]['处理'], '保持原样')
        # 戒律的治疗调谐和伤害调谐是同一光环里的不同效果。
        priest = {p['效果编号']:p for p in rows[137032]['分量']}
        self.assertEqual(priest[1]['处理'], '保持原样')
        self.assertEqual(priest[4]['处理'], '剔除公共分量')

    def test_only_reviewed_public_selectors_and_damage_categories_match(self):
        effect = {'_type':6, '_subtype':108, '_misc_value':0, '_class_flags':[512,0,0,0]}
        self.assertIsNotNone(classify_component(4, effect))
        effect['_class_flags'] = [128,0,0,0]
        self.assertIsNone(classify_component(4, effect))
        effect.update(_subtype=344, _class_flags=[0,0,0,0])
        self.assertIsNotNone(classify_component(4, effect))

    def test_contract_catalog_exactly_covers_exclusions(self):
        result = extend_contract(self.base(), self.review)
        entries = re.findall(r'\{(\d+), (\d+), (\d+), (\d+), true\}', result)
        excluded = {(int(s),int(i),int(e)) for s,i,e,_ in entries}
        catalog = []
        for line in result.splitlines():
            match = re.fullmatch(r'  \{\d+, "[^"]+", (".*")\},', line)
            if match:
                catalog.append(json.loads(json.loads(match[1])))
        displayed = {(p['spell_id'],p['effect_index'],p['effect_id']) for r in catalog for p in r['global_components']}
        self.assertEqual(excluded, displayed)
        self.assertEqual(len(catalog), 40)
        self.assertEqual(len(excluded), 177)
        self.assertEqual(len(entries), len(excluded))
        for row in catalog:
            self.assertEqual(len(row['specializations']),1)
            self.assertTrue(row['excluded_before_probe'])

    def test_drift_conflict_and_repeated_application_fail_closed(self):
        result = extend_contract(self.base(), self.review)
        with self.assertRaises(ValueError):
            extend_contract(result, self.review)
        review = copy.deepcopy(self.review)
        review['客户端版本'] = '漂移版本'
        with self.assertRaises(ValueError):
            extend_contract(self.base(), review)
        base = self.base().replace('skill_damage_scope_effects[] = {',
                                   'skill_damage_scope_effects[] = {\n  {137050, 1, 179734, 4, false},')
        with self.assertRaises(ValueError):
            extend_contract(base, self.review)
