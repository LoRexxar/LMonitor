"""验证团队光环跨职业排除和全能归一化的真实实现。"""
import json,os,re,shutil,subprocess,unittest
from pathlib import Path
from scripts.build_simc_shared_scope import extend_shared_contract,extend_exporter

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
