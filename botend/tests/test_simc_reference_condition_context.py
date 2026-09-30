"""Absolute formulas must disclose the native reference talent configuration."""
import copy
from unittest import TestCase
from types import SimpleNamespace
from botend.services.simc_skill_damage import (
    _SpoolBackedTalentVariants, flatten_single_talent_damage_variants,
    project_skill_damage_product_payload,
)


def actor(multiplier, scenario=None):
    def amount(factor):
        return {'direct': {'hit': 100 * factor, 'crit': 200 * factor,
            'expected': 120 * factor, 'crit_multiplier': 2, 'crit_chance': .2,
            'damage_equivalent_count': 1, 'native_base_damage': 100,
            'runtime_layers': {'da_multiplier': factor}}, 'tick': None, 'unresolved_reason': None}
    action = {'token': 'measured', 'name': 'Measured', 'spell_id': 500,
        'supported': True, 'harmful': True, 'reporting_root_token': 'measured',
        'reporting_root_spell_id': 500, 'reporting_root_component': True,
        'dbc_scaling': {'direct': {'normalized_base': 100,
            'attack_power_coefficient': 1, 'spell_power_coefficient': 0}},
        'baseline': amount(multiplier), 'scenarios': []}
    if scenario is not None:
        action['scenarios'] = [{'buffs': [{'token': 'buff.measured', 'spell_id': 600,
            'scope': 'self', 'stacks': 1}], 'values': amount(scenario)}]
    return {'class': 'test', 'specialization': 'test', 'actions': [action]}


class ReferenceConditionContextTests(TestCase):
    def test_native_reference_selection_survives_flatten_and_product(self):
        reference = actor(1.35)
        selected = actor(1.35, scenario=1.485)
        reference['selected_trait_ids'] = [30, 40]
        selected['selected_trait_ids'] = [20, 30, 40]
        item = {'talent': {'id': 10, 'node_id': 20, 'name': 'Target', 'tree_type': 'spec'},
                'reference_traits': [{'trait_entry_id': 30, 'name': 'Prerequisite', 'name_zh': '固定前置'}],
                'reference_high': reference, 'reference_low': reference,
                'high': selected, 'low': selected}
        frozen = copy.deepcopy(item)
        rows = flatten_single_talent_damage_variants(reference, reference, [item])
        row = next(r for r in rows if r['variant']['trait_entry_id'] == 20)
        context = row['variant']['reference_context']
        self.assertEqual(context['trait_entry_ids'], [30, 40])
        self.assertEqual(context['source'], 'native_reference_selection')
        self.assertEqual(context['traits'][0]['name_zh'], '固定前置')
        self.assertEqual(context['traits'][1], {'trait_entry_id': 40})
        result = project_skill_damage_product_payload({'actors': [{'actions': [row]}]})
        output = result['actors'][0]['actions'][0]
        self.assertEqual(output['variant']['reference_context'], context)
        self.assertAlmostEqual(output['product']['noncrit_damage'], 148.5)
        self.assertEqual(item, frozen)

    def test_spool_freezes_metadata_from_the_planned_reference_config(self):
        def talent(pk, node_id, name):
            return SimpleNamespace(pk=pk, node_id=node_id, name=name, name_zh=name,
                description='', description_zh='', tree_type='spec', db2_subtree_id=None)
        target, parent = talent(10, 20, '目标'), talent(11, 30, '前置')
        aliases = {f'skill_damage_{kind}_10_trait_20': {'canonical_name': name,
                    'talent_effectiveness': state}
                   for kind, name, state in [('reference', 'reference', 'inactive'), ('talent', 'selected', 'active')]}
        plan = {'aliases': aliases, 'actors': [{'name': 'reference', 'selected_talents': [parent]},
                                               {'name': 'selected', 'selected_talents': [parent, target]}]}
        raw = {'reference': {'selected_trait_ids': [30], 'actions': []},
               'selected': {'selected_trait_ids': [30, 20], 'actions': []}}
        spool = SimpleNamespace(load=lambda health, name: copy.deepcopy(raw[name]))
        item = next(iter(_SpoolBackedTalentVariants(profile=SimpleNamespace(spec='fixture'),
            talents=[target], actor_plan=plan, actor_spool=spool)))
        self.assertIn({'trait_entry_id': 30, 'name': '前置', 'name_zh': '前置'}, item['reference_traits'])
