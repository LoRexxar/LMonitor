"""Revalidate only when validated runtime shells were actually discarded."""
import copy
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase

from botend.services import simc_skill_damage as damage
from botend.services.simc_skill_factor_sources import factor_source_layers


class ProfileExportValidationPassTests(SimpleTestCase):
    def setUp(self):
        self.snapshot = SimpleNamespace(simc_revision='a' * 40, game_build='12.1.0.69497')
        self.profile = SimpleNamespace(class_name='rogue', spec='rogue_outlaw', talent='')
        self.service = damage.SimcSkillDamageSnapshotService(
            self.snapshot, backend=SimpleNamespace(),
        )

    def component(self, kind='direct', *, empty=False):
        component = {
            'hit': 100.0, 'crit': 200.0, 'expected': 120.0,
            'crit_multiplier': 2.0, 'crit_chance': 0.2, 'crit_chance_uncapped': 0.2,
            'can_crit': True, 'armor_multiplier': 1.0, 'damage_equivalent_count': 1.0,
            'base_damage_layers': {'base_multiplier': 1.0, 'component_multiplier': 1.0},
            'runtime_layers': {
                **dict.fromkeys(damage._RUNTIME_LAYER_FIELDS[kind], 1.0),
                'specialization_passive_effects': [],
            },
            'runtime_factor_sources': [],
            'runtime_factor_source_layers': factor_source_layers(kind),
        }
        for field, value in (
            ('target_hit', 100.0), ('target_crit', 200.0), ('target_expected', 120.0),
            ('target_noncrit_contribution', 80.0), ('target_crit_contribution', 40.0),
        ):
            component[field] = dict.fromkeys(('1', '2', '5', '10', '20'), value)
        if empty:
            component.update(hit=None, crit=None, expected=None)
        return component

    def payload(self):
        amount = {'direct': self.component(), 'tick': None}
        action = {
            'token': 'test_action', 'spell_id': 1, 'supported': True, 'player_skill': True,
            'selected_trait_effects': [], 'reporting_root_token': 'test_action',
            'reporting_root_spell_id': 1, 'reporting_root_component': True,
            'dbc_scaling': {
                'source': 'spell_effect', 'requires_weapon_data': False,
                'direct': {'attack_power_coefficient': 1.0, 'spell_power_coefficient': 0.0,
                           'normalized_base': 100.0, 'effect_indexes': [0]},
                'tick': None,
            },
            'baseline': amount,
            'scenarios': [{
                'buffs': [{'token': 'test_state', 'scope': 'self', 'spell_id': 2, 'stacks': 1}],
                'affected_target_counts': [1, 2, 5, 10, 20],
                'values': copy.deepcopy(amount),
            }],
        }
        return {
            'schema_version': self.service.EXPORTER_SCHEMA_REVISION,
            'simc_revision': self.snapshot.simc_revision, 'game_build': self.snapshot.game_build,
            'normalization_basis': dict(self.service.FIXED_PRESET),
            'actors': [{
                'name': 'skill_damage_base', 'class': 'rogue', 'spec': 'outlaw',
                'talent_effectiveness': 'unknown',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'global_damage_policy': 'exclude_before_probe',
                'global_damage_states': [], 'global_scope_candidates': [], 'actions': [action],
            }],
        }

    @contextmanager
    def export(self, payload, *, reject_pass=None):
        self.events = []
        self.validated = []
        validate = self.service._validate_export
        discard = damage._discard_empty_runtime_amount_components

        def validate_export(value, **kwargs):
            self.events.append('validate')
            self.validated.append(copy.deepcopy(value))
            validate(value, **kwargs)
            if len(self.validated) == reject_pass:
                raise ValueError('validation rejected')

        def discard_shells(marked):
            self.events.append('discard')
            discard(marked)

        def run_export(command, **_kwargs):
            output = next(arg.split('=', 1)[1] for arg in command
                          if arg.startswith('skill_damage_export='))
            Path(output).write_text(json.dumps(payload), encoding='utf-8')
            return SimpleNamespace(returncode=0, stderr='', stdout='')

        with mock.patch.object(damage.SimcComposer, 'compose_validation_input',
                               return_value='rogue="reference"\nspec=outlaw\n'), \
                mock.patch.object(damage, 'build_single_talent_actor_input',
                                  return_value='rogue="skill_damage_base"\nspec=outlaw\n'), \
                mock.patch.object(self.service, '_binary_path', return_value='/unused/simc'), \
                mock.patch.object(damage.subprocess, 'run', side_effect=run_export), \
                mock.patch.object(self.service, '_validate_export', side_effect=validate_export), \
                mock.patch.object(damage, '_discard_empty_runtime_amount_components',
                                  side_effect=discard_shells), \
                mock.patch.object(damage, 'collect_skill_damage_unresolved',
                                  wraps=damage.collect_skill_damage_unresolved) as collect:
            yield lambda: self.service._run_profile_export(self.profile, []), collect

    def test_unchanged_graph_is_validated_once_including_existing_unresolved_amounts(self):
        for unresolved in (False, True):
            with self.subTest(unresolved=unresolved):
                payload = self.payload()
                if unresolved:
                    payload['actors'][0]['actions'][0]['baseline'] = {
                        'direct': None, 'tick': None, 'unresolved_reason': 'native_unknown',
                    }
                with self.export(payload) as (run, collect):
                    result = run()
                self.assertEqual(self.events, ['validate'])
                self.assertEqual(self.validated, [payload])
                self.assertEqual(result['actors'], payload['actors'])
                collect.assert_called_once()

    def test_shells_and_mixed_amounts_keep_validation_before_and_after_discard(self):
        for mixed in (False, True):
            for original_reason in (None, 'native_unknown'):
                with self.subTest(mixed=mixed, original_reason=original_reason):
                    payload = self.payload()
                    action = payload['actors'][0]['actions'][0]
                    for amount in (action['baseline'], action['scenarios'][0]['values']):
                        amount['tick'] = self.component('tick', empty=True)
                        if not mixed:
                            amount['direct'] = self.component(empty=True)
                        if original_reason:
                            amount['unresolved_reason'] = original_reason
                    with self.export(payload) as (run, collect):
                        result = run()
                    self.assertEqual(self.events, ['validate', 'discard', 'validate'])
                    self.assertEqual(len(self.validated), 2)
                    before = self.validated[0]['actors'][0]['actions'][0]
                    after = result['actors'][0]['actions'][0]
                    for old, new in ((before['baseline'], after['baseline']),
                                     (before['scenarios'][0]['values'], after['scenarios'][0]['values'])):
                        self.assertIsInstance(old['tick'], dict)
                        self.assertEqual(old['unresolved_reason'],
                                         original_reason or 'runtime_non_finite_amount')
                        self.assertIsNone(new['tick'])
                        if mixed:
                            self.assertEqual(new['direct'], action['baseline']['direct'])
                            self.assertEqual(new.get('unresolved_reason'), original_reason)
                        else:
                            self.assertIsNone(new['direct'])
                            self.assertEqual(new['unresolved_reason'],
                                             original_reason or 'runtime_non_finite_amount')
                    self.assertEqual(self.validated[1]['actors'], result['actors'])
                    collect.assert_called_once()

    def test_malformed_shell_is_rejected_before_discard_or_unresolved_collection(self):
        payload = self.payload()
        shell = self.component('tick', empty=True)
        del shell['armor_multiplier']
        payload['actors'][0]['actions'][0]['baseline']['tick'] = shell
        with self.export(payload) as (run, collect):
            with self.assertRaisesRegex(ValueError, '护甲结算因子'):
                run()
        self.assertEqual(self.events, ['validate'])
        self.assertEqual(self.validated[0]['actors'][0]['actions'][0]['baseline']['tick'], shell)
        collect.assert_not_called()

    def test_validation_exceptions_preserve_rejection_order(self):
        for shells, reject_pass in ((False, 1), (True, 1), (True, 2)):
            with self.subTest(shells=shells, reject_pass=reject_pass):
                payload = self.payload()
                if shells:
                    payload['actors'][0]['actions'][0]['baseline']['tick'] = self.component(
                        'tick', empty=True,
                    )
                with self.export(payload, reject_pass=reject_pass) as (run, collect):
                    with self.assertRaisesRegex(ValueError, 'validation rejected'):
                        run()
                self.assertEqual(self.events, ['validate'] if reject_pass == 1 else
                                 ['validate', 'discard', 'validate'])
                collect.assert_not_called()
