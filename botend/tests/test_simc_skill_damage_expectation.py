import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest import skipUnless

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import (
    attach_runtime_product_metrics,
    flatten_single_talent_damage_variants, project_skill_damage_product_payload,
)


class SkillDamageNativeExpectationTests(SimpleTestCase):
    @skipUnless(os.environ.get('SIMC_SKILL_DAMAGE_SOURCE') and shutil.which('g++'), '需要指定已应用补丁的 SimC 源文件及 g++')
    def test_actual_cpp_preserves_extra_crit_and_weights_each_target(self):
        source = Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE']).read_text(encoding='utf-8')
        snippets = []
        for marker, suffix in (
            ('void skill_damage_normalize_crit(', ''),
            ('struct skill_damage_target_amount_t', ';'),
            ('skill_damage_target_amount_t skill_damage_target_amount(', ''),
        ):
            start = source.index(marker)
            end = source.index('{', start) + 1
            depth = 1
            while depth:
                depth += (source[end] == '{') - (source[end] == '}')
                end += 1
            snippets.append(source[start:end] + suffix)
        harness = Path(__file__).with_name('simc_skill_damage_expectation_harness.cpp').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory(prefix='skill-expectation-cpp-') as tmp:
            cpp = Path(tmp) / 'expectation.cpp'
            executable = Path(tmp) / ('expectation.exe' if os.name == 'nt' else 'expectation')
            cpp.write_text(harness.replace('// SOURCE_FUNCTIONS', '\n'.join(snippets)), encoding='utf-8')
            compiled = subprocess.run([shutil.which('g++'), '-std=c++17', str(cpp), '-o', str(executable)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)

    @skipUnless(os.environ.get('SIMC_SKILL_DAMAGE_SOURCE') and shutil.which('g++'), '需要指定已应用补丁的 SimC 源文件及 g++')
    def test_actual_cpp_uses_independent_targets_and_restores_scenario_state(self):
        source = Path(os.environ['SIMC_SKILL_DAMAGE_SOURCE']).read_text(encoding='utf-8')
        snippets = []
        for marker, suffix in (
            ('struct skill_damage_target_amount_t', ';'),
            ('skill_damage_target_amount_t skill_damage_target_amount(', ''),
            ('void skill_damage_populate_target_scenarios(', ''),
        ):
            start = source.index(marker)
            end = source.index('{', start) + 1
            depth = 1
            while depth:
                depth += (source[end] == '{') - (source[end] == '}')
                end += 1
            snippets.append(source[start:end] + suffix)
        harness = Path(__file__).with_name('simc_skill_damage_targets_harness.cpp').read_text(encoding='utf-8')
        with tempfile.TemporaryDirectory(prefix='skill-expectation-cpp-') as tmp:
            cpp = Path(tmp) / 'expectation.cpp'
            executable = Path(tmp) / ('expectation.exe' if os.name == 'nt' else 'expectation')
            cpp.write_text(harness.replace('// SOURCE_FUNCTIONS', '\n'.join(snippets)), encoding='utf-8')
            compiled = subprocess.run([shutil.which('g++'), '-std=c++17', str(cpp), '-o', str(executable)], capture_output=True, text=True)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            result = subprocess.run([str(executable)], capture_output=True, text=True, encoding='utf-8')
            self.assertEqual(result.returncode, 0, result.stderr)


def with_target_crit_evidence(payload):
    """为历史测试的均匀多目标夹具补齐新版导出协议。"""
    for actor in payload.get('actors', []):
        if not isinstance(actor, dict):
            continue
        for action in actor.get('actions', []):
            if not isinstance(action, dict):
                continue
            for amount in [action.get('baseline'), *(s.get('values') for s in action.get('scenarios', []) if isinstance(s, dict))]:
                if not isinstance(amount, dict):
                    continue
                for kind in ('direct', 'tick'):
                    component = amount.get(kind)
                    if not isinstance(component, dict) or not isinstance(component.get('target_hit'), dict):
                        continue
                    if isinstance(component.get('runtime_layers'), dict):
                        component['runtime_layers'].setdefault('aoe_multiplier', 1.0)
                    hit, crit, chance = (component.get(key, 0) for key in ('hit', 'crit', 'crit_chance'))
                    if not all(isinstance(value, (float, int)) for value in (hit, crit, chance)):
                        continue
                    targets = component['target_hit']
                    if not all(isinstance(value, (float, int)) for value in targets.values()):
                        continue
                    target_crit = {key: value * crit / hit if hit else 0 for key, value in targets.items()}
                    component.setdefault('target_crit', target_crit)
                    component.setdefault('target_noncrit_contribution', {key: value * (1 - chance) for key, value in targets.items()})
                    component.setdefault('target_crit_contribution', {key: value * chance for key, value in target_crit.items()})
                    component.setdefault('target_expected', {
                        key: targets[key] * (1 - chance) + target_crit[key] * chance for key in targets
                    })
    return payload


def skill(hit=100.0, crit=200.0, chance=0.2, *, count=1.0, component='direct', token='skill'):
    amount = {
        'hit': hit, 'crit': crit, 'crit_multiplier': crit / hit if hit else 1,
        'crit_chance': chance, 'crit_chance_uncapped': chance, 'can_crit': chance != 0,
        'expected': hit * (1 - chance) + crit * chance,
        'damage_equivalent_count': count, 'native_base_damage': hit,
        'runtime_layers': {'da_multiplier' if component == 'direct' else 'ta_multiplier': 1.0},
        'target_hit': {str(n): hit * n for n in (1, 2, 5, 10, 20)},
    }
    action = {
        'token': token, 'spell_id': 42, 'supported': True, 'player_skill': True,
        'reporting_root_token': 'skill', 'reporting_root_spell_id': 42, 'reporting_root_component': True,
        'dbc_scaling': {component: {'normalized_base': hit, 'attack_power_coefficient': hit / 100, 'spell_power_coefficient': 0.0}},
        'baseline': {component: amount, 'unresolved_reason': None}, 'scenarios': [],
    }
    return with_target_crit_evidence({'actors': [{'actions': [action]}]})['actors'][0]['actions'][0]


def project(*actions):
    actor = attach_runtime_product_metrics({'actions': list(actions)})
    return project_skill_damage_product_payload({'actors': [actor]})['actors'][0]['actions'][0]


class SkillDamageExpectationTests(SimpleTestCase):
    def test_twenty_percent_crit_changes_final_damage_to_expectation(self):
        row = project(skill())
        self.assertEqual(row['product']['final_normalized_damage'], 120)
        self.assertEqual(row['product']['damage_metric'], 'critical_expectation')
        formula = row['product']['formula_components'][0]
        self.assertEqual((formula['noncrit_damage'], formula['crit_damage'], formula['crit_chance']), (100, 200, 0.2))
        self.assertEqual((formula['noncrit_contribution'], formula['crit_contribution']), (80, 40))
        self.assertNotIn('status', formula)

    def test_crit_damage_talent_changes_expectation_without_changing_normal_hit(self):
        self.assertEqual(project(skill(crit=250))['product']['final_normalized_damage'], 130)
        self.assertEqual(project(skill(chance=0.3))['product']['final_normalized_damage'], 130)
        self.assertEqual(project(skill(crit=100, chance=0))['product']['final_normalized_damage'], 100)
        self.assertEqual(project(skill(chance=1))['product']['final_normalized_damage'], 200)

    def test_components_use_their_own_crit_chance_and_periodic_count(self):
        row = project(skill(100, 250, 0.2), skill(10, 20, 0.5, count=4.5, component='tick', token='dot'))
        self.assertEqual(row['product']['final_normalized_damage'], 197.5)
        self.assertEqual(row['product']['noncrit_damage'], 145)
        self.assertEqual(row['product']['final_normalized_damage_by_target']['5'], 987.5)

    def test_multi_target_uses_exported_expectation_instead_of_single_target_crit_rate(self):
        action = skill()
        component = action['baseline']['direct']
        component['target_hit']['2'] = 150
        component['target_crit']['2'] = 350
        component['target_expected']['2'] = 300
        component['target_noncrit_contribution']['2'] = 50
        component['target_crit_contribution']['2'] = 250
        row = project(action)
        self.assertEqual(row['product']['final_normalized_damage_by_target']['2'], 300)
        formula = row['product']['formula_components'][0]
        self.assertEqual(formula['crit_contribution_by_target']['2'], 250)

    def test_missing_multi_target_crit_evidence_is_not_fabricated(self):
        action = skill()
        action['baseline']['direct'].pop('target_expected')
        row = project(action)
        self.assertNotIn('final_normalized_damage_by_target', row['product'])

    def test_crit_only_talent_is_not_removed_as_a_global_damage_modifier(self):
        base = {'actions': [skill()]}
        selected = {'actions': [skill(chance=0.3)]}
        effect = {
            'source_type': 'talent', 'talent_id': 5, 'tree_type': 'spec',
            'scenario_tokens': [], 'projections': [{'kind': 'crit_chance', 'value': 0.1}],
        }
        rows = flatten_single_talent_damage_variants(base, base, [{
            'talent': {'id': 5, 'tree_type': 'spec'}, 'reference_high': base, 'reference_low': base,
            'high': selected, 'low': selected,
        }], global_effects=[effect])
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]['baseline']['direct']['expected'], 130)
