"""TDD contracts for benchmark configuration validation and matrix planning."""
import math
from unittest.mock import patch

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkCandidate, SimcBenchmarkPanel,
    SimcBenchmarkProfile, SimcBenchmarkScenario, SimcBenchmarkSpec,
    SimcContentTemplate, SimcProfile, WowItemSnapshot,
)
from botend.services.simc_benchmark_config import (
    MAX_PROFILES_PER_SPEC, MAX_SCENARIOS, MAX_SPECS,
    build_execution_plan, normalize_panel_payload,
    replace_panel_config, serialize_panel_config, _locked_panel_snapshot_queryset,
)


class SimcBenchmarkConfigServiceTests(TestCase):
    user_id = 101

    def setUp(self):
        self.backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production', defaults={'name': 'Production', 'is_active': True},
        )
        self.apl = SimcApl.objects.create(
            name='Fury APL', spec='warrior_fury', content='actions=/auto_attack',
            owner_user_id=self.user_id, is_active=True, is_selectable=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='Fury template', spec='warrior_fury', content='iterations=1000',
            owner_user_id=self.user_id, is_active=True, is_selectable=True,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id, name='My fury', spec='warrior_fury', is_active=True,
        )
        self.payload = {
            'name': 'Weekly benchmark', 'slug': 'weekly-benchmark',
            'description': 'safe description',
            'specs': [{
                'class_name': 'warrior', 'spec_key': 'warrior_fury', 'label': 'Fury',
                'apl_id': self.apl.pk, 'template_id': self.template.pk,
                'backend_id': self.backend.pk,
                'profiles': [{'profile_id': self.profile.pk, 'label': 'My fury'}],
            }],
            'scenarios': [{
                'key': 'patchwerk', 'name': 'Patchwerk',
                'simulation_params': {'iterations': 1000, 'desired_targets': 1},
            }],
            'candidates': [{
                'key': 'trinket-1', 'label': 'Trinket 1', 'candidate_type': 'gear_swap',
                'params': {'slot': 'trinket1', 'raw_value': 'trinket1=id=123,ilevel=700'},
                'spec_keys': ['warrior_fury'],
            }],
        }

    def test_published_limits_only_cover_structural_choices(self):
        self.assertEqual((MAX_SPECS, MAX_PROFILES_PER_SPEC, MAX_SCENARIOS), (40, 5, 8))

    def test_normalize_strict_shapes_unknown_options_and_types(self):
        for field in ('specs', 'scenarios', 'candidates'):
            payload = dict(self.payload); payload[field] = {}
            with self.subTest(field=field), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload)
        payload['specs'] = [dict(self.payload['specs'][0], profiles={})]
        with self.assertRaises(ValidationError):
            normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload)
        payload['scenarios'] = [dict(self.payload['scenarios'][0], simulation_params={'threads': 8})]
        with self.assertRaisesMessage(ValidationError, 'threads'):
            normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload)
        payload['candidates'] = [dict(self.payload['candidates'][0], params=123)]
        with self.assertRaises(ValidationError):
            normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload)
        payload['candidates'] = [dict(self.payload['candidates'][0], spec_keys='warrior_fury')]
        with self.assertRaises(ValidationError):
            normalize_panel_payload(payload, self.user_id)

    def test_normalizes_single_line_and_structured_gear_to_executor_shape(self):
        payload = dict(self.payload)
        payload['candidates'] = [
            {'key': 'a', 'label': 'A', 'candidate_type': 'gear_swap',
             'params': 'trinket1=id=123,ilevel=700', 'spec_keys': []},
            {'key': 'b', 'label': 'B', 'candidate_type': 'gear_swap',
             'params': {'slot': 'trinket2', 'raw_value': 'id=456,ilevel=700'}, 'spec_keys': []},
        ]
        result = normalize_panel_payload(payload, self.user_id)
        self.assertEqual(result['candidates'][0]['params'], {
            'candidate_type': 'gear_swap', 'is_base': False,
            'gear_swap': {
                'slot': 'trinket1', 'raw_value': ',id=123,ilevel=700',
                'item_id': 123, 'source': 'manual',
            },
        })
        self.assertEqual(result['candidates'][1]['params']['gear_swap'], {
            'slot': 'trinket2', 'raw_value': ',id=456,ilevel=700',
            'item_id': 456, 'source': 'manual',
        })

    def test_derives_spec_and_candidate_display_metadata_from_identity(self):
        spec = dict(self.payload['specs'][0])
        spec.pop('label')
        candidate = {
            'candidate_type': 'gear_swap',
            'params': 'trinket1=id=123,ilevel=700',
            'spec_keys': ['warrior_fury'],
        }

        result = normalize_panel_payload(
            dict(self.payload, specs=[spec], candidates=[candidate]), self.user_id,
        )

        self.assertEqual(result['specs'][0]['label'], '狂怒')
        self.assertEqual(result['candidates'][0]['key'], 'item-123-ilvl-700')
        self.assertEqual(result['candidates'][0]['label'], '物品 123 · 700')

    def test_freezes_chinese_item_metadata_into_new_candidate(self):
        WowItemSnapshot.objects.create(
            item_id=123, name='Test Trinket', name_zh='测试饰品', icon='inv_trinket_raid_01',
        )

        result = normalize_panel_payload(self.payload, self.user_id)

        candidate = result['candidates'][0]
        self.assertEqual(candidate['label'], '测试饰品 · 700')
        self.assertEqual(candidate['icon_url'], '/static/wow_icons/small/inv_trinket_raid_01.jpg')
        self.assertEqual(candidate['source_label'], '物品 #123')

    def test_preserves_explicit_variant_suffix_when_localizing_item_name(self):
        WowItemSnapshot.objects.create(
            item_id=123, name='Test Trinket', name_zh='测试饰品', icon='inv_trinket_raid_01',
        )
        payload = dict(self.payload)
        payload['candidates'] = [dict(payload['candidates'][0], label='测试饰品 · 暴击')]

        result = normalize_panel_payload(payload, self.user_id)

        self.assertEqual(result['candidates'][0]['label'], '测试饰品 · 暴击 · 700')

    def test_generated_keys_distinguish_execution_relevant_gear_params(self):
        candidates = [
            {'candidate_type': 'gear_swap',
             'params': 'trinket1=id=123,ilevel=700,bonus_id=1', 'spec_keys': []},
            {'candidate_type': 'gear_swap',
             'params': 'trinket2=id=123,ilevel=700,bonus_id=2', 'spec_keys': []},
        ]

        result = normalize_panel_payload(
            dict(self.payload, candidates=candidates), self.user_id,
        )

        keys = [candidate['key'] for candidate in result['candidates']]
        self.assertEqual(len(set(keys)), 2)
        self.assertTrue(all(key.startswith('item-123-ilvl-700-') for key in keys))

    def test_rejects_duplicate_item_identity_tokens(self):
        ambiguous_lines = [
            'trinket1=id=123,ilevel=700,id=456',
            'trinket1=id=123,ilevel=700,item_level=701',
        ]

        for params in ambiguous_lines:
            candidate = {'candidate_type': 'gear_swap', 'params': params, 'spec_keys': []}
            with self.subTest(params=params), self.assertRaisesMessage(
                    ValidationError, '只能包含一个'):
                normalize_panel_payload(
                    dict(self.payload, candidates=[candidate]), self.user_id,
                )

    def test_requires_item_level_for_benchmark_gear_candidates(self):
        candidate = {
            'candidate_type': 'gear_swap',
            'params': 'trinket1=id=123',
            'spec_keys': [],
        }

        with self.assertRaisesMessage(ValidationError, '装等'):
            normalize_panel_payload(
                dict(self.payload, candidates=[candidate]), self.user_id,
            )

    def test_simc_options_are_exact_allowlisted_assignments(self):
        allowed = 'midnight.crucible_of_erratic_energies_predation=1'
        candidate = dict(self.payload['candidates'][0], params={
            'slot': 'trinket1', 'raw_value': 'id=264507,ilevel=700', 'simc_options': [allowed],
        })
        normalized = normalize_panel_payload(dict(self.payload, candidates=[candidate]), self.user_id)
        self.assertEqual(normalized['candidates'][0]['params']['simc_options'], [allowed])
        for options in (["iterations=1"], [allowed + '\nactions=/kill'], [allowed, allowed], []):
            candidate = dict(candidate, params={
                'slot': 'trinket1', 'raw_value': 'id=264507,ilevel=700', 'simc_options': options,
            })
            with self.subTest(options=options), self.assertRaises(ValidationError):
                normalize_panel_payload(dict(self.payload, candidates=[candidate]), self.user_id)

    def test_simulation_params_reject_non_finite_json_numbers(self):
        for value in (math.nan, math.inf, -math.inf):
            payload = dict(self.payload)
            payload['scenarios'] = [dict(
                self.payload['scenarios'][0], simulation_params={'target_error': value},
            )]
            with self.subTest(value=value), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)

    def test_fight_style_must_be_a_value_accepted_by_current_simc_source(self):
        accepted = [
            'Patchwerk', 'CastingPatchwerk', 'HecticAddCleave', 'DungeonSlice',
            'DungeonRoute', 'CleaveAdd', 'LightMovement', 'HeavyMovement',
            'beastlord', 'HelterSkelter', 'Ultraxion',
        ]
        for fight_style in accepted:
            payload = dict(self.payload)
            payload['scenarios'] = [dict(
                self.payload['scenarios'][0], simulation_params={'fight_style': fight_style},
            )]
            with self.subTest(fight_style=fight_style):
                self.assertEqual(
                    normalize_panel_payload(payload, self.user_id)['scenarios'][0]
                    ['simulation_params']['fight_style'],
                    fight_style,
                )

        payload = dict(self.payload)
        payload['scenarios'] = [dict(
            self.payload['scenarios'][0], simulation_params={'fight_style': '自己输入'},
        )]
        with self.assertRaisesMessage(ValidationError, 'fight_style'):
            normalize_panel_payload(payload, self.user_id)

    def test_rejects_unsafe_gear_and_unsupported_candidate_types(self):
        bad_values = [
            'trinket1=id=1\nactions=/kill', 'actions=id=1', 'input=id=1',
            '/tmp/profile.simc', 'iterations=id=1',
        ]
        for raw in bad_values:
            payload = dict(self.payload)
            payload['candidates'] = [{
                'key': 'bad', 'label': 'Bad', 'candidate_type': 'gear_swap',
                'params': raw, 'spec_keys': [],
            }]
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload)
        payload['candidates'] = [dict(self.payload['candidates'][0], candidate_type='talent')]
        with self.assertRaises(ValidationError):
            normalize_panel_payload(payload, self.user_id)

    def test_rejects_user_baseline_candidates_and_unsafe_canonical_gear_params(self):
        for key in ('baseline', ' baseline ', 'BASELINE', 'Baseline'):
            candidate = dict(self.payload['candidates'][0], key=key)
            payload = dict(self.payload, candidates=[candidate])
            with self.subTest(key=key), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)

        for type_field in ('candidate_type', 'type', 'kind'):
            candidate = dict(self.payload['candidates'][0])
            if type_field != 'candidate_type':
                candidate.pop('candidate_type')
            candidate[type_field] = 'baseline'
            payload = dict(self.payload, candidates=[candidate])
            with self.subTest(type_field=type_field), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)

        canonical = {
            'candidate_type': 'gear_swap', 'is_base': False,
            'gear_swap': {'slot': 'trinket1', 'raw_value': 'id=123'},
        }
        for overrides in (
            {'candidate_type': 'apl_override'},
            {'is_base': 'yes'},
            {'is_base': True},
        ):
            params = dict(canonical, **overrides)
            candidate = dict(self.payload['candidates'][0], params=params)
            payload = dict(self.payload, candidates=[candidate])
            with self.subTest(overrides=overrides), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)

    def test_resource_permissions_activity_and_spec_consistency(self):
        other_profile = SimcProfile.objects.create(user_id=999, name='Other', spec='warrior_fury')
        payload = dict(self.payload)
        payload['specs'] = [dict(self.payload['specs'][0], profiles=[{'profile_id': other_profile.pk}])]
        with self.assertRaises(ValidationError): normalize_panel_payload(payload, self.user_id)
        self.backend.is_active = False; self.backend.save(update_fields=['is_active'])
        with self.assertRaises(ValidationError): normalize_panel_payload(self.payload, self.user_id)
        self.backend.is_active = True; self.backend.save(update_fields=['is_active'])
        self.apl.spec = 'mage_fire'; self.apl.save(update_fields=['spec'])
        with self.assertRaises(ValidationError): normalize_panel_payload(self.payload, self.user_id)

    def test_rejects_unknown_spec_even_when_resources_claim_the_same_identity(self):
        self.apl.spec = 'warrior_not_a_spec'; self.apl.save(update_fields=['spec'])
        self.template.spec = 'warrior_not_a_spec'; self.template.save(update_fields=['spec'])
        self.profile.spec = 'warrior_not_a_spec'; self.profile.save(update_fields=['spec'])
        payload = dict(self.payload)
        payload['specs'] = [dict(
            self.payload['specs'][0], spec_key='warrior_not_a_spec', label='Fake',
        )]
        with self.assertRaises(ValidationError):
            normalize_panel_payload(payload, self.user_id)

    def test_profile_class_name_and_spec_must_both_match(self):
        self.profile.class_name = 'mage'
        self.profile.save(update_fields=['class_name'])
        with self.assertRaisesMessage(ValidationError, 'Profile'):
            normalize_panel_payload(self.payload, self.user_id)

    def test_system_resources_and_default_upstream_profile(self):
        self.apl.owner_user_id = None; self.apl.is_system = True; self.apl.save()
        self.template.owner_user_id = None; self.template.save()
        default = SimcProfile.objects.create(
            user_id=None, source=SimcProfile.SOURCE_SIMC_UPSTREAM,
            name='Upstream Fury', spec='warrior_fury', is_active=True,
        )
        payload = dict(self.payload)
        payload['specs'] = [{k: v for k, v in self.payload['specs'][0].items() if k != 'profiles'}]
        result = normalize_panel_payload(payload, self.user_id)
        self.assertEqual(result['specs'][0]['profiles'][0]['profile_id'], default.pk)

    def test_missing_default_profile_is_explicit(self):
        payload = dict(self.payload)
        payload['specs'] = [dict(self.payload['specs'][0], profiles=[])]
        with self.assertRaisesMessage(ValidationError, '默认'):
            normalize_panel_payload(payload, self.user_id)

    def test_limits_and_duplicate_business_keys(self):
        payload = dict(self.payload); payload['scenarios'] = self.payload['scenarios'] * 9
        with self.assertRaises(ValidationError): normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload); payload['candidates'] = self.payload['candidates'] * 51
        with self.assertRaises(ValidationError): normalize_panel_payload(payload, self.user_id)
        payload = dict(self.payload); payload['scenarios'] = self.payload['scenarios'] * 2
        with self.assertRaisesMessage(ValidationError, '重复'):
            normalize_panel_payload(payload, self.user_id)

    def test_empty_candidates_is_baseline_only_and_candidate_runs_have_no_fixed_count_cap(self):
        payload = dict(self.payload, candidates=[])
        panel, plan = replace_panel_config(payload, self.user_id)
        self.assertEqual(panel.candidates.count(), 0)
        self.assertEqual((plan['case_count'], plan['run_count']), (1, 1))
        self.assertEqual(
            [candidate['candidate_key'] for candidate in plan['cases'][0]['candidates']],
            ['baseline'],
        )

        def candidate(index):
            return {
                'key': f'gear-{index}', 'label': f'Gear {index}',
                'candidate_type': 'gear_swap',
                'params': {'slot': 'trinket1', 'raw_value': f'id={1000 + index},ilevel=700'},
                'spec_keys': [],
            }

        payload = dict(self.payload, slug='expanded-candidates', candidates=[
            candidate(index) for index in range(130)
        ])
        _panel, plan = replace_panel_config(payload, self.user_id)
        self.assertEqual(plan['run_count'], 131)

    def test_internal_or_blank_candidate_icon_is_valid_display_metadata(self):
        for icon_url in ('', '/static/wow_icons/small/inv_trinket.jpg'):
            payload = dict(self.payload, slug=f'icon-{int(bool(icon_url))}', candidates=[dict(
                self.payload['candidates'][0], icon_url=icon_url,
            )])
            with self.subTest(icon_url=icon_url):
                panel, _plan = replace_panel_config(payload, self.user_id)
                self.assertEqual(panel.candidates.get().icon_url, icon_url)

    def test_same_item_at_different_item_levels_has_distinct_stable_keys(self):
        candidates = [{
            'label': f'Item 123 {item_level}', 'candidate_type': 'gear_swap',
            'params': {'slot': 'trinket1', 'raw_value': f'id=123,ilevel={item_level}'},
            'spec_keys': [],
        } for item_level in (700, 710)]
        normalized = normalize_panel_payload(
            dict(self.payload, candidates=candidates), self.user_id,
        )
        self.assertEqual(
            [row['key'] for row in normalized['candidates']],
            ['item-123-ilvl-700', 'item-123-ilvl-710'],
        )

    def test_replace_is_atomic_owner_checked_and_calls_full_clean(self):
        panel, estimate = replace_panel_config(self.payload, self.user_id)
        self.assertEqual((panel.specs.count(), panel.scenarios.count(), panel.candidates.count()), (1, 1, 1))
        self.assertEqual((estimate['case_count'], estimate['run_count']), (1, 2))
        old_name = panel.name
        broken = dict(self.payload, name='Must roll back', scenarios=[dict(self.payload['scenarios'][0], key='new')])
        with patch.object(SimcBenchmarkScenario, 'full_clean', side_effect=ValidationError('boom')):
            with self.assertRaises(ValidationError): replace_panel_config(broken, self.user_id, panel=panel)
        panel.refresh_from_db()
        self.assertEqual(panel.name, old_name)
        with self.assertRaises(ValidationError): replace_panel_config(self.payload, 999, panel=panel)

    def test_replace_whole_nested_config_and_safe_serializer(self):
        panel, _ = replace_panel_config(self.payload, self.user_id)
        changed = dict(self.payload)
        changed['scenarios'] = [{'key': 'dungeon', 'name': 'Dungeon', 'simulation_params': {}}]
        panel, _ = replace_panel_config(changed, self.user_id, panel=panel)
        self.assertEqual(list(panel.scenarios.values_list('key', flat=True)), ['dungeon'])
        serialized = serialize_panel_config(panel)
        text = repr(serialized)
        self.assertNotIn(self.apl.content, text)
        self.assertNotIn(self.template.content, text)
        self.assertNotIn('simc_path', text)
        self.assertNotIn('player_equipment', text)

    def test_plan_is_stable_three_dimensional_and_filters_candidates(self):
        profile2 = SimcProfile.objects.create(user_id=self.user_id, name='Second', spec='warrior_fury')
        payload = dict(self.payload)
        payload['specs'] = [dict(self.payload['specs'][0], profiles=[
            {'profile_id': profile2.pk, 'label': 'Second', 'display_order': 2},
            {'profile_id': self.profile.pk, 'label': 'First', 'display_order': 1},
        ])]
        payload['scenarios'] = [
            dict(self.payload['scenarios'][0], key='second', display_order=2),
            dict(self.payload['scenarios'][0], key='first', display_order=1),
        ]
        payload['candidates'] = [
            dict(self.payload['candidates'][0], key='all', spec_keys=[]),
            dict(self.payload['candidates'][0], key='other', spec_keys=['mage_fire']),
        ]
        panel, _ = replace_panel_config(payload, self.user_id)
        plan = build_execution_plan(panel)
        self.assertEqual(plan['case_count'], 4)
        self.assertEqual(plan['run_count'], 8)  # auto baseline + applicable candidate
        self.assertEqual([(c['scenario_key'], c['profile_label']) for c in plan['cases']], [
            ('first', 'First'), ('first', 'Second'), ('second', 'First'), ('second', 'Second'),
        ])
        self.assertEqual([c['candidate_key'] for c in plan['cases'][0]['candidates']], ['baseline', 'all'])

    def test_execution_plan_has_no_fixed_case_count_cap(self):
        panel, estimate = replace_panel_config(self.payload, self.user_id)
        self.assertEqual(estimate['case_count'], 1)
        # Saving an oversized Cartesian product is allowed.
        spec = panel.specs.get()
        for index in range(1, 5):
            profile = SimcProfile.objects.create(user_id=self.user_id, name=f'P{index}', spec='warrior_fury')
            SimcBenchmarkProfile.objects.create(panel_spec=spec, profile=profile, label=f'P{index}')
        for index in range(1, 8):
            SimcBenchmarkScenario.objects.create(panel=panel, key=f's{index}', name=f'S{index}')
        # 5 profiles * 8 scenarios is still valid; make four specs directly to exceed 120.
        for index in range(3):
            extra = SimcBenchmarkSpec.objects.create(
                panel=panel, class_name='warrior', spec_key=f'warrior_fury_{index}', label='Fury',
                apl=self.apl, template=self.template, backend=self.backend,
            )
            for profile in SimcProfile.objects.filter(user_id=self.user_id)[:5]:
                SimcBenchmarkProfile.objects.create(panel_spec=extra, profile=profile, label=profile.name)
        estimate = build_execution_plan(panel, validate_for_execution=False)
        execution_plan = build_execution_plan(panel, validate_for_execution=True)
        self.assertGreater(estimate['case_count'], 120)
        self.assertEqual(execution_plan['case_count'], estimate['case_count'])
        self.assertEqual(execution_plan['run_count'], estimate['run_count'])

    def test_plan_rejects_missing_enabled_axes_but_allows_no_enabled_candidates(self):
        panel, _ = replace_panel_config(self.payload, self.user_id)
        for manager, message in ((panel.specs, '专精'), (panel.scenarios, '场景')):
            manager.update(is_enabled=False)
            with self.subTest(message=message), self.assertRaisesMessage(ValidationError, message):
                build_execution_plan(panel)
            manager.update(is_enabled=True)
        panel.candidates.update(is_enabled=False)
        plan = build_execution_plan(panel)
        self.assertEqual(plan['run_count'], plan['case_count'])
        self.assertEqual(len(plan['cases'][0]['candidates']), 1)
        panel.candidates.update(is_enabled=True)
        panel.specs.get().profiles.update(is_enabled=False)
        with self.assertRaisesMessage(ValidationError, 'Profile'):
            build_execution_plan(panel)

    def test_simulation_params_use_composer_schema_before_save(self):
        invalid = (
            {'iterations': '1000'}, {'iterations': True}, {'iterations': 0},
            {'iterations': 100000001}, {'desired_targets': -1},
            {'desired_targets': True}, {'max_time': 0}, {'max_time': 86401},
            {'fight_style': 1}, {'fight_style': 'Patchwerk\nthreads'},
            {'target_error': 1.01}, {'vary_combat_length': -0.01},
        )
        for simulation_params in invalid:
            payload = dict(self.payload, scenarios=[dict(
                self.payload['scenarios'][0], simulation_params=simulation_params,
            )])
            with self.subTest(simulation_params=simulation_params), self.assertRaises(ValidationError):
                normalize_panel_payload(payload, self.user_id)
        valid = dict(self.payload, scenarios=[dict(
            self.payload['scenarios'][0],
            simulation_params={'iterations': 1000, 'desired_targets': 2, 'max_time': 12.5},
        )])
        normalized = normalize_panel_payload(valid, self.user_id)
        self.assertEqual(normalized['scenarios'][0]['simulation_params']['max_time'], 12.5)
        self.assertFalse(SimcBenchmarkPanel.objects.exists())

    def test_slug_unique_integrity_error_is_field_validation_error(self):
        replace_panel_config(self.payload, self.user_id)
        duplicate = dict(self.payload, name='Duplicate')
        with patch.object(SimcBenchmarkPanel, 'full_clean'):
            with self.assertRaises(ValidationError) as raised:
                replace_panel_config(duplicate, self.user_id)
        self.assertIn('slug', raised.exception.message_dict)

    def test_unrelated_integrity_error_is_not_swallowed(self):
        with patch.object(SimcBenchmarkPanel, 'save', side_effect=IntegrityError('other constraint')):
            with self.assertRaises(IntegrityError):
                replace_panel_config(self.payload, self.user_id)

    def test_plan_requeries_stale_panel_and_prefetches_in_constant_queries(self):
        panel, _ = replace_panel_config(self.payload, self.user_id)
        stale = SimcBenchmarkPanel.objects.get(pk=panel.pk)
        panel.scenarios.update(name='DB current')
        with CaptureQueriesContext(connection) as captured:
            plan = build_execution_plan(stale)
        self.assertEqual(plan['cases'][0]['scenario_label'], 'DB current')
        one_spec_queries = len(captured)
        # Locked planning is fixed at 11 statements under TestCase: savepoint pair,
        # Panel + four config-axis reads, then four batched resource in_bulk reads.
        self.assertLessEqual(one_spec_queries, 11)

        mage_apl = SimcApl.objects.create(
            name='Fire APL', spec='mage_fire', content='actions=/fireball',
            owner_user_id=self.user_id, is_active=True, is_selectable=True,
        )
        mage_template = SimcContentTemplate.objects.create(
            name='Fire template', spec='mage_fire', content='iterations=1000',
            owner_user_id=self.user_id, is_active=True, is_selectable=True,
        )
        mage_spec = SimcBenchmarkSpec.objects.create(
            panel=panel, class_name='mage', spec_key='mage_fire', label='Fire',
            apl=mage_apl, template=mage_template, backend=self.backend,
        )
        for index in range(3):
            profile = SimcProfile.objects.create(
                user_id=self.user_id, name=f'Fire {index}', class_name='mage',
                spec='mage_fire', is_active=True,
            )
            SimcBenchmarkProfile.objects.create(
                panel_spec=mage_spec, profile=profile, label=profile.name,
            )
        with CaptureQueriesContext(connection) as captured:
            expanded = build_execution_plan(stale)
        self.assertEqual(len(expanded['specs']), 2)
        self.assertEqual(len(captured), one_spec_queries)
        self.assertLessEqual(len(captured), 11)

        spec = panel.specs.get(spec_key='warrior_fury')
        for index in range(3):
            profile = SimcProfile.objects.create(
                user_id=self.user_id, name=f'Extra {index}', spec='warrior_fury',
            )
            SimcBenchmarkProfile.objects.create(panel_spec=spec, profile=profile, label=profile.name)
        with CaptureQueriesContext(connection) as captured:
            serialized = serialize_panel_config(stale)
        self.assertLessEqual(len(captured), 5)
        self.assertEqual(len(serialized['specs'][0]['profiles']), 4)

    def test_forty_spec_plan_keeps_batched_resource_queries_constant(self):
        panel, _ = replace_panel_config(self.payload, self.user_id)
        for index in range(1, MAX_SPECS):
            spec = SimcBenchmarkSpec.objects.create(
                panel=panel, class_name='warrior', spec_key=f'warrior_fury_{index}',
                label=f'Fury {index}', apl=self.apl, template=self.template,
                backend=self.backend,
            )
            SimcBenchmarkProfile.objects.create(
                panel_spec=spec, profile=self.profile, label=self.profile.name,
            )

        with CaptureQueriesContext(connection) as captured:
            plan = build_execution_plan(panel)

        self.assertEqual(len(plan['specs']), MAX_SPECS)
        self.assertEqual(plan['case_count'], MAX_SPECS)
        # Resource FKs are loaded by four in_bulk statements, not per Spec/Profile.
        self.assertLessEqual(len(captured), 11)

    def test_execution_snapshot_queryset_locks_every_config_axis(self):
        """SQLite ignores row locks at runtime, so assert the ORM lock contract itself."""
        root = _locked_panel_snapshot_queryset()
        self.assertTrue(root.query.select_for_update)
        children = {item.prefetch_to: item.queryset
                    for item in root._prefetch_related_lookups}
        self.assertEqual(set(children), {'_snapshot_specs', '_snapshot_scenarios',
                                         '_snapshot_candidates'})
        for queryset in children.values():
            self.assertTrue(queryset.query.select_for_update)
        profile_prefetch = children['_snapshot_specs']._prefetch_related_lookups[0]
        self.assertEqual(profile_prefetch.prefetch_to, '_snapshot_profiles')
        self.assertTrue(profile_prefetch.queryset.query.select_for_update)

    def test_returned_json_is_deepcopy_isolated(self):
        panel, plan = replace_panel_config(self.payload, self.user_id)
        plan['cases'][0]['simulation_params']['iterations'] = -1
        plan['cases'][0]['candidates'][1]['candidate_params']['gear_swap']['item_id'] = -1
        fresh_plan = build_execution_plan(panel)
        self.assertEqual(fresh_plan['cases'][0]['simulation_params']['iterations'], 1000)
        self.assertEqual(
            fresh_plan['cases'][0]['candidates'][1]['candidate_params']['gear_swap']['item_id'], 123,
        )
        serialized = serialize_panel_config(panel)
        serialized['scenarios'][0]['simulation_params']['iterations'] = -1
        self.assertEqual(
            serialize_panel_config(panel)['scenarios'][0]['simulation_params']['iterations'], 1000,
        )
