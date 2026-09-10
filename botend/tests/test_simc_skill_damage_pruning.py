import copy
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest import skipUnless

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService,
    _validate_global_scope_catalog,
    classify_global_skill_effects,
    plan_unique_talent_actor_configs,
    prune_global_damage_talents,
)


class SkillDamageNativePruningTests(SimpleTestCase):
    @skipUnless(os.environ.get('SIMC_SKILL_DAMAGE_SOURCE') and shutil.which('g++'), '需要指定已应用补丁的 SimC 源文件及 g++')
    def test_actual_cpp_scenarios_exclude_globals_before_expansion(self):
        source = Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE']).read_text(encoding='utf-8')
        if '#include "skill_damage_scope_contract.inc"' in source:
            self._assert_reviewed_native_scope(source)
            return

        def definition(marker, suffix=''):
            start = source.index(marker)
            opening = source.index('{', start)
            end = source.index('}', opening) + 1 if marker.startswith('enum ') else source.index('\n}', opening) + 2
            return source[start:end] + suffix

        snippets = [definition('enum class skill_damage_condition_scope_e', ';'),
                    'std::map<std::pair<const player_t*, const buff_t*>, const char*> skill_damage_global_scope_cache;',
                    'bool skill_damage_declared_linked_global_buff(const player_t&, const buff_t*) { return false; }']
        for marker, suffix in (
            ('bool skill_damage_global_aura(', ''),
            ('bool skill_damage_text_declares_global_damage(', ''),
            ('bool skill_damage_damage_aura(', ''),
            ('bool skill_damage_declared_global_spell(', ''),
            ('bool skill_damage_native_global_buff(', ''),
            ('bool skill_damage_result_modifier(', ''),
            ('std::vector<std::string> skill_damage_scope_text_variants(', ''),
            ('std::set<unsigned> skill_damage_declared_damage_indices_impl(', ''),
            ('std::set<unsigned> skill_damage_declared_damage_indices( const dbc_t& dbc', ''),
            ('const char* skill_damage_global_buff_basis(', ''),
            ('bool skill_damage_excluded_global_buff(', ''),
            ('bool skill_damage_pure_global_talent(', ''),
            ('struct skill_damage_excluded_buff_cache_t', ';'),
        ):
            snippets.append(definition(marker, suffix))
        snippets.append('std::map<const player_t*, skill_damage_excluded_buff_cache_t> skill_damage_excluded_buff_cache;')
        for marker, suffix in (
            ('const std::vector<buff_t*>& skill_damage_excluded_buffs(', ''),
            ('struct skill_damage_condition_t', ';'),
            ('bool skill_damage_available_buff(', ''),
            ('buff_t* skill_damage_owned_player_buff(', ''),
            ('std::vector<std::vector<skill_damage_condition_t>> skill_damage_scenarios(', ''),
        ):
            snippets.append(definition(marker, suffix))
        harness = Path(__file__).with_name('simc_skill_damage_pruning_harness.cpp').read_text(encoding='utf-8')
        cases = json.loads(Path(__file__).with_name('fixtures').joinpath('simc_global_damage_scope.json').read_text(encoding='utf-8'))
        checks = ['int fixture_errors = 0;']
        for row in cases['cases']:
            sid = row['spell_id']
            checks.append(f'{{ spell_data_t s; s.spell_id = {sid};')
            checks.append(f'fake_dbc.texts[{sid}].description = ' + json.dumps(row['description']) + ';')
            for effect in row['effects']:
                checks.append('{ spelleffect_data_t e; ' +
                    f'e.type_value={effect["type"]}; e.subtype_value={effect["subtype"]}; '
                    f'e.raw_base={effect["value"]}; e.use_raw_base=true; e.bonus={effect["value"]}/100.0; e.schools={effect["misc1"]}; e.label={effect["misc2"]}; '
                    f'e.effect_index={effect["index"]-1}; e.family=' + '{' + ','.join(map(str,effect['flags'])) + '}; s.rows.push_back(e); }')
            checks.append('const std::set<unsigned> expected{' + ','.join(map(str,row['expected'])) + '};')
            checks.append(f'if (skill_damage_declared_damage_indices(fake_dbc, s) != expected) {{ std::cerr << "DBC 分类错误 {sid}:"; for(auto i: skill_damage_declared_damage_indices(fake_dbc, s)) std::cerr << i << ","; std::cerr << "\\n"; ++fixture_errors; }} }}')
        checks.append('if (fixture_errors) return 30;')
        harness = harness.replace('// DBC_FIXTURE_CASES', '\n'.join(checks))
        with tempfile.TemporaryDirectory(prefix='skill-pruning-cpp-') as tmp:
            cpp = Path(tmp) / 'pruning.cpp'
            executable = Path(tmp) / ('pruning.exe' if os.name == 'nt' else 'pruning')
            cpp.write_text(harness.replace('// SOURCE_FUNCTIONS', '\n'.join(snippets)), encoding='utf-8')
            compiled = subprocess.run([shutil.which('g++'), '-std=c++17', str(cpp), '-o', str(executable)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)

    def _assert_reviewed_native_scope(self, source):
        """执行真实 C++ 分类函数，逐项核对混合状态与外部来源。"""
        def definition(marker):
            start = re.search(re.escape(marker) + r'[^;{]*\{', source).start()
            return source[start:source.index('\n}', source.index('{', start))+2]
        source_path = Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE'])
        header = (source_path.parent/'skill_damage_scope_contract.inc').read_text(encoding='utf-8')
        snippets = '\n'.join(definition(marker) for marker in (
            'const skill_damage_scope_buff_t* skill_damage_reviewed_buff(',
            'const char* skill_damage_global_buff_basis(',
            'bool skill_damage_excluded_global_buff(',
            'std::set<unsigned> skill_damage_declared_damage_indices( const dbc_t&',
            'std::map<unsigned, std::set<unsigned>> skill_damage_talent_damage_components(',
        ))
        harness = r'''
#include <set>
#include <map>
#include <cassert>
struct player_t { int type=4; player_t* target=nullptr; };
struct spell_data_t { unsigned sid; bool ok() const {return sid!=0;} unsigned id() const {return sid;} unsigned class_family() const {return 0;} };
struct buff_t {player_t* source; player_t* player; spell_data_t spell; const spell_data_t& data() const{return spell;} };
struct dbc_t {}; struct sim_t {};
namespace dbc { int get_class_spell_family(int t){return t;} }
template<class T,class U> T as(U value){return static_cast<T>(value);}
'''+header+'\n'+snippets+r'''
int main(){
 player_t owner,other; dbc_t db; sim_t sim;
 for(const auto& fact:skill_damage_scope_buffs){
   buff_t buff{&owner,&owner,{fact.spell}};
   assert(bool(skill_damage_global_buff_basis(owner,&buff,false))==fact.global);
   assert(skill_damage_excluded_global_buff(owner,&buff,false)==(fact.global&&!fact.local));
   owner.target=&other;
   buff.player=&other;
   assert(bool(skill_damage_global_buff_basis(owner,&buff,true))==fact.global);
   assert(skill_damage_excluded_global_buff(owner,&buff,true)==(fact.global&&!fact.local));
   buff.source=&other;
   assert(!skill_damage_global_buff_basis(owner,&buff,true));
   assert(!skill_damage_global_buff_basis(owner,&buff,false));
 }
 for(const auto& fact:skill_damage_scope_effects){
   auto indices=skill_damage_declared_damage_indices(db,spell_data_t{fact.spell});
   assert(bool(indices.count(fact.index))==fact.global);
 }
 for(const auto& fact:skill_damage_scope_parents){
   auto components=skill_damage_talent_damage_components(sim,spell_data_t{fact.parent});
   assert(components[fact.spell].count(fact.index));
 }
}
'''
        work = Path(__file__).resolve().parents[2]/'.cache/simc-reviewed-contract-test'
        work.mkdir(parents=True, exist_ok=True)
        cpp, binary = work/'contract.cpp',work/'contract.exe'
        cpp.write_text(harness, encoding='utf-8')
        compiled = subprocess.run([shutil.which('g++'),'-std=c++17',str(cpp),'-o',str(binary)],capture_output=True,text=True)
        self.assertEqual(compiled.returncode,0,compiled.stderr)
        checked = subprocess.run([str(binary)],capture_output=True,text=True)
        self.assertEqual(checked.returncode,0,checked.stderr)


def talent(entry, **kwargs):
    return SimpleNamespace(
        pk=entry, node_id=entry, talent_id=entry, tree_type='spec',
        max_points=1, name=f'天赋{entry}', name_zh='', db2_subtree_id=0,
        description='', description_zh='', spell_id=entry + 1000, **kwargs,
    )


def global_fact(entry):
    return {
        'trait_entry_id': entry, 'spell_id': entry + 1000,
        'name': '全局伤害', 'evidence': 'dbc_global_damage_talent',
        'remove_talent': True, 'global_effect_indices': [1], 'global_components': [{'spell_id': entry + 1000, 'effect_indices': [1]}],
        'dbc_base_multiplier': 1.2, 'has_rank_scaling': False,
    }


class SkillDamagePruningTests(SimpleTestCase):
    def test_scope_catalog_detects_missing_extra_and_duplicate_declarations(self):
        state = {
            'token': 'buff.unfamiliar', 'scope': 'self', 'spell_id': 918273,
            'available': True, 'scope_basis': 'dbc_damage_scope_declaration',
            'name': '无关名称', 'evidence': 'precomputed_global_damage_scope',
            'excluded_before_probe': True,
        }
        actor = {'global_damage_states': [state], 'global_scope_candidates': [dict(state)]}
        _validate_global_scope_catalog(actor)
        for changes in (
            {'global_damage_states': []}, {'global_scope_candidates': []},
            {'global_damage_states': [state, state]},
            {'global_scope_candidates': [state, state]},
            {'global_scope_candidates': [{**state, 'available': False}]},
            {'global_scope_candidates': [{**state, 'scope_basis': '按名称猜测'}]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                _validate_global_scope_catalog({**actor, **changes})

    def test_unavailable_shared_class_buff_is_not_shown_until_a_talent_enables_it(self):
        state = {
            'token': 'buff.unfamiliar', 'scope': 'self', 'spell_id': 918273,
            'available': False, 'scope_basis': 'dbc_damage_scope_declaration',
            'name': '无关名称', 'evidence': 'precomputed_global_damage_scope',
            'excluded_before_probe': True, 'dbc_base_multiplier': 1.2,
        }
        actor = {'actions': [], 'global_damage_policy': 'exclude_before_probe',
                 'global_damage_states': [state]}
        self.assertEqual(classify_global_skill_effects(actor, actor, []), [])
        selected = {**actor, 'global_damage_states': [{**state, 'available': True}]}
        effects = classify_global_skill_effects(actor, actor, [{'talent': {'id': 1},
            'high': selected, 'low': selected, 'reference_high': actor, 'reference_low': actor}])
        self.assertEqual(len(effects), 1)
        self.assertEqual(effects[0]['source_spell_ids'], [918273])

    def test_pure_global_talent_is_removed_from_scaffold_and_all_prerequisites(self):
        root, global_talent, attack = talent(1), talent(2), talent(3)
        original = [root, global_talent, attack]
        kept, scaffold, prerequisites, effects = prune_global_damage_talents(
            original, [global_talent], {1: [], 2: [root], 3: [root, global_talent]},
            {2: global_fact(2)},
        )
        self.assertEqual(kept, [root, attack])
        self.assertEqual(scaffold, [])
        self.assertEqual(prerequisites[3], [root])
        self.assertEqual(original, [root, global_talent, attack])
        plan = plan_unique_talent_actor_configs(kept, scaffold_talents=scaffold, talent_prerequisites=prerequisites)
        self.assertTrue(all(global_talent not in row['selected_talents'] for row in plan['actors']))
        self.assertEqual(effects[0]['source_spell_ids'], [1002])
        self.assertEqual(effects[0]['projections'][0]['value'], 1.2)
        self.assertIsNone(effects[0]['hero_subtree_id'])

    def test_mixed_global_talent_keeps_node_and_only_declares_damage_components(self):
        mixed, attack = talent(2), talent(3)
        fact = {**global_fact(2), 'remove_talent': False, 'dbc_base_multiplier': None,
                'global_effect_indices': [1, 2, 3], 'description': '装备条件不改变全局作用域'}
        kept, scaffold, prerequisites, effects = prune_global_damage_talents(
            [mixed, attack], [mixed], {3: [mixed]}, {2: fact},
        )
        self.assertEqual(kept, [mixed, attack])
        self.assertEqual(scaffold, [mixed])
        self.assertEqual(prerequisites[3], [mixed])
        self.assertEqual(effects[0]['global_effect_indices'], [1, 2, 3])
        self.assertEqual(effects[0]['projections'], [])
        self.assertTrue(effects[0]['excluded_before_probe'])

    def test_linked_global_catalog_preserves_active_skill_and_rejects_invalid_components(self):
        snapshot = SimpleNamespace(simc_revision='c' * 40, game_build='12.1.0.70000')
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
        fact = {**global_fact(2), 'remove_talent': False, 'global_effect_indices': [],
                'dbc_base_multiplier': None,
                'global_components': [{'spell_id': 3001, 'effect_indices': [2]}]}
        payload = {'schema_version': service.EXPORTER_SCHEMA_REVISION,
                   'simc_revision': snapshot.simc_revision, 'game_build': snapshot.game_build,
                   'talents': [fact]}
        catalog = service._load_global_damage_talent_catalog(payload)
        active = talent(2)
        kept, _, _, effects = prune_global_damage_talents([active], [], {}, catalog)
        self.assertEqual(kept, [active])
        self.assertEqual(effects[0]['global_components'], fact['global_components'])
        for parts in (None, [{'spell_id': 0, 'effect_indices': [1]}],
                      [{'spell_id': 3001, 'effect_indices': [0]}]):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                service._load_global_damage_talent_catalog({**payload, 'talents': [{**fact, 'global_components': parts}]})

    def test_remaining_structural_reference_configuration_is_materialized_once(self):
        structural, first, second = talent(7), talent(8), talent(9)
        plan = plan_unique_talent_actor_configs(
            [first, second], talent_prerequisites={8: [structural], 9: [structural]},
        )
        references = [alias['canonical_name'] for name, alias in plan['aliases'].items() if 'reference' in name]
        self.assertEqual(len(set(references)), 1)
        self.assertEqual(len(plan['actors']), 4)

    def test_implicit_global_prerequisite_is_removed_even_outside_candidate_list(self):
        implicit, attack = talent(2), talent(3)
        kept, scaffold, prerequisites, effects = prune_global_damage_talents(
            [attack], [implicit], {3: [implicit]}, {2: global_fact(2)},
        )
        self.assertEqual(kept, [attack])
        self.assertEqual(scaffold, [])
        self.assertEqual(prerequisites, {3: []})
        self.assertEqual([effect['source_spell_ids'] for effect in effects], [[1002]])

    def test_rank_dependent_global_talent_does_not_invent_max_rank_multiplier(self):
        fact = {**global_fact(2), 'has_rank_scaling': True}
        _, _, _, effects = prune_global_damage_talents([talent(2)], [], {}, {2: fact})
        self.assertEqual(effects[0]['projections'], [])
        self.assertEqual(effects[0]['value_status'], 'rank_dependent')

    def test_physical_export_never_receives_excluded_talents(self):
        snapshot = SimpleNamespace(simc_revision='c' * 40, game_build='12.1.0.70000', schema_revision=24)
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
        root, global_talent, attack = talent(1), talent(2), talent(3)
        profile = SimpleNamespace(class_name='warrior', spec='warrior_fury')
        calls = []

        def export(_profile, _talents, **kwargs):
            calls.append(kwargs)
            self.assertNotIn(global_talent, kwargs['scaffold_talents'])
            return {'actors': [{
                'name': spec['name'], 'class': 'warrior', 'spec': 'fury',
                'actions': [], 'global_damage_states': [], 'global_scope_candidates': [],
                'global_damage_policy': 'exclude_before_probe',
            } for spec in kwargs['actor_plan']], 'unresolved': []}

        with mock.patch.object(service, '_talent_entries', return_value=[root, global_talent, attack]), \
             mock.patch.object(service, '_hero_talent_trees', return_value=[]), \
             mock.patch.object(service, '_spec_root_scaffold', return_value=[root]), \
             mock.patch.object(service, '_implicit_prerequisite_nodes', return_value=[]), \
             mock.patch.object(service, '_talent_prerequisite_map', return_value={1: [], 2: [root], 3: [root, global_talent]}), \
             mock.patch.object(service, '_global_damage_talent_catalog', return_value={2: global_fact(2)}), \
             mock.patch.object(service, '_run_profile_export', side_effect=export), \
             mock.patch('botend.services.simc_skill_damage.localize_skill_damage_payload', side_effect=lambda value: value):
            actor, unresolved, count = service._generate_profile_product_actor(profile)
        self.assertEqual([call['target_health'] for call in calls], [100, 34])
        for call in calls:
            self.assertEqual(len(call['actor_plan']), 2)
            self.assertTrue(all(global_talent not in spec['selected_talents'] for spec in call['actor_plan']))
        self.assertEqual(actor['global_skill_effects'][0]['source_spell_ids'], [1002])
        self.assertEqual(unresolved, [])
        self.assertEqual(count, 0)

    def test_precomputed_global_table_does_not_read_damage_samples(self):
        actor = {
            'class': 'warrior', 'global_damage_policy': 'exclude_before_probe', 'actions': [],
            'global_damage_states': [{
                'token': 'buff.avatar', 'scope': 'self', 'spell_id': 107574, 'name': '天神下凡',
                'evidence': 'precomputed_global_damage_scope', 'excluded_before_probe': True,
                'dbc_base_multiplier': 1.2,
            }, {
                'token': 'buff.enrage', 'scope': 'self', 'spell_id': 184362, 'name': '激怒',
                'evidence': 'precomputed_global_damage_scope', 'excluded_before_probe': True,
                'dbc_base_multiplier': None,
            }],
        }
        with mock.patch('botend.services.simc_skill_damage._uniform_amount_ratios', side_effect=AssertionError('不应探测伤害')):
            effects = classify_global_skill_effects(actor, actor, [])
        self.assertEqual(len(effects), 2)
        self.assertEqual(effects[0]['projections'][0]['value'], 1.2)
        self.assertEqual(effects[1]['projections'], [])
        self.assertTrue(all(effect['excluded_before_probe'] for effect in effects))

    def test_catalog_export_has_no_actor_input_and_is_reused(self):
        snapshot = SimpleNamespace(simc_revision='c' * 40, game_build='12.1.0.70000')
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
        payload = {
            'schema_version': service.EXPORTER_SCHEMA_REVISION,
            'simc_revision': snapshot.simc_revision, 'game_build': snapshot.game_build,
            'talents': [global_fact(2)],
        }

        def run(command, **kwargs):
            self.assertFalse(any(str(arg).endswith('.simc') for arg in command))
            self.assertFalse(any(str(arg).startswith('skill_damage_export=') for arg in command))
            output = next(arg.split('=', 1)[1] for arg in command if arg.startswith('skill_damage_scope_export='))
            Path(output).write_text(json.dumps(payload), encoding='utf-8')
            return SimpleNamespace(returncode=0)

        with mock.patch.object(service, '_binary_path', return_value='simc'), \
             mock.patch('botend.services.simc_skill_damage.subprocess.run', side_effect=run) as execute:
            self.assertEqual(service._global_damage_talent_catalog(), {2: global_fact(2)})
            service._global_damage_talent_catalog()
            execute.assert_called_once()
            for changes in ({'schema_version': 13}, {'talents': [global_fact(2), global_fact(2)]}):
                invalid_service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
                invalid_service._binary_path = lambda: 'simc'
                original = copy.deepcopy(payload)
                payload.update(changes)
                with self.assertRaises(ValueError):
                    invalid_service._global_damage_talent_catalog()
                payload = original

    def test_exporter_contract_rejects_global_damage_scenarios_instead_of_filtering(self):
        snapshot = SimpleNamespace(simc_revision='c' * 40, game_build='12.1.0.70000')
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())
        actor = {
            'class': 'warrior', 'spec': 'arms', 'talent_effectiveness': 'unknown',
            'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
            'global_damage_policy': 'exclude_before_probe', 'global_damage_states': [], 'global_scope_candidates': [], 'actions': [],
        }
        payload = {
            'schema_version': service.EXPORTER_SCHEMA_REVISION, 'simc_revision': snapshot.simc_revision,
            'game_build': snapshot.game_build, 'normalization_basis': service.FIXED_PRESET, 'actors': [actor],
        }
        service._validate_export(payload)
        actor.pop('global_damage_policy')
        with self.assertRaisesRegex(ValueError, '缺少全局增伤前置排除约定'):
            service._validate_export(payload)
        actor['global_damage_policy'] = 'exclude_before_probe'
        actor['actions'] = [{
            'token': 'colossus_smash', 'spell_id': 167105, 'supported': False, 'player_skill': True,
            'reporting_root_token': 'colossus_smash', 'reporting_root_spell_id': 167105,
            'reporting_root_component': True, 'selected_trait_effects': [], 'unsupported_reason': 'weapon_dependent',
            'scenarios': [{'buffs': [{
                'token': 'debuff.colossus_smash', 'spell_id': 208086, 'scope': 'target', 'stacks': 1,
            }], 'values': {'unresolved_reason': 'weapon_dependent'}}],
        }]
        state = {
            'token': 'debuff.colossus_smash', 'spell_id': 208086, 'scope': 'target',
            'available': True, 'scope_basis': 'dbc_damage_scope_declaration',
            'evidence': 'precomputed_global_damage_scope', 'excluded_before_probe': True,
        }
        actor['global_damage_states'] = [state]
        actor['global_scope_candidates'] = [dict(state)]
        with self.assertRaisesRegex(ValueError, '违反前置排除约定'):
            service._validate_export(payload)
        # 混合状态的全局分量已归零，局部分量仍允许进入技能条件。
        state['scope_basis'] = 'reviewed_dbc_native_effect_scope'
        state['partial_state'] = True
        actor['global_scope_candidates'] = [dict(state)]
        with self.assertRaisesRegex(ValueError, '归零回读'):
            service._validate_export(payload)
        actor['scope_contract_sha256'] = 'a' * 64
        actor['normalized_scope_effects'] = [{'spell_id':208086,'effect_index':1,'effect_id':42,'actual_base_value':0}]
        with self.assertRaisesRegex(ValueError, '完整展示目录'):
            service._validate_export(payload)
        actor['reviewed_global_effects'] = []
        with self.assertRaisesRegex(ValueError, '展示目录与实际剔除分量不一致'):
            service._validate_export(payload)
        actor['reviewed_global_effects'] = [{'global_components':[{'spell_id':208086,'effect_index':1,'effect_id':42}]}]
        service._validate_export(payload)
        actor['normalized_scope_effects'][0]['actual_base_value'] = 1
        with self.assertRaisesRegex(ValueError, '没有归零'):
            service._validate_export(payload)
