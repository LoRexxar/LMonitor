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
    SimcSkillDamageSnapshot, WowTalentNodeMetadata, WowTalentVersion,
)
from botend.services.simc_skill_damage import (
    SimcSkillDamageSnapshotService,
    attach_runtime_product_metrics,
    build_single_talent_actor_input,
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
    def test_single_talent_input_uses_one_baseline_actor_and_trait_entry_actors_only(self):
        profile_input = (
            'warrior="Fury Reference"\n'
            'spec=fury\n'
            'talents=FULL_PRESET_BUILD\n'
            'class_talents=old:1\n'
            'head=,id=1\n'
        )
        talents = [
            SimpleNamespace(pk=1, tree_type='spec', node_id=136454, max_points=1),
            SimpleNamespace(pk=2, tree_type='hero', node_id=117404, max_points=1),
        ]

        generated = build_single_talent_actor_input(profile_input, 'warrior', talents)

        self.assertNotIn('talents=FULL_PRESET_BUILD', generated)
        self.assertNotIn('class_talents=old:1', generated)
        self.assertEqual(generated.count('warrior="skill_damage_'), 3)
        self.assertIn('warrior="skill_damage_base"', generated)
        self.assertIn('warrior="skill_damage_talent_1"', generated)
        self.assertIn('spec_talents=136454:1', generated)
        self.assertIn('warrior="skill_damage_talent_2"', generated)
        self.assertIn('hero_talents=117404:1', generated)

    def test_single_talent_input_applies_generated_spec_root_scaffold_to_every_actor(self):
        root = SimpleNamespace(pk=74850, tree_type='spec', node_id=112261, max_points=1)
        scent = SimpleNamespace(pk=74957, tree_type='spec', node_id=136454, max_points=1)

        generated = build_single_talent_actor_input(
            'warrior="Fury Reference"\nspec=fury\ntalents=legacy\n',
            'warrior',
            [scent],
            scaffold_talents=[root],
        )

        self.assertEqual(generated.count('spec_talents=112261:1'), 2)
        self.assertEqual(generated.count('spec_talents=112261:1/136454:1'), 1)
        self.assertNotIn('talents=legacy', generated)

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
                'talent': {'id': 11, 'name': 'Scent of Blood', 'name_zh': '血之气息'},
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
                ('', '无单项增伤天赋', 200.0),
                ('血之气息', '需要 bloodcraze buff 激活', 240.0),
                ('恶毒蔑视', '目标生命值低于 35%', 300.0),
                ('能量爆发', '需要 burst_of_power buff 激活', 220.0),
            ],
        )

    def test_product_metrics_combine_dbc_base_with_selected_talent_runtime(self):
        selected = {'actions': [
            {
                'token': 'bloodthirst', 'spell_id': 23881, 'supported': True,
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
            {'token': 'valid', 'spell_id': 1, 'supported': True, 'baseline': {'direct': {
                'product': {'dbc_base_damage_min': 100.0, 'dbc_base_damage_max': 100.0,
                            'current_talent_damage': 120.0, 'crit_damage': 240.0,
                            'crit_multiplier': 2.0, 'actual_crit_chance': 0.2,
                            'normalized_expected': 144.0,
                            'dbc_unresolved_reason': ''}}}},
            {'token': 'unsupported', 'spell_id': 2, 'supported': False,
             'unsupported_reason': 'action_has_no_damage_component'},
            {'token': 'unresolved', 'spell_id': 3, 'supported': True,
             'baseline': {'unresolved_reason': 'snapshot_child_signal_11'}},
        ]}]}
        original = json.loads(json.dumps(payload))

        projected = project_skill_damage_product_payload(payload)

        self.assertEqual(payload, original)
        self.assertEqual(len(projected['actors'][0]['actions']), 1)
        self.assertEqual(projected['actors'][0]['actions'][0]['component'], 'direct')

    def test_talent_entries_come_from_active_metadata_without_talent_strings(self):
        active = WowTalentVersion.objects.create(key='active', is_active=True)
        inactive = WowTalentVersion.objects.create(key='inactive', is_active=False)
        expected = WowTalentNodeMetadata.objects.create(
            talent_version=active, class_name='Warrior', spec_name='Fury', tree_type='spec',
            node_id=136454, spell_id=1265355, name='Scent of Blood', max_points=1,
        )
        WowTalentNodeMetadata.objects.create(
            talent_version=inactive, class_name='Warrior', spec_name='Fury', tree_type='spec',
            node_id=136448, spell_id=383885, name='Stale Vicious Contempt', max_points=1,
        )
        profile = SimpleNamespace(spec='warrior_fury', class_name='warrior')

        rows = SimcSkillDamageSnapshotService(mock.Mock())._talent_entries(profile)

        self.assertEqual([row.pk for row in rows], [expected.pk])

    def test_existing_schema_three_snapshot_creates_new_schema_four_identity(self):
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
            schema_revision=3,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'specialization': 'fury',
                'hero_talent_tree': '屠戮者',
                'actions': [],
            }]},
        )

        service = SimcSkillDamageSnapshotService.create_for_current_backend()

        self.assertNotEqual(service.snapshot.pk, existing.pk)
        self.assertEqual(service.snapshot.schema_revision, 4)
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

    def test_generate_merges_single_talent_actor_outputs_and_preserves_dataset_identity(self):
        snapshot = SimcSkillDamageSnapshot.objects.create(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=4,
        )
        profile = SimpleNamespace(pk=1, spec='warrior_fury', class_name='warrior')
        talent = SimpleNamespace(
            pk=11, node_id=136454, tree_type='spec', name='Scent of Blood',
            name_zh='血之气息', description='', description_zh='',
        )
        outputs = [
            {'actors': [
                {'name': 'skill_damage_base', 'class': 'warrior', 'spec': 'fury',
                 'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': []},
                {'name': 'skill_damage_talent_11', 'class': 'warrior', 'spec': 'fury',
                 'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': []},
            ], 'unresolved': []},
            {'actors': [
                {'name': 'skill_damage_base', 'class': 'warrior', 'spec': 'fury',
                 'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': []},
                {'name': 'skill_damage_talent_11', 'class': 'warrior', 'spec': 'fury',
                 'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions', 'actions': []},
            ], 'unresolved': []},
        ]
        service = SimcSkillDamageSnapshotService(snapshot)
        with mock.patch.object(service, '_profiles', return_value=[profile]), \
             mock.patch.object(service, '_talent_entries', return_value=[talent]), \
             mock.patch.object(service, '_run_profile_export', side_effect=outputs) as run:
            result = service.generate()

        snapshot.refresh_from_db()
        self.assertEqual(run.call_args_list, [
            mock.call(profile, [talent], scaffold_talents=[], target_health=100),
            mock.call(profile, [talent], scaffold_talents=[], target_health=34),
        ])
        self.assertEqual(snapshot.status, snapshot.STATUS_SUCCEEDED)
        self.assertEqual(result['actors'][0]['specialization'], 'fury')
        self.assertEqual(result['actors'][0]['variant_model'], 'single_talent_runtime')
        self.assertEqual(result['identity'], {
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'schema_revision': 4,
        })
        self.assertNotIn('talent', result['identity'])

    def test_schema_three_requires_exported_runtime_crit_multiplier_and_expectation(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=3,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        payload = {
            'schema_version': 3,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior',
                'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                'supported': True,
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
                }, 'tick': None},
            }]}],
        }
        service._validate_export(payload)
        direct = payload['actors'][0]['actions'][0]['baseline']['direct']
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

    def test_schema_three_rejects_missing_or_malformed_dbc_spell_effect_scaling(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=3,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        payload = {
            'schema_version': 3,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
            'actors': [{
                'class': 'warrior',
                'spec': 'fury',
                'action_universe': 'dbc_spellbook_selected_traits_and_derived_actions',
                'actions': [{
                'supported': True,
                'baseline': {
                    'direct': {'hit': 220.0, 'crit': 440.0, 'crit_multiplier': 2.0,
                               'crit_chance': 0.2, 'expected': 264.0},
                    'tick': None,
                },
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

    def test_schema_three_requires_baseline_plus_every_single_talent_actor(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=4,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        base = {
            'schema_version': 3,
            'simc_revision': 'c' * 40,
            'game_build': '12.1.0.69299',
            'normalization_basis': dict(service.FIXED_PRESET),
        }
        for actors in ([], [{'actions': []}], [{'actions': []}, {'actions': []}, {'actions': []}]):
            with self.subTest(actor_count=len(actors)):
                with self.assertRaisesRegex(ValueError, '期望 2'):
                    service._validate_export({**base, 'actors': actors}, expected_actor_count=2)

    def test_schema_three_rejects_incomplete_actor_identity_and_non_boolean_supported(self):
        snapshot = SimcSkillDamageSnapshot(
            simc_revision='c' * 40, game_build='12.1.0.69299', schema_revision=3,
        )
        service = SimcSkillDamageSnapshotService(snapshot)
        base = {
            'schema_version': 3,
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
                'actions': [{'supported': None}],
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
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=4,
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

    def test_get_returns_latest_schema_four_success_without_profile_filters(self):
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='d' * 40, game_build='12.1.0.69299', schema_revision=4,
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
        self.assertIn('schema 4', body['data']['snapshot_unavailable_reason'])

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
        SimcSkillDamageSnapshot.objects.create(
            simc_revision='e' * 40, game_build='12.1.0.69300', schema_revision=4,
            status=SimcSkillDamageSnapshot.STATUS_SUCCEEDED,
            payload={'actors': [{
                'class': 'warrior', 'specialization': 'fury',
                'hero_talent_tree': '屠戮者', 'talent_name': 'Fury Slayer',
                'actions': [
                    {
                        'name': 'talent_action', 'token': 'talent_action', 'spell_id': 1001,
                        'supported': True,
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
                        'baseline': {'direct': {'product': {
                            'dbc_base_damage_min': None, 'dbc_base_damage_max': None,
                            'dbc_unresolved_reason': 'dbc_damage_effect_unresolved',
                            'current_talent_damage': 50.0,
                            'crit_damage': 100.0, 'crit_multiplier': 2.0,
                            'actual_crit_chance': 0.2, 'normalized_expected': 60.0,
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
            [('天赋中文技能', 1001), ('APL中文技能', 2002)],
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
        self.assertNotIn('独立无可选天赋 actor', template)
        self.assertNotIn('AP/SP 归一化为 1', template)
        self.assertIn('simc-skill-damage-table', template)
        self.assertIn('data-dashboard-section="simc-skill-damage"', template)
        self.assertIn('id="simc-skill-damage"', template)
        self.assertIn("'skill-damage': 'simc-skill-damage'", script)
        self.assertIn('/api/simc-skill-damage/', script)
        self.assertIn('renderSimcSkillDamageSnapshot', script)
        self.assertIn('initSimcSkillDamagePanel();', script)
        self.assertNotIn('bg-gray-900 simc-skill-damage', template)

    def test_dashboard_shows_runtime_product_semantics_without_frontend_damage_math(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertIn('AP/SP 100.00', template)
        self.assertIn('全局暴击 20.00%', template)
        self.assertIn('精通 50.00%', template)
        for label in ('DBC 基础伤害', '该条件实际伤害', '技能实际暴击率', '归一化伤害期望'):
            self.assertIn(label, template)
        for field in (
            'dbc_base_damage_min', 'dbc_base_damage_max', 'current_talent_damage',
            'crit_damage', 'crit_multiplier', 'actual_crit_chance',
            'normalized_expected',
        ):
            self.assertIn(field, renderer)
        for removed_field in ('pre_talent_base', 'talent_gain_pct', 'selected_talent_expected'):
            self.assertNotIn(removed_field, renderer)
        self.assertNotIn('amount.expected', renderer)
        self.assertNotIn('multiplier * 100', renderer)
        self.assertNotRegex(renderer, r'\.toFixed\((?!2\))')
        self.assertIn('html[data-dashboard-theme="dark"] #simc-skill-damage-panel', template)

    def test_dashboard_requires_only_spec_and_renders_single_talent_runtime_conditions(self):
        template = Path('templates/dashboard/index.html').read_text(encoding='utf-8')
        script = Path('static/dashboard/js/main.js').read_text(encoding='utf-8')
        renderer = script.split('function renderSimcSkillDamageSnapshot(snapshot) {', 1)[1].split(
            'function initSimcSkillDamagePanel()', 1,
        )[0]

        self.assertNotIn('id="simc-skill-damage-hero-tree"', template)
        self.assertIn('请选择专精', template)
        self.assertIn('单项天赋条件', template)
        self.assertIn('id="simc-skill-damage-sort-expected"', template)
        self.assertIn('归一化伤害期望', template)
        self.assertIn('技能实际暴击率', template)
        self.assertNotIn('selectedHeroTree', renderer)
        self.assertIn('variant.runtime_condition', renderer)
        self.assertIn('variant.talent_name_zh', renderer)
        self.assertIn('sortDirection', renderer)
        self.assertIn('expectedSortValue', renderer)
        self.assertNotIn('全部专精', renderer)
        self.assertNotIn("component === 'direct' ? 'Direct' : 'Tick'", renderer)
        self.assertNotIn('font-bold uppercase text-stone-500', renderer)
        self.assertNotIn('职业 Buff', template)
