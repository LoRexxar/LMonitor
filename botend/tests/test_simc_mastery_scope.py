"""验证精通逐分量归一化、手写来源证据和旧审计器的接口兼容。"""
import copy
import json
from pathlib import Path
import re
import unittest

from scripts.build_simc_mastery_scope import extend_contract
from scripts.simc_native_scope_evidence import damage_function_spans


class MasteryScopeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.review=json.loads((Path(__file__).resolve().parents[2]/'scripts/simc_mastery_scope_1d3897.json').read_text(encoding='utf-8'))

    def base(self):
        return (f'constexpr auto revision="{self.review["源码提交"]}";\n'
                f'constexpr auto build="{self.review["客户端版本"]}";\n'
                f'constexpr const char* skill_damage_scope_sha256 = "{"a"*64}";\n'
                'constexpr auto skill_damage_scope_effects[] = {\n};\n'
                'constexpr auto skill_damage_scope_display[] = {\n};\n')

    def test_all_masteries_reviewed_without_removing_healing_or_local_parts(self):
        rows={r['专精ID']:r for r in self.review['条目']}
        self.assertEqual(len(rows),40)
        for spec in (65,105,264,270,1468):
            self.assertFalse(any(p['处理']=='剔除公共分量' for p in rows[spec]['分量']))
        hunter={p['效果编号']:p for p in rows[253]['分量']}
        self.assertEqual(hunter[1]['处理'],'剔除公共分量')
        self.assertEqual(hunter[5]['处理'],'保持原样')

    def test_handwritten_public_mastery_has_versioned_native_evidence(self):
        rows={r['法术ID']:r for r in self.review['条目']}
        for sid in (362980,343690,77220,115636):
            part=rows[sid]['分量'][0]
            self.assertEqual(part['处理'],'剔除公共分量')
            proof=part['手写证据']
            self.assertRegex(proof['摘要'],r'^[a-f0-9]{64}$')
            self.assertIn('engine/class_modules/',proof['文件'])
            self.assertTrue(proof['入口'] and proof['计算'] and proof['条件'])

    def test_upper_catalog_exactly_matches_excluded_effects(self):
        result=extend_contract(self.base(),self.review)
        excluded={(int(s),int(i),int(e)) for s,i,e in re.findall(r'\{(\d+), (\d+), (\d+), \d+, true\}',result)}
        facts=[]
        for line in result.splitlines():
            m=re.fullmatch(r'  \{\d+, "[^"]+", (".*")\},',line)
            if m:facts.append(json.loads(json.loads(m[1])))
        displayed={(p['spell_id'],p['effect_index'],p['effect_id']) for f in facts for p in f['global_components']}
        self.assertEqual(excluded,displayed)
        self.assertEqual(len(excluded),30)
        for fact in facts:
            self.assertTrue(fact['excluded_before_probe'])
            self.assertEqual(len(fact['effect_details']),len(fact['global_components']))
        evoker=next(f for f in facts if 362980 in f['source_spell_ids'])
        self.assertEqual(evoker['effect_details'][0]['normalized_mastery_percent'],50)
        self.assertIn('满血',evoker['effect_details'][0]['runtime_condition'])

    def test_conflicts_version_drift_and_duplicate_application_rejected(self):
        result=extend_contract(self.base(),self.review)
        with self.assertRaises(ValueError):extend_contract(result,self.review)
        review=copy.deepcopy(self.review);review['客户端版本']='其他版本'
        with self.assertRaises(ValueError):extend_contract(self.base(),review)
        base=self.base().replace('skill_damage_scope_effects[] = {','skill_damage_scope_effects[] = {\n  {362980, 1, 902053, 224, false},')
        with self.assertRaises(ValueError):extend_contract(base,self.review)

    def test_source_audit_recognizes_traced_template_and_target_methods(self):
        text='''template<class Base>
damage_multiplier_t action_t<Base>::composite_target_ta_multiplier(player_t* target) const {
  // 不把注释中的右括号 } 当成函数结束
  return 1.1;
}
double action_t::composite_da_multiplier() const { return 1.2; }
'''
        spans=damage_function_spans(text)
        self.assertEqual([s[2] for s in spans],['action_t<Base>::composite_target_ta_multiplier','action_t::composite_da_multiplier'])
        self.assertIn('return 1.1;',text[spans[0][0]:spans[0][1]])
