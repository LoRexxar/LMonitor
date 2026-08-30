import copy
import gc
import json
import sys
import weakref
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
    _base_damage_layer_candidate,
    _player_skill_actions,
    _runtime_layer_candidate,
    _runtime_layer_candidates,
    _scenario_has_target_marginal_change,
    _scenario_token_universe,
    _talent_declares_all_damage_modifier,
    attach_runtime_product_metrics,
    build_single_talent_actor_input,
    classify_global_damage_modifiers,
    classify_global_skill_effects,
    flatten_single_talent_damage_variants,
    localize_skill_damage_payload,
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
    def test_apex_entries_add_authoritative_stage_prerequisites(self):
        stage_one = SimpleNamespace(
            pk=1, node_id=101, talent_id=900, max_points=None,
            parents_json=[],
        )
        stage_two = SimpleNamespace(
            pk=2, node_id=102, talent_id=900, max_points=2,
            parents_json=[],
        )
        stage_three = SimpleNamespace(
            pk=3, node_id=103, talent_id=900, max_points=1,
            parents_json=[],
        )
        ordinary_one = SimpleNamespace(
            pk=4, node_id=201, talent_id=901, max_points=1,
            parents_json=[],
        )
        ordinary_two = SimpleNamespace(
            pk=5, node_id=202, talent_id=901, max_points=1,
            parents_json=[],
        )
        prerequisite_map = SimcSkillDamageSnapshotService._talent_prerequisite_map(
            [stage_three, ordinary_two, stage_one, ordinary_one, stage_two],
            entry_order={101: 0, 102: 1, 103: 2, 201: 0, 202: 1},
        )
        self.assertEqual(prerequisite_map[stage_one.pk], [])
        self.assertEqual(prerequisite_map[stage_two.pk], [stage_one])
        self.assertEqual(
            prerequisite_map[stage_three.pk], [stage_one, stage_two],
        )
        self.assertEqual(prerequisite_map[ordinary_one.pk], [])
        self.assertEqual(prerequisite_map[ordinary_two.pk], [])

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
        self.assertIn('warrior="skill_damage_reference_1_trait_136454"', generated)
        self.assertIn('warrior="skill_damage_talent_1_trait_136454"', generated)
        self.assertIn('spec_talents=136454:1', generated)
        self.assertIn('warrior="skill_damage_reference_2_trait_117404"', generated)
        self.assertIn('warrior="skill_damage_talent_2_trait_117404"', generated)
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

        reference = generated.split('warrior="skill_damage_reference_74890_trait_117392"', 1)[1].split(
            'warrior="skill_damage_talent_74890_trait_117392"', 1
        )[0]
        selected = generated.split('warrior="skill_damage_talent_74890_trait_117392"', 1)[1]
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

        reference = generated.split('warrior="skill_damage_reference_2_trait_102"', 1)[1].split(
            'warrior="skill_damage_talent_2_trait_102"', 1
        )[0]
        selected = generated.split('warrior="skill_damage_talent_2_trait_102"', 1)[1]
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
                ('血之气息', '点出血之气息天赋', 240.0),
                ('恶毒蔑视', '血量低于35%', 300.0),
                ('恶毒蔑视', '点出恶毒蔑视天赋，血量低于35%', 270.0),
                ('能量爆发', '点出能量爆发天赋', 220.0),
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

    def test_runtime_conditions_freeze_authoritative_chinese_spell_names_without_tokens(self):
        for spell_id, name, name_zh in (
            (1001, 'Executioner', '刽子手'),
            (1002, 'Overwhelmed', '势不可挡'),
        ):
            WowSpellSnapshot.objects.create(
                branch='wow', locale='zhCN', spell_id=spell_id,
                name=name, name_zh=name_zh, snapshot_build='12.1.0.69497',
            )

        def amount(hit):
            return {
                'direct': {
                    'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit * 1.2,
                },
                'tick': None,
            }

        def action(hit, scenarios=()):
            return {
                'name': 'execute', 'token': 'execute', 'spell_id': 5308,
                'supported': True, 'player_skill': True,
                'reporting_root_token': 'execute', 'reporting_root_spell_id': 5308,
                'reporting_root_component': True,
                'baseline': amount(hit),
                'scenarios': [
                    {
                        'active_buffs': [token],
                        'buffs': [{
                            'token': token, 'spell_id': spell_id,
                            'scope': scope, 'stacks': stacks,
                        }],
                        'amount': amount(scenario_hit),
                    }
                    for token, spell_id, scope, stacks, scenario_hit in scenarios
                ],
            }

        reference = {'actions': [action(100.0)]}
        selected = {'actions': [action(100.0, (
            ('buff.executioner', 1001, 'self', 1, 120.0),
            ('debuff.overwhelmed', 1002, 'target', 1, 130.0),
            ('debuff.overwhelmed', 1002, 'target', 2, 140.0),
        ))]}
        rows = flatten_single_talent_damage_variants(reference, reference, [{
            'talent': {'id': 74900, 'name': "Slayer's Malice", 'name_zh': '屠戮者之怨'},
            'reference_high': reference, 'reference_low': reference,
            'high': selected, 'low': selected,
        }])
        localized = localize_skill_damage_payload({
            'identity': {'game_build': '12.1.0.69497'},
            'actors': [{'class': 'warrior', 'specialization': 'fury', 'actions': rows}],
        })
        variants = [
            row['variant'] for row in localized['actors'][0]['actions']
            if row['variant']['scenario_tokens']
        ]

        self.assertEqual(
            [variant['runtime_condition'] for variant in variants],
            [
                '点出屠戮者之怨天赋，且自身存在刽子手效果时',
                '点出屠戮者之怨天赋，且目标存在势不可挡效果时',
                '点出屠戮者之怨天赋，且目标存在势不可挡效果（2层）时',
            ],
        )
        self.assertEqual(
            [variant['runtime_conditions'] for variant in variants],
            [
                [{'token': 'buff.executioner', 'spell_id': 1001,
                  'scope': 'self', 'name_zh': '刽子手'}],
                [{'token': 'debuff.overwhelmed', 'spell_id': 1002,
                  'scope': 'target', 'name_zh': '势不可挡'}],
                [{'token': 'debuff.overwhelmed', 'spell_id': 1002,
                  'scope': 'target', 'stacks': 2, 'name_zh': '势不可挡'}],
            ],
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
            ('血量低于35%', 80, 100),
            ('', 110, 90),
            ('点出真实天赋', 150, 100),
            ('点出真实天赋', 140, 100),
            ('血量低于35%', 130, 90),
            ('点出真实天赋，血量低于35%', 170, 100),
            ('点出真实天赋，血量低于35%', 160, 100),
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
            [('', 100.0), ('血量低于35%', 130.0)],
        )

    def test_same_scenario_token_with_different_stacks_projects_distinct_rows(self):
        def amount(multiplier):
            return {
                'direct': {
                    'hit': 100.0 * multiplier, 'crit': 200.0 * multiplier,
                    'crit_multiplier': 2.0, 'crit_chance': 0.2,
                    'expected': 120.0 * multiplier, 'damage_equivalent_count': 1.0,
                    'runtime_layers': {'da_multiplier': multiplier},
                },
                'tick': None, 'unresolved_reason': None,
            }

        def action(multiplier, scenarios=()):
            return {
                'name': 'stack_probe', 'token': 'stack_probe', 'spell_id': 90002,
                'supported': True, 'player_skill': True,
                'reporting_root_token': 'stack_probe',
                'reporting_root_spell_id': 90002,
                'reporting_root_component': True,
                'baseline': amount(multiplier),
                'scenarios': [{
                    'buffs': [{
                        'token': 'buff.stack_probe', 'scope': 'self',
                        'spell_id': 90003, 'class_family': 1, 'stacks': stacks,
                    }],
                    'values': amount(value),
                } for stacks, value in scenarios],
                'dbc_scaling': {
                    'direct': {
                        'attack_power_coefficient': 1.0,
                        'spell_power_coefficient': 0.0,
                        'normalized_base': 100.0,
                    },
                    'tick': None,
                },
            }

        reference = {'actions': [action(1.0)]}
        selected = {'actions': [action(1.0, ((1, 1.10), (2, 1.20)))]}
        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [{
                'talent': {'id': 901, 'name': 'Stack Probe', 'name_zh': '叠层探针'},
                'reference_high': reference, 'reference_low': reference,
                'high': selected, 'low': selected,
            }], global_effects=[],
        )
        scenario_rows = [
            row for row in rows
            if (row.get('variant') or {}).get('scenario_tokens') == ['buff.stack_probe']
        ]
        self.assertEqual(len(scenario_rows), 2)
        self.assertEqual(
            [row['variant']['runtime_conditions'][0].get('stacks', 1) for row in scenario_rows],
            [1, 2],
        )
        projected = project_skill_damage_product_payload({
            'actors': [{'actions': scenario_rows}],
        })['actors'][0]['actions']
        self.assertEqual(len(projected), 2)
        projected_damage = sorted(
            row['product']['final_normalized_damage'] for row in projected
        )
        self.assertAlmostEqual(projected_damage[0], 110.0)
        self.assertAlmostEqual(projected_damage[1], 120.0)

        stack_two_global = [{
            'source_type': 'runtime_state',
            'scenario_tokens': ['buff.stack_probe'],
            'runtime_conditions': [{
                'token': 'buff.stack_probe', 'scope': 'self',
                'spell_id': 90003, 'stacks': 2,
            }],
            'projections': [{'kind': 'damage_multiplier', 'value': 1.20}],
        }]
        retained = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [{
                'talent': {'id': 901, 'name': 'Stack Probe', 'name_zh': '叠层探针'},
                'reference_high': reference, 'reference_low': reference,
                'high': selected, 'low': selected,
            }], global_effects=stack_two_global,
        )
        retained_scenarios = [
            row['variant']['runtime_conditions'][0].get('stacks', 1)
            for row in retained if row['variant']['scenario_tokens'] == ['buff.stack_probe']
        ]
        self.assertEqual(retained_scenarios, [1])

    def test_low_scenario_formula_decomposes_health_and_scenario_runtime_factors(self):
        def amount(multiplier):
            return {
                'direct': {
                    'hit': 100.0 * multiplier, 'crit': 200.0 * multiplier,
                    'crit_multiplier': 2.0, 'crit_chance': 0.2,
                    'expected': 120.0 * multiplier, 'damage_equivalent_count': 1.0,
                    'runtime_layers': {'da_multiplier': multiplier},
                },
                'tick': None, 'unresolved_reason': None,
            }

        def action(multiplier, scenario=None):
            scenarios = []
            if scenario is not None:
                scenarios.append({
                    'buffs': [{
                        'token': 'buff.low_probe', 'scope': 'self',
                        'spell_id': 90004, 'class_family': 1, 'stacks': 1,
                    }],
                    'values': amount(scenario),
                })
            return {
                'name': 'factor_probe', 'token': 'factor_probe', 'spell_id': 90005,
                'supported': True, 'player_skill': True,
                'reporting_root_token': 'factor_probe',
                'reporting_root_spell_id': 90005,
                'reporting_root_component': True,
                'baseline': amount(multiplier), 'scenarios': scenarios,
                'dbc_scaling': {
                    'direct': {
                        'attack_power_coefficient': 1.0,
                        'spell_power_coefficient': 0.0,
                        'normalized_base': 100.0,
                    },
                    'tick': None,
                },
            }

        reference = {'actions': [action(1.0)]}
        high = {'actions': [action(1.0)]}
        low = {'actions': [action(1.25, 1.375)]}
        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [{
                'talent': {'id': 902, 'name': 'Factor Probe', 'name_zh': '乘区探针'},
                'reference_high': reference, 'reference_low': reference,
                'high': high, 'low': low,
            }], global_effects=[],
        )
        scenario_row = next(
            row for row in rows
            if (row.get('variant') or {}).get('scenario_tokens') == ['buff.low_probe']
        )
        formula = project_skill_damage_product_payload({
            'actors': [{'actions': [scenario_row]}],
        })['actors'][0]['actions'][0]['product']['formula_components'][0]
        self.assertAlmostEqual(formula['base_damage'], 100.0)
        self.assertEqual(len(formula['runtime_factors']), 2)
        self.assertAlmostEqual(formula['runtime_factors'][0], 1.25)
        self.assertAlmostEqual(formula['runtime_factors'][1], 1.10)
        self.assertAlmostEqual(formula['final_damage'], 137.5)

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
        with self.assertRaisesRegex(ValueError, '同一 scenario identity 返回冲突数值'):
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

        mountain_leaf = leaf('mountain_leaf', 4, 'direct', 100.0, 120.0)
        mountain_leaf['hero_subtree_ids'] = [61]
        slayer_leaf = leaf('slayer_leaf', 5, 'direct', 100.0, 120.0)
        slayer_leaf['hero_subtree_ids'] = [60]
        ownership_payload = {'actors': [{'actions': [mountain_leaf, slayer_leaf]}]}
        forward = project_skill_damage_product_payload(ownership_payload)['actors'][0]['actions']
        ownership_payload['actors'][0]['actions'].reverse()
        reverse = project_skill_damage_product_payload(ownership_payload)['actors'][0]['actions']
        self.assertEqual(
            sorted(row['hero_subtree_ids'] for row in forward),
            [[60], [61]],
        )
        self.assertEqual(
            sorted(row['hero_subtree_ids'] for row in reverse),
            [[60], [61]],
        )

    def test_product_projection_preserves_native_runtime_factor_formula_components(self):
        def leaf(token, spell_id, base, hit, runtime_layers):
            return {
                'token': token, 'spell_id': spell_id, 'name': '公式技能', 'supported': True,
                'reporting_root_token': 'formula_skill', 'reporting_root_spell_id': 9001,
                'reporting_root_component': True,
                'variant': {'talent_id': None, 'runtime_condition': ''},
                'dbc_scaling': {'direct': {
                    'attack_power_coefficient': base / 100.0,
                    'spell_power_coefficient': 0.0,
                }},
                'baseline': {'direct': {
                    'damage_equivalent_count': 1.0,
                    'runtime_layers': runtime_layers,
                    'product': {
                        'dbc_base_damage_min': base, 'dbc_base_damage_max': base,
                        'current_talent_damage': hit, 'crit_damage': hit * 2,
                        'crit_multiplier': 2.0, 'actual_crit_chance': 0.2,
                        'normalized_expected': hit * 1.2, 'dbc_unresolved_reason': '',
                    },
                }},
            }

        payload = {'actors': [{'actions': [
            leaf('formula_skill_main', 1, 100.0, 132.6, {
                'da_multiplier': 1.02,
                'target_da_multiplier': 1.30,
                'versatility': 1.0,
            }),
            # SimC 的副手基础换算不属于 runtime layer；公式基础数必须吸收该换算。
            leaf('formula_skill_offhand', 2, 100.0, 76.5, {
                'da_multiplier': 1.53,
                'target_da_multiplier': 1.0,
            }),
        ]}]}

        action = project_skill_damage_product_payload(payload)['actors'][0]['actions'][0]

        self.assertEqual(action['product']['normalized_base_damage'], 200.0)
        self.assertEqual(action['product']['final_normalized_damage'], 209.1)
        self.assertEqual(action['product']['formula_components'], [
            {
                'base_damage': 100.0,
                'base_source': 'attack_power',
                'base_multiplier': 1.0,
                'runtime_factors': [1.02, 1.30],
                'final_damage': 132.6,
            },
            {
                'base_damage': 50.0,
                'base_source': 'attack_power',
                'base_multiplier': 0.5,
                'runtime_factors': [1.53],
                'final_damage': 76.5,
            },
        ])

    def test_product_projection_moves_native_specialization_passive_out_of_skill_formula(self):
        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=137050,
            name='Fury Warrior', name_zh='狂怒战士', snapshot_build='12.1.0.69404',
        )
        payload = {
            'identity': {'game_build': '12.1.0.69404'},
            'actors': [{'class': 'warrior', 'specialization': 'fury', 'actions': [{
            'token': 'native_passive_skill', 'spell_id': 9001,
            'name': '原生专精被动测试技能', 'supported': True,
            'reporting_root_token': 'native_passive_skill',
            'reporting_root_spell_id': 9001,
            'reporting_root_component': True,
            'variant': {'talent_id': None, 'runtime_condition': ''},
            'dbc_scaling': {'direct': {
                'attack_power_coefficient': 1.0,
                'spell_power_coefficient': 0.0,
            }},
            'baseline': {'direct': {
                'damage_equivalent_count': 1.0,
                'runtime_layers': {
                    'da_multiplier': 1.281,
                    'target_da_multiplier': 1.0,
                    'specialization_passive_effects': [{
                        'source_spell_id': 137050,
                        'source_name': 'Fury Warrior',
                        'effect_index': 1,
                        'component': 'direct',
                        'factor': 1.22,
                    }],
                },
                'product': {
                    'dbc_base_damage_min': 100.0,
                    'dbc_base_damage_max': 100.0,
                    'current_talent_damage': 128.1,
                    'crit_damage': 256.2,
                    'crit_multiplier': 2.0,
                    'actual_crit_chance': 0.2,
                    'normalized_expected': 153.72,
                    'dbc_unresolved_reason': '',
                },
            }},
        }]}]}
        original = json.loads(json.dumps(payload))

        actor = localize_skill_damage_payload(
            project_skill_damage_product_payload(payload)
        )['actors'][0]
        action = actor['actions'][0]

        self.assertEqual(payload, original)
        self.assertEqual(action['product']['normalized_base_damage'], 100.0)
        self.assertEqual(action['product']['final_normalized_damage'], 105.0)
        self.assertEqual(action['product']['formula_components'], [{
            'base_damage': 100.0,
            'base_source': 'attack_power',
            'base_multiplier': 1.0,
            'runtime_factors': [1.05],
            'final_damage': 105.0,
        }])
        self.assertEqual(actor['global_skill_effects'], [{
            'effect_id': 'specialization_passive:137050:1.22',
            'source_type': 'specialization_passive',
            'source_spell_ids': [137050],
            'source_name': 'Fury Warrior',
            'scenario_tokens': [],
            'runtime_condition': '专精被动（适用于受影响技能）',
            'display_name': '狂怒战士',
            'projections': [{
                'kind': 'damage_multiplier',
                'value': 1.22,
                'bonus_percent': 22.0,
                'component': 'direct',
            }],
        }])

    def test_hand_suffixes_use_base_translation_and_merge_complementary_self_roots(self):
        for token, name_zh in (
            ('raging_blow', '怒击'),
            ('odyns_fury', '奥丁之怒'),
            ('fracture', '破裂'),
            ('soul_carver', '灵魂切削'),
        ):
            symbol = SimcAplSymbol.objects.create(token=token, symbol_kind='action')
            SimcAplSymbolScope.objects.create(
                symbol=symbol, class_name='warrior', spec='fury', name_zh=name_zh,
            )

        WowSpellSnapshot.objects.create(
            branch='wow', locale='zhCN', spell_id=342857,
            name='Glaive Tempest', name_zh='战刃风暴', snapshot_build='12.1.0.69404',
        )

        def leaf(token, spell_id, *, root=None, root_spell_id=None, variant_id=1):
            return {
                'name': token, 'token': token, 'spell_id': spell_id,
                'display_name': token, 'supported': True,
                'reporting_root_token': root or token,
                'reporting_root_spell_id': root_spell_id or spell_id,
                'reporting_root_component': True,
                'variant': {'talent_id': variant_id, 'runtime_condition': ''},
                'dbc_scaling': {'direct': {
                    'attack_power_coefficient': 1.0,
                    'spell_power_coefficient': 0.0,
                }},
                'baseline': {'direct': {'product': {
                    'dbc_base_damage_min': 100.0,
                    'dbc_base_damage_max': 100.0,
                    'current_talent_damage': 120.0,
                    'crit_damage': 240.0,
                    'crit_multiplier': 2.0,
                    'actual_crit_chance': 0.2,
                    'normalized_expected': 144.0,
                }}},
            }

        payload = {
            'identity': {'game_build': '12.1.0.69497'},
            'actors': [{
                'class': 'warrior', 'specialization': 'fury',
                'actions': [
                    leaf('raging_blow_mh', 96103, root='raging_blow', root_spell_id=85288),
                    leaf('raging_blow_oh', 85384, root='raging_blow', root_spell_id=85288),
                    leaf('odyns_fury_mh', 385060, root='odyns_fury', root_spell_id=385059),
                    leaf('odyns_fury_oh', 385061, root='odyns_fury', root_spell_id=385059),
                    leaf('fracture_mh', 225919),
                    leaf('fracture_oh', 225921),
                    leaf('soul_carver_oh', 214743),
                    leaf('glaive_tempest_mh', 342857),
                    leaf('glaive_tempest_oh', 342857),
                ],
            }],
        }

        localized = localize_skill_damage_payload(payload)
        self.assertEqual(
            [row['display_name'] for row in localized['actors'][0]['actions']],
            ['怒击', '怒击', '奥丁之怒', '奥丁之怒', '破裂', '破裂', '灵魂切削', '战刃风暴', '战刃风暴'],
        )

        rows = project_skill_damage_product_payload(localized)['actors'][0]['actions']
        by_token = {row['token']: row for row in rows}
        self.assertEqual(set(by_token), {
            'raging_blow', 'odyns_fury', 'fracture', 'soul_carver_oh',
            'glaive_tempest',
        })
        for token in ('raging_blow', 'odyns_fury', 'fracture', 'glaive_tempest'):
            self.assertEqual(by_token[token]['component_count'], 2)
            self.assertEqual(by_token[token]['product']['attack_power_coefficient'], 2.0)
            self.assertEqual(by_token[token]['product']['normalized_base_damage'], 200.0)
            self.assertEqual(by_token[token]['product']['final_normalized_damage'], 240.0)
        self.assertEqual(by_token['raging_blow']['display_name'], '怒击')
        self.assertEqual(by_token['odyns_fury']['display_name'], '奥丁之怒')
        self.assertEqual(by_token['fracture']['display_name'], '破裂')
        self.assertEqual(by_token['glaive_tempest']['display_name'], '战刃风暴')
        self.assertIsNone(by_token['fracture']['spell_id'])
        self.assertEqual(
            {item['spell_id'] for item in by_token['fracture']['components']},
            {225919, 225921},
        )
        self.assertEqual(by_token['soul_carver_oh']['component_count'], 1)
        self.assertEqual(by_token['soul_carver_oh']['display_name'], '灵魂切削')

    def test_derived_action_names_prefer_authoritative_recent_chinese_spell_names(self):
        for spell_id, name, name_zh in (
            (5308, 'Execute', '斩杀'),
            (184367, 'Rampage', '暴怒'),
            (388539, 'Rend', '撕裂'),
            (999999, 'Authoritative Name', '权威中文'),
        ):
            WowSpellSnapshot.objects.create(
                branch='wow', locale='zhCN', spell_id=spell_id,
                name=name, name_zh=name_zh, snapshot_build='12.1.0.69404',
            )

        def action(token, spell_id, *, root=None, root_spell_id=None, display_name=None):
            return {
                'name': token, 'token': token, 'spell_id': spell_id,
                'display_name': display_name or token,
                'reporting_root_token': root or token,
                'reporting_root_spell_id': root_spell_id or spell_id,
                'reporting_root_component': True,
            }

        payload = {
            'identity': {'game_build': '12.1.0.69497'},
            'actors': [{
                'class': 'warrior', 'specialization': 'fury',
                'actions': [
                    action('execute_mainhand', 280849, root='execute', root_spell_id=5308),
                    action('rampage1', 218617, root='rampage', root_spell_id=184367),
                    action('rend_dot', 388539),
                    action('already_localized', 999999, display_name='已有中文'),
                ],
            }],
        }

        localized = localize_skill_damage_payload(payload)
        self.assertEqual(
            [row['display_name'] for row in localized['actors'][0]['actions']],
            ['斩杀', '暴怒', '撕裂', '已有中文'],
        )

    def test_all_damage_text_scope_requires_player_positive_unrestricted_damage(self):
        accepted = (
            'Increases all damage you deal by 20%.',
            'Successfully interrupting an enemy increases the damage you deal to them by 5% for 10 sec.',
            'Your damage is increased by 10%.',
            'Enemies take 5% increased damage from you.',
            'Increases your damage dealt to the target by 10%.',
            '你造成的所有伤害提高20%。',
            '目标受到来自你的伤害提高5%。',
        )
        rejected = (
            'You heal for 10% of all damage you deal.',
            'Reduces all damage you take by 10%.',
            'All damage dealt by your pet is increased by 10%.',
            'Your Fireball damage is increased by 10%.',
            'Increases the damage you deal when using Fireball by 10%.',
            'Increases the damage you deal using Fireball and Frostbolt by 10%.',
            'Increases the damage you deal from Fireball and Frostbolt by 10%.',
            'Increases damage you deal with Garrote, Rupture, and Deadly Poison by 10%.',
            'All damage you deal with Tempest and Lightning Bolt is copied.',
            'Reduces all damage you deal by 10%.',
            '你的宠物造成的所有伤害提高10%。',
            '目标对你造成的所有伤害降低10%。',
            '终结技造成的所有伤害提高10%。',
            'Increases damage dealt by Agony and Corruption by 10%.',
            "Increases damage dealt by Hand of Gul'dan to its main target by 10%.",
            'Spending extra Energy on Ferocious Bite increases damage dealt by up to 25%.',
            '你对目标施放的锁喉、割裂和致命药膏造成的伤害提高20%。',
        )
        for description in accepted:
            with self.subTest(description=description):
                self.assertTrue(_talent_declares_all_damage_modifier({
                    'description': description,
                }))
        for description in rejected:
            with self.subTest(description=description):
                self.assertFalse(_talent_declares_all_damage_modifier({
                    'description': description,
                }))

    def test_global_modifier_uses_authoritative_scope_and_cross_skill_runtime_layers(self):
        direct_layers = (
            'da_multiplier', 'player_multiplier', 'versus_multiplier',
            'persistent_multiplier', 'target_da_multiplier', 'versatility',
            'pet_multiplier', 'target_pet_multiplier',
        )
        tick_layers = (
            'ta_multiplier', 'player_multiplier', 'versus_multiplier',
            'persistent_multiplier', 'target_ta_multiplier', 'versatility',
            'pet_multiplier', 'target_pet_multiplier',
        )

        def amount(multiplier=1.0, *, layer='da_multiplier', component='direct'):
            layer_fields = direct_layers if component == 'direct' else tick_layers
            runtime_layers = {name: 1.0 for name in layer_fields}
            runtime_layers[layer] = multiplier
            values = {
                'hit': 100.0 * multiplier, 'crit': 200.0 * multiplier,
                'crit_multiplier': 2.0, 'crit_chance': 0.2,
                'expected': 120.0 * multiplier, 'damage_equivalent_count': 1.0,
                'runtime_layers': runtime_layers,
            }
            return {
                'direct': values if component == 'direct' else None,
                'tick': values if component == 'tick' else None,
                'unresolved_reason': None,
            }

        def action(token, *, baseline=1.0, scenario=None, layer='da_multiplier',
                   component='direct', player_skill=True, harmful=True):
            row = {
                'token': token, 'spell_id': token, 'supported': True,
                'player_skill': player_skill, 'harmful': harmful,
                'reporting_root_token': token, 'reporting_root_spell_id': token,
                'reporting_root_component': True,
                'baseline': amount(baseline, layer=layer, component=component),
                'scenarios': [],
            }
            if scenario is not None:
                row['scenarios'].append({
                    'active_buffs': ['runtime_probe'],
                    'amount': amount(scenario, layer=layer, component=component),
                })
            return row

        def actor(*, baseline=1.0, scenario=None, layer='da_multiplier',
                  component='direct', roots=('a', 'b'), effectiveness=None):
            if effectiveness is None:
                effectiveness = 'active' if baseline != 1.0 or scenario is not None else 'inactive'
            return {'talent_effectiveness': effectiveness, 'actions': [
                *(action(
                    root, baseline=baseline, scenario=scenario,
                    layer=layer, component=component,
                ) for root in roots),
                action('racial', baseline=1.0, player_skill=False),
            ]}

        talent = {
            'id': 9, 'name': 'Universal', 'name_zh': '全局增伤',
            'description': 'Increases all damage you deal.',
        }
        cases = (
            ('passive_da', 'direct', 'da_multiplier', 1.06, False),
            ('buff_da', 'direct', 'da_multiplier', 1.20, True),
            ('buff_ta', 'tick', 'ta_multiplier', 1.20, True),
            ('buff_player', 'direct', 'player_multiplier', 1.12, True),
            ('buff_versus', 'direct', 'versus_multiplier', 1.07, True),
            ('buff_persistent', 'tick', 'persistent_multiplier', 1.09, True),
            ('buff_target_da', 'direct', 'target_da_multiplier', 1.08, True),
            ('buff_target_ta', 'tick', 'target_ta_multiplier', 1.08, True),
            ('buff_versatility', 'direct', 'versatility', 1.05, True),
            ('buff_pet', 'direct', 'pet_multiplier', 1.11, True),
            ('buff_target_pet', 'tick', 'target_pet_multiplier', 1.04, True),
        )
        for name, component, layer, multiplier, conditional in cases:
            with self.subTest(name=name):
                reference = actor(layer=layer, component=component)
                selected = actor(
                    baseline=1.0 if conditional else multiplier,
                    scenario=multiplier if conditional else None,
                    layer=layer,
                    component=component,
                )
                variant = {
                    'talent': talent,
                    'reference_high': copy.deepcopy(reference),
                    'high': copy.deepcopy(selected),
                    'reference_low': copy.deepcopy(reference),
                    'low': copy.deepcopy(selected),
                }
                modifiers = classify_global_damage_modifiers([variant])
                self.assertEqual(len(modifiers), 1)
                self.assertAlmostEqual(modifiers[0]['damage_multiplier'], multiplier)
                self.assertEqual(
                    modifiers[0]['scenario_tokens'],
                    ['runtime_probe'] if conditional else [],
                )
                self.assertEqual(modifiers[0]['evidence_root_count'], 2)
                self.assertEqual(modifiers[0]['runtime_layer'], layer)
                self.assertEqual(modifiers[0]['runtime_components'], [component])
                self.assertEqual(
                    {row['token'] for row in modifiers[0]['evidence_roots']},
                    {'a', 'b'},
                )

        shared_reference_scenario = actor(scenario=1.0, effectiveness='inactive')
        shared_selected_scenario = actor(scenario=1.20, effectiveness='active')
        shared_runtime_token = {
            'talent': talent,
            'reference_high': copy.deepcopy(shared_reference_scenario),
            'high': copy.deepcopy(shared_selected_scenario),
            'reference_low': copy.deepcopy(shared_reference_scenario),
            'low': copy.deepcopy(shared_selected_scenario),
        }
        shared_modifiers = classify_global_damage_modifiers([shared_runtime_token])
        self.assertEqual(len(shared_modifiers), 1)
        self.assertEqual(shared_modifiers[0]['scenario_tokens'], ['runtime_probe'])

        registered_reference = actor(scenario=1.20, effectiveness='inactive')
        registered_selected = actor(scenario=1.20, effectiveness='active')
        for registered_actor in (registered_reference, registered_selected):
            for registered_action in registered_actor['actions']:
                for scenario_row in registered_action.get('scenarios', []):
                    scenario_row['active_buffs'] = ['buff.runtime_probe']
        registered_selected['actions'].append({
            'name': 'Runtime Probe', 'spell_id': 999,
            'supported': False, 'player_skill': True, 'harmful': False,
        })
        registered_state_variant = {
            'talent': talent,
            'reference_high': copy.deepcopy(registered_reference),
            'high': copy.deepcopy(registered_selected),
            'reference_low': copy.deepcopy(registered_reference),
            'low': copy.deepcopy(registered_selected),
        }
        registered_modifiers = classify_global_damage_modifiers(
            [registered_state_variant],
        )
        self.assertEqual(len(registered_modifiers), 1)
        self.assertEqual(
            registered_modifiers[0]['scenario_tokens'], ['buff.runtime_probe'],
        )
        self.assertAlmostEqual(registered_modifiers[0]['damage_multiplier'], 1.20)

        damage_and_crit_variant = copy.deepcopy(registered_state_variant)
        for actor_key in ('high', 'low'):
            for action_row in damage_and_crit_variant[actor_key]['actions']:
                for scenario_row in action_row.get('scenarios', []):
                    component_row = scenario_row['amount'].get('direct')
                    if not component_row:
                        continue
                    component_row['crit_chance'] = 0.30
                    component_row['expected'] = 156.0
        damage_and_crit_modifiers = classify_global_damage_modifiers(
            [damage_and_crit_variant],
        )
        self.assertEqual(len(damage_and_crit_modifiers), 1)
        self.assertAlmostEqual(
            damage_and_crit_modifiers[0]['damage_multiplier'], 1.20,
        )

        sparse_reference_variant = {
            'talent': talent,
            'reference_high': actor(scenario=None, effectiveness='inactive'),
            'high': actor(scenario=1.20, effectiveness='active'),
            'reference_low': actor(scenario=None, effectiveness='inactive'),
            'low': actor(scenario=1.20, effectiveness='active'),
        }
        for actor_row in (
            sparse_reference_variant['high'], sparse_reference_variant['low'],
        ):
            for action_row in actor_row['actions']:
                for scenario_row in action_row.get('scenarios', []):
                    scenario_row['active_buffs'] = ['sparse_state']
        sparse_reference_modifiers = classify_global_damage_modifiers([
            sparse_reference_variant,
        ])
        self.assertEqual(len(sparse_reference_modifiers), 1)
        self.assertAlmostEqual(
            sparse_reference_modifiers[0]['damage_multiplier'], 1.20,
        )

        unresolved_actor = actor(scenario=1.0)
        unresolved_action = action('unresolved', scenario=1.0)
        unresolved_action['baseline']['unresolved_reason'] = (
            'periodic_damage_count_unavailable'
        )
        unresolved_actor['actions'].append(unresolved_action)
        evidence_actions = _player_skill_actions(unresolved_actor)
        self.assertNotIn(('unresolved', 'unresolved'), evidence_actions)

        zero_direct_variant = {
            'talent': talent,
            'reference_high': actor(component='tick', layer='ta_multiplier', scenario=1.0, effectiveness='inactive'),
            'high': actor(component='tick', layer='ta_multiplier', scenario=1.20, effectiveness='active'),
            'reference_low': actor(component='tick', layer='ta_multiplier', scenario=1.0, effectiveness='inactive'),
            'low': actor(component='tick', layer='ta_multiplier', scenario=1.20, effectiveness='active'),
        }
        zero_direct = amount(1.0)['direct']
        zero_direct.update({'hit': 0.0, 'crit': 0.0, 'expected': 0.0})
        for actor_row in (
            zero_direct_variant['reference_high'], zero_direct_variant['high'],
            zero_direct_variant['reference_low'], zero_direct_variant['low'],
        ):
            for action_row in actor_row['actions']:
                if action_row.get('player_skill') is not True:
                    continue
                action_row['baseline']['direct'] = copy.deepcopy(zero_direct)
                for scenario_row in action_row.get('scenarios', []):
                    scenario_row['amount']['direct'] = copy.deepcopy(zero_direct)
        zero_direct_modifiers = classify_global_damage_modifiers(
            [zero_direct_variant],
        )
        self.assertEqual(len(zero_direct_modifiers), 1)
        self.assertAlmostEqual(
            zero_direct_modifiers[0]['damage_multiplier'], 1.20,
        )

        unrelated_action_variant = copy.deepcopy(registered_state_variant)
        for actor_key in ('high', 'low'):
            unrelated_action_variant[actor_key]['actions'][-1]['name'] = 'Other Ability'
        self.assertEqual(
            classify_global_damage_modifiers([unrelated_action_variant]),
            [],
        )

        marginal_reference = actor(scenario=1.10, effectiveness='inactive')
        marginal_selected = actor(scenario=1.32, effectiveness='active')
        for reference_action, selected_action in zip(
            marginal_reference['actions'], marginal_selected['actions'], strict=True,
        ):
            if reference_action.get('player_skill') is not True:
                continue
            reference_action['scenarios'].append({
                'active_buffs': ['unrelated_probe'],
                'amount': amount(1.10),
            })
            selected_action['scenarios'].append({
                'active_buffs': ['unrelated_probe'],
                'amount': amount(1.10),
            })
        marginal_runtime_token = {
            'talent': talent,
            'reference_high': copy.deepcopy(marginal_reference),
            'high': copy.deepcopy(marginal_selected),
            'reference_low': copy.deepcopy(marginal_reference),
            'low': copy.deepcopy(marginal_selected),
        }
        marginal_modifiers = classify_global_damage_modifiers([marginal_runtime_token])
        self.assertEqual(len(marginal_modifiers), 1)
        self.assertEqual(marginal_modifiers[0]['scenario_tokens'], ['runtime_probe'])
        self.assertAlmostEqual(marginal_modifiers[0]['damage_multiplier'], 1.20)

        active_ability_reference = actor(scenario=1.0, effectiveness='inactive')
        active_ability_selected = actor(scenario=1.20, effectiveness='active')
        active_ability_selected['actions'].insert(
            -1,
            action('talent_active_ability', baseline=1.0, scenario=1.20),
        )
        active_ability_variant = {
            'talent': talent,
            'reference_high': copy.deepcopy(active_ability_reference),
            'high': copy.deepcopy(active_ability_selected),
            'reference_low': copy.deepcopy(active_ability_reference),
            'low': copy.deepcopy(active_ability_selected),
        }
        active_ability_modifiers = classify_global_damage_modifiers(
            [active_ability_variant],
        )
        self.assertEqual(len(active_ability_modifiers), 1)
        self.assertEqual(
            active_ability_modifiers[0]['scenario_tokens'], ['runtime_probe'],
        )
        self.assertEqual(active_ability_modifiers[0]['evidence_root_count'], 2)

        non_damage_action_variant = copy.deepcopy(shared_runtime_token)
        for actor_key in ('reference_high', 'high', 'reference_low', 'low'):
            non_damage_action_variant[actor_key]['actions'].extend((
                action('healing_absorb', harmful=False),
                action('zero_damage_trigger', baseline=0.0),
            ))
        non_damage_modifiers = classify_global_damage_modifiers(
            [non_damage_action_variant],
        )
        self.assertEqual(len(non_damage_modifiers), 1)
        self.assertEqual(
            non_damage_modifiers[0]['scenario_tokens'], ['runtime_probe'],
        )

        sparse_reference = actor(scenario=1.0, effectiveness='inactive')
        sparse_selected = actor(scenario=1.20, effectiveness='active')
        sparse_reference['actions'].insert(-1, action('third_skill', scenario=1.0))
        sparse_selected['actions'].insert(-1, action('third_skill', scenario=None))
        sparse_runtime_token = {
            'talent': talent,
            'reference_high': copy.deepcopy(sparse_reference),
            'high': copy.deepcopy(sparse_selected),
            'reference_low': copy.deepcopy(sparse_reference),
            'low': copy.deepcopy(sparse_selected),
        }
        sparse_modifiers = classify_global_damage_modifiers([sparse_runtime_token])
        self.assertEqual(len(sparse_modifiers), 1)
        self.assertEqual(sparse_modifiers[0]['evidence_root_count'], 2)

        def target_layer_amount(component_name, multiplier):
            result = amount(multiplier)
            component = result.pop('direct')
            layers = component['runtime_layers']
            layers['da_multiplier'] = 1.0
            layers['target_da_multiplier'] = multiplier
            if component_name == 'tick':
                layers['ta_multiplier'] = layers.pop('da_multiplier')
                layers['target_ta_multiplier'] = layers.pop('target_da_multiplier')
                result['tick'] = component
            else:
                result['direct'] = component
            return result

        split_layer_reference = actor(scenario=None, effectiveness='inactive')
        split_layer_selected = actor(scenario=None, effectiveness='active')
        for index, action_row in enumerate(split_layer_reference['actions'][:-1]):
            component_name = 'direct' if index == 0 else 'tick'
            action_row['baseline'] = target_layer_amount(component_name, 1.0)
        for index, action_row in enumerate(split_layer_selected['actions'][:-1]):
            component_name = 'direct' if index == 0 else 'tick'
            action_row['baseline'] = target_layer_amount(component_name, 1.0)
            action_row['scenarios'] = [{
                'active_buffs': ['runtime_probe'],
                'amount': target_layer_amount(component_name, 1.16),
            }]
        split_layer_runtime_token = {
            'talent': talent,
            'reference_high': copy.deepcopy(split_layer_reference),
            'high': copy.deepcopy(split_layer_selected),
            'reference_low': copy.deepcopy(split_layer_reference),
            'low': copy.deepcopy(split_layer_selected),
        }
        split_layer_modifiers = classify_global_damage_modifiers(
            [split_layer_runtime_token],
        )
        self.assertEqual(len(split_layer_modifiers), 1)
        self.assertEqual(split_layer_modifiers[0]['evidence_root_count'], 2)
        self.assertEqual(
            split_layer_modifiers[0]['runtime_components'], ['direct', 'tick'],
        )

        unrelated_sparse_variant = copy.deepcopy(shared_runtime_token)
        for actor_key in ('reference_high', 'high', 'reference_low', 'low'):
            unrelated_sparse_variant[actor_key]['actions'][0]['scenarios'].append({
                'active_buffs': ['unrelated_sparse_probe'],
                'amount': amount(1.20),
            })
        unrelated_sparse_modifiers = classify_global_damage_modifiers(
            [unrelated_sparse_variant],
        )
        self.assertEqual(len(unrelated_sparse_modifiers), 1)
        self.assertEqual(
            unrelated_sparse_modifiers[0]['scenario_tokens'], ['runtime_probe'],
        )

        wrong_effectiveness = copy.deepcopy(shared_runtime_token)
        wrong_effectiveness['high']['talent_effectiveness'] = 'inactive'
        self.assertEqual(classify_global_damage_modifiers([wrong_effectiveness]), [])

        mixed_reference = actor(layer='da_multiplier')
        mixed_selected = actor(baseline=1.20, layer='da_multiplier')
        for selected_action in mixed_selected['actions']:
            if selected_action['token'] == 'a':
                selected_action['baseline']['direct']['runtime_layers']['player_multiplier'] = 1.10
        mixed = {
            'talent': talent,
            'reference_high': copy.deepcopy(mixed_reference),
            'high': copy.deepcopy(mixed_selected),
            'reference_low': copy.deepcopy(mixed_reference),
            'low': copy.deepcopy(mixed_selected),
        }
        self.assertEqual(classify_global_damage_modifiers([mixed]), [])
        mixed_rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [mixed],
        )
        self.assertTrue(mixed_rows)
        self.assertEqual({row['variant']['talent_id'] for row in mixed_rows}, {talent['id']})

        partially_affected_reference = actor(roots=('a', 'b', 'c'))
        partially_affected_selected = actor(baseline=1.20, roots=('a', 'b', 'c'))
        for selected_action in partially_affected_selected['actions']:
            if selected_action['token'] == 'c':
                selected_action['baseline'] = amount()
        partially_affected = {
            'talent': talent,
            'reference_high': copy.deepcopy(partially_affected_reference),
            'high': copy.deepcopy(partially_affected_selected),
            'reference_low': copy.deepcopy(partially_affected_reference),
            'low': copy.deepcopy(partially_affected_selected),
        }
        self.assertEqual(classify_global_damage_modifiers([partially_affected]), [])

        negative_focused_selected = actor(baseline=1.20, layer='da_multiplier')
        for selected_action in negative_focused_selected['actions']:
            if selected_action['token'] == 'a':
                selected_action['baseline']['direct']['runtime_layers']['player_multiplier'] = 0.80
        negative_focused = {
            'talent': talent,
            'reference_high': actor(layer='da_multiplier'),
            'high': copy.deepcopy(negative_focused_selected),
            'reference_low': actor(layer='da_multiplier'),
            'low': copy.deepcopy(negative_focused_selected),
        }
        self.assertEqual(classify_global_damage_modifiers([negative_focused]), [])
        self.assertTrue(flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [negative_focused],
        ))

        passive_with_focused_scenario = {
            'talent': talent,
            'reference_high': actor(),
            'high': actor(baseline=1.20),
            'reference_low': actor(),
            'low': actor(baseline=1.20),
        }
        for key in ('high', 'low'):
            selected_action = passive_with_focused_scenario[key]['actions'][0]
            focused_amount = amount(1.20)
            focused_amount['direct']['runtime_layers']['player_multiplier'] = 1.10
            selected_action['scenarios'].append({
                'active_buffs': ['focused_probe'],
                'amount': focused_amount,
            })
        self.assertEqual(
            classify_global_damage_modifiers([passive_with_focused_scenario]), [],
        )
        self.assertTrue(flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [passive_with_focused_scenario],
        ))

        passive_with_focused_math = {
            'talent': talent,
            'reference_high': actor(),
            'high': actor(baseline=1.20),
            'reference_low': actor(),
            'low': actor(baseline=1.20),
        }
        for key in ('high', 'low'):
            passive_with_focused_math[key]['actions'][0]['baseline']['direct'][
                'damage_equivalent_count'
            ] = 2.0
        self.assertEqual(
            classify_global_damage_modifiers([passive_with_focused_math]), [],
        )
        self.assertTrue(flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [passive_with_focused_math],
        ))

        tiny_focused_math = copy.deepcopy(passive_with_focused_math)
        for key in ('high', 'low'):
            tiny_focused_math[key]['actions'][0]['baseline']['direct'][
                'damage_equivalent_count'
            ] = 1.000009
        self.assertEqual(classify_global_damage_modifiers([tiny_focused_math]), [])

        global_with_non_player_change = {
            'talent': talent,
            'reference_high': actor(),
            'high': actor(baseline=1.20),
            'reference_low': actor(),
            'low': actor(baseline=1.20),
        }
        for key in ('high', 'low'):
            global_with_non_player_change[key]['actions'][-1]['baseline']['direct'][
                'damage_equivalent_count'
            ] = 2.0
        non_player_modifiers = classify_global_damage_modifiers(
            [global_with_non_player_change],
        )
        self.assertEqual(len(non_player_modifiers), 1)
        non_player_rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [global_with_non_player_change],
        )
        self.assertEqual([row['token'] for row in non_player_rows], ['racial'])

        identity_drift = {
            'talent': talent,
            'reference_high': actor(roots=('a', 'b', 'c')),
            'high': actor(baseline=1.20, roots=('a', 'b', 'c')),
            'reference_low': actor(roots=('a', 'b', 'c')),
            'low': actor(baseline=1.20, roots=('a', 'b', 'c')),
        }
        for key in ('high', 'low'):
            drifted = identity_drift[key]['actions'][2]
            drifted['token'] = 'changed_child_identity'
            drifted['spell_id'] = 'changed_child_identity'
        self.assertEqual(classify_global_damage_modifiers([identity_drift]), [])

        restricted = {
            'talent': {
                **talent,
                'description': 'Increases your damage with Fireball by 20%.',
            },
            'reference_high': actor(), 'high': actor(baseline=1.20),
            'reference_low': actor(), 'low': actor(baseline=1.20),
        }
        self.assertEqual(classify_global_damage_modifiers([restricted]), [])

        target_condition = {
            'talent': {
                **talent,
                'description': 'Increases your damage against stunned targets by 10%.',
            },
            'reference_high': actor(), 'high': actor(baseline=1.10),
            'reference_low': actor(), 'low': actor(baseline=1.10),
        }
        self.assertEqual(len(classify_global_damage_modifiers([target_condition])), 1)

        focused = {
            'talent': {
                'id': 10, 'name': 'Focused',
                'description': 'Increases one ability damage.',
            },
            'reference_high': actor(), 'high': actor(baseline=1.20),
            'reference_low': actor(), 'low': actor(baseline=1.20),
        }
        self.assertEqual(classify_global_damage_modifiers([focused]), [])

        one_root = copy.deepcopy(focused)
        one_root['talent'] = talent
        for key in ('reference_high', 'high', 'reference_low', 'low'):
            one_root[key] = actor(
                baseline=1.20 if key in ('high', 'low') else 1.0,
                roots=('a',),
            )
        self.assertEqual(classify_global_damage_modifiers([one_root]), [])

        inconsistent = copy.deepcopy(one_root)
        inconsistent['reference_high'] = actor()
        inconsistent['high'] = actor(baseline=1.20)
        inconsistent['reference_low'] = actor()
        inconsistent['low'] = actor(baseline=1.10)
        self.assertEqual(classify_global_damage_modifiers([inconsistent]), [])

        missing_low = copy.deepcopy(inconsistent)
        missing_low.pop('reference_low')
        missing_low.pop('low')
        self.assertEqual(classify_global_damage_modifiers([missing_low]), [])

    def test_variant_only_uniform_debuff_is_global_and_stripped_from_action_rows(self):
        def amount(multiplier):
            layers = {
                name: 1.0 for name in (
                    'da_multiplier', 'player_multiplier', 'versus_multiplier',
                    'persistent_multiplier', 'target_da_multiplier', 'versatility',
                    'pet_multiplier', 'target_pet_multiplier',
                )
            }
            layers['da_multiplier'] = multiplier
            return {
                'direct': {
                    'hit': 100.0 * multiplier, 'crit': 200.0 * multiplier,
                    'crit_multiplier': 2.0, 'crit_chance': 0.2,
                    'crit_chance_uncapped': 0.2, 'can_crit': True,
                    'expected': 120.0 * multiplier, 'damage_equivalent_count': 1.0,
                    'base_damage_layers': {
                        'base_multiplier': 1.0, 'component_multiplier': 1.0,
                    },
                    'runtime_layers': layers,
                },
                'tick': None, 'unresolved_reason': None,
            }

        def action(token, spell_id, with_debuff=False):
            scenarios = []
            if with_debuff:
                scenarios.append({
                    'buffs': [{
                        'token': 'debuff.exposed', 'scope': 'target',
                        'spell_id': 90001, 'class_family': 1, 'stacks': 1,
                    }],
                    'values': amount(1.20),
                })
            return {
                'token': token, 'spell_id': spell_id, 'supported': True,
                'player_skill': True, 'harmful': True,
                'reporting_root_token': token,
                'reporting_root_spell_id': spell_id,
                'reporting_root_component': True,
                'baseline': amount(1.0), 'scenarios': scenarios,
                'dbc_scaling': {
                    'direct': {
                        'attack_power_coefficient': 1.0,
                        'spell_power_coefficient': 0.0,
                        'normalized_base': 100.0,
                    },
                    'tick': None,
                },
            }

        reference = {
            'talent_effectiveness': 'inactive',
            'actions': [action('a', 100), action('b', 200)],
        }
        selected = {
            'talent_effectiveness': 'active',
            'actions': [action('a', 100, True), action('b', 200, True)],
        }
        variant = {
            'talent': {
                'id': 903, 'name': 'Expose Weakness', 'name_zh': '揭示弱点',
                'description': (
                    'Applies Exposed to the target. '
                    'Increases all damage you deal to affected targets.'
                ),
            },
            'reference_high': reference, 'reference_low': reference,
            'high': selected, 'low': selected,
        }
        effects = classify_global_skill_effects(
            {'actions': []}, {'actions': []}, [variant],
        )
        self.assertEqual(len([
            effect for effect in effects
            if effect.get('scenario_tokens') == ['debuff.exposed']
        ]), 1)
        state_effect = next(
            effect for effect in effects
            if effect.get('scenario_tokens') == ['debuff.exposed']
        )
        self.assertEqual(state_effect['source_type'], 'talent')
        self.assertEqual(state_effect['talent_id'], 903)
        self.assertEqual(state_effect['runtime_conditions'], [{
            'token': 'debuff.exposed', 'scope': 'target', 'spell_id': 90001,
        }])
        self.assertAlmostEqual(state_effect['projections'][0]['value'], 1.20)
        self.assertEqual(state_effect['evidence']['damage']['evidence_root_count'], 2)

        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, [variant], global_effects=effects,
        )
        self.assertFalse(any(
            (row.get('variant') or {}).get('scenario_tokens') == ['debuff.exposed']
            for row in rows
        ))

        inherited_variant = copy.deepcopy(variant)
        inherited_variant['reference_high'] = selected
        inherited_variant['reference_low'] = selected
        self.assertEqual(
            classify_global_skill_effects(
                {'actions': []}, {'actions': []}, [inherited_variant],
            ),
            [],
        )

    def test_global_scenario_is_stripped_from_every_talent_actor(self):
        direct_layers = (
            'da_multiplier', 'player_multiplier', 'versus_multiplier',
            'persistent_multiplier', 'target_da_multiplier', 'versatility',
            'pet_multiplier', 'target_pet_multiplier',
        )

        def amount(multiplier=1.0):
            layers = {name: 1.0 for name in direct_layers}
            layers['da_multiplier'] = multiplier
            return {
                'direct': {
                    'hit': 100.0 * multiplier,
                    'crit': 200.0 * multiplier,
                    'crit_multiplier': 2.0,
                    'crit_chance': 0.2,
                    'expected': 120.0 * multiplier,
                    'damage_equivalent_count': 1.0,
                    'runtime_layers': layers,
                },
                'tick': None,
                'unresolved_reason': None,
            }

        def action(token, baseline=1.0, scenarios=()):
            return {
                'token': token, 'spell_id': token, 'supported': True,
                'player_skill': True, 'harmful': True,
                'reporting_root_token': token,
                'reporting_root_spell_id': token,
                'reporting_root_component': True,
                'baseline': amount(baseline),
                'scenarios': [
                    {'active_buffs': list(tokens), 'amount': amount(multiplier)}
                    for tokens, multiplier in scenarios
                ],
                'dbc_scaling': {
                    'direct': {'normalized_base': 100.0}, 'tick': None,
                },
            }

        def actor(*, avatar=1.0, local=None, effectiveness):
            scenarios = [
                (('buff.avatar',), avatar),
                (('buff.avatar_secondary',), avatar),
            ]
            if local is not None:
                scenarios.append((('buff.local_focus',), local))
            return {
                'talent_effectiveness': effectiveness,
                'actions': [
                    action('bloodthirst', scenarios=scenarios),
                    action('raging_blow', scenarios=[
                        (('buff.avatar',), avatar),
                        (('buff.avatar_secondary',), avatar),
                    ]),
                ],
            }

        avatar_reference = actor(avatar=1.0, local=1.0, effectiveness='inactive')
        avatar_selected = actor(avatar=1.20, local=1.10, effectiveness='active')
        child_action = action(
            'avatar_child', scenarios=[
                (('buff.avatar',), 1.20),
                (('buff.avatar_secondary',), 1.20),
            ],
        )
        child_action['player_skill'] = False
        avatar_selected['actions'].append(child_action)
        unrelated_reference = actor(avatar=1.0, effectiveness='inactive')
        unrelated_selected = actor(
            avatar=1.20, local=1.10, effectiveness='active',
        )
        variants = [
            {
                'talent': {
                    'id': 1, 'name': 'Avatar', 'name_zh': '天神下凡',
                    'description': 'Increases all damage you deal.',
                },
                'reference_high': copy.deepcopy(avatar_reference),
                'high': copy.deepcopy(avatar_selected),
                'reference_low': copy.deepcopy(avatar_reference),
                'low': copy.deepcopy(avatar_selected),
            },
            {
                'talent': {
                    'id': 2, 'name': 'Focused Talent', 'name_zh': '局部天赋',
                    'description': 'Improves one ability.',
                },
                'reference_high': copy.deepcopy(unrelated_reference),
                'high': copy.deepcopy(unrelated_selected),
                'reference_low': copy.deepcopy(unrelated_reference),
                'low': copy.deepcopy(unrelated_selected),
            },
        ]

        modifiers = classify_global_damage_modifiers(variants)
        self.assertEqual(
            [item['scenario_tokens'] for item in modifiers],
            [['buff.avatar'], ['buff.avatar_secondary']],
        )
        effects = classify_global_skill_effects(
            {'actions': []}, {'actions': []}, variants,
        )
        self.assertEqual(
            [effect['scenario_tokens'] for effect in effects if effect['source_type'] == 'talent'],
            [['buff.avatar'], ['buff.avatar_secondary']],
        )
        rows = flatten_single_talent_damage_variants(
            {'actions': []}, {'actions': []}, variants, global_effects=effects,
        )
        self.assertFalse(any(
            {'buff.avatar', 'buff.avatar_secondary'}
            & set((row.get('variant') or {}).get('scenario_tokens', []))
            for row in rows
        ))
        self.assertTrue(any(
            (row.get('variant') or {}).get('scenario_tokens') == ['buff.local_focus']
            and (row.get('variant') or {}).get('talent_id') == 1
            for row in rows
        ))

    def test_global_skill_effects_cover_damage_crit_and_base_layers(self):
        runtime_fields = (
            'da_multiplier', 'player_multiplier', 'versus_multiplier',
            'persistent_multiplier', 'target_da_multiplier', 'versatility',
            'pet_multiplier', 'target_pet_multiplier',
        )

        def component(*, damage=1.0, crit_delta=0.0, base=1.0, can_crit=True):
            layers = {name: 1.0 for name in runtime_fields}
            layers['da_multiplier'] = damage
            crit_chance = 0.2 + crit_delta if can_crit else 0.0
            hit = 100.0 * damage * base
            crit = 200.0 * damage * base
            return {
                'hit': hit, 'crit': crit, 'crit_multiplier': 2.0,
                'crit_chance': crit_chance,
                'crit_chance_uncapped': crit_chance,
                'can_crit': can_crit,
                'expected': hit * (1.0 - crit_chance) + crit * crit_chance,
                'damage_equivalent_count': 1.0,
                'base_damage_layers': {
                    'base_multiplier': 1.0,
                    'component_multiplier': base,
                },
                'runtime_layers': layers,
            }

        def amount(**kwargs):
            return {'direct': component(**kwargs), 'tick': None, 'unresolved_reason': None}

        def action(token, *, baseline=None, scenarios=()):
            return {
                'token': token, 'spell_id': 100 if token == 'a' else 200,
                'supported': True, 'player_skill': True, 'harmful': True,
                'reporting_root_token': token,
                'reporting_root_spell_id': 100 if token == 'a' else 200,
                'reporting_root_component': True,
                'baseline': baseline or amount(),
                'scenarios': [
                    {
                        'buffs': [{
                            'token': scenario_token, 'scope': 'self',
                            'spell_id': spell_id, 'class_family': 4, 'stacks': 1,
                        }],
                        'values': scenario_amount,
                    }
                    for scenario_token, spell_id, scenario_amount in scenarios
                ],
                'dbc_scaling': {
                    'direct': {'normalized_base': 100.0}, 'tick': None,
                },
            }

        shared_scenarios = (
            ('buff.avatar', 107574, amount(damage=1.20)),
            ('buff.recklessness', 1719, amount(crit_delta=0.20)),
            ('buff.compound_global', 999001, amount(damage=1.20, crit_delta=0.20)),
        )
        base_actor = {
            'talent_effectiveness': 'unknown',
            'actions': [
                action('a', scenarios=shared_scenarios),
                action('b', scenarios=shared_scenarios),
            ],
        }
        weapon_scenarios = (
            ('buff.avatar', 107574, amount(damage=1.20)),
            ('buff.recklessness', 1719, amount(crit_delta=0.20)),
        )
        selected_weapon_scenarios = (
            ('buff.avatar', 107574, amount(damage=1.20, base=1.06)),
            ('buff.recklessness', 1719, amount(crit_delta=0.20, base=1.06)),
        )
        weapon_reference = {
            'talent_effectiveness': 'inactive',
            'actions': [
                action('a', scenarios=weapon_scenarios),
                action('b', scenarios=weapon_scenarios),
            ],
        }
        weapon_selected = {
            'talent_effectiveness': 'active',
            'actions': [
                action('a', baseline=amount(base=1.06), scenarios=selected_weapon_scenarios),
                action('b', baseline=amount(base=1.06), scenarios=selected_weapon_scenarios),
            ],
        }
        for selected_action in weapon_selected['actions']:
            selected_action['baseline']['direct']['runtime_layers']['da_multiplier'] = 1.06
        variants = [{
            'talent': {
                'id': 9, 'name': 'Weapon Specialization', 'name_zh': '武器专精',
                'description': 'While wielding this weapon your damage is increased.',
            },
            'reference_high': copy.deepcopy(weapon_reference),
            'high': copy.deepcopy(weapon_selected),
            'reference_low': copy.deepcopy(weapon_reference),
            'low': copy.deepcopy(weapon_selected),
        }]

        runtime_only_variant = copy.deepcopy(variants[0])
        for actor_key in ('reference_high', 'reference_low', 'high', 'low'):
            for runtime_action in runtime_only_variant[actor_key]['actions']:
                runtime_action['scenarios'] = [
                    scenario for scenario in runtime_action.get('scenarios') or []
                    if (scenario.get('buffs') or [{}])[0].get('token') == 'buff.avatar'
                ]
                selected_side = actor_key in ('high', 'low')
                runtime_action['baseline']['direct'] = amount(
                    base=1.06 if selected_side else 1.0,
                )['direct']
                runtime_action['scenarios'][0]['values']['direct'] = amount(
                    base=1.06 if selected_side else 1.0, damage=1.20,
                )['direct']
                runtime_action['baseline']['direct']['base_damage_layers']['component_multiplier'] = 1.0
                runtime_action['scenarios'][0]['values']['direct']['base_damage_layers']['component_multiplier'] = 1.0
                runtime_action['baseline']['direct']['runtime_layers']['da_multiplier'] = (
                    1.06 if selected_side else 1.0
                )
                runtime_action['scenarios'][0]['values']['direct']['runtime_layers']['da_multiplier'] = (
                    1.272 if selected_side else 1.20
                )
                for amount_row in (
                    runtime_action['baseline'], runtime_action['scenarios'][0]['values'],
                ):
                    amount_row['direct']['runtime_layers']['specialization_passive_effects'] = [{
                        'source_spell_id': 137050,
                        'effect_index': 0,
                        'component': 'direct',
                        'factor': 1.22,
                    }]
        self.assertIsNotNone(_runtime_layer_candidate(
            runtime_only_variant['reference_high'], runtime_only_variant['high'], (),
        ))
        for scenario_tokens in _scenario_token_universe(runtime_only_variant['high']):
            self.assertFalse(
                _scenario_has_target_marginal_change(
                    runtime_only_variant['reference_high'], runtime_only_variant['high'],
                    scenario_tokens,
                ),
                scenario_tokens,
            )
        self.assertIn((), _runtime_layer_candidates(
            runtime_only_variant['reference_high'], runtime_only_variant['high'],
        ))
        runtime_only_effects = classify_global_skill_effects(
            copy.deepcopy(base_actor), copy.deepcopy(base_actor), [runtime_only_variant],
        )
        runtime_only_weapon = {
            effect['effect_id']: effect for effect in runtime_only_effects
        }['talent:9:passive']
        self.assertAlmostEqual(runtime_only_weapon['projections'][0]['value'], 1.06)

        effects = classify_global_skill_effects(
            copy.deepcopy(base_actor), copy.deepcopy(base_actor), variants,
        )
        by_source = {effect['effect_id']: effect for effect in effects}
        avatar = by_source['runtime_state:buff.avatar']
        recklessness = by_source['runtime_state:buff.recklessness']
        compound = by_source['runtime_state:buff.compound_global']
        weapon = by_source['talent:9:passive']
        self.assertEqual(avatar['projections'][0]['kind'], 'damage_multiplier')
        self.assertAlmostEqual(avatar['projections'][0]['value'], 1.20)
        self.assertEqual(recklessness['projections'][0]['kind'], 'crit_chance')
        self.assertAlmostEqual(recklessness['projections'][0]['percentage_points'], 20.0)
        self.assertEqual(
            {projection['kind'] for projection in compound['projections']},
            {'damage_multiplier', 'crit_chance'},
        )
        self.assertEqual(weapon['projections'][0]['kind'], 'damage_multiplier')
        self.assertAlmostEqual(weapon['projections'][0]['value'], 1.06)

        base_layer_evidence = _base_damage_layer_candidate(weapon_reference, weapon_selected)
        self.assertAlmostEqual(base_layer_evidence['multiplier'], 1.06)
        self.assertEqual(base_layer_evidence['mirrored_runtime_layers'], ['da_multiplier'])
        mismatched_mirror = copy.deepcopy(weapon_selected)
        mismatched_mirror['actions'][0]['baseline']['direct']['runtime_layers']['da_multiplier'] = 1.05
        self.assertIsNone(_base_damage_layer_candidate(weapon_reference, mismatched_mirror))

        mixed_component_reference = copy.deepcopy(weapon_reference)
        mixed_component_selected = copy.deepcopy(weapon_selected)
        for actor in (mixed_component_reference, mixed_component_selected):
            periodic = actor['actions'][1]['baseline'].pop('direct')
            periodic['runtime_layers']['ta_multiplier'] = periodic['runtime_layers'].pop('da_multiplier')
            actor['actions'][1]['baseline']['tick'] = periodic
        mixed_component_evidence = _base_damage_layer_candidate(
            mixed_component_reference, mixed_component_selected,
        )
        self.assertAlmostEqual(mixed_component_evidence['multiplier'], 1.06)
        self.assertEqual(
            mixed_component_evidence['mirrored_runtime_layers'],
            ['da_multiplier', 'ta_multiplier'],
        )

        rows = flatten_single_talent_damage_variants(
            base_actor, base_actor, variants, global_effects=effects,
        )
        self.assertFalse(any((row.get('variant') or {}).get('talent_id') == 9 for row in rows))
        self.assertFalse(any(
            set((row.get('variant') or {}).get('scenario_tokens') or [])
            & {'buff.avatar', 'buff.recklessness'}
            for row in rows
        ))

        mixed_crit_actor = copy.deepcopy(base_actor)
        mixed_crit_actor['actions'].append(action(
            'c', baseline=amount(can_crit=False), scenarios=((
                'buff.recklessness', 1719, amount(damage=1.10, can_crit=False),
            ),),
        ))
        mixed_effects = classify_global_skill_effects(
            mixed_crit_actor, mixed_crit_actor, [],
        )
        self.assertNotIn(
            'runtime_state:buff.recklessness',
            {effect['effect_id'] for effect in mixed_effects},
        )

        partially_scaled_selected = copy.deepcopy(weapon_selected)
        partially_scaled_reference = copy.deepcopy(weapon_reference)
        partially_scaled_reference['actions'].append(action('c'))
        partially_scaled_selected['actions'].append(action('c'))
        partial_variant = copy.deepcopy(variants[0])
        for key in ('reference_high', 'reference_low'):
            partial_variant[key] = copy.deepcopy(partially_scaled_reference)
        for key in ('high', 'low'):
            partial_variant[key] = copy.deepcopy(partially_scaled_selected)
        partial_effects = classify_global_skill_effects(
            base_actor, base_actor, [partial_variant],
        )
        self.assertNotIn(
            'talent:9:passive', {effect['effect_id'] for effect in partial_effects},
        )
        partial_rows = flatten_single_talent_damage_variants(
            base_actor, base_actor, [partial_variant], global_effects=partial_effects,
        )
        self.assertTrue(any(
            (row.get('variant') or {}).get('talent_id') == 9
            for row in partial_rows
        ))

        selected_only_variant = copy.deepcopy(variants[0])
        for key in ('high', 'low'):
            selected_only_variant[key]['actions'].append(action('c'))
        selected_only_effects = classify_global_skill_effects(
            base_actor, base_actor, [selected_only_variant],
        )
        self.assertNotIn(
            'talent:9:passive',
            {effect['effect_id'] for effect in selected_only_effects},
        )

        runtime_selected_only_variant = copy.deepcopy(variants[0])
        for selected_key in ('high', 'low'):
            runtime_selected_only_variant[selected_key] = {
                'talent_effectiveness': 'active',
                'actions': [
                    action('a', baseline=amount(damage=1.20)),
                    action('b', baseline=amount(damage=1.20)),
                    action('c'),
                ],
            }
        runtime_selected_only_effects = classify_global_skill_effects(
            base_actor, base_actor, [runtime_selected_only_variant],
        )
        self.assertNotIn(
            'talent:9:passive',
            {effect['effect_id'] for effect in runtime_selected_only_effects},
        )

    def test_inactive_derived_action_is_owned_by_hero_tree_with_passive_activation(self):
        def amount(hit):
            return {
                'direct': {
                    'hit': hit, 'crit': hit * 2, 'crit_multiplier': 2.0,
                    'crit_chance': 0.2, 'expected': hit * 1.2,
                },
                'tick': None, 'unresolved_reason': None,
            }

        def action(hit, *, player_skill=False, scenarios=(), selected_trait_effects=()):
            return {
                'token': 'derived_action', 'spell_id': 42, 'supported': True,
                'player_skill': player_skill,
                'reporting_root_token': 'derived_action',
                'reporting_root_spell_id': 42,
                'reporting_root_component': True,
                'selected_trait_effects': list(selected_trait_effects),
                'baseline': amount(hit),
                'scenarios': [
                    {
                        'buffs': [{'token': token, 'spell_id': 99, 'scope': 'target'}],
                        'values': amount(value),
                    }
                    for token, value in scenarios
                ],
            }

        baseline = action(100)
        mountain_reference = action(100)
        mountain_selected = action(120, selected_trait_effects=({
            'trait_entry_id': 1001,
            'source_spell_id': 435607,
            'effect_index': 1,
        },))
        slayer_reference = action(100)
        slayer_selected = action(130, scenarios=(('debuff.side_effect', 150),))
        rows = flatten_single_talent_damage_variants(
            {'actions': [baseline]}, {'actions': [baseline]}, [
                {
                    'talent': {
                        'id': 1, 'node_id': 1001, 'name': 'Passive activator', 'tree_type': 'hero',
                        'hero_subtree_id': 61, 'hero_subtree_name': 'Mountain',
                    },
                    'reference_high': {'actions': [mountain_reference]},
                    'high': {'actions': [mountain_selected]},
                    'reference_low': {'actions': [mountain_reference]},
                    'low': {'actions': [mountain_selected]},
                },
                {
                    'talent': {
                        'id': 2, 'node_id': 1002, 'name': 'Conditional side effect', 'tree_type': 'hero',
                        'hero_subtree_id': 60, 'hero_subtree_name': 'Slayer',
                    },
                    'reference_high': {'actions': [slayer_reference]},
                    'high': {'actions': [slayer_selected]},
                    'reference_low': {'actions': [slayer_reference]},
                    'low': {'actions': [slayer_selected]},
                },
            ],
        )

        derived_rows = [row for row in rows if row.get('token') == 'derived_action']
        self.assertEqual(
            {tuple(row.get('hero_subtree_ids') or ()) for row in derived_rows},
            {(61,)},
        )

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

    def test_existing_schema_thirteen_snapshot_creates_new_schema_eighteen_identity(self):
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
            schema_revision=13,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'specialization': 'fury',
                'hero_talent_tree': '屠戮者',
                'actions': [],
            }]},
        )

        service = SimcSkillDamageSnapshotService.create_for_current_backend()

        self.assertNotEqual(service.snapshot.pk, existing.pk)
        self.assertEqual(service.snapshot.schema_revision, 19)
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
            mock.call(profile, talents[:12], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=100),
            mock.call(profile, talents[12:24], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=100),
            mock.call(profile, talents[24:], scaffold_talents=[], talent_prerequisites=mock.ANY, target_health=100),
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

    def test_generate_releases_completed_profile_raw_export_graph_before_next_profile(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='b' * 40, game_build='12.1.0.69299', schema_revision=19,
        )
        profiles = [
            SimpleNamespace(pk=1, spec='warrior_fury', class_name='warrior'),
            SimpleNamespace(pk=2, spec='warrior_arms', class_name='warrior'),
        ]
        talents = {
            1: [SimpleNamespace(
                pk=101, node_id=1001, tree_type='spec', name='First Talent',
                name_zh='第一天赋', description='', description_zh='',
            )],
            2: [SimpleNamespace(
                pk=201, node_id=2001, tree_type='spec', name='Second Talent',
                name_zh='第二天赋', description='', description_zh='',
            )],
        }
        first_profile_raw_refs = []

        class RawGraphProbe:
            pass

        def iter_profiles():
            yield profiles[0]
            snapshot.refresh_from_db()
            self.assertEqual(snapshot.status, SimcSkillDamageSnapshot.STATUS_RUNNING)
            self.assertEqual(snapshot.generated_spec_count, 1)
            self.assertEqual(
                [actor['specialization'] for actor in snapshot.payload['actors']],
                ['fury'],
            )
            gc.collect()
            self.assertTrue(first_profile_raw_refs)
            self.assertTrue(
                all(ref() is None for ref in first_profile_raw_refs),
                '完成首个专精后仍保留其完整 exporter raw actor 图',
            )
            yield profiles[1]

        def run_target(profile, profile_talents, **_kwargs):
            base = {
                'name': 'skill_damage_base', 'class': 'warrior',
                'spec': 'fury' if profile.pk == 1 else 'arms',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [],
            }
            actor_map = {}
            for talent in profile_talents:
                identity = f'{talent.pk}_trait_{talent.node_id}'
                for prefix in ('reference', 'talent'):
                    probe = RawGraphProbe()
                    if profile.pk == 1:
                        first_profile_raw_refs.append(weakref.ref(probe))
                    actor_map[f'skill_damage_{prefix}_{identity}'] = {
                        'name': f'skill_damage_{prefix}_{identity}',
                        'class': 'warrior', 'spec': base['spec'], 'actions': [],
                        '_raw_graph_probe': probe,
                    }
            return base, actor_map, []

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_profiles', side_effect=iter_profiles), \
             mock.patch.object(
                 service, '_talent_entries', side_effect=lambda profile: talents[profile.pk],
             ), \
             mock.patch.object(service, '_hero_talent_trees', return_value=[]), \
             mock.patch.object(service, '_run_profile_target_resilient', side_effect=run_target):
            result = service.generate()

        self.assertEqual([actor['specialization'] for actor in result['actors']], ['fury', 'arms'])

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
                'talent_effectiveness': 'unknown',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': [],
            }]
            for talent in batch:
                for prefix in ('reference', 'talent'):
                    actors.append({
                        'name': f'skill_damage_{prefix}_{talent.pk}_trait_{talent.node_id}',
                        'class': 'druid', 'spec': 'guardian',
                        'talent_effectiveness': 'inactive' if prefix == 'reference' else 'active',
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

    def test_resilient_export_isolates_simc_assertion_failure(self):
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
                    "simc: class_modules/sc_druid.cpp:5687: virtual void "
                    "maul_base_t::snapshot_state(action_state_t*, result_amount_type): "
                    "Assertion `p()->resources.current[ RESOURCE_RAGE ] >= cost()' failed."
                )
            return baseline

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_run_profile_export', side_effect=export_batch):
            _base, actors, unresolved = service._run_profile_target_resilient(
                profile, [talent], scaffold_talents=[],
                talent_prerequisites={talent.pk: []}, target_health=100,
            )

        self.assertEqual(actors, {})
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['reason'], 'simc_actor_initialization_failed')
        self.assertIn('Assertion `', unresolved[0]['diagnostic'])

    def test_resilient_export_isolates_missing_action_spell_data(self):
        snapshot = SimpleNamespace(simc_revision='e' * 40, game_build='12.1.0.69299')
        profile = SimpleNamespace(pk=1, spec='death_knight_blood', class_name='death_knight')
        talent = SimpleNamespace(
            pk=22, node_id=68470, tree_type='spec', name='Boiling Point', name_zh='沸点',
        )
        baseline = {
            'actors': [{'name': 'skill_damage_base', 'class': 'death_knight', 'spec': 'blood',
                        'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': []}],
            'unresolved': [],
        }

        def export_batch(
            _profile, batch, *, scaffold_talents, talent_prerequisites=None, target_health,
        ):
            if batch:
                raise RuntimeError(
                    "Error: Player 'skill_damage_talent_22' could not find spell data "
                    "for Action 'blood_boil_boiling_point' (0)."
                )
            return baseline

        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_run_profile_export', side_effect=export_batch):
            _base, actors, unresolved = service._run_profile_target_resilient(
                profile, [talent], scaffold_talents=[],
                talent_prerequisites={talent.pk: []}, target_health=100,
            )

        self.assertEqual(actors, {})
        self.assertEqual(len(unresolved), 1)
        self.assertEqual(unresolved[0]['reason'], 'simc_actor_initialization_failed')
        self.assertIn('blood_boil_boiling_point', unresolved[0]['diagnostic'])

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
                'talent_effectiveness': 'unknown',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': [],
            }]
            for row in batch:
                for prefix in ('reference', 'talent'):
                    actors.append({
                        'name': f'skill_damage_{prefix}_{row.pk}_trait_{row.node_id}',
                        'class': 'druid', 'spec': 'guardian',
                        'talent_effectiveness': 'inactive' if prefix == 'reference' else 'active',
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
        self.assertEqual(variants[0]['high']['name'], 'skill_damage_talent_11_trait_137059')
        self.assertIsNone(variants[0]['low'])
        self.assertEqual(
            [(row['talent']['id'], row['target_health_percentage']) for row in result['unresolved']],
            [(137059, 34)],
        )

    def test_schema_five_requires_player_skill_and_complete_runtime_evidence(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=10,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        direct_runtime_layers = {
            'da_multiplier': 1.0,
            'player_multiplier': 1.0,
            'versus_multiplier': 1.0,
            'persistent_multiplier': 1.0,
            'target_da_multiplier': 1.0,
            'versatility': 1.0,
            'pet_multiplier': 1.0,
            'target_pet_multiplier': 1.0,
            'specialization_passive_effects': [],
        }
        payload = {
            'schema_version': 11,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior',
                'spec': 'fury',
                'talent_effectiveness': 'unknown',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                'token': 'test_action',
                'spell_id': 1,
                'supported': True,
                'player_skill': True,
                'selected_trait_effects': [],
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
                    'crit_chance': 0.2, 'crit_chance_uncapped': 0.2,
                    'can_crit': True, 'expected': 509.04,
                    'damage_equivalent_count': 1.0,
                    'base_damage_layers': {
                        'base_multiplier': 1.0, 'component_multiplier': 1.0,
                    },
                    'runtime_layers': dict(direct_runtime_layers),
                }, 'tick': None},
                'scenarios': [],
            }]}],
        }
        service._validate_export(payload)
        action = payload['actors'][0]['actions'][0]
        action['scenarios'] = [
            {
                'buffs': [{
                    'token': 'buff.test_stack', 'scope': 'self',
                    'spell_id': 12345, 'class_family': 4, 'stacks': stacks,
                }],
                'values': copy.deepcopy(action['baseline']),
                'direct_multiplier': 1.0,
                'tick_multiplier': 1.0,
            }
            for stacks in (1, 2)
        ]
        service._validate_export(payload)
        action['scenarios'] = []

        player_skill = action.pop('player_skill')
        with self.assertRaisesRegex(ValueError, 'player skill'):
            service._validate_export(payload)
        action['player_skill'] = player_skill

        direct = action['baseline']['direct']
        runtime_layers = direct.pop('runtime_layers')
        with self.assertRaisesRegex(ValueError, 'runtime layers'):
            service._validate_export(payload)
        direct['runtime_layers'] = runtime_layers

        direct['runtime_layers']['da_multiplier'] = float('nan')
        with self.assertRaisesRegex(ValueError, 'runtime layers'):
            service._validate_export(payload)
        direct['runtime_layers']['da_multiplier'] = 1.0

        direct['runtime_layers']['specialization_passive_effects'] = [{
            'effect_index': 0,
            'source_spell_id': 137050,
            'source_name': 'Fury Warrior',
            'component': 'direct',
            'factor': 1.22,
        }]
        service._validate_export(payload)

        direct['runtime_layers']['specialization_passive_effects'][0]['component'] = 'tick'
        with self.assertRaisesRegex(ValueError, 'specialization passive effects'):
            service._validate_export(payload)
        direct['runtime_layers']['specialization_passive_effects'][0]['component'] = 'direct'

        direct['runtime_layers']['specialization_passive_effects'][0]['factor'] = float('nan')
        with self.assertRaisesRegex(ValueError, 'specialization passive effects'):
            service._validate_export(payload)
        direct['runtime_layers']['specialization_passive_effects'][0]['factor'] = 1.22

        direct['runtime_layers']['unexpected'] = 1.0
        with self.assertRaisesRegex(ValueError, 'runtime layers'):
            service._validate_export(payload)
        del direct['runtime_layers']['unexpected']

        tick_payload = copy.deepcopy(payload)
        tick_action = tick_payload['actors'][0]['actions'][0]
        tick_action['baseline'] = {
            'direct': None,
            'tick': {
                **copy.deepcopy(direct),
                'runtime_layers': {
                    'ta_multiplier': 1.0,
                    'player_multiplier': 1.0,
                    'versus_multiplier': 1.0,
                    'persistent_multiplier': 1.0,
                    'target_ta_multiplier': 1.0,
                    'versatility': 1.0,
                    'pet_multiplier': 1.0,
                    'target_pet_multiplier': 1.0,
                    'specialization_passive_effects': copy.deepcopy(
                        direct['runtime_layers']['specialization_passive_effects']
                    ),
                },
            },
        }
        tick_passive_effects = tick_action['baseline']['tick']['runtime_layers'][
            'specialization_passive_effects'
        ]
        for effect in tick_passive_effects:
            effect['component'] = 'tick'
        service._validate_export(tick_payload)
        tick_action['baseline']['tick']['runtime_layers'].pop('target_ta_multiplier')
        with self.assertRaisesRegex(ValueError, 'runtime layers'):
            service._validate_export(tick_payload)

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
                'crit_chance': 0.2, 'crit_chance_uncapped': 0.2,
                'can_crit': True, 'expected': 120.0,
                'damage_equivalent_count': 1.0,
                'base_damage_layers': {
                    'base_multiplier': 1.0, 'component_multiplier': 1.0,
                },
                'runtime_layers': {
                    'da_multiplier': 1.0,
                    'player_multiplier': 1.0,
                    'versus_multiplier': 1.0,
                    'persistent_multiplier': 1.0,
                    'target_da_multiplier': 1.0,
                    'versatility': 1.0,
                    'pet_multiplier': 1.0,
                    'target_pet_multiplier': 1.0,
                    'specialization_passive_effects': [],
                },
            }, 'tick': None}

        def action(token, spell_id):
            return {
                'token': token, 'spell_id': spell_id, 'supported': True,
                'player_skill': True,
                'selected_trait_effects': [],
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
                    'buffs': [{
                        'token': 'probe', 'scope': 'self',
                        'spell_id': 123, 'class_family': 4, 'stacks': 1,
                    }],
                    'values': amount(),
                }],
            }

        payload = {
            'schema_version': 11, 'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior', 'spec': 'fury',
                'talent_effectiveness': 'unknown',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [action('leaf', 1)],
            }],
        }
        service._validate_export(payload)

        rounded_expected = copy.deepcopy(payload)
        rounded_expected['actors'][0]['actions'][0]['baseline']['direct']['expected'] = 120.0149
        rounded_expected['actors'][0]['actions'][0]['scenarios'][0]['values']['direct']['expected'] = 120.0149
        service._validate_export(rounded_expected)

        decimal_boundary = copy.deepcopy(payload)
        decimal_component = decimal_boundary['actors'][0]['actions'][0]['scenarios'][0]['values']['direct']
        decimal_component.update({
            'hit': 0.1,
            'crit': 0.2,
            'crit_chance': 0.2,
            'crit_chance_uncapped': 0.2,
            'expected': 0.135,
        })
        with self.assertRaisesRegex(ValueError, '数学期望一致性'):
            service._validate_export(decimal_boundary)

        shared_root_spell = copy.deepcopy(payload)
        second = action('blood_plague_heal', 2)
        second['reporting_root_token'] = 'blood_plague_heal'
        # SimC can expose distinct damage/heal action roots for one DBC spell ID.
        second['reporting_root_spell_id'] = 9000
        shared_root_spell['actors'][0]['actions'].append(second)
        service._validate_export(shared_root_spell)

        conflicting_buff_identity = copy.deepcopy(payload)
        second = action('second_leaf', 2)
        second['scenarios'][0]['buffs'][0]['spell_id'] = 124
        conflicting_buff_identity['actors'][0]['actions'].append(second)
        with self.assertRaisesRegex(ValueError, 'canonical identity 冲突'):
            service._validate_export(conflicting_buff_identity)

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
            (lambda row: row['scenarios'][0]['buffs'].append({
                'token': 'probe', 'scope': 'self', 'spell_id': 124, 'stacks': 1,
            }),
             'buff token identity'),
            (lambda row: row['scenarios'][0]['buffs'][0].__setitem__('scope', 'external'),
             'buff scope'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__('hit', float('nan')),
             '数学期望字段'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__(
                'damage_equivalent_count', 0.0), 'damage equivalent count'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__(
                'crit_chance_uncapped', 0.4), '暴击率一致性'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__(
                'can_crit', False), '暴击率一致性'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__(
                'expected', 120.015), '数学期望一致性'),
            (lambda row: row['scenarios'][0]['values']['direct'].__setitem__(
                'expected', 121.0), '数学期望一致性'),
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
            'schema_version': 11,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior',
                'spec': 'fury',
                'talent_effectiveness': 'unknown',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                'token': 'test_action',
                'spell_id': 1,
                'supported': True,
                'player_skill': True,
                'selected_trait_effects': [],
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
            'schema_version': 11,
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
            'schema_version': 11,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
        }
        invalid_actors = [
            {'talent_effectiveness': 'unknown', 'actions': []},
            {'class': 'warrior', 'spec': 'fury', 'talent_effectiveness': 'unknown',
             'action_universe': 'wrong', 'actions': []},
            {
                'class': 'warrior', 'spec': 'fury',
                'talent_effectiveness': 'unknown',
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
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=19,
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

    def test_get_returns_frozen_schema_eighteen_product_without_reprojection(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='d' * 40, game_build='12.1.0.69299', schema_revision=19,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={
                'payload_format': 'skill_damage_product_v1',
                'display_action_count': 1,
                'actors': [{
                    'specialization': 'fury',
                    'actions': [{
                        'spell_id': 123,
                        'product': {'final_normalized_damage': 456.0},
                    }],
                }],
            },
        )
        request = self.factory.get('/api/simc-skill-damage/', {'profile_id': 99, 'talent': 'x'})
        request.user = self.user
        response = SimcSkillDamageSnapshotAPIView.as_view()(request)
        body = json.loads(response.content)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(body['data']['snapshot']['identity']['game_build'], '12.1.0.69299')
        self.assertEqual(body['data']['snapshot']['payload_format'], 'skill_damage_product_v1')
        self.assertEqual(
            body['data']['snapshot']['actors'][0]['actions'][0]['product']['final_normalized_damage'],
            456.0,
        )
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
        self.assertIn('schema 19', body['data']['snapshot_unavailable_reason'])

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
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=19,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload=localize_skill_damage_payload(project_skill_damage_product_payload({'actors': [{
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
            }]})),
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
    def test_skill_table_sums_formula_components_and_supports_name_sorting(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('id="simc-skill-damage-sort-name"', template)
        self.assertIn('aria-sort="none"', template)
        self.assertIn("localeCompare(rightName, 'zh-CN'", renderer)
        self.assertIn('const formulaGroups = new Map()', renderer)
        self.assertIn("const formulaBaseLabel = '基础伤害'", renderer)
        self.assertIn('component.runtime_factors', renderer)
        self.assertNotIn('formulaBaseLabels', renderer)
        self.assertNotIn('component.base_multiplier', renderer)
        self.assertNotIn('baseFactorFormula', renderer)
        self.assertNotIn('formatSimcSkillDamageNumber(group.baseDamage)', renderer)
        self.assertIn(
            'const renderSimcTalentProbeCondition = (runtimeCondition, scenarioTokens, talentName)',
            renderer,
        )
        self.assertIn('`点出${talentLabel}`', renderer)
        self.assertIn("scope === 'debuff' ? '目标' : '自身'", renderer)
        self.assertIn('`${owner}存在 ${stateToken} 效果时`', renderer)
        self.assertIn("'血量低于35%'", renderer)
        self.assertNotIn('目标生命值低于 35%', renderer)
        self.assertNotIn('「${name}」', renderer)
        self.assertNotIn('分量 ${index + 1}', renderer)
        self.assertNotIn('合并 ${action.component_count} 个施法分量', renderer)
        self.assertIn("const fallbackTalentLabel = talentName.endsWith('天赋') ? talentName : `${talentName}天赋`", renderer)
        self.assertIn("const variantLabel = conditionLabel || (talentName === '基础技能' ? talentName : `点出${fallbackTalentLabel}`)", renderer)
        self.assertIn('${escapeHtml(variantLabel)}</div>`', renderer)
        self.assertNotIn('const treeLabel =', renderer)
        self.assertNotIn('${escapeHtml(talentName)}</div>${treeLabel', renderer)

        identity_renderer = script.split('function renderSimcSkillIdentity(action) {', 1)[1].split(
            'function renderSimcSkillDamageSnapshot(snapshot) {', 1,
        )[0]
        self.assertIn('${escapeHtml(name)}', identity_renderer)
        self.assertIn('技能 ID：${escapeHtml(spellId)}', identity_renderer)
        self.assertNotIn('</div><div', identity_renderer)

    def test_global_effects_are_semantically_deduplicated_and_rendered_as_compact_cards(self):
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('globalEffectDisplayKey(effect)', renderer)
        self.assertIn('globalEffectDisplayPriority(effect)', renderer)
        self.assertIn("effect.source_type === 'talent'", renderer)
        self.assertIn('grid grid-cols-1 gap-2 md:grid-cols-2 xl:grid-cols-3', renderer)
        self.assertNotIn('flex items-center justify-between gap-4 border-t', renderer)

    def test_dashboard_has_independent_light_skill_damage_panel(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn('id="simc-skill-damage-panel"', template)
        self.assertIn('技能归一化伤害', template)
        self.assertIn('DBC 基础伤害直接读取技能 SpellEffect', template)
        self.assertIn('由 SimC reporting root 证明的多段、主副手和周期分量会合并', template)
        self.assertIn('统一基础暴击率：20%', template)
        self.assertIn('id="simc-skill-damage-global-modifiers"', template)
        self.assertIn('全局效果', script)
        self.assertIn('effect.runtime_condition', script)
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
            'normalized_base_damage', 'final_normalized_damage', 'formula_components',
            'base_damage', 'runtime_factors', 'final_damage',
        ):
            self.assertIn(field, renderer)
        for removed_field in (
            'attack_power_coefficient', 'spell_power_coefficient', 'runtime_multiplier',
        ):
            self.assertNotIn(removed_field, renderer)
        self.assertIn('formatSimcSkillDamageFactor', renderer)
        self.assertIn('toFixed(6)', renderer)
        self.assertNotIn('等效总倍率', renderer)
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
        self.assertIn('action.hero_subtree_ids', renderer)
        self.assertIn('variant.runtime_condition', renderer)
        self.assertIn('variant.talent_name_zh', renderer)
        self.assertIn("const sortMode = nameSortButton.dataset.active === 'true' ? 'name' : 'final';", renderer)
        self.assertIn("if (sortMode === 'final')", renderer)
        self.assertIn("finalSortButton.dataset.active = 'true';", script)
        self.assertIn('sortDirection', renderer)
        self.assertIn('finalSortValue', renderer)
        self.assertNotIn('全部专精', renderer)
        self.assertNotIn("component === 'direct' ? 'Direct' : 'Tick'", renderer)
        self.assertNotIn('font-bold uppercase text-stone-500', renderer)
        self.assertNotIn('职业 Buff', template)
