import copy
import json
from pathlib import Path
import re
import unittest
from scripts.build_simc_mastery_scope import extend_contract
from scripts.simc_native_scope_evidence import damage_function_spans
import os
import shutil
import subprocess
from scripts.build_simc_shared_scope import extend_shared_contract
from scripts.build_simc_shared_scope import extend_exporter
from scripts.build_simc_specialization_scope import classify_component
from scripts.build_simc_specialization_scope import extend_contract as specialization_scope_extend_contract


"""验证精通逐分量归一化、手写来源证据和旧审计器的接口兼容。"""



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


"""验证团队光环跨职业排除和全能归一化的真实实现。"""

class SharedScopeTests(unittest.TestCase):
    def test_shared_aura_preserves_real_family_and_displays_for_every_class(self):
        base='''constexpr const char* skill_damage_scope_sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";
struct skill_damage_scope_effect_t { unsigned spell, index, id, family; bool global; };
constexpr auto skill_damage_scope_effects[] = {
};
struct skill_damage_scope_display_t { unsigned family; const char* specialization; const char* json; };
constexpr auto skill_damage_scope_display[] = {
};
'''
        result=extend_shared_contract(base,effect_id=404,base_value=3)
        self.assertIn('{1126, 1, 404, 7, true, true}',result)
        fact=json.loads(json.loads(re.search(r'  \{7, "", (".*"), true\},',result)[1]))
        from botend.constants.simc_effect_ownership import global_effect_matches_owner
        for cls,spec in [('mage','fire'),('hunter','beast_mastery'),('druid','restoration')]:
            self.assertTrue(global_effect_matches_owner(fact,cls,spec))
        self.assertEqual(fact['effect_details'][0]['base_value'],3)
        self.assertEqual(fact['global_components'],[{'spell_id':1126,'effect_index':1,'effect_id':404}])
        with self.assertRaises(ValueError):extend_shared_contract(result,effect_id=404,base_value=3)
        with self.assertRaises(ValueError):extend_shared_contract(base,effect_id=405,base_value=3)

    @unittest.skipUnless(os.environ.get('SIMC_MULTIPLIER_SOURCE') and shutil.which('g++'),'需要原生源码与编译器')
    def test_actual_versatility_normalizer_removes_total_rating_and_buff_bonus(self):
        text=(Path(os.environ['SIMC_MULTIPLIER_SOURCE'])/'engine/sc_main.cpp').read_text(encoding='utf-8')
        match=re.search(r'void skill_damage_normalize_versatility\( player_t& player \)\s*\{[\s\S]*?\n\}',text)
        self.assertIsNotNone(match)
        output=Path(__file__).resolve().parents[2]/'tmp/simc-shared-native-tests';output.mkdir(exist_ok=True)
        cpp=output/'全能归一化.cpp';exe=output/'全能归一化.exe'
        cpp.write_text('''#include <cmath>
#include <stdexcept>
#include <cassert>
constexpr int CACHE_VERSATILITY=1;
struct player_t {
  struct { double versatility=0.04; } current;
  double rating_bonus=0.07,buff_bonus=0.03;
  struct cache_t { player_t* p; double damage_versatility() const { return p->current.versatility+p->rating_bonus+p->buff_bonus; } } cache{this};
  void invalidate_cache(int) {}
};
'''+match[0]+'''
int main() {
  player_t p; skill_damage_normalize_versatility(p);
  assert(std::abs(p.cache.damage_versatility())<1e-12);
  auto value=p.current.versatility; skill_damage_normalize_versatility(p);
  assert(std::abs(p.current.versatility-value)<1e-12);
  p.buff_bonus+=0.05; skill_damage_normalize_versatility(p);
  assert(std::abs(p.cache.damage_versatility())<1e-12);
}
''',encoding='utf-8')
        result=subprocess.run([shutil.which('g++'),'-std=c++17',str(cpp),'-o',str(exe)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        subprocess.run([str(exe)],check=True)


"""核对专精光环分量边界、版本漂移和上表与归一化使用同一事实。"""



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
        result = specialization_scope_extend_contract(self.base(), self.review)
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
        result = specialization_scope_extend_contract(self.base(), self.review)
        with self.assertRaises(ValueError):
            specialization_scope_extend_contract(result, self.review)
        review = copy.deepcopy(self.review)
        review['客户端版本'] = '漂移版本'
        with self.assertRaises(ValueError):
            specialization_scope_extend_contract(self.base(), review)
        base = self.base().replace('skill_damage_scope_effects[] = {',
                                   'skill_damage_scope_effects[] = {\n  {137050, 1, 179734, 4, false},')
        with self.assertRaises(ValueError):
            specialization_scope_extend_contract(base, self.review)
