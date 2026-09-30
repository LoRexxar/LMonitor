"""A candidate relationship never authorizes damage normalization."""
import copy
import unittest
from botend.services.simc_skill_passive_projection import normalize_native_specialization_passives
AMOUNTS = ('hit', 'crit', 'expected', 'noncrit_contribution', 'crit_contribution')
PRODUCT_FIELDS = (('hit', 'current_talent_damage'), ('crit', 'crit_damage'),
                  ('expected', 'normalized_expected'),
                  ('noncrit_contribution', 'noncrit_contribution'),
                  ('crit_contribution', 'crit_contribution'))

def native_execute_actor(*, proven=False):
    actions = []
    for token, spell_id, hit, crit, expected, noncrit, crit_part, native_base in (
        ('execute_mainhand', 280849, 587.5337, 1198.568748, 709.7407096,
         470.02696, 239.7137496, 385.268),
        ('execute_offhand', 163558, 293.76685, 599.284374, 354.8703548,
         235.01348, 119.8568748, 192.634),
    ):
        component = dict(zip(AMOUNTS, (hit, crit, expected, noncrit, crit_part)))
        component.update({
            'crit_chance': 0.2, 'crit_chance_uncapped': 0.2,
            'crit_multiplier': 2.04, 'can_crit': True,
            'damage_equivalent_count': 1, 'native_base_damage': native_base,
            'base_damage_layers': {'base_multiplier': 1, 'component_multiplier': 1.525},
            'runtime_layers': {
                'da_multiplier': 1.525, 'target_da_multiplier': 1.0,
                'specialization_passive_effects': [{
                    'source_spell_id': 137050, 'source_name': 'Fury Warrior',
                    'effect_index': 0, 'component': 'direct', 'factor': 1.22,
                }],
            },
            'product': {
                'dbc_base_damage_min': 385.268, 'dbc_base_damage_max': 385.268,
                'crit_multiplier': 2.04, 'actual_crit_chance': 0.2,
            },
        })
        for amount, product_field in PRODUCT_FIELDS:
            component['target_' + amount] = {
                str(count): component[amount] for count in (1, 2, 5, 10, 20)
            }
            component['product'][product_field] = component[amount]
            component['product'][product_field + '_by_target'] = dict(component['target_' + amount])
        actions.append({
            'token': token, 'spell_id': spell_id, 'supported': True,
            'reporting_root_token': 'execute', 'reporting_root_spell_id': 5308,
            'hero_subtree_ids': [999],  # action ownership must NOT become source ownership
            'variant': {'talent_id': 74887},
            'dbc_scaling': {'direct': {'normalized_base': 385.268, 'attack_power_coefficient': 3.85268, 'spell_power_coefficient': 0}},
            'baseline': {'direct': component, 'tick': None, 'unresolved_reason': None},
            'scenarios': [],
        })
    return {'class': 'warrior', 'spec': 'fury', 'actions': actions}

class NativeSpecializationPassiveProjectionTests(unittest.TestCase):
    def assert_preserved(self, actor):
        before = copy.deepcopy(actor)
        diagnostics = []
        self.assertEqual(normalize_native_specialization_passives(actor, diagnostics=diagnostics), [])
        self.assertEqual(actor, before)
        return diagnostics

    def test_legacy_candidate_preserves_all_amounts_targets_caches_and_sources(self):
        diagnostics = self.assert_preserved(native_execute_actor())
        self.assertEqual(len(diagnostics), 2)
        self.assertEqual({r['reason'] for r in diagnostics}, {'missing_runtime_application'})

    def test_parser_consistency_does_not_prove_provenance_survives_overwrite(self):
        actor = native_execute_actor()
        for row in actor['actions']:
            effect = row['baseline']['direct']['runtime_layers']['specialization_passive_effects'][0]
            effect['parser_consistency'] = {
                'method': 'registered_passive_base_consistency_v1',
                'action_spell_id': row['spell_id'], 'layer': 'da_multiplier',
                'parser_multiplier': 1.22, 'base_multiplier': 1.22,
                'runtime_multiplier': 1.525, 'runtime_without_parser_multiplier': 1.25,
            }
        diagnostics = self.assert_preserved(actor)
        self.assertEqual({r['reason'] for r in diagnostics}, {'parser_consistency_not_application'})

    def test_arbitrary_application_claim_is_not_trusted(self):
        actor = native_execute_actor()
        for row in actor['actions']:
            row['baseline']['direct']['runtime_layers']['specialization_passive_effects'][0]['application'] = {'applied': True}
        self.assertEqual({r['reason'] for r in self.assert_preserved(actor)}, {'unsupported_application_evidence'})

    def test_invalid_consistency_is_diagnostic_not_permission(self):
        actor = native_execute_actor()
        actor['actions'][0]['baseline']['direct']['runtime_layers']['specialization_passive_effects'][0]['parser_consistency'] = {}
        self.assertEqual(self.assert_preserved(actor)[0]['reason'], 'invalid_parser_consistency')

    def test_direct_tick_and_factor_phases_are_never_guessed(self):
        actor = native_execute_actor()
        row = actor['actions'][0]
        row['baseline']['tick'] = copy.deepcopy(row['baseline']['direct'])
        row['baseline']['direct']['runtime_factor_layers'] = [
            {'phase': 'actor_baseline', 'layer': 'da_multiplier', 'factor': 1.22},
            {'phase': 'talent_marginal', 'layer': 'da_multiplier', 'factor': 1.25},
        ]
        self.assertEqual({r['component'] for r in self.assert_preserved(actor)}, {'direct', 'tick'})

    def test_no_provenance_is_not_reverse_inferred(self):
        actor = native_execute_actor()
        for row in actor['actions']:
            row['baseline']['direct']['runtime_layers'].pop('specialization_passive_effects')
        self.assertEqual(self.assert_preserved(actor), [])

    def test_diagnostics_are_stable_and_do_not_duplicate_inside_the_actor(self):
        actor = native_execute_actor()
        self.assertEqual(self.assert_preserved(actor), self.assert_preserved(actor))
