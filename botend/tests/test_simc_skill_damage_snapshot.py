import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase, override_settings

from botend.models import (
    SimcAplSymbol, SimcAplSymbolScope, SimcBackendBinary,
    SimcSkillDamageSnapshot, WowSpellSnapshot, WowTalentNodeMetadata, WowTalentVersion,
)
from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService,
    attach_runtime_product_metrics,
    build_single_talent_actor_input,
    classify_global_damage_modifiers,
    flatten_single_talent_damage_variants,
    project_skill_damage_product_payload,
)
from botend.dashboard.api import SimcSkillDamageSnapshotAPIView


class SimcSkillDamageSnapshotModelTests(TestCase):
    def test_identity_is_only_revision_game_build_and_schema_revision(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='a' * 40, game_build='12.1.0.69299', schema_revision=1,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                SimcSkillDamageSnapshot.objects.create(
                    simc_revision='a' * 40, game_build='12.1.0.69299', schema_revision=1,
                )

    def test_latest_success_ignores_newer_failed_snapshot(self):
        succeeded = SimcSkillDamageSnapshot.objects.create(
            simc_revision='a' * 40, game_build='12.1.0.69299', schema_revision=1,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{'specialization': 'fury'}]},
        )
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='b' * 40, game_build='12.1.0.69300', schema_revision=1,
            status=SimcSkillDamageSnapshot.STATUS_FAILED, error_text='broken',
        )
        self.assertEqual(SimcSkillDamageSnapshot.latest_success().pk, succeeded.pk)


class SimcSkillDamageSnapshotServiceTests(TestCase):
    def test_profile_export_normalizes_destruction_mastery_rng(self):
        snapshot = SimpleNamespace(simc_revision='a' * 40, game_build='12.1.0.69404')
        profile = SimpleNamespace(class_name='warlock', spec='warlock_destruction', talent='')
        service = SimcSkillDamageSnapshotService(snapshot, backend=SimpleNamespace())

        def run_export(command, **_kwargs):
            output_path = next(
                value.split('=', 1)[1]
                for value in command
                if value.startswith('skill_damage_export=')
            )
            Path(output_path).write_text(json.dumps({'actors': []}), encoding='utf-8')
            return SimpleNamespace(returncode=0, stderr='', stdout='')

        with mock.patch(
            'botend.services.simc_skill_damage.SimcComposer.compose_validation_input',
            return_value='warlock="reference"\nspec=destruction\n',
        ), mock.patch(
            'botend.services.simc_skill_damage.build_single_talent_actor_input',
            return_value='warlock="skill_damage_base"\nspec=destruction\n',
        ) as build, mock.patch.object(
            service, '_binary_path', return_value='/tmp/simc',
        ), mock.patch.object(
            service, '_validate_export',
        ), mock.patch(
            'botend.services.simc_skill_damage.subprocess.run', side_effect=run_export,
        ):
            service._run_profile_export(profile, [])

        self.assertIn(
            'warlock.normalize_destruction_mastery=1',
            build.call_args.args[0].splitlines(),
        )

    def test_single_talent_input_uses_one_baseline_actor_and_trait_entry_actors_only(self):
        profile_input = (
            'warrior="Fury Reference"\n'
            'spec=fury\n'
            'talents=FULL_PRESET_BUILD\n'
            'class_talents=old:1\n'
            'waist = scabrous_zombie_leather_belt,id=49810,bonus_id=1808/6652\n'
            'shoulders=stale_shoulders,id=2\n'
            'wrists=stale_wrists,id=3\n'
            'main_hand = stale_weapon_name,id=4,ilevel=289\n'
            'off_hand=invalid_weapon_without_id\n'
            'iterations=100\n'
        )
        talents = [
            SimpleNamespace(pk=1, tree_type='spec', node_id=136454, max_points=1),
            SimpleNamespace(pk=2, tree_type='hero', node_id=117404, max_points=1),
        ]

        generated = build_single_talent_actor_input(profile_input, 'warrior', talents)

        self.assertNotIn('talents=FULL_PRESET_BUILD', generated)
        self.assertNotIn('class_talents=old:1', generated)
        self.assertNotIn('scabrous_zombie_leather_belt', generated)
        self.assertNotIn('waist', generated)
        self.assertNotIn('shoulders=', generated)
        self.assertNotIn('wrists=', generated)
        self.assertNotIn('stale_weapon_name', generated)
        self.assertNotIn('invalid_weapon_without_id', generated)
        self.assertEqual(generated.count('main_hand=,id=4,ilevel=289'), 5)
        self.assertEqual(generated.count('iterations=100'), 5)
        self.assertEqual(generated.count('warrior="skill_damage_'), 5)
        self.assertIn('warrior="skill_damage_base"', generated)
        self.assertIn('warrior="skill_damage_reference_1"', generated)
        self.assertIn('warrior="skill_damage_talent_1"', generated)
        self.assertIn('spec_talents=136454:1', generated)
        self.assertIn('warrior="skill_damage_reference_2"', generated)
        self.assertIn('warrior="skill_damage_talent_2"', generated)
        self.assertIn('hero_talents=117404:1', generated)

    def test_single_talent_input_builds_prerequisite_reference_and_selected_actor_pair(self):
        slayer_root = SimpleNamespace(
            pk=74911, tree_type='hero', node_id=117411, max_points=1,
        )
        relentless = SimpleNamespace(
            pk=74890, tree_type='hero', node_id=117392, max_points=1,
        )

        generated = build_single_talent_actor_input(
            'warrior="Fury Reference"\nspec=fury\ntalents=legacy\n',
            'warrior',
            [relentless],
            talent_prerequisites={relentless.pk: [slayer_root]},
        )

        reference = generated.split('warrior="skill_damage_reference_74890"', 1)[1].split(
            'warrior="skill_damage_talent_74890"', 1
        )[0]
        selected = generated.split('warrior="skill_damage_talent_74890"', 1)[1]
        self.assertIn('hero_talents=117411:1', reference)
        self.assertNotIn('117392:1', reference)
        self.assertIn('hero_talents=117411:1/117392:1', selected)
        self.assertEqual(generated.count('warrior="skill_damage_'), 3)

    def test_prerequisite_map_chooses_one_shortest_valid_parent_path(self):
        left_root = SimpleNamespace(
            pk=1, tree_type='hero', node_id=101, parents_json=[], flags=0,
        )
        left_parent = SimpleNamespace(
            pk=2, tree_type='hero', node_id=102, parents_json=[101], flags=0,
        )
        right_parent = SimpleNamespace(
            pk=3, tree_type='hero', node_id=103, parents_json=[], flags=0,
        )
        target = SimpleNamespace(
            pk=4, tree_type='hero', node_id=104, parents_json=[102, 103], flags=0,
        )

        prerequisite_map = SimcSkillDamageSnapshotService._talent_prerequisite_map(
            [left_root, left_parent, right_parent, target],
        )

        self.assertEqual(
            [talent.node_id for talent in prerequisite_map[target.pk]],
            [103],
        )

    def test_prerequisite_map_uses_valid_or_path_when_another_parent_is_missing(self):
        valid_parent = SimpleNamespace(
            pk=1, tree_type='hero', node_id=103, parents_json=[], flags=0,
        )
        target = SimpleNamespace(
            pk=2, tree_type='hero', node_id=104, parents_json=[999, 103], flags=0,
        )

        prerequisite_map = SimcSkillDamageSnapshotService._talent_prerequisite_map(
            [valid_parent, target],
        )

        self.assertEqual(
            [talent.node_id for talent in prerequisite_map[target.pk]],
            [103],
        )

    def test_prerequisite_map_rejects_talent_when_every_parent_path_is_missing(self):
        target = SimpleNamespace(
            pk=1, tree_type='hero', node_id=104, parents_json=[998, 999], flags=0,
        )

        with self.assertRaisesRegex(ValueError, '没有有效前置路径'):
            SimcSkillDamageSnapshotService._talent_prerequisite_map([target])

    def test_prerequisite_map_treats_hero_anchor_as_implicit_metadata(self):
        anchor = SimpleNamespace(
            pk=1, tree_type='hero_anchor', node_id=125051, parents_json=[],
        )
        unity_within = SimpleNamespace(
            pk=2, tree_type='hero', node_id=125058, parents_json=[125051],
        )

        prerequisite_map = SimcSkillDamageSnapshotService._talent_prerequisite_map(
            [unity_within], metadata_nodes=[anchor],
        )

        self.assertEqual(prerequisite_map[unity_within.pk], [])

    def test_single_talent_input_keeps_granted_scaffold_out_of_probe_pairs(self):
        root = SimpleNamespace(
            pk=74850, tree_type='spec', node_id=112261, max_points=1, flags=8,
        )
        scent = SimpleNamespace(
            pk=74957, tree_type='spec', node_id=136454, max_points=1, flags=0,
        )

        generated = build_single_talent_actor_input(
            'warrior="Fury Reference"\nspec=fury\ntalents=legacy\n',
            'warrior',
            [root, scent],
            scaffold_talents=[root],
        )

        self.assertEqual(generated.count('warrior="skill_damage_'), 3)
        self.assertNotIn('skill_damage_reference_74850', generated)
        self.assertNotIn('skill_damage_talent_74850', generated)
        self.assertEqual(generated.count('spec_talents=112261:1'), 3)
        self.assertEqual(generated.count('spec_talents=112261:1/136454:1'), 1)
        self.assertNotIn('talents=legacy', generated)

    def test_single_talent_input_replaces_conflicting_scaffold_choice_in_selected_actor(self):
        scaffold = SimpleNamespace(
            pk=1, talent_id=900, tree_type='spec', node_id=101, max_points=1,
        )
        alternative = SimpleNamespace(
            pk=2, talent_id=900, tree_type='spec', node_id=102, max_points=1,
        )

        generated = build_single_talent_actor_input(
            'warrior="Reference"\nspec=fury\n',
            'warrior',
            [alternative],
            scaffold_talents=[scaffold],
        )

        reference = generated.split('warrior="skill_damage_reference_2"', 1)[1].split(
            'warrior="skill_damage_talent_2"', 1
        )[0]
        selected = generated.split('warrior="skill_damage_talent_2"', 1)[1]
        self.assertIn('spec_talents=101:1', reference)
        self.assertNotIn('102:1', reference)
        self.assertIn('spec_talents=102:1', selected)
        self.assertNotIn('101:1', selected)

    def test_bloodthirst_is_flattened_to_base_and_each_runtime_single_talent_condition(self):
        def action(hit, *, scenario=None):
            amount = {
                'direct': {
                    'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit * 1.2,
                },
                'tick': None,
            }
            row = {
                'name': 'bloodthirst', 'token': 'bloodthirst', 'spell_id': 23881,
                'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'dbc_scaling': {
                    'source': 'spell_effect',
                    'direct': {'attack_power_coefficient': 2.0, 'spell_power_coefficient': 0.0,
                               'normalized_base': 200.0, 'effect_indexes': [0]},
                    'tick': None, 'requires_weapon_data': False,
                },
                'baseline': amount,
                'scenarios': [],
            }
            if scenario:
                row['scenarios'].append({'active_buffs': [scenario], 'amount': amount})
            return row

        base_high = {'actions': [action(200.0)]}
        base_low = {'actions': [action(200.0)]}
        base_high['actions'][0]['scenarios'] = [
            {'active_buffs': ['defensive_stance'], 'amount': action(180.0)['baseline']},
        ]
        base_low['actions'][0]['scenarios'] = [
            {'active_buffs': ['defensive_stance'], 'amount': action(180.0)['baseline']},
        ]
        variants = [
            {
                'talent': {
                    'id': 11, 'name': 'Scent of Blood', 'name_zh': '血之气息',
                    'tree_type': 'hero', 'hero_subtree_id': 60,
                    'hero_subtree_name': 'Slayer', 'hero_subtree_name_zh': '屠戮者',
                },
                'high': {'actions': [action(200.0, scenario='bloodcraze')]},
                'low': {'actions': [action(200.0)]},
                'scenario_amounts': {'bloodcraze': 240.0},
            },
            {
                'talent': {'id': 12, 'name': 'Vicious Contempt', 'name_zh': '恶毒蔑视'},
                'high': {'actions': [action(200.0)]},
                'low': {'actions': [action(300.0)]},
            },
            {
                'talent': {'id': 13, 'name': 'Burst of Power', 'name_zh': '能量爆发'},
                'high': {'actions': [action(200.0, scenario='burst_of_power')]},
                'low': {'actions': [action(200.0)]},
                'scenario_amounts': {'burst_of_power': 220.0},
            },
        ]
        for variant in variants:
            variant['reference_high'] = base_high
            variant['reference_low'] = base_low
        variants[1]['high']['actions'][0]['scenarios'] = [
            {'active_buffs': ['defensive_stance'], 'amount': action(180.0)['baseline']},
        ]
        variants[1]['low']['actions'][0]['scenarios'] = [
            {'active_buffs': ['defensive_stance'], 'amount': action(270.0)['baseline']},
        ]
        # Replace scenario values with independently exported SimC values.
        for variant in variants:
            for scenario in variant['high']['actions'][0]['scenarios']:
                hit = variant.get('scenario_amounts', {}).get(scenario['active_buffs'][0])
                if hit is not None:
                    scenario['amount'] = action(hit)['baseline']

        rows = flatten_single_talent_damage_variants(base_high, base_low, variants)

        self.assertEqual(
            [(row['variant']['talent_name_zh'], row['variant']['runtime_condition'],
              row['baseline']['direct']['hit']) for row in rows],
            [
                ('', '', 200.0),
                ('血之气息', '探针条件：启用 bloodcraze buff', 240.0),
                ('恶毒蔑视', '目标生命值低于 35%', 300.0),
                ('恶毒蔑视', '目标生命值低于 35% + 探针启用 defensive_stance buff', 270.0),
                ('能量爆发', '探针条件：启用 burst_of_power buff', 220.0),
            ],
        )
        hero_row = next(row for row in rows if row['variant']['talent_id'] == 11)
        self.assertEqual(
            {key: hero_row['variant'][key] for key in (
                'hero_subtree_id', 'hero_subtree_name', 'hero_subtree_name_zh',
            )},
            {'hero_subtree_id': 60, 'hero_subtree_name': 'Slayer',
             'hero_subtree_name_zh': '屠戮者'},
        )

    def test_flatten_preserves_component_changes_same_token_scenarios_and_each_health_condition(self):
        def amount(direct, tick):
            return {
                'direct': {
                    'hit': direct, 'crit': direct * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': direct,
                },
                'tick': {
                    'hit': tick, 'crit': tick * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': tick,
                },
            }

        def action(direct, tick, scenarios=()):
            return {
                'name': 'test_action', 'token': 'test_action', 'spell_id': 42,
                'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'dbc_scaling': {
                    'source': 'spell_effect',
                    'direct': {'attack_power_coefficient': 1.0, 'spell_power_coefficient': 0.0,
                               'normalized_base': 100.0, 'effect_indexes': [1]},
                    'tick': {'attack_power_coefficient': 1.0, 'spell_power_coefficient': 0.0,
                             'normalized_base': 100.0, 'effect_indexes': [2]},
                    'requires_weapon_data': False,
                },
                'baseline': amount(direct, tick),
                'scenarios': [
                    {'active_buffs': list(tokens), 'amount': amount(scenario_direct, scenario_tick)}
                    for tokens, scenario_direct, scenario_tick in scenarios
                ],
            }

        base_high = {'actions': [action(100, 100, [(('foo',), 110, 100)])]}
        base_low = {'actions': [action(80, 100, [(('foo',), 90, 100)])]}
        variants = [{
            'talent': {'id': 7, 'name': 'Truthful Talent', 'name_zh': '真实天赋'},
            'reference_high': base_high,
            'reference_low': base_low,
            'high': {'actions': [action(110, 90, [
                (('foo',), 150, 100),
                (('bar',), 140, 100),
            ])]},
            'low': {'actions': [action(130, 90, [
                (('foo',), 170, 100),
                (('bar',), 160, 100),
            ])]},
        }]

        rows = flatten_single_talent_damage_variants(base_high, base_low, variants)
        observed = [
            (
                row['variant']['runtime_condition'],
                row['baseline']['direct']['hit'],
                row['baseline']['tick']['hit'],
            )
            for row in rows
        ]
        self.assertEqual(observed, [
            ('', 100, 100),
            ('目标生命值低于 35%', 80, 100),
            ('', 110, 90),
            ('探针条件：启用 foo buff', 150, 100),
            ('探针条件：启用 bar buff', 140, 100),
            ('目标生命值低于 35%', 130, 90),
            ('目标生命值低于 35% + 探针启用 foo buff', 170, 100),
            ('目标生命值低于 35% + 探针启用 bar buff', 160, 100),
        ])

    def test_flatten_compares_each_talent_against_its_prerequisite_actor(self):
        def amount(hit):
            return {
                'direct': {
                    'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit,
                },
                'tick': None,
            }

        def action(token, spell_id, hit):
            return {
                'name': token, 'token': token, 'spell_id': spell_id,
                'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True, 'baseline': amount(hit), 'scenarios': [],
                'dbc_scaling': {
                    'source': 'spell_effect',
                    'direct': {'attack_power_coefficient': 1.0, 'spell_power_coefficient': 0.0,
                               'normalized_base': 100.0, 'effect_indexes': [0]},
                    'tick': None, 'requires_weapon_data': False,
                },
            }

        slayers_strike = action('slayers_strike', 445579, 100.0)
        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [{
                'talent': {
                    'id': 74890, 'name': 'Relentless Pursuit', 'name_zh': '冷酷追杀',
                },
                'reference_high': {'actions': [slayers_strike]},
                'reference_low': {'actions': [slayers_strike]},
                'high': {'actions': [slayers_strike]},
                'low': {'actions': [slayers_strike]},
            }],
        )

        self.assertFalse(any(
            row.get('token') == 'slayers_strike'
            and (row.get('variant') or {}).get('talent_name') == 'Relentless Pursuit'
            for row in rows
        ))

    def test_flatten_keeps_health_difference_for_action_introduced_by_talent(self):
        def amount(hit):
            return {
                'direct': {
                    'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit,
                },
                'tick': None,
            }

        def action(hit):
            return {
                'name': 'introduced_action', 'spell_id': 42, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'baseline': amount(hit), 'scenarios': [],
                'dbc': {'resolved': True, 'scaling_type': 'ap', 'coefficient': 1.0,
                        'base_value_min': 100.0, 'base_value_max': 100.0},
            }

        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [{
                'talent': {'id': 1, 'name': 'Introducer', 'name_zh': '引入技能'},
                'high': {'actions': [action(100.0)]},
                'low': {'actions': [action(130.0)]},
            }],
        )
        self.assertEqual(
            [(row['variant']['runtime_condition'], row['baseline']['direct']['hit']) for row in rows],
            [('', 100.0), ('目标生命值低于 35%', 130.0)],
        )

    def test_flatten_rejects_conflicting_duplicate_scenario_tokens(self):
        def amount(hit):
            return {
                'direct': {'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
                           'crit_chance': 0.2, 'expected': hit},
                'tick': None,
            }

        action = {
            'name': 'conflict', 'spell_id': 43, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
            'baseline': amount(100.0),
            'scenarios': [
                {'active_buffs': ['foo', 'bar'], 'amount': amount(120.0)},
                {'active_buffs': ['bar', 'foo'], 'amount': amount(140.0)},
            ],
            'dbc': {'resolved': True, 'scaling_type': 'ap', 'coefficient': 1.0,
                    'base_value_min': 100.0, 'base_value_max': 100.0},
        }
        with self.assertRaisesRegex(ValueError, '同一 scenario tokens 返回冲突数值'):
            flatten_single_talent_damage_variants(
                {'actions': []}, {'actions': []}, [{
                    'talent': {'id': 1, 'name': 'Conflict'},
                    'high': {'actions': [action]}, 'low': {'actions': []},
                }],
            )

    def test_product_metrics_combine_dbc_base_with_selected_talent_runtime(self):
        selected = {'actions': [
            {
                'token': 'bloodthirst', 'spell_id': 23881, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'dbc_scaling': {
                    'source': 'spell_effect',
                    'direct': {
                        'attack_power_coefficient': 2.0,
                        'spell_power_coefficient': 0.0,
                        'normalized_base': 200.0,
                        'effect_indexes': [1],
                    },
                    'tick': None,
                    'requires_weapon_data': False,
                },
                'baseline': {
                    'direct': {'hit': 220.0, 'crit': 484.0, 'crit_multiplier': 2.2,
                               'crit_chance': 0.2, 'expected': 272.8},
                },
            },
        ]}

        attach_runtime_product_metrics(selected)

        bloodthirst = selected['actions'][0]['baseline']['direct']['product']
        self.assertEqual(bloodthirst, {
            'dbc_base_damage_min': 200.0,
            'dbc_base_damage_max': 200.0,
            'current_talent_damage': 220.0,
            'crit_damage': 484.0,
            'crit_multiplier': 2.2,
            'actual_crit_chance': 0.2,
            'normalized_expected': 272.8,
            'dbc_unresolved_reason': '',
        })
        self.assertNotIn('talent_gain_pct', bloodthirst)

    def test_product_projection_only_returns_valid_damage_components_without_mutating_raw_payload(self):
        payload = {'actors': [{'actions': [
            {'token': 'valid', 'spell_id': 1, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
             'dbc_scaling': {'direct': {
                 'attack_power_coefficient': 0.75,
                 'spell_power_coefficient': 0.25,
             }},
             'baseline': {'direct': {
                'product': {'dbc_base_damage_min': 100.0, 'dbc_base_damage_max': 100.0,
                            'current_talent_damage': 120.0, 'crit_damage': 240.0,
                            'crit_multiplier': 2.0, 'actual_crit_chance': 0.2,
                            'normalized_expected': 144.0,
                            'dbc_unresolved_reason': ''}}}},
            {'token': 'unsupported', 'spell_id': 2, 'supported': False,
             'unsupported_reason': 'action_has_no_damage_component'},
            {'token': 'unresolved', 'spell_id': 3, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
             'baseline': {'unresolved_reason': 'snapshot_child_signal_11'}},
        ]}]}
        original = json.loads(json.dumps(payload))

        projected = project_skill_damage_product_payload(payload)

        self.assertEqual(payload, original)
        self.assertEqual(len(projected['actors'][0]['actions']), 1)
        action = projected['actors'][0]['actions'][0]
        self.assertEqual(action['component'], 'combined')
        self.assertEqual(action['component_count'], 1)
        self.assertEqual(action['product']['attack_power_coefficient'], 0.75)
        self.assertEqual(action['product']['spell_power_coefficient'], 0.25)
        self.assertEqual(action['product']['normalized_base_damage'], 100.0)
        self.assertEqual(action['product']['runtime_multiplier'], 1.2)
        self.assertEqual(action['product']['final_normalized_damage'], 120.0)

    def test_product_projection_does_not_create_groups_for_invalid_dbc_components(self):
        def action(token, *, base=100.0, ap=1.0, count=1.0):
            return {
                'token': token, 'spell_id': token, 'supported': True,
                'reporting_root_token': token, 'reporting_root_spell_id': token,
                'reporting_root_component': True,
                'dbc_scaling': {'direct': {
                    'attack_power_coefficient': ap,
                    'spell_power_coefficient': 0.0,
                }},
                'baseline': {'direct': {
                    'damage_equivalent_count': count,
                    'product': {
                        'dbc_base_damage_min': base,
                        'dbc_base_damage_max': 100.0,
                        'current_talent_damage': 120.0,
                        'crit_damage': 240.0,
                        'crit_multiplier': 2.0,
                        'actual_crit_chance': 0.2,
                        'normalized_expected': 144.0,
                    },
                }},
            }

        payload = {'actors': [{'actions': [
            action('bad_base', base=90.0),
            action('bad_count', count=0.0),
            action('bad_coefficient', ap=None),
        ]}]}

        projected = project_skill_damage_product_payload(payload)

        self.assertEqual(projected['actors'][0]['actions'], [])
        self.assertEqual(projected['display_action_count'], 0)

    def test_flatten_preserves_talent_that_only_changes_dot_equivalent_tick_count(self):
        def amount(count):
            return {
                'direct': None,
                'tick': {
                    'hit': 10.0, 'crit': 20.0, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': 12.0,
                    'damage_equivalent_count': count,
                },
                'unresolved_reason': None,
            }

        def action(count):
            return {
                'token': 'dot', 'spell_id': 1, 'supported': True,
                'reporting_root_token': 'dot', 'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'dbc_scaling': {'tick': {
                    'attack_power_coefficient': 0.1,
                    'spell_power_coefficient': 0.0,
                    'normalized_base': 10.0,
                }},
                'baseline': amount(count), 'scenarios': [],
            }

        reference = {'actions': [action(4.0)]}
        selected = {'actions': [action(5.0)]}
        rows = flatten_single_talent_damage_variants({}, {}, [{
            'talent': {'id': 77, 'name': 'Longer Dot'},
            'reference_high': reference, 'high': selected,
            'reference_low': reference, 'low': selected,
        }])

        talent_rows = [row for row in rows if row['variant']['talent_id'] == 77]
        self.assertEqual(len(talent_rows), 1)
        self.assertEqual(
            talent_rows[0]['baseline']['tick']['damage_equivalent_count'], 5.0,
        )

    def test_product_projection_merges_reporting_root_components_and_periodic_total(self):
        def leaf(token, spell_id, component, base, hit, *, count=1.0):
            return {
                'token': token, 'spell_id': spell_id, 'name': '暴怒', 'supported': True,
                'reporting_root_token': 'rampage', 'reporting_root_spell_id': 184367,
                'reporting_root_component': True,
                'variant': {'talent_id': None, 'runtime_condition': ''},
                'dbc_scaling': {component: {
                    'attack_power_coefficient': base / 100.0,
                    'spell_power_coefficient': 0.0,
                }},
                'baseline': {component: {'damage_equivalent_count': count, 'product': {
                    'dbc_base_damage_min': base, 'dbc_base_damage_max': base,
                    'current_talent_damage': hit, 'crit_damage': hit * 2,
                    'crit_multiplier': 2.0, 'actual_crit_chance': 0.2,
                    'normalized_expected': hit * 1.2, 'dbc_unresolved_reason': '',
                }}},
            }

        payload = {'actors': [{'actions': [
            leaf('rampage1', 1, 'direct', 100.0, 120.0),
            leaf('rampage2', 2, 'direct', 150.0, 180.0),
            leaf('rampage_dot', 3, 'tick', 10.0, 12.0, count=4.0),
        ]}]}

        actor = project_skill_damage_product_payload(payload)['actors'][0]

        self.assertEqual(len(actor['actions']), 1)
        action = actor['actions'][0]
        self.assertEqual(action['token'], 'rampage')
        self.assertEqual(action['spell_id'], 184367)
        self.assertEqual(action['component_count'], 3)
        self.assertEqual(action['product']['normalized_base_damage'], 290.0)
        self.assertEqual(action['product']['final_normalized_damage'], 348.0)
        self.assertEqual(action['product']['runtime_multiplier'], 1.2)

    def test_global_modifier_requires_uniform_high_low_baselines_and_scenarios(self):
        def amount(hit, *, count=1.0):
            return {
                'direct': {
                    'hit': hit, 'crit': hit * 2.0, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit * 1.2,
                    'damage_equivalent_count': count,
                },
                'tick': None,
                'unresolved_reason': None,
            }

        def action(token, hit, *, scenario_hit=None, count=1.0):
            row = {
                'token': token, 'spell_id': token, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'baseline': amount(hit, count=count),
                'scenarios': [],
            }
            if scenario_hit is not None:
                row['scenarios'] = [{
                    'active_buffs': ['probe'], 'amount': amount(scenario_hit, count=count),
                }]
            return row

        talent = {
            'id': 9, 'name': 'Universal', 'name_zh': '全局增伤',
            'description': 'Increases all damage you deal by 10%.',
        }
        reference_high = {
            'actions': [action('a', 100.0, scenario_hit=80.0), action('b', 200.0)],
        }
        high = {
            'actions': [action('a', 110.0, scenario_hit=88.0), action('b', 220.0)],
        }
        reference_low = {
            'actions': [action('a', 90.0, scenario_hit=70.0), action('b', 180.0)],
        }
        low = {
            'actions': [action('a', 99.0, scenario_hit=77.0), action('b', 198.0)],
        }

        def classify():
            return classify_global_damage_modifiers([{
                'talent': talent,
                'reference_high': reference_high, 'high': high,
                'reference_low': reference_low, 'low': low,
            }])

        modifiers = classify()
        self.assertEqual(modifiers[0]['talent_id'], 9)
        self.assertAlmostEqual(modifiers[0]['damage_multiplier'], 1.1)

        low['actions'][1]['baseline'] = amount(180.0)
        self.assertEqual(classify(), [])
        low['actions'][1]['baseline'] = amount(198.0)

        high['actions'][0]['scenarios'][0]['amount'] = amount(90.0)
        self.assertEqual(classify(), [])
        high['actions'][0]['scenarios'][0]['amount'] = amount(88.0)

        high['actions'][0]['baseline']['direct']['damage_equivalent_count'] = 2.0
        self.assertEqual(classify(), [])

    def test_global_modifier_accepts_selected_talent_buff_missing_from_reference_actor(self):
        def amount(hit):
            return {
                'direct': {
                    'hit': hit, 'crit': hit * 2.0, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit * 1.2,
                    'damage_equivalent_count': 1.0,
                },
                'tick': None,
                'unresolved_reason': None,
            }

        def action(token, hit, *, avatar_hit=None):
            row = {
                'token': token, 'spell_id': token, 'supported': True,
                'baseline': amount(hit), 'scenarios': [],
            }
            if avatar_hit is not None:
                row['scenarios'].append({
                    'active_buffs': ['avatar'], 'amount': amount(avatar_hit),
                })
            return row

        variant = {
            'talent': {
                'id': 90415, 'name': 'Avatar', 'name_zh': '天神下凡',
                'tree_type': 'spec',
                'description': 'Transform into a colossus, increasing all damage you deal by 20%.',
                'description_zh': '化身为巨人，使你造成的所有伤害提高20%。',
            },
            'reference_high': {'actions': [
                action('a', 100.0), action('b', 200.0, avatar_hit=240.0),
            ]},
            'high': {'actions': [
                action('a', 100.0, avatar_hit=120.0),
                action('b', 200.0, avatar_hit=240.0006),
            ]},
            'reference_low': {'actions': [
                action('a', 90.0), action('b', 180.0, avatar_hit=216.0),
            ]},
            'low': {'actions': [
                action('a', 90.0, avatar_hit=108.0),
                action('b', 180.0, avatar_hit=216.0005),
            ]},
        }

        description = variant['talent'].pop('description')
        description_zh = variant['talent'].pop('description_zh')
        self.assertEqual(classify_global_damage_modifiers([variant]), [])
        variant['talent']['description'] = description
        variant['talent']['description_zh'] = description_zh

        modifiers = classify_global_damage_modifiers([variant])

        self.assertEqual(len(modifiers), 1)
        self.assertEqual(modifiers[0]['talent_id'], 90415)
        self.assertAlmostEqual(modifiers[0]['damage_multiplier'], 1.2)
        self.assertEqual(modifiers[0]['runtime_condition'], '探针条件：启用 avatar buff')
        self.assertEqual(
            flatten_single_talent_damage_variants({'actions': []}, {'actions': []}, [variant]),
            [],
        )

        variant['high']['actions'][0]['scenarios'].append({
            'active_buffs': ['focused'], 'amount': amount(130.0),
        })
        variant['low']['actions'][0]['scenarios'].append({
            'active_buffs': ['focused'], 'amount': amount(117.0),
        })
        modifiers_with_focused = classify_global_damage_modifiers([variant])
        self.assertEqual(len(modifiers_with_focused), 1)
        focused_rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [variant],
        )
        self.assertTrue(focused_rows)
        self.assertTrue(all(
            row['variant']['scenario_tokens'] == ['focused']
            for row in focused_rows
        ))
        variant['high']['actions'][0]['scenarios'].pop()
        variant['low']['actions'][0]['scenarios'].pop()

        variant['high']['actions'][0]['scenarios'][0]['amount'] = amount(100.0005)
        variant['high']['actions'][1]['scenarios'][0]['amount'] = amount(200.001)
        variant['low']['actions'][0]['scenarios'][0]['amount'] = amount(90.00045)
        variant['low']['actions'][1]['scenarios'][0]['amount'] = amount(180.0009)
        self.assertEqual(classify_global_damage_modifiers([variant]), [])

    def test_global_modifier_fails_closed_without_low_target_proof(self):
        component = {
            'hit': 100.0, 'crit': 200.0, 'crit_multiplier': 2.0,
            'crit_chance': 0.2, 'expected': 120.0, 'damage_equivalent_count': 1.0,
        }
        action = {
            'token': 'a', 'spell_id': 1, 'supported': True,
            'baseline': {'direct': component, 'tick': None, 'unresolved_reason': None},
        }
        selected = copy.deepcopy(action)
        for field in ('hit', 'crit', 'expected'):
            selected['baseline']['direct'][field] *= 1.1
        self.assertEqual(classify_global_damage_modifiers([{
            'talent': {'id': 9},
            'reference_high': {'actions': [action]}, 'high': {'actions': [selected]},
        }]), [])

    def test_global_modifier_requires_positive_unique_talent_id_and_never_deletes_bad_ids(self):
        component = {
            'hit': 100.0, 'crit': 200.0, 'crit_multiplier': 2.0,
            'crit_chance': 0.2, 'expected': 120.0, 'damage_equivalent_count': 1.0,
        }
        reference_action = {
            'token': 'a', 'spell_id': 1, 'supported': True,
            'baseline': {'direct': component, 'tick': None}, 'scenarios': [],
        }
        selected_action = copy.deepcopy(reference_action)
        for field in ('hit', 'crit', 'expected'):
            selected_action['baseline']['direct'][field] *= 1.1

        def variant(talent_id, name):
            return {
                'talent': {'id': talent_id, 'name': name},
                'reference_high': {'actions': [reference_action]},
                'reference_low': {'actions': [reference_action]},
                'high': {'actions': [selected_action]},
                'low': {'actions': [selected_action]},
            }

        for variants in (
            [variant(None, 'missing')],
            [variant(True, 'boolean')],
            [variant(0, 'zero')],
            [variant(7, 'duplicate one'), variant(7, 'duplicate two')],
        ):
            with self.subTest(ids=[row['talent']['id'] for row in variants]):
                self.assertEqual(classify_global_damage_modifiers(variants), [])
                rows = flatten_single_talent_damage_variants({'actions': []}, {'actions': []}, variants)
                self.assertEqual(
                    [row['variant']['talent_name'] for row in rows],
                    [row['talent']['name'] for row in variants],
                )

    def test_talent_constant_condition_is_blank_instead_of_single_talent_always_on(self):
        amount = {'direct': {'hit': 100.0, 'crit': 200.0, 'crit_multiplier': 2.0,
                             'crit_chance': 0.2, 'expected': 120.0},
                  'tick': None, 'unresolved_reason': None}
        action = {'token': 'x', 'spell_id': 1, 'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                  'dbc_scaling': {'direct': {'normalized_base': 100.0}},
                  'baseline': amount}
        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [{
                'talent': {'id': 1, 'name': 'X'},
                'reference_high': {'actions': []}, 'reference_low': {'actions': []},
                'high': {'actions': [action]}, 'low': {'actions': [action]},
            }],
        )
        self.assertEqual(rows[0]['variant']['runtime_condition'], '')

    def test_talent_entries_use_canonical_profile_identity_for_active_metadata(self):
        active = WowTalentVersion.objects.create(key='active', is_active=True)
        inactive = WowTalentVersion.objects.create(key='inactive', is_active=False)
        expected = WowTalentNodeMetadata.objects.create(
            talent_version=active, class_name='Hunter', spec_name='BeastMastery', tree_type='spec',
            node_id=136454, spell_id=1265355, name='Barbed Shot', max_points=1,
        )
        WowTalentNodeMetadata.objects.create(
            talent_version=inactive, class_name='Hunter', spec_name='BeastMastery', tree_type='spec',
            node_id=136448, spell_id=383885, name='Stale Talent', max_points=1,
        )
        profile = SimpleNamespace(spec='hunter_beast_mastery', class_name='hunter')

        rows = SimcSkillDamageSnapshotService(mock.Mock())._talent_entries(profile)

        self.assertEqual([row.pk for row in rows], [expected.pk])

    def test_talent_entries_keep_only_the_two_hero_subtrees_available_to_the_spec(self):
        active = WowTalentVersion.objects.create(key='active', is_active=True)
        common = WowTalentNodeMetadata.objects.create(
            talent_version=active, class_name='Warrior', spec_name='Fury', tree_type='spec',
            node_id=1, spell_id=101, name='Common', max_points=1,
        )
        slayer = WowTalentNodeMetadata.objects.create(
            talent_version=active, class_name='Warrior', spec_name='Fury', tree_type='hero',
            node_id=2, db2_subtree_id=60, spell_id=102, name='Slayer Node', max_points=1,
        )
        mountain_thane = WowTalentNodeMetadata.objects.create(
            talent_version=active, class_name='Warrior', spec_name='Fury', tree_type='hero',
            node_id=3, db2_subtree_id=61, spell_id=103, name='Mountain Thane Node', max_points=1,
        )
        WowTalentNodeMetadata.objects.create(
            talent_version=active, class_name='Warrior', spec_name='Fury', tree_type='hero',
            node_id=4, db2_subtree_id=62, spell_id=104, name='Colossus Node', max_points=1,
        )

        rows = SimcSkillDamageSnapshotService(mock.Mock())._talent_entries(
            SimpleNamespace(spec='warrior_fury', class_name='warrior')
        )

        self.assertEqual({row.pk for row in rows}, {common.pk, slayer.pk, mountain_thane.pk})

    def test_existing_schema_seven_snapshot_creates_new_schema_eight_identity(self):
        backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'is_active': True,
                'current_version': 'f' * 40, 'latest_version': 'f' * 40,
                'game_build': '12.1.0.69300', 'simc_path': sys.executable,
            },
        )
        existing = SimcSkillDamageSnapshot.objects.create(
            simc_revision='f' * 40,
            game_build='12.1.0.69300',
            schema_revision=7,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'specialization': 'fury',
                'hero_talent_tree': '屠戮者',
                'actions': [],
            }]},
        )

        service = SimcSkillDamageSnapshotService.create_for_current_backend()

        self.assertNotEqual(service.snapshot.pk, existing.pk)
        self.assertEqual(service.snapshot.schema_revision, 8)
        self.assertEqual(service.snapshot.status, SimcSkillDamageSnapshot.STATUS_PENDING)
        self.assertEqual(service.backend.pk, backend.pk)

    @override_settings(SIMC_CONFIG={'simc_path': sys.executable})
    def test_configured_runtime_binary_overrides_stale_backend_path(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=1,
        )
        service = SimcSkillDamageSnapshotService(
            snapshot,
            backend=mock.Mock(simc_path='/stale/machine/simc'),
        )
        self.assertEqual(service._binary_path(), sys.executable)

    def test_generate_batches_all_single_talent_actors_and_preserves_dataset_identity(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=8,
        )
        profile = SimpleNamespace(pk=1, spec='warrior_fury', class_name='warrior')
        talents = [SimpleNamespace(
            pk=11 + index, node_id=136454 + index, tree_type='spec', name=f'Talent {index}',
            name_zh=f'天赋 {index}', description='', description_zh='',
        ) for index in range(26)]

        def export_batch(
            _profile, batch, *, scaffold_talents, talent_prerequisites=None, target_health,
        ):
            actors = [{
                'name': 'skill_damage_base', 'class': 'warrior', 'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': [],
            }]
            for talent in batch:
                for prefix in ('reference', 'talent'):
                    actors.append({
                        'name': f'skill_damage_{prefix}_{talent.pk}',
                        'class': 'warrior', 'spec': 'fury',
                        'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                        'actions': [],
                    })
            return {'actors': actors, 'unresolved': []}

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_profiles', return_value=[profile]), \
             mock.patch.object(service, '_talent_entries', return_value=talents), \
             mock.patch.object(service, '_hero_talent_trees', return_value=[
                 {'id': 60, 'name': 'Slayer', 'name_zh': '屠戮者'},
                 {'id': 61, 'name': 'Mountain Thane', 'name_zh': '山丘领主'},
             ]), \
             mock.patch.object(service, '_run_profile_export', side_effect=export_batch) as run:
            result = service.generate()

        snapshot.refresh_from_db()
        self.assertEqual(run.call_args_list, [
            mock.call(profile, [], scaffold_talents=[], target_health=100),
            mock.call(profile, talents[:12], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=100),
            mock.call(profile, talents[12:24], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=100),
            mock.call(profile, talents[24:], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=100),
            mock.call(profile, [], scaffold_talents=[], target_health=34),
            mock.call(profile, talents[:12], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=34),
            mock.call(profile, talents[12:24], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=34),
            mock.call(profile, talents[24:], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=34),
        ])
        self.assertEqual(snapshot.status, snapshot.STATUS_SUCCEEDED)
        self.assertEqual(result['actors'][0]['specialization'], 'fury')
        self.assertEqual(result['actors'][0]['variant_model'], 'single_talent_runtime')
        self.assertEqual(result['actors'][0]['hero_talent_trees'], [
            {'id': 60, 'name': 'Slayer', 'name_zh': '屠戮者'},
            {'id': 61, 'name': 'Mountain Thane', 'name_zh': '山丘领主'},
        ])
        self.assertEqual(result['identity'], {
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'schema_revision': 8,
        })
        self.assertNotIn('talent', result['identity'])

    def test_generate_isolates_crashing_talent_actor_and_publishes_explicit_unresolved(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='d' * 40, game_build='12.1.0.69299', schema_revision=8,
        )
        profile = SimpleNamespace(pk=1, spec='druid_guardian', class_name='druid')
        bad = SimpleNamespace(
            pk=11, node_id=137059, tree_type='spec', name='Wild Guardian',
            name_zh='荒野守护者', description='', description_zh='',
        )
        good = SimpleNamespace(
            pk=12, node_id=137060, tree_type='spec', name='Good Talent',
            name_zh='正常天赋', description='', description_zh='',
        )

        def export_batch(
            _profile, batch, *, scaffold_talents, talent_prerequisites=None, target_health,
        ):
            if bad in batch:
                raise RuntimeError(
                    'Severe: The precise proc chance of Frostbane is unknown. '
                    'Results will be incorrect.\n'
                    'sim_signal_handler: Segmentation fault! '
                    'Iteration=-1 Seed=1506411349249261642 TargetHealth=0'
                )
            actors = [{
                'name': 'skill_damage_base', 'class': 'druid', 'spec': 'guardian',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': [],
            }]
            for talent in batch:
                for prefix in ('reference', 'talent'):
                    actors.append({
                        'name': f'skill_damage_{prefix}_{talent.pk}',
                        'class': 'druid', 'spec': 'guardian',
                        'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                        'actions': [],
                    })
            return {'actors': actors, 'unresolved': []}

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_profiles', return_value=[profile]), \
             mock.patch.object(service, '_talent_entries', return_value=[bad, good]), \
             mock.patch.object(service, '_hero_talent_trees', return_value=[
                 {'id': 55, 'name': 'Druid of the Claw', 'name_zh': '利爪德鲁伊'},
                 {'id': 59, 'name': 'Elune’s Chosen', 'name_zh': '艾露恩钦选者'},
             ]), \
             mock.patch.object(service, '_run_profile_export', side_effect=export_batch):
            result = service.generate()

        snapshot.refresh_from_db()
        self.assertEqual(snapshot.status, snapshot.STATUS_SUCCEEDED)
        self.assertEqual(len(result['unresolved']), 2)
        self.assertEqual(
            [(row['specialization'], row['talent']['id'], row['target_health_percentage'], row['reason'])
             for row in result['unresolved']],
            [
                ('guardian', 137059, 100, 'simc_actor_initialization_failed'),
                ('guardian', 137059, 34, 'simc_actor_initialization_failed'),
            ],
        )
        self.assertIn('Iteration=-1', result['unresolved'][0]['diagnostic'])

    def test_resilient_export_does_not_swallow_unknown_runtime_failure(self):
        snapshot = SimpleNamespace(simc_revision='e' * 40, game_build='12.1.0.69299')
        profile = SimpleNamespace(pk=1, spec='druid_guardian', class_name='druid')
        talent = SimpleNamespace(
            pk=11, node_id=137059, tree_type='spec', name='Wild Guardian', name_zh='荒野守护者',
        )
        baseline = {
            'actors': [{'name': 'skill_damage_base', 'class': 'druid', 'spec': 'guardian',
                        'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': []}],
            'unresolved': [],
        }

        def export_batch(
            _profile, batch, *, scaffold_talents, talent_prerequisites=None, target_health,
        ):
            if batch:
                raise RuntimeError(
                    'configuration failed: expected marker was absent\n'
                    'sim_signal_handler: Segmentation fault! was only quoted'
                )
            return baseline

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_run_profile_export', side_effect=export_batch):
            with self.assertRaisesRegex(RuntimeError, 'configuration failed'):
                service._run_profile_target_resilient(
                    profile, [talent], scaffold_talents=[],
                    talent_prerequisites={talent.pk: []}, target_health=100,
                )

    def test_generate_keeps_successful_target_when_other_target_actor_crashes(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='f' * 40, game_build='12.1.0.69299', schema_revision=8,
        )
        profile = SimpleNamespace(pk=1, spec='druid_guardian', class_name='druid')
        talent = SimpleNamespace(
            pk=11, node_id=137059, tree_type='spec', name='Wild Guardian',
            name_zh='荒野守护者', description='', description_zh='',
        )

        def export_batch(
            _profile, batch, *, scaffold_talents, talent_prerequisites=None, target_health,
        ):
            if batch and target_health == 34:
                raise RuntimeError('sim_signal_handler: Segmentation fault! signal_11')
            actors = [{
                'name': 'skill_damage_base', 'class': 'druid', 'spec': 'guardian',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': [],
            }]
            for row in batch:
                for prefix in ('reference', 'talent'):
                    actors.append({
                        'name': f'skill_damage_{prefix}_{row.pk}',
                        'class': 'druid', 'spec': 'guardian',
                        'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                        'actions': [],
                    })
            return {'actors': actors, 'unresolved': []}

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_profiles', return_value=[profile]), \
             mock.patch.object(service, '_talent_entries', return_value=[talent]), \
             mock.patch.object(service, '_hero_talent_trees', return_value=[
                 {'id': 52, 'name': 'Sentinel', 'name_zh': '哨兵'},
                 {'id': 72, 'name': 'Dark Ranger', 'name_zh': '黑暗游侠'},
             ]), \
             mock.patch.object(service, '_run_profile_export', side_effect=export_batch), \
             mock.patch('botend.services.simc_skill_damage.flatten_single_talent_damage_variants', return_value=[]) as flatten:
            result = service.generate()

        variants = flatten.call_args.args[2]
        self.assertEqual(len(variants), 1)
        self.assertEqual(variants[0]['high']['name'], 'skill_damage_talent_11')
        self.assertIsNone(variants[0]['low'])
        self.assertEqual(
            [(row['talent']['id'], row['target_health_percentage']) for row in result['unresolved']],
            [(137059, 34)],
        )

    def test_schema_four_requires_exported_runtime_crit_multiplier_and_expectation(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=3,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        payload = {
            'schema_version': 4,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior',
                'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                'token': 'test_action',
                'spell_id': 1,
                'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'dbc_scaling': {
                    'source': 'spell_effect',
                    'direct': {
                        'attack_power_coefficient': 4.242,
                        'spell_power_coefficient': 0.0,
                        'normalized_base': 424.2,
                        'effect_indexes': [0],
                    },
                    'tick': None,
                    'requires_weapon_data': False,
                },
                'baseline': {'direct': {
                    'hit': 424.2, 'crit': 848.4, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': 509.04,
                    'damage_equivalent_count': 1.0,
                }, 'tick': None},
                'scenarios': [],
            }]}],
        }
        service._validate_export(payload)
        action = payload['actors'][0]['actions'][0]
        reporting_root_token = action.pop('reporting_root_token')
        with self.assertRaisesRegex(ValueError, 'reporting root'):
            service._validate_export(payload)
        action['reporting_root_token'] = reporting_root_token

        direct = action['baseline']['direct']
        equivalent_count = direct.pop('damage_equivalent_count')
        with self.assertRaisesRegex(ValueError, 'damage equivalent count'):
            service._validate_export(payload)
        direct['damage_equivalent_count'] = equivalent_count

        del direct['expected']
        with self.assertRaisesRegex(ValueError, '数学期望字段无效'):
            service._validate_export(payload)

        direct['expected'] = None
        direct['hit'] = None
        direct['crit'] = None
        payload['actors'][0]['actions'][0]['baseline']['unresolved_reason'] = 'runtime_non_finite_amount'
        service._validate_export(payload)

        payload['actors'][0]['actions'][0]['baseline'] = {
            'direct': None,
            'tick': None,
            'unresolved_reason': 'snapshot_child_signal_11',
        }
        service._validate_export(payload)

    def test_schema_four_validates_action_root_and_every_scenario_identity_and_amount(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=4,
        )
        service = SimcSkillDamageSnapshotService(snapshot)

        def amount():
            return {'direct': {
                'hit': 100.0, 'crit': 200.0, 'crit_multiplier': 2.0,
                'crit_chance': 0.2, 'expected': 120.0,
                'damage_equivalent_count': 1.0,
            }, 'tick': None}

        def action(token, spell_id):
            return {
                'token': token, 'spell_id': spell_id, 'supported': True,
                # A reporting root need not be one of the damaging exported actions.
                'reporting_root_token': 'non_damaging_root',
                'reporting_root_spell_id': 9000,
                'reporting_root_component': True,
                'dbc_scaling': {
                    'source': 'spell_effect', 'requires_weapon_data': False,
                    'direct': {
                        'attack_power_coefficient': 1.0,
                        'spell_power_coefficient': 0.0,
                        'normalized_base': 100.0,
                        'effect_indexes': [0],
                    }, 'tick': None,
                },
                'baseline': amount(),
                'scenarios': [{
                    'buffs': [{'token': 'probe'}],
                    'values': amount(),
                }],
            }

        payload = {
            'schema_version': 4, 'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior', 'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [action('leaf', 1)],
            }],
        }
        service._validate_export(payload)

        shared_root_spell = copy.deepcopy(payload)
        second = action('blood_plague_heal', 2)
        second['reporting_root_token'] = 'blood_plague_heal'
        # SimC can expose distinct damage/heal action roots for one DBC spell ID.
        second['reporting_root_spell_id'] = 9000
        shared_root_spell['actors'][0]['actions'].append(second)
        service._validate_export(shared_root_spell)

        duplicate_action = copy.deepcopy(payload)
        duplicate_action['actors'][0]['actions'].append(
            copy.deepcopy(duplicate_action['actors'][0]['actions'][0]),
        )
        with self.assertRaisesRegex(ValueError, 'token identity 重复'):
            service._validate_export(duplicate_action)

        shared_root_token = copy.deepcopy(payload)
        second = action('blood_death_knight_variant', 2)
        second['reporting_root_spell_id'] = 9001
        shared_root_token['actors'][0]['actions'].append(second)
        service._validate_export(shared_root_token)

        cross_actor_root = copy.deepcopy(payload)
        second_actor = copy.deepcopy(cross_actor_root['actors'][0])
        second_actor['actions'][0] = action('other_leaf', 3)
        second_actor['actions'][0]['reporting_root_spell_id'] = 9001
        cross_actor_root['actors'].append(second_actor)
        service._validate_export(cross_actor_root, expected_actor_count=2)

        for mutate, message in (
            (lambda row: row['scenarios'].__setitem__(0, None), 'scenario 结构'),
            (lambda row: row['scenarios'][0]['buffs'].append({'token': 'probe'}),
             'buff token identity'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__('hit', float('nan')),
             '数学期望字段'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__(
                'damage_equivalent_count', 0.0), 'damage equivalent count'),
        ):
            invalid = copy.deepcopy(payload)
            mutate(invalid['actors'][0]['actions'][0])
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                service._validate_export(invalid)

    def test_schema_four_rejects_missing_or_malformed_dbc_spell_effect_scaling(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=3,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        payload = {
            'schema_version': 4,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior',
                'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                'token': 'test_action',
                'spell_id': 1,
                'supported': True,
                'reporting_root_token': 'test_action',
                'reporting_root_spell_id': 1,
                'reporting_root_component': True,
                'baseline': {
                    'direct': {'hit': 220.0, 'crit': 440.0, 'crit_multiplier': 2.0,
                               'crit_chance': 0.2, 'expected': 264.0,
                               'damage_equivalent_count': 1.0},
                    'tick': None,
                },
                'scenarios': [],
            }]}],
        }

        with self.assertRaisesRegex(ValueError, 'DBC SpellEffect'):
            service._validate_export(payload)

        payload['actors'][0]['actions'][0]['dbc_scaling'] = {
            'source': 'spell_effect',
            'direct': {
                'attack_power_coefficient': 2.0,
                'spell_power_coefficient': 0.0,
                'normalized_base': float('nan'),
                'effect_indexes': [0],
            },
            'tick': None,
            'requires_weapon_data': False,
        }
        with self.assertRaisesRegex(ValueError, 'DBC SpellEffect'):
            service._validate_export(payload)

    def test_schema_four_requires_baseline_plus_every_single_talent_actor(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=5,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        base = {
            'schema_version': 4,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
        }
        for actors in ([], [{'actions': []}], [{'actions': []}, {'actions': []}, {'actions': []}]):
            with self.subTest(actor_count=len(actors)):
                with self.assertRaisesRegex(ValueError, '期望 2'):
                    service._validate_export({**base, 'actors': actors}, expected_actor_count=2)

    def test_schema_four_rejects_incomplete_actor_identity_and_non_boolean_supported(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=3,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        base = {
            'schema_version': 4,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
        }
        invalid_actors = [
            {'actions': []},
            {'class': 'warrior', 'spec': 'fury', 'action_universe': 'wrong', 'actions': []},
            {
                'class': 'warrior', 'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                    'token': 'test_action', 'spell_id': 1, 'supported': None,
                    'reporting_root_token': 'test_action', 'reporting_root_spell_id': 1,
                    'reporting_root_component': True, 'scenarios': [],
                }],
            },
        ]
        for actor in invalid_actors:
            with self.subTest(actor=actor):
                with self.assertRaisesRegex(ValueError, 'actor 身份|supported'):
                    service._validate_export({**base, 'actors': [actor]})

    def test_dbc_refresh_uses_latest_backend_revision_and_only_runs_for_new_build(self):
        backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production',
            defaults={
                'name': '正式服', 'is_active': True,
                'current_version': 'e' * 40, 'latest_version': 'e' * 40,
                'game_build': '12.1.0.69300', 'simc_path': sys.executable,
            },
        )
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=8,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'specialization': 'fury',
                'variant_model': 'single_talent_runtime',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [],
            }]},
        )

        with mock.patch.object(SimcSkillDamageSnapshotService, 'generate') as generate:
            self.assertIsNone(SimcSkillDamageSnapshotService.refresh_after_dbc_update())
            generate.assert_not_called()

            backend.game_build = '12.1.0.69301'
            backend.save(update_fields=['game_build'])
            snapshot = SimcSkillDamageSnapshotService.refresh_after_dbc_update()

        self.assertEqual(snapshot.simc_revision, 'e' * 40)
        self.assertEqual(snapshot.game_build, '12.1.0.69301')
        generate.assert_called_once_with()


class SimcSkillDamageSnapshotAPITests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.user = get_user_model().objects.create_user(username='viewer', password='x')
        self.staff = get_user_model().objects.create_user(username='staff', password='x', is_staff=True)

    def test_get_returns_latest_schema_eight_success_without_profile_filters(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='d' * 40, game_build='12.1.0.69299', schema_revision=8,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{'specialization': 'fury'}]},
        )
        request = self.factory.get('/api/simc-skill-damage/', {'profile_id': 99, 'talent': 'x'})
        request.user = self.user
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['data']['snapshot']['identity']['game_build'], '12.1.0.69299')
        self.assertNotIn('profile_id', body['data'])
        self.assertFalse(body['data']['can_generate'])

    def test_get_does_not_render_legacy_schema_as_single_talent_rows(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='c' * 40, game_build='12.1.0.69298', schema_revision=3,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{'specialization': 'fury', 'actions': []}]},
        )
        request = self.factory.get('/api/simc-skill-damage/')
        request.user = self.user

        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        body = json.loads(response.content)

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(body['data']['snapshot'])
        self.assertIn('schema 8', body['data']['snapshot_unavailable_reason'])

    def test_get_localizes_skill_identity_and_left_cell_only_shows_name_and_spell_id(self):
        version = WowTalentVersion.objects.create(key='current', is_active=True)
        WowTalentNodeMetadata.objects.create(
            talent_version=version, class_name='Warrior', spec_name='Fury',
            node_id=1, spell_id=1001, display_spell_id=1001, name_zh='天赋中文技能',
        )
        symbol = SimcAplSymbol.objects.create(token='apl_action', symbol_kind='action')
        SimcAplSymbolScope.objects.create(
            symbol=symbol, class_name='warrior', spec='fury', spell_id=2002,
            name_zh='APL中文技能',
        )
        stale_symbol = SimcAplSymbol.objects.create(token='stale_action', symbol_kind='action')
        SimcAplSymbolScope.objects.create(
            symbol=stale_symbol, class_name='warrior', spec='fury', spell_id=3003,
            name_zh='精确版本回退名',
        )
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=2002,
            name='APL Action', name_zh='SpellName数据库中文', snapshot_build='12.1.0.69300',
        )
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=3003,
            name='Stale Action', name_zh='过期版本中文名', snapshot_build='12.1.0.69299',
        )
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=8,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'class': 'warrior', 'specialization': 'fury',
                'hero_talent_tree': '屠戮者', 'talent_name': 'Fury Slayer',
                'actions': [
                    {
                        'name': 'talent_action', 'token': 'talent_action', 'spell_id': 1001,
                        'supported': True,
                        'reporting_root_token': 'talent_action',
                        'reporting_root_spell_id': 1001,
                'reporting_root_component': True,
                        'dbc_scaling': {'direct': {
                            'attack_power_coefficient': 0.9,
                            'spell_power_coefficient': 0.0,
                        }},
                        'baseline': {'direct': {'product': {
                            'dbc_base_damage_min': 90.0, 'dbc_base_damage_max': 90.0,
                            'current_talent_damage': 100.0,
                            'crit_damage': 200.0, 'crit_multiplier': 2.0,
                            'actual_crit_chance': 0.2, 'normalized_expected': 120.0,
                        }}, 'tick': None},
                    },
                    {
                        'name': 'apl_action', 'token': 'apl_action', 'spell_id': 2002,
                        'supported': True,
                        'reporting_root_token': 'apl_action',
                        'reporting_root_spell_id': 2002,
                'reporting_root_component': True,
                        'baseline': {'direct': {'product': {
                            'dbc_base_damage_min': None, 'dbc_base_damage_max': None,
                            'dbc_unresolved_reason': 'dbc_damage_effect_unresolved',
                            'current_talent_damage': 50.0,
                            'crit_damage': 100.0, 'crit_multiplier': 2.0,
                            'actual_crit_chance': 0.2, 'normalized_expected': 60.0,
                        }}, 'tick': None},
                    },
                    {
                        'name': 'stale_action', 'token': 'stale_action', 'spell_id': 3003,
                        'supported': True,
                        'reporting_root_token': 'stale_action',
                        'reporting_root_spell_id': 3003,
                'reporting_root_component': True,
                        'baseline': {'direct': {'product': {
                            'dbc_base_damage_min': None, 'dbc_base_damage_max': None,
                            'dbc_unresolved_reason': 'dbc_damage_effect_unresolved',
                            'current_talent_damage': 25.0,
                            'crit_damage': 50.0, 'crit_multiplier': 2.0,
                            'actual_crit_chance': 0.2, 'normalized_expected': 30.0,
                        }}, 'tick': None},
                    },
                ],
            }]},
        )
        request = self.factory.get('/api/simc-skill-damage/')
        request.user = self.user

        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        actions = json.loads(response.content)['data']['snapshot']['actors'][0]['actions']
        self.assertEqual(
            [(row['display_name'], row['spell_id']) for row in actions],
            [('天赋中文技能', 1001)],
        )

        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        identity_renderer = script.split('function renderSimcSkillIdentity(action) {', 1)[1].split(
            'function renderSimcSkillDamageSnapshot(snapshot) {', 1,
        )[0]
        self.assertIn('action.display_name', identity_renderer)
        self.assertIn('action.spell_id', identity_renderer)
        self.assertNotIn('action.token', identity_renderer)
        self.assertNotIn('hero_talent_tree', identity_renderer)
        self.assertNotIn('talent_name', identity_renderer)
        damage_renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel() {', 1,
        )[0]
        self.assertNotIn('${skillMeta}<div', damage_renderer)

    def test_post_requires_staff(self):
        request = self.factory.post('/api/simc-skill-damage/', data='{}', content_type='application/json')
        request.user = self.user
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        self.assertEqual(response.status_code, 403)


class SimcSkillDamageDashboardContractTests(TestCase):
    def test_dashboard_has_independent_light_skill_damage_panel(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn('id="simc-skill-damage-panel"', template)
        self.assertIn('技能归一化伤害', template)
        self.assertIn('DBC 基础伤害直接读取技能 SpellEffect', template)
        self.assertIn('由 SimC reporting root 证明的多段、主副手和周期分量会合并', template)
        self.assertIn('统一基础暴击率：20%', template)
        self.assertIn('id="simc-skill-damage-global-modifiers"', template)
        self.assertIn('全技能伤害加成', script)
        self.assertIn('modifier.runtime_condition', script)
        self.assertNotIn('单项天赋常驻', script)
        self.assertNotIn('与无单项天赋基线进行成对', template)
        self.assertNotIn('所选完整天赋', template)
        self.assertNotIn('独立无可选天赋 actor', template)
        self.assertNotIn('AP/SP 归一化为 1', template)
        self.assertIn('simc-skill-damage-table', template)
        self.assertIn('id="simc-skill-damage-unresolved"', template)
        self.assertIn('snapshot.unresolved', script)
        self.assertIn('unresolved.slice(0, 50)', script)
        self.assertIn('未解析', script)
        self.assertIn('data-dashboard-section="simc-skill-damage"', template)
        self.assertIn('id="simc-skill-damage"', template)
        self.assertIn("'skill-damage': 'simc-skill-damage'", script)
        self.assertIn('/api/simc-skill-damage/', script)
        self.assertIn('renderSimcSkillDamageSnapshot', script)
        self.assertIn('initSimcSkillDamagePanel();', script)
        self.assertNotIn('bg-gray-900 simc-skill-damage', template)

    def test_dashboard_shows_base_formula_and_final_normalized_damage_without_crit(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        for label in ('基础 AP/SP 伤害', '伤害公式', '最终归一化伤害'):
            self.assertIn(label, template)
        for removed_label in ('该条件实际伤害', '技能实际暴击率', '归一化伤害期望'):
            self.assertNotIn(removed_label, template)
        for field in (
            'attack_power_coefficient', 'spell_power_coefficient',
            'normalized_base_damage', 'runtime_multiplier', 'final_normalized_damage',
        ):
            self.assertIn(field, renderer)
        self.assertIn('formatSimcSkillDamageFactor', renderer)
        self.assertIn('toFixed(6)', renderer)
        self.assertIn('等效总倍率', renderer)
        self.assertNotIn('SimC 总乘区', renderer)
        for crit_field in ('crit_damage', 'crit_multiplier', 'actual_crit_chance', 'normalized_expected'):
            self.assertNotIn(crit_field, renderer)
        self.assertIn('统一基础暴击率：20%', template)
        self.assertIn('html[data-dashboard-theme="dark"] #simc-skill-damage-panel', template)

    def test_dashboard_requires_spec_and_hero_tree_and_renders_single_talent_runtime_conditions(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('id="simc-skill-damage-hero-tree"', template)
        self.assertIn('请选择专精', template)
        self.assertIn('请选择英雄天赋', template)
        self.assertIn('单项天赋条件', template)
        self.assertIn('id="simc-skill-damage-sort-final"', template)
        self.assertIn('最终归一化伤害', template)
        self.assertNotIn('技能实际暴击率', template)
        self.assertIn('selectedHeroTree', renderer)
        self.assertIn('hero_talent_trees', renderer)
        self.assertIn('variant.hero_subtree_id', renderer)
        self.assertIn('variant.runtime_condition', renderer)
        self.assertIn('variant.talent_name_zh', renderer)
        self.assertIn("const sortMode = sortButton.dataset.sortMode === 'final' ? 'final' : 'name';", renderer)
        self.assertIn("if (sortMode === 'final')", renderer)
        self.assertIn("sortButton.dataset.sortMode = 'final';", script)
        self.assertIn('sortDirection', renderer)
        self.assertIn('finalSortValue', renderer)
        self.assertNotIn('全部专精', renderer)
        self.assertNotIn("component === 'direct' ? 'Direct' : 'Tick'", renderer)
        self.assertNotIn('font-bold uppercase text-stone-500', renderer)
        self.assertNotIn('职业 Buff', template)
