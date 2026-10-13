"""验证独立来源、覆盖丢弃和协议拒绝，不按合并数值猜来源。"""
import copy
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from botend.services.simc_skill_factor_sources import (
    factor_source_layers, formula_factor_rows, validate_factor_sources, validate_excluded_sources,
)


def factor(value, spell=101, name='来源甲'):
    return {'layer': 'da_multiplier', 'factor': value, 'source_spell_id': spell,
            'effect_index': 1, 'source_name': name, 'evidence': 'native_multiplier_dataflow_v1',
            'source_kind': 'spell_effect'}


class FactorSourcesTests(unittest.TestCase):
    def component(self):
        return {'runtime_layers': {'da_multiplier': 1.21, 'player_multiplier': 1.0},
                'runtime_factor_sources': [factor(1.1), factor(1.1, 102, '来源乙')]}

    def test_same_value_distinct_sources_remain_independent(self):
        result = formula_factor_rows(self.component(), 'direct')
        self.assertEqual([row['factor'] for row in result], [1.1, 1.1, 1.0])
        self.assertEqual([r['source_spell_id'] for r in result[:2]], [101, 102])

    def test_scenario_uses_final_native_sources_not_aggregate_difference(self):
        component = self.component()
        component['runtime_factor_layers'] = [{'layer': 'da_multiplier', 'factor': 1.21}]
        self.assertEqual(formula_factor_rows(component, 'direct')[:2], component['runtime_factor_sources'])

    def all_layers(self):
        component = {'runtime_layers': {key: 1.0 for key in factor_source_layers('direct')},
                     'runtime_factor_source_layers': factor_source_layers('direct'),
                     'runtime_factor_sources': []}
        for layer, values in {'player_multiplier': [1.1, 1.1],
                              'target_da_multiplier': [1.2], 'persistent_multiplier': [1.3]}.items():
            import math
            component['runtime_layers'][layer] = math.prod(values)
            component['runtime_factor_sources'].extend(
                {**factor(value, 201+i), 'layer': layer} for i, value in enumerate(values))
        return component

    def test_all_layers_preserve_distinct_sources_without_duplicate_scenario_layers(self):
        component = self.all_layers()
        component['runtime_factor_layers'] = [
            {'layer':'player_multiplier', 'factor':1.0},
            {'layer':'player_multiplier', 'factor':1.21},
            {'layer':'target_da_multiplier', 'factor':1.2},
            {'layer':'persistent_multiplier', 'factor':1.3},
        ]
        validate_factor_sources(component, 'direct', require_complete=True)
        self.assertEqual([r['factor'] for r in formula_factor_rows(component, 'direct')], [1.1,1.1,1.2,1.3])

    def test_equal_total_cannot_hide_wrong_layer_sources(self):
        component = self.all_layers()
        component['runtime_layers']['target_da_multiplier'] = 1.3
        component['runtime_layers']['persistent_multiplier'] = 1.2
        with self.assertRaises(ValueError):
            validate_factor_sources(component, 'direct', require_complete=True)

    def test_handwritten_factors_need_precise_safe_source_location(self):
        component=self.all_layers()
        source=component['runtime_factor_sources'][0]
        source.update(source_spell_id=0,effect_index=0,source_name='',source_kind='native_operation',
                      source_file='engine/class_modules/sc_evoker.cpp',source_line=4557)
        validate_factor_sources(component,'direct',require_complete=True)
        for field,value in [('source_file','../engine/a.cpp'),('source_file','C:/private/a.cpp'),
                            ('source_line',0),('source_line',True),('source_kind','spell_effect')]:
            broken=copy.deepcopy(component);broken['runtime_factor_sources'][0][field]=value
            with self.assertRaises(ValueError):validate_factor_sources(broken,'direct',require_complete=True)

    def test_raid_versatility_cannot_return_even_with_matching_sources(self):
        component=self.all_layers()
        component['runtime_layers']['versatility']=1.03
        component['runtime_factor_sources'].append({**factor(1.03),'layer':'versatility'})
        with self.assertRaisesRegex(ValueError,'全能'):
            validate_factor_sources(component,'direct',require_complete=True)

    @unittest.skipUnless(os.environ.get('SIMC_MULTIPLIER_SOURCE') and shutil.which('g++'), '需要原生源码与编译器')
    def test_handwritten_native_arithmetic_preserves_distinct_origins_and_names(self):
        header=Path(os.environ['SIMC_MULTIPLIER_SOURCE'])/'engine/action/damage_multiplier.hpp'
        output=Path(__file__).resolve().parents[2]/'tmp/simc-handwritten-native-tests'
        output.mkdir(exist_ok=True)
        cpp=output/'手写来源.cpp';exe=output/'手写来源.exe'
        cpp.write_text('#include "'+header.as_posix()+'"\n'+'''#include <cassert>
#include <cmath>
int main() {
  damage_multiplier_t::enabled=true;
  auto a=native_damage_factor(1.1);
  auto b=native_damage_factor(1.1);
  auto value=a*b;
  assert(value.factors.size()==2);
  assert(value.factors[0].line!=value.factors[1].line);
  assert(std::abs(double(value)-1.21)<1e-12);
  auto named=native_damage_factor(damage_factor(1.2,102,2,"具名来源"));
  assert(named.factors[0].spell_id==102);
  auto original=value;
  value=1.5;
  assert(value.factors.size()==1 && original.factors.size()==2);
  damage_multiplier_t::enabled=false;
  assert(native_damage_factor(1.1).factors.empty());
}
''',encoding='utf-8')
        result=subprocess.run([shutil.which('g++'),'-std=c++17',str(cpp),'-o',str(exe)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        subprocess.run([str(exe)],check=True)

    @unittest.skipUnless(os.environ.get('SIMC_MULTIPLIER_SOURCE'), '需要原生源码')
    def test_damage_hooks_do_not_erase_inherited_sources_with_double_locals(self):
        import re
        from scripts.simc_native_scope_evidence import damage_function_spans
        root=Path(os.environ['SIMC_MULTIPLIER_SOURCE'])/'engine'
        failures=[]
        for file in root.rglob('*'):
            if file.suffix not in ('.cpp','.hpp'):continue
            text=file.read_text(encoding='utf-8')
            for start,end,name in damage_function_spans(text):
                if not text[start:end].startswith('damage_multiplier_t'):continue
                if re.search(r'\bdouble\s+\w+\s*=\s*[^;\n]*\b(?:composite_\w*multiplier|action_(?:da_|ta_)?multiplier)\s*\(',text[start:end]):
                    failures.append((file.relative_to(root).as_posix(),name))
        self.assertEqual(failures,[])

    def test_new_export_requires_complete_unique_coverage(self):
        for change in (lambda c:c.pop('runtime_factor_source_layers'),
                       lambda c:c['runtime_factor_source_layers'].pop(),
                       lambda c:c['runtime_factor_source_layers'].append('player_multiplier')):
            component = self.all_layers()
            change(component)
            with self.assertRaises(ValueError):
                validate_factor_sources(component, 'direct', require_complete=True)

    def test_global_leak_in_target_scenario_is_rejected(self):
        component = self.all_layers()
        actor = {'normalized_scope_effects': [{'spell_id':201, 'effect_index':1}],
                 'actions': [{'scenarios': [{'values': {'direct':component}}]}]}
        with self.assertRaises(ValueError):
            validate_excluded_sources(actor)

    def test_missing_wrong_component_and_forged_sources_rejected(self):
        for mutate in (lambda c: c.pop('runtime_factor_sources'),
                       lambda c: c['runtime_factor_sources'][0].update(factor=1.2),
                       lambda c: c['runtime_factor_sources'][0].update(evidence='parser_consistency'),
                       lambda c: c['runtime_factor_sources'][0].update(effect_index=0)):
            component = self.component()
            mutate(component)
            with self.assertRaises(ValueError): validate_factor_sources(component, 'direct')
        with self.assertRaises(ValueError): validate_factor_sources(self.component(), 'tick')

    def test_legacy_factors_are_preserved_without_fabricated_sources(self):
        component = self.component()
        component.pop('runtime_factor_sources')
        self.assertEqual(formula_factor_rows(component, 'direct')[0], {'layer': 'da_multiplier', 'factor': 1.21})

    def test_global_exclusion_declaration_cannot_hide_actual_leak(self):
        actor = {'normalized_scope_effects': [{'spell_id': 101, 'effect_index': 1}],
                 'actions': [{'baseline': {'direct': self.component()}}]}
        with self.assertRaisesRegex(ValueError, '全局效果'):
            validate_excluded_sources(actor)
        actor['normalized_scope_effects'][0]['effect_index'] = 2
        validate_excluded_sources(actor)

    def test_product_projection_preserves_sources_and_damage(self):
        from botend.services.simc_skill_damage import project_skill_damage_product_payload
        from botend.tests.test_simc_skill_passive_projection import native_execute_actor
        actor = native_execute_actor()
        for action in actor['actions']:
            action['baseline']['direct']['runtime_factor_sources'] = [factor(1.22), factor(1.25, 102, '来源乙')]
        original = copy.deepcopy(actor)
        result = project_skill_damage_product_payload({'actors': [actor]})['actors'][0]
        self.assertEqual(actor, original)
        components = [c for a in result['actions'] for c in a['product']['formula_components']]
        self.assertTrue(components)
        for component in components:
            self.assertEqual(component['runtime_factors'], [1.22, 1.25])
            self.assertEqual([f['source_name'] for f in component['runtime_factor_sources']], ['来源甲', '来源乙'])

    @unittest.skipUnless(shutil.which('g++'), '需要 C++ 编译器')
    def test_native_value_propagation_discards_overwritten_sources(self):
        patch = (Path(__file__).resolve().parents[2] / 'simc_patches/0065-trace-native-damage-multipliers.patch').read_text(encoding='utf-8')
        section = patch.split('+++ b/engine/action/damage_multiplier.hpp\n', 1)[1].split('\ndiff --git ', 1)[0]
        header = '\n'.join(line[1:] for line in section.splitlines() if line.startswith('+'))
        output = Path(__file__).resolve().parents[2] / 'tmp/simc-factor-native-tests'
        output.mkdir(parents=True, exist_ok=True)
        cpp = output / '来源传播.cpp'
        cpp.write_text(header + '''
#include <cassert>
#include <cmath>
int main() {
  damage_multiplier_t::enabled=true;
  auto a=damage_factor(1.1,101,1,"甲");
  auto b=damage_factor(1.1,102,1,"乙");
  auto combined=a*b;
  assert(combined.factors.size()==2);
  assert(std::abs(double(combined)-1.21)<1e-12);
  auto copied=combined;
  combined=1.21;
  assert(combined.factors.size()==1 && combined.factors[0].spell_id==0);
  assert(copied.factors.size()==2);
  copied*=2;
  assert(copied.factors.size()==3);
  copied/=2;
  assert(std::abs(double(copied)-1.21)<1e-12);
  a*=a;
  assert(a.factors.size()==2);
  a+=0.1;
  assert(a.factors.size()==1 && a.factors[0].spell_id==0);
  damage_multiplier_t::enabled=false;
  assert(damage_multiplier_t(1.1).factors.empty());
}
''', encoding='utf-8')
        exe = output / ('来源传播.exe' if os.name == 'nt' else '来源传播')
        result = subprocess.run([shutil.which('g++'), '-std=c++17', str(cpp), '-o', str(exe)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run([str(exe)], check=True)

    @unittest.skipUnless(os.environ.get('SIMC_MULTIPLIER_SOURCE') and shutil.which('g++'),
                         '需要已应用补丁的原生源码和 C++ 编译器')
    def test_actual_native_cache_and_school_selection_keep_matching_sources(self):
        root = Path(os.environ['SIMC_MULTIPLIER_SOURCE'])
        header = (root / 'engine/action/damage_multiplier.hpp').read_text(encoding='utf-8')

        def function(path, marker):
            text = (root / path).read_text(encoding='utf-8')
            start = text.index(marker)
            end = text.index('{', start) + 1
            depth = 1
            while depth:
                depth += (text[end] == '{') - (text[end] == '}')
                end += 1
            return text[start:end]

        snippets = '\n'.join([
            function('engine/player/player_stat_cache.cpp',
                     'damage_multiplier_t player_stat_cache_t::player_multiplier('),
            function('engine/action/action.cpp',
                     'damage_multiplier_t action_t::composite_player_multiplier('),
        ])
        harness = r'''
#include <array>
#include <cassert>
#include <cmath>
using school_e = unsigned;
struct player_t;
struct action_state_t {};
struct player_stat_cache_t {
  player_t* player;
  bool active = true;
  mutable std::array<bool, 2> player_mult_valid{false,false};
  mutable std::array<damage_multiplier_t, 2> _player_mult;
  damage_multiplier_t player_multiplier(school_e) const;
};
struct player_t {
  std::array<damage_multiplier_t,2> source;
  player_stat_cache_t cache{this};
  damage_multiplier_t composite_player_multiplier(school_e s) const { return source[s]; }
};
struct action_t {
  player_t* player;
  std::vector<school_e> base_schools{0,1};
  damage_multiplier_t composite_player_multiplier(const action_state_t*) const;
};
// 原生函数
int main() {
  damage_multiplier_t::enabled = true;
  player_t p;
  p.source[0] = damage_factor(1.1,101,1,"甲") * damage_factor(1.1,102,1,"乙");
  p.source[1] = damage_factor(1.4,103,1,"丙");
  auto first = p.cache.player_multiplier(0);
  assert(first.factors.size()==2);
  p.source[0] = damage_factor(1.1,201,1,"新甲") * damage_factor(1.1,202,1,"新乙");
  auto hit = p.cache.player_multiplier(0);
  assert(hit.factors[0].spell_id==101);
  p.cache.player_mult_valid[0] = false;
  auto refreshed = p.cache.player_multiplier(0);
  assert(refreshed.factors.size()==2 && refreshed.factors[0].spell_id==201);
  assert(first.factors[0].spell_id==101);
  action_t action{&p};
  auto selected = action.composite_player_multiplier(nullptr);
  assert(selected.factors.size()==1 && selected.factors[0].spell_id==103);
  p.source[1] = damage_factor(1.05,303,1,"新丙");
  p.cache.player_mult_valid[1] = false;
  auto changed = action.composite_player_multiplier(nullptr);
  assert(changed.factors.size()==2 && changed.factors[0].spell_id==201);
  assert(std::abs(double(changed)-1.21)<1e-12);
}
'''
        output = Path(__file__).resolve().parents[2] / 'tmp/simc-factor-native-tests'
        output.mkdir(parents=True, exist_ok=True)
        cpp = output / '缓存与学校来源.cpp'
        cpp.write_text(header + harness.replace('// 原生函数', snippets), encoding='utf-8')
        exe = output / ('缓存与学校来源.exe' if os.name == 'nt' else '缓存与学校来源')
        result = subprocess.run([shutil.which('g++'), '-std=c++17', str(cpp), '-o', str(exe)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        subprocess.run([str(exe)], check=True)
