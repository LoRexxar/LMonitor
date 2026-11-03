"""参数链验证必须同时满足真实干预、全局归零、局部参数存活及同一运算变化。"""
import copy
from unittest import TestCase

from scripts.audit_simc_single_talent_scope import local_parameter_witness


class LocalParameterWitnessTests(TestCase):
    def fixture(self):
        def actor(selected):
            return {
                'selected_trait_ids': [20, 1] if selected else [20],
                'global_damage_policy': 'exclude_before_probe',
                'normalized_scope_effects': [{'spell_id': 100, 'effect_index': 1,
                                             'actual_base_value': 0, 'actual_mastery_coefficient': 0}],
                'normalized_talent_effects': ([{'trait_entry_id': 1, 'spell_id': 100, 'effect_index': 2,
                                               'excluded': False, 'actual_base_value': 10}] if selected else []),
                'actions': [{'token': 'skill', 'spell_id': 200, 'baseline': {'direct': {
                    'runtime_factor_sources': [{'source_kind': 'native_operation', 'source_file': 'engine/operation.cpp',
                                               'source_line': 10, 'layer': 'da_multiplier',
                                               'evidence': 'native_multiplier_dataflow_v1',
                                               'factor': 1.1 if selected else 1.0}]}}}],
            }
        return {
            'row': {'components': [{'token': 'skill', 'spell_id': 200, 'component': 'direct'}]}, 'entry': 1,
            'talent': {'spell_id': 100, 'effects': [{'type': 6, 'subtype': 108, 'misc1': 12, 'index': 2}]},
            'global_definition': {'global_components': [{'spell_id': 100, 'effect_indices': [1]}]},
            'after': {h: actor(True) for h in (100, 34)}, 'before': {h: actor(False) for h in (100, 34)},
        }

    def test_parameter_chain_is_independent_of_spell_names(self):
        data = self.fixture()
        self.assertTrue(local_parameter_witness(**data))

    def test_global_readback_and_exact_selection_are_required_at_both_health_levels(self):
        for health in (100, 34):
            for role in ('after', 'before'):
                with self.subTest(health=health, role=role):
                    for field, value in (('actual_base_value', 10), ('actual_mastery_coefficient', .1)):
                        data = self.fixture()
                        data[role][health]['normalized_scope_effects'][0][field] = value
                        self.assertFalse(local_parameter_witness(**data))
                    data = self.fixture()
                    data[role][health]['selected_trait_ids'].append(999)
                    self.assertFalse(local_parameter_witness(**data))
                    data = self.fixture()
                    data[role][health]['normalized_scope_effects'] = []
                    self.assertFalse(local_parameter_witness(**data))

    def test_declared_policy_or_active_parameter_without_operation_change_is_insufficient(self):
        data = self.fixture()
        for h in (100, 34):
            data['after'][h]['actions'] = copy.deepcopy(data['before'][h]['actions'])
        self.assertFalse(local_parameter_witness(**data))
        data = self.fixture()
        data['after'][100]['normalized_talent_effects'][0]['excluded'] = True
        self.assertFalse(local_parameter_witness(**data))
        data = self.fixture()
        data['talent']['effects'][0]['misc1'] = 0
        self.assertFalse(local_parameter_witness(**data))
