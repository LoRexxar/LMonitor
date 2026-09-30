"""Partial-state evidence gates, using an unmodified real exporter action slice.

Fixture: reckless_cruelty high/low, schema 22, build 12.1.0.69814,
revision 1d3897ef4adefc8e47a43989eed81b6618ce7bef. The selected slice is
identical in both health probes. All amount fields are copied, not calculated.
Full JSON replay lives in the external probe evidence, not in this unit fixture.
"""
import copy

from django.test import SimpleTestCase

from botend.services.simc_skill_damage import (
    classify_global_skill_effects,
    flatten_single_talent_damage_variants,
)


REAL_ACTOR = {'class': 'warrior',
 'spec': 'fury',
 'global_damage_policy': 'exclude_before_probe',
 'global_damage_states': [{'token': 'buff.enrage',
                           'scope': 'self',
                           'spell_id': 184362,
                           'name': 'Enrage',
                           'available': True,
                           'scope_basis': 'reviewed_dbc_native_effect_scope',
                           'partial_state': True,
                           'evidence': 'precomputed_global_damage_scope',
                           'excluded_before_probe': True,
                           'dbc_base_multiplier': None}],
 'reviewed_global_effects': [{'effect_id': 'reviewed_scope:自身状态:184362:None',
                              'source_type': 'reviewed_scope',
                              'source_name': '激怒',
                              'display_name': '激怒',
                              'source_spell_ids': [184362],
                              'specializations': ['fury'],
                              'source_class': 'warrior',
                              'source_kind': 'buff',
                              'scope_evidence': 'reviewed_dbc_native_effect_scope',
                              'excluded_before_probe': True,
                              'partial_state': True,
                              'projections': [],
                              'global_components': [{'spell_id': 76856, 'effect_index': 1, 'effect_id': 68045},
                                                    {'spell_id': 76856, 'effect_index': 2, 'effect_id': 88047}],
                              'local_components': [{'spell_id': 392936, 'effect_index': 2, 'effect_id': 1181491}],
                              'local_skill_bindings': [{'spell_id': 392936,
                                                        'effect_index': 2,
                                                        'effect_id': 1181491,
                                                        'skill_spell_ids': [85384, 96103, 335098, 335100],
                                                        'evidence': 'DBC 部分技能选择器'}],
                              'lower_skill_policy': 'exclude_global_keep_explicit_local',
                              'local_scope_evidence': '已解析技能应用关系',
                              'effect_details': [{'label': '直接伤害',
                                                  'source_spell_id': 76856,
                                                  'effect_index': 1,
                                                  'evidence': 'dbc_native',
                                                  'base_value': 0,
                                                  'conditional': True,
                                                  'value_kind': 'mastery',
                                                  'coefficient': 0.014,
                                                  'normalized_mastery_percent': 50.00000000000001},
                                                 {'label': '周期伤害',
                                                  'source_spell_id': 76856,
                                                  'effect_index': 2,
                                                  'evidence': 'dbc_native',
                                                  'base_value': 0,
                                                  'conditional': True,
                                                  'value_kind': 'mastery',
                                                  'coefficient': 0.014,
                                                  'normalized_mastery_percent': 50.00000000000001}],
                              'runtime_condition': '自身效果生效时'}],
 'actions': [{'token': 'bloodbath',
              'name': 'bloodbath',
              'spell_id': 335096,
              'weapon_hand': 'main_hand',
              'background': False,
              'harmful': True,
              'supported': True,
              'unsupported_reason': None,
              'parent_token': None,
              'reporting_root_token': 'bloodbath',
              'reporting_root_spell_id': 335096,
              'reporting_root_component': True,
              'player_skill': True,
              'activation_conditions': [{'token': 'buff.recklessness',
                                         'scope': 'self',
                                         'spell_id': 1719,
                                         'name': 'Recklessness',
                                         'stacks': 1}],
              'selected_trait_effects': [{'trait_entry_id': 112299, 'source_spell_id': 392931, 'effect_index': 0}],
              'dbc_scaling': {'source': 'spell_effect',
                              'direct': {'attack_power_coefficient': 4.45413,
                                         'spell_power_coefficient': 0,
                                         'normalized_base': 445.413,
                                         'source_spell_id': 335096,
                                         'effect_indexes': [0]},
                              'tick': None,
                              'weapon_multiplier': 0,
                              'requires_weapon_data': False},
              'baseline': {'direct': {'hit': 559.7059758,
                                      'crit': 1119.4119516,
                                      'crit_multiplier': 2,
                                      'crit_chance': 0.2,
                                      'crit_chance_uncapped': 0.2,
                                      'can_crit': True,
                                      'expected': 671.64717096,
                                      'target_hit': {'1': 559.7059758,
                                                     '2': 559.7059758,
                                                     '5': 559.7059758,
                                                     '10': 559.7059758,
                                                     '20': 559.7059758},
                                      'target_crit': {'1': 1119.4119516,
                                                      '2': 1119.4119516,
                                                      '5': 1119.4119516,
                                                      '10': 1119.4119516,
                                                      '20': 1119.4119516},
                                      'target_expected': {'1': 671.64717096,
                                                          '2': 671.64717096,
                                                          '5': 671.64717096,
                                                          '10': 671.64717096,
                                                          '20': 671.64717096},
                                      'target_noncrit_contribution': {'1': 447.76478064,
                                                                      '2': 447.76478064,
                                                                      '5': 447.76478064,
                                                                      '10': 447.76478064,
                                                                      '20': 447.76478064},
                                      'target_crit_contribution': {'1': 223.88239032,
                                                                   '2': 223.88239032,
                                                                   '5': 223.88239032,
                                                                   '10': 223.88239032,
                                                                   '20': 223.88239032},
                                      'damage_equivalent_count': 1,
                                      'base_damage_layers': {'base_multiplier': 1, 'component_multiplier': 1.22},
                                      'single_target_eligible': True,
                                      'native_base_damage': 445.413,
                                      'runtime_layers': {'da_multiplier': 1.22,
                                                         'aoe_multiplier': 1,
                                                         'player_multiplier': 1,
                                                         'versus_multiplier': 1,
                                                         'persistent_multiplier': 1,
                                                         'target_da_multiplier': 1,
                                                         'versatility': 1.03,
                                                         'pet_multiplier': 1,
                                                         'target_pet_multiplier': 1,
                                                         'specialization_passive_effects': [{'source_spell_id': 137050,
                                                                                             'source_name': 'Fury '
                                                                                                            'Warrior',
                                                                                             'effect_index': 0,
                                                                                             'component': 'direct',
                                                                                             'factor': 1.22}]}},
                           'tick': None,
                           'unresolved_reason': None},
              'scenarios': [{'affected_target_counts': [1, 2, 5, 10, 20],
                             'buffs': [{'token': 'buff.enrage',
                                        'scope': 'self',
                                        'spell_id': 184362,
                                        'class_family': 4,
                                        'stacks': 1}],
                             'values': {'direct': {'hit': 615.67657338,
                                                   'crit': 1231.35314676,
                                                   'crit_multiplier': 2,
                                                   'crit_chance': 0.2,
                                                   'crit_chance_uncapped': 0.2,
                                                   'can_crit': True,
                                                   'expected': 738.811888056,
                                                   'target_hit': {'1': 615.67657338,
                                                                  '2': 615.67657338,
                                                                  '5': 615.67657338,
                                                                  '10': 615.67657338,
                                                                  '20': 615.67657338},
                                                   'target_crit': {'1': 1231.35314676,
                                                                   '2': 1231.35314676,
                                                                   '5': 1231.35314676,
                                                                   '10': 1231.35314676,
                                                                   '20': 1231.35314676},
                                                   'target_expected': {'1': 738.811888056,
                                                                       '2': 738.811888056,
                                                                       '5': 738.811888056,
                                                                       '10': 738.811888056,
                                                                       '20': 738.811888056},
                                                   'target_noncrit_contribution': {'1': 492.54125870400003,
                                                                                   '2': 492.54125870400003,
                                                                                   '5': 492.54125870400003,
                                                                                   '10': 492.54125870400003,
                                                                                   '20': 492.54125870400003},
                                                   'target_crit_contribution': {'1': 246.27062935200001,
                                                                                '2': 246.27062935200001,
                                                                                '5': 246.27062935200001,
                                                                                '10': 246.27062935200001,
                                                                                '20': 246.27062935200001},
                                                   'damage_equivalent_count': 1,
                                                   'base_damage_layers': {'base_multiplier': 1.1,
                                                                          'component_multiplier': 1.22},
                                                   'single_target_eligible': True,
                                                   'native_base_damage': 445.413,
                                                   'runtime_layers': {'da_multiplier': 1.342,
                                                                      'aoe_multiplier': 1,
                                                                      'player_multiplier': 1,
                                                                      'versus_multiplier': 1,
                                                                      'persistent_multiplier': 1,
                                                                      'target_da_multiplier': 1,
                                                                      'versatility': 1.03,
                                                                      'pet_multiplier': 1,
                                                                      'target_pet_multiplier': 1,
                                                                      'specialization_passive_effects': [{'source_spell_id': 137050,
                                                                                                          'source_name': 'Fury '
                                                                                                                         'Warrior',
                                                                                                          'effect_index': 0,
                                                                                                          'component': 'direct',
                                                                                                          'factor': 1.22}]}},
                                        'tick': None,
                                        'unresolved_reason': None},
                             'direct_multiplier': 1.1,
                             'tick_multiplier': None,
                             'delta_pct': 10.000000000000009},
                            {'affected_target_counts': [2, 5, 10, 20],
                             'buffs': [{'token': 'buff.whirlwind',
                                        'scope': 'self',
                                        'spell_id': 85739,
                                        'class_family': 4,
                                        'stacks': 1}],
                             'values': {'direct': {'hit': 559.7059758,
                                                   'crit': 1119.4119516,
                                                   'crit_multiplier': 2,
                                                   'crit_chance': 0.2,
                                                   'crit_chance_uncapped': 0.2,
                                                   'can_crit': True,
                                                   'expected': 671.64717096,
                                                   'target_hit': {'1': 559.7059758,
                                                                  '2': 923.51486007,
                                                                  '5': 2014.9415128800001,
                                                                  '10': 2014.9415128800001,
                                                                  '20': 2014.9415128800001},
                                                   'target_crit': {'1': 1119.4119516,
                                                                   '2': 1847.02972014,
                                                                   '5': 4029.8830257600002,
                                                                   '10': 4029.8830257600002,
                                                                   '20': 4029.8830257600002},
                                                   'target_expected': {'1': 671.64717096,
                                                                       '2': 1108.217832084,
                                                                       '5': 2417.929815456,
                                                                       '10': 2417.929815456,
                                                                       '20': 2417.929815456},
                                                   'target_noncrit_contribution': {'1': 447.76478064,
                                                                                   '2': 738.811888056,
                                                                                   '5': 1611.953210304,
                                                                                   '10': 1611.953210304,
                                                                                   '20': 1611.953210304},
                                                   'target_crit_contribution': {'1': 223.88239032,
                                                                                '2': 369.405944028,
                                                                                '5': 805.976605152,
                                                                                '10': 805.976605152,
                                                                                '20': 805.976605152},
                                                   'damage_equivalent_count': 1,
                                                   'base_damage_layers': {'base_multiplier': 1,
                                                                          'component_multiplier': 1.22},
                                                   'single_target_eligible': True,
                                                   'native_base_damage': 445.413,
                                                   'runtime_layers': {'da_multiplier': 1.22,
                                                                      'aoe_multiplier': 1,
                                                                      'player_multiplier': 1,
                                                                      'versus_multiplier': 1,
                                                                      'persistent_multiplier': 1,
                                                                      'target_da_multiplier': 1,
                                                                      'versatility': 1.03,
                                                                      'pet_multiplier': 1,
                                                                      'target_pet_multiplier': 1,
                                                                      'specialization_passive_effects': [{'source_spell_id': 137050,
                                                                                                          'source_name': 'Fury '
                                                                                                                         'Warrior',
                                                                                                          'effect_index': 0,
                                                                                                          'component': 'direct',
                                                                                                          'factor': 1.22}]}},
                                        'tick': None,
                                        'unresolved_reason': None},
                             'direct_multiplier': 1,
                             'tick_multiplier': None,
                             'delta_pct': 0}]}]}


class PartialStateLocalDamageTests(SimpleTestCase):
    def setUp(self):
        self.high = copy.deepcopy(REAL_ACTOR)
        self.low = copy.deepcopy(REAL_ACTOR)

    def rows(self, high=None, low=None, variants=()):
        high = self.high if high is None else high
        low = self.low if low is None else low
        effects = classify_global_skill_effects(high, low, variants)
        return flatten_single_talent_damage_variants(high, low, variants, global_effects=effects)

    def enrage_rows(self, rows):
        return [row for row in rows if 'buff.enrage' in row['variant']['scenario_tokens']]

    def test_excluded_partial_state_retains_observed_local_delta_without_dbc_binding(self):
        effects = classify_global_skill_effects(self.high, self.low, [])
        self.assertTrue(effects[0]['partial_state'])
        self.assertTrue(effects[0]['excluded_before_probe'])
        self.assertNotIn(335096, [sid for binding in effects[0]['local_skill_bindings']
                                for sid in binding['skill_spell_ids']])
        rows = self.enrage_rows(self.rows())
        self.assertEqual(len(rows), 1)
        original = self.high['actions'][0]['scenarios'][0]
        # Flatten adds product metadata, but must not alter any exported amount.
        for key, value in original['values']['direct'].items():
            self.assertEqual(rows[0]['baseline']['direct'][key], value)
        self.assertEqual(rows[0]['activation_conditions'], self.high['actions'][0]['activation_conditions'])

    def test_no_local_delta_does_not_create_a_row(self):
        for actor in (self.high, self.low):
            action = actor['actions'][0]
            action['scenarios'][0]['values'] = copy.deepcopy(action['baseline'])
        self.assertEqual(self.enrage_rows(self.rows()), [])

    def test_missing_exclusion_proof_still_requires_local_binding(self):
        for missing in ('actor_policy', 'state_exclusion'):
            with self.subTest(missing=missing):
                high, low = copy.deepcopy(REAL_ACTOR), copy.deepcopy(REAL_ACTOR)
                for actor in (high, low):
                    if missing == 'actor_policy':
                        actor.pop('global_damage_policy')
                    else:
                        actor['global_damage_states'][0].pop('excluded_before_probe')
                self.assertEqual(self.enrage_rows(self.rows(high, low)), [])
                for actor in (high, low):
                    actor['reviewed_global_effects'][0]['local_skill_bindings'][0]['skill_spell_ids'].append(335096)
                self.assertEqual(len(self.enrage_rows(self.rows(high, low))), 1)

    def test_pure_global_state_is_excluded_even_with_delta(self):
        for actor in (self.high, self.low):
            actor['global_damage_states'][0]['partial_state'] = False
            actor['reviewed_global_effects'][0]['partial_state'] = False
        self.assertEqual(self.enrage_rows(self.rows()), [])

    def test_shared_state_without_talent_marginal_delta_is_not_attributed(self):
        variant = {
            'talent': {'id': 7, 'node_id': 7, 'tree_type': 'spec'},
            'reference_high': self.high, 'reference_low': self.low,
            'high': copy.deepcopy(self.high), 'low': copy.deepcopy(self.low),
        }
        self.assertFalse([r for r in self.rows(variants=[variant]) if r['variant']['talent_id'] == 7])

    def test_runtime_delta_uses_selected_actor_not_shared_base(self):
        reference_high, reference_low = copy.deepcopy(self.high), copy.deepcopy(self.low)
        for actor in (reference_high, reference_low):
            actor['actions'][0]['scenarios'] = []
        variant = {
            'talent': {'id': 7, 'node_id': 7, 'tree_type': 'spec'},
            'reference_high': reference_high, 'reference_low': reference_low,
            'high': self.high, 'low': self.low,
        }
        shared_base = {'actions': []}
        rows = self.enrage_rows(self.rows(shared_base, shared_base, [variant]))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['variant']['talent_id'], 7)
        self.assertTrue(rows[0]['variant']['reference_available'])
        for actor in (self.high, self.low):
            actor.pop('global_damage_policy')
        # A normalized shared actor cannot prove exclusion for the selected actor.
        self.assertEqual([r for r in self.enrage_rows(self.rows(REAL_ACTOR, REAL_ACTOR, [variant]))
                          if r['variant']['talent_id'] == 7], [])

    def test_unresolved_reference_does_not_prove_residual_delta(self):
        for actor in (self.high, self.low):
            actor['actions'][0]['baseline']['unresolved_reason'] = 'probe_unavailable'
        self.assertEqual(self.enrage_rows(self.rows()), [])

    def test_combination_cannot_borrow_another_states_delta(self):
        for actor in (self.high, self.low):
            action = actor['actions'][0]
            enrage = action['scenarios'].pop(0)
            action['scenarios'][0]['buffs'].extend(enrage['buffs'])
        # Whirlwind's actual AoE delta is not singleton Enrage evidence.
        self.assertEqual(self.enrage_rows(self.rows()), [])
