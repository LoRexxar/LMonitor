"""直接编译原生选择器规则，防止把漏收 Buff 或全局效果当作局部顺劈。"""
import os
import shutil
import subprocess
from pathlib import Path
import unittest


class NativeTargetScopeTests(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('SIMC_SKILL_DAMAGE_SOURCE') and shutil.which('g++'), '需要原生源码与编译器')
    def test_direct_repetition_change_is_a_damage_condition(self):
        source=Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE']).read_text(encoding='utf-8')
        structs=source[source.index('struct skill_damage_runtime_layers_t'):source.index('struct skill_damage_dbc_scaling_t')]
        start=source.index('bool skill_damage_amount_changed(')
        function=source[start:source.index('\n}',start)+2]
        harness='#include <map>\n#include <vector>\n#include <string>\n#include <cmath>\n#include <algorithm>\n#include <cassert>\n'+structs+function+r'''
int main(){
 skill_damage_amount_t before,after;
 before.direct=after.direct=true;
 before.direct_amount.expected=after.direct_amount.expected=100;
 before.direct_amount.damage_equivalent_count=7;
 after.direct_amount.damage_equivalent_count=8;
 assert(skill_damage_amount_changed(before,after));
 after.direct_amount.damage_equivalent_count=7;
 assert(!skill_damage_amount_changed(before,after));
}
'''
        out=Path(__file__).resolve().parents[2]/'.cache/simc-target-selector-tests'
        out.mkdir(parents=True,exist_ok=True)
        cpp,exe=out/'direct_repetition.cpp',out/'direct_repetition.exe'
        cpp.write_text(harness,encoding='utf-8')
        result=subprocess.run([shutil.which('g++'),'-std=c++17',str(cpp),'-o',str(exe)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(subprocess.run([str(exe)]).returncode,0)

    @unittest.skipUnless(os.environ.get('SIMC_SKILL_DAMAGE_SOURCE') and shutil.which('g++'), '需要原生源码与编译器')
    def test_unreviewed_buff_availability_uses_ownership_and_talent_state(self):
        source = Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE']).read_text(encoding='utf-8')
        start = source.index('bool skill_damage_available_buff(')
        function = source[start:source.index('\n}', start) + 2]
        harness = r"""
#include <map>
#include <vector>
#include <tuple>
#include <string>
#include <cassert>
template<class T> T as(int v){return static_cast<T>(v);}
namespace dbc {int get_class_spell_family(int){return 4;}}
namespace util {int class_id(int){return 1;} std::string tokenize_fn(const std::string& s){return s;}}
struct spell_t {int family=4; unsigned spell=100; int class_family()const{return family;} unsigned id()const{return spell;}};
struct buff_t {spell_t spell; double default_chance=1; std::string name_str="未收录状态"; const auto& data()const{return spell;}};
struct player_t {int type=1; std::vector<std::tuple<int,unsigned,int>> player_traits; bool is_ptr()const{return false;}};
struct trait_data_t {
 unsigned id_class=1, id_spell=101, id_trait_node_entry=10; std::string name="天赋状态";
 static const auto& data(bool){static const std::vector<trait_data_t> rows{trait_data_t{}};return rows;}
};
// FUNCTION
int main(){
 player_t p; buff_t unreviewed, external, disabled, unselected, selected;
 assert(skill_damage_available_buff(p,unreviewed));
 external.spell.family=6; assert(!skill_damage_available_buff(p,external));
 disabled.default_chance=0; assert(!skill_damage_available_buff(p,disabled));
 unselected.spell.spell=101; assert(!skill_damage_available_buff(p,unselected));
 selected.spell.spell=101; p.player_traits.emplace_back(2,10,1); assert(skill_damage_available_buff(p,selected));
}
"""
        out = Path(__file__).resolve().parents[2] / '.cache/simc-target-selector-tests'
        out.mkdir(parents=True, exist_ok=True)
        cpp, exe = out / 'buff_availability.cpp', out / 'buff_availability.exe'
        cpp.write_text(harness.replace('// FUNCTION', function), encoding='utf-8')
        result = subprocess.run([shutil.which('g++'), '-std=c++17', str(cpp), '-o', str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = subprocess.run([str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get('SIMC_SKILL_DAMAGE_SOURCE') and shutil.which('g++'),'需要原生源码与编译器')
    def test_actual_native_target_selector(self):
        source=Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE']).read_text(encoding='utf-8')
        snippets=[]
        for marker in ('bool skill_damage_global_aura(', 'bool skill_damage_local_target_buff('):
            start=source.index(marker)
            snippets.append(source[start:source.index('\n}',start)+2])
        harness=r"""
#include <vector>
#include <cassert>
constexpr unsigned NUM_CLASS_FAMILY_FLAGS=4;
enum {E_APPLY_AURA=6,A_ADD_FLAT_MODIFIER=107,P_CHAIN_TARGETS=17,P_MAX_TARGETS=40,
      A_MOD_DAMAGE_FROM_CASTER=270,A_MOD_DAMAGE_PERCENT_TAKEN=87,A_MOD_DAMAGE_PERCENT_DONE=79};
struct effect_t {
 int aura=107, property=17; double value=1; unsigned mask=1;
 int type() const{return E_APPLY_AURA;} int subtype() const{return aura;}
 int misc_value1() const{return property;} double base_value() const{return value;}
 double percent() const{return value/100;} unsigned class_flags(unsigned i) const{return i==0?mask:0;}
};
using spelleffect_data_t=effect_t;
struct spell_t {
 std::vector<effect_t> rows; unsigned mask=1;
 const auto& effects() const{return rows;} bool affected_by(const effect_t& e) const{return (mask&e.mask)!=0;}
};
struct buff_t {spell_t spell; const auto& data() const{return spell;}};
struct action_t {spell_t spell; const auto& data() const{return spell;}};
// FUNCTIONS
int main(){
 buff_t b; b.spell.rows.push_back(effect_t{}); action_t a;
 assert(skill_damage_local_target_buff(b)); assert(skill_damage_local_target_buff(b,&a));
 a.spell.mask=2; assert(!skill_damage_local_target_buff(b,&a));
 b.spell.rows[0].mask=0; assert(!skill_damage_local_target_buff(b));
 b.spell.rows[0].mask=1; b.spell.rows[0].value=0; assert(!skill_damage_local_target_buff(b));
 b.spell.rows[0].value=1; b.spell.rows[0].property=P_MAX_TARGETS; assert(skill_damage_local_target_buff(b));
 effect_t global; global.aura=A_MOD_DAMAGE_PERCENT_DONE; global.property=127; global.value=20; global.mask=0;
 b.spell.rows.push_back(global); assert(!skill_damage_local_target_buff(b));
}
"""
        out=Path(__file__).resolve().parents[2]/'.cache/simc-target-selector-tests'
        out.mkdir(parents=True,exist_ok=True)
        cpp=out/'target_selector.cpp';exe=out/'target_selector.exe'
        cpp.write_text(harness.replace('// FUNCTIONS','\n'.join(snippets)),encoding='utf-8')
        result=subprocess.run([shutil.which('g++'),'-std=c++17',str(cpp),'-o',str(exe)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        result=subprocess.run([str(exe)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
