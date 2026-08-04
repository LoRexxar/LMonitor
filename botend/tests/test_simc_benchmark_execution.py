"""TDD contracts for benchmark execution orchestration and safe publication."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from unittest.mock import patch

from django.core.exceptions import PermissionDenied, ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

import botend.services.simc_benchmark_execution as benchmark_execution_service
from botend.models import (
    SimcApl, SimcBackendBinary, SimcBenchmarkCandidate, SimcBenchmarkCase,
    SimcBenchmarkExecution, SimcBenchmarkPanel, SimcBenchmarkProfile,
    SimcBenchmarkResult,
    SimcBenchmarkScenario, SimcBenchmarkSpec, SimcContentTemplate, SimcProfile,
    SimcTask, SimcTaskArtifact, SimulationRun, WowItemSnapshot,
)
from botend.services.simc_benchmark_execution import (
    BenchmarkExecutionConflict, backfill_completed_case_results, create_execution, reconcile_execution,
    rerun_failed_cases, serialize_incremental_panel_results,
    serialize_public_execution, summarize_execution, _spec_icon_url,
)


class SimcBenchmarkExecutionTests(TestCase):
    user_id = 701

    def setUp(self):
        self.backend = SimcBackendBinary.objects.create(
            identifier='benchmark-execution', name='Benchmark', is_active=True,
            current_version='a' * 40,
        )
        self.apl = SimcApl.objects.create(
            name='Fury APL', spec='warrior_fury', content='actions=/auto_attack',
            owner_user_id=self.user_id, is_active=True, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(b'actions=/auto_attack').hexdigest(),
            validation_revision='a' * 40, validation_game_build='12.0.1',
        )
        self.template = SimcContentTemplate.objects.create(
            name='Template', spec='warrior_fury', content='iterations=1000',
            owner_user_id=self.user_id,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.user_id, name='Profile', class_name='warrior',
            spec='warrior_fury', is_active=True,
        )
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Weekly', slug='weekly-execution', created_by_id=self.user_id,
            is_active=True, schedule_enabled=True,
        )
        spec = SimcBenchmarkSpec.objects.create(
            panel=self.panel, class_name='warrior', spec_key='warrior_fury',
            label='Fury', apl=self.apl, template=self.template, backend=self.backend,
        )
        SimcBenchmarkProfile.objects.create(
            panel_spec=spec, profile=self.profile, label='Raid profile',
        )
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='patchwerk', name='Patchwerk',
            simulation_params={'iterations': 1000},
        )
        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='trinket', label='Trinket',
            candidate_type='gear_swap', params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': {'slot': 'trinket1', 'raw_value': ',id=123',
                              'item_id': 123, 'source': 'manual'},
                'simc_options': [
                    'midnight.crucible_of_erratic_energies_predation=1',
                ],
            },
        )
        self.validation = {
            'valid': True,
            'content_hash': hashlib.sha256(self.apl.content.encode()).hexdigest(),
            'revision': 'a' * 40, 'game_build': '12.0.1', 'diagnostics': [],
        }

    def _create(self, **kwargs):
        with patch('botend.services.simc_task_service.current_validation_identity',
                   return_value=('a' * 40, '12.0.1')), patch(
            'botend.services.simc_task_service.validate_apl_for_profile',
            return_value=self.validation,
        ):
            return create_execution(
                self.panel, requested_by=self.user_id, **kwargs,
            )

    def test_scenario_core_parameters_are_frozen_into_execution_and_task(self):
        scenario = self.panel.scenarios.get(key='patchwerk')
        scenario.simulation_params = {
            'desired_targets': 5, 'max_time': 40, 'iterations': 10000,
            'fight_style': 'CastingPatchwerk',
        }
        scenario.save(update_fields=['simulation_params'])

        execution = self._create()

        frozen = execution.config_snapshot['scenarios'][0]['simulation_params']
        self.assertEqual(frozen, scenario.simulation_params)
        self.assertEqual(execution.cases.get().task.simulation_params, scenario.simulation_params)

    def test_explicit_raid_buffs_are_frozen_into_execution_and_task(self):
        scenario = self.panel.scenarios.get(key='patchwerk')
        scenario.simulation_params = {
            'iterations': 1000,
            'raid_buffs': ['arcane_intellect', 'battle_shout'],
        }
        scenario.save(update_fields=['simulation_params'])

        execution = self._create()

        frozen = execution.config_snapshot['scenarios'][0]['simulation_params']
        self.assertEqual(frozen['raid_buffs'], ['arcane_intellect', 'battle_shout'])
        self.assertEqual(
            execution.cases.get().task.simulation_params['raid_buffs'],
            frozen['raid_buffs'],
        )

    def test_trinket_benchmark_replaces_both_profile_slots_with_frozen_reference_pair(self):
        candidate = self.panel.candidates.get(key='trinket')
        candidate.params = {
            **candidate.params,
            'benchmark_profile': {
                'kind': 'trinket_standard_reference',
                'item_level': 240,
            },
        }
        candidate.save(update_fields=['params'])
        self.profile.player_equipment = (
            'warrior="Tester"\nlevel=80\nspec=fury\n'
            'trinket1=Original One,id=111,ilevel=300\n'
            'trinket2=Original Two,id=123,ilevel=300\n'
        )
        self.profile.save(update_fields=['player_equipment'])

        execution = self._create()
        task = execution.cases.get().task
        frozen = task.mode_params['initial_candidates']
        self.assertEqual(frozen[0]['candidate_params']['equipment_preset'], {
            'trinket1': '',
            'trinket2': 'id=142508,ilevel=240,bonus_id=607',
        })
        self.assertEqual(
            frozen[1]['candidate_params']['equipment_preset'],
            frozen[0]['candidate_params']['equipment_preset'],
        )

        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        rendered = [SimcMonitor.apply_candidate_overrides(
            {'player_equipment': self.profile.player_equipment}, row['candidate_params'],
        )['player_equipment'] for row in frozen]
        self.assertIn('trinket1=', rendered[0])
        self.assertNotIn('id=111', rendered[0])
        self.assertNotIn('id=123', rendered[0])
        self.assertIn('trinket2=,id=142508,ilevel=240,bonus_id=607', rendered[0])
        self.assertIn('trinket1=,id=123', rendered[1])
        self.assertEqual(rendered[1].count('id=123'), 1)

        candidate.params['benchmark_profile']['item_level'] = 999
        candidate.save(update_fields=['params'])
        task.refresh_from_db()
        self.assertEqual(
            task.mode_params['initial_candidates'][0]['candidate_params']['equipment_preset'],
            frozen[0]['candidate_params']['equipment_preset'],
        )

    def test_trinket_benchmark_internal_controls_reject_wrong_slot_and_malformed_preset(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor
        from botend.services.simc_benchmark_config import _normalize_candidate_params
        from botend.services.simc_task_service import TaskCreationError, _normalize_candidates

        with self.assertRaises(ValidationError):
            _normalize_candidate_params('gear_swap', {
                'slot': 'trinket2', 'raw_value': 'id=123,ilevel=240',
                'benchmark_profile': {
                    'kind': 'trinket_standard_reference', 'item_level': 240,
                },
            })

        malformed = {
            'candidate_key': 'invalid-preset',
            'candidate_params': {
                'candidate_type': 'base', 'is_base': True,
                'equipment_preset': {'trinket1': '', 'head': 'id=1'},
            },
        }
        with self.assertRaises(TaskCreationError):
            _normalize_candidates([malformed])
        with self.assertRaisesRegex(ValueError, '精确包含两个饰品槽'):
            SimcMonitor.apply_candidate_overrides(
                {'player_equipment': 'trinket1=id=1\ntrinket2=id=2'},
                malformed['candidate_params'],
            )

    def test_historical_gear_swap_without_benchmark_profile_keeps_original_second_slot(self):
        from botend.controller.plugins.simc.SimcMonitor import SimcMonitor

        original = (
            'warrior="Tester"\nlevel=80\nspec=fury\n'
            'trinket1=Original One,id=111,ilevel=300\n'
            'trinket2=Original Two,id=123,ilevel=300\n'
        )
        rendered = SimcMonitor.apply_candidate_overrides(
            {'player_equipment': original},
            {'candidate_type': 'gear_swap', 'gear_swap': {
                'slot': 'trinket1', 'raw_value': 'id=456,ilevel=300',
            }},
        )['player_equipment']
        self.assertIn('trinket1=,id=456,ilevel=300', rendered)
        self.assertIn('trinket2=Original Two,id=123,ilevel=300', rendered)

    def _run(self, task, sequence, status, key=None, label=None, dps=None):
        return SimulationRun.objects.create(
            task=task, sequence=sequence, candidate_key=key or f'candidate-{sequence}',
            candidate_label=label or key or f'Candidate {sequence}', status=status,
            result_summary={'dps': dps} if dps is not None else None,
        )

    def _aggregate_candidate(self, key, dps, task_id, *, label=None, candidate_type=None,
                             icon_url='', source_label=''):
        return {
            'key': key, 'label': label or key.title(),
            'type': candidate_type or ('base' if key == 'baseline' else 'gear_swap'),
            'icon_url': icon_url, 'source_label': source_label,
            'dps': dps, 'task_id': task_id,
        }

    def _published_success(self):
        # A completed immutable coordinate is intentionally not scheduled again.
        # Serializer-contract subtests need an independent fixture, so vary the
        # frozen scenario input after the first completed fixture in this database.
        completed = SimcBenchmarkExecution.objects.filter(
            panel=self.panel, results_finalized_at__isnull=False,
        ).count()
        if completed:
            SimcBenchmarkScenario.objects.filter(panel=self.panel, key='patchwerk').update(
                simulation_params={'iterations': 1000 + completed},
            )
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)
        self._run(task, 2, 'completed', 'trinket', dps=1300)
        reconcile_execution(execution)
        self.panel.is_public = True
        self.panel.save(update_fields=['is_public'])
        return execution

    def test_semantically_invalid_completed_run_is_not_published_as_benchmark_result(self):
        execution = self._create()
        case = execution.cases.get()
        task = case.task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        invalid = self._run(task, 1, 'completed', 'baseline', dps=3512)
        invalid.result_summary = {
            'dps': 3512,
            'valid': False,
            'failure_type': 'talent_apl_dispatch',
            'reason': '英雄天赋未进入任何有效 APL 分流',
        }
        invalid.save(update_fields=['result_summary'])
        self._run(task, 2, 'completed', 'trinket', dps=1300)

        reconcile_execution(execution)

        execution.refresh_from_db()
        case.refresh_from_db()
        self.assertEqual(execution.status, 'partial')
        self.assertEqual(case.status, 'partial')
        self.assertEqual(case.results.count(), 0)
        self.assertIn('英雄天赋未进入任何有效 APL 分流', case.error_detail)

    def test_failed_case_rerun_copies_only_failed_task_and_keeps_original_result_immutable(self):
        successful = self._published_success()
        original_success_case = successful.cases.get()
        original_success_task_id = original_success_case.task_id
        original_result_ids = list(original_success_case.results.values_list('id', flat=True))
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='retry-coordinate', name='Retry coordinate',
            simulation_params={'iterations': 2000},
        )

        failed_execution = self._create()
        failed_case = failed_execution.cases.get()
        failed_task = failed_case.task
        failed_task.current_status = 3
        failed_task.save(update_fields=['current_status'])
        self._run(failed_task, 1, 'failed', 'baseline')
        reconcile_execution(failed_execution)
        failed_execution.refresh_from_db()
        self.assertEqual(failed_execution.status, 'failed')

        rerun_execution = rerun_failed_cases(failed_execution, requested_by=self.user_id)

        rerun_case = rerun_execution.cases.get()
        self.assertNotEqual(rerun_execution.id, failed_execution.id)
        self.assertEqual(rerun_case.task.source_task_id, failed_task.id)
        self.assertEqual(rerun_case.task.current_status, 0)
        self.assertEqual(
            list(rerun_case.task.simulation_runs.values_list('candidate_key', 'status')),
            [('baseline', 'pending'), ('trinket', 'pending')],
        )
        self.assertEqual(
            SimcTask.objects.filter(source_task=failed_task).count(), 1,
        )
        self.assertEqual(
            SimcTask.objects.exclude(pk__in=[failed_task.id, rerun_case.task_id]).count(), 1,
        )
        original_success_case.refresh_from_db()
        self.assertEqual(original_success_case.task_id, original_success_task_id)
        self.assertEqual(
            list(original_success_case.results.values_list('id', flat=True)), original_result_ids,
        )

    def test_failed_rerun_keeps_full_coordinate_snapshot_but_only_schedules_failed_cases(self):
        scenario = self.panel.scenarios.get(key='patchwerk')
        scenario.simulation_params = {
            'iterations': 1000, 'desired_targets': 1, 'max_time': 300,
        }
        scenario.save(update_fields=['simulation_params'])
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='failed-coordinate', name='Failed coordinate',
            simulation_params={'iterations': 2000},
        )
        execution = self._create()
        cases = {case.scenario_key: case for case in execution.cases.select_related('task')}
        successful_case = cases['patchwerk']
        failed_case = cases['failed-coordinate']

        successful_task = successful_case.task
        successful_task.current_status = 2
        successful_task.save(update_fields=['current_status'])
        self._run(successful_task, 1, 'completed', 'baseline', dps=1234)
        self._run(successful_task, 2, 'completed', 'trinket', dps=1300)
        failed_task = failed_case.task
        failed_task.current_status = 3
        failed_task.save(update_fields=['current_status'])
        self._run(failed_task, 1, 'failed', 'baseline')
        self._run(failed_task, 2, 'failed', 'trinket')
        reconcile_execution(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.status, 'partial')

        retry = rerun_failed_cases(execution, requested_by=self.user_id)

        self.assertEqual(retry.cases.count(), 1)
        retry_case = retry.cases.select_related('task').get()
        self.assertEqual(retry_case.scenario_key, 'failed-coordinate')
        self.assertEqual(retry_case.task.source_task_id, failed_task.id)
        self.assertEqual(retry.config_snapshot['case_count'], 2)
        self.assertEqual(len(retry.config_snapshot['cases']), 2)
        self.assertEqual(retry.config_snapshot['run_count'], 4)
        self.assertEqual(successful_case.results.count(), 2)

        aggregate = serialize_incremental_panel_results(self.panel)
        coordinates = {row['scenario_key']: row for row in aggregate['coordinates']}
        self.assertEqual(coordinates['patchwerk']['labels'], {
            'spec': '狂怒-战士', 'scenario': 'Patchwerk', 'profile': 'Raid profile',
        })
        self.assertEqual(
            benchmark_execution_service._spec_display_name('Warrior Fury', 'warrior_fury'),
            '狂怒-战士',
        )
        self.assertEqual(coordinates['patchwerk']['scenario_detail'], {
            'desired_targets': 1, 'max_time': 300,
        })
        self.assertEqual(coordinates['patchwerk']['candidates'], [
            self._aggregate_candidate('baseline', 1234.0, successful_task.id, label='Baseline'),
            self._aggregate_candidate('trinket', 1300.0, successful_task.id, label='Trinket'),
        ])
        self.assertEqual(coordinates['failed-coordinate']['candidates'], [])

    def test_incremental_projection_reuses_results_from_older_larger_execution(self):
        """A smaller later Execution must not hide results on older panel coordinates."""
        original = self._published_success()
        original_case = original.cases.select_related('task').get()

        later = SimcBenchmarkExecution.objects.create(
            panel=self.panel,
            status=SimcBenchmarkExecution.STATUS_PARTIAL,
            config_snapshot={'case_count': 0, 'run_count': 0}, config_hash='f' * 64,
        )
        self.assertEqual(later.config_snapshot['case_count'], 0)

        aggregate = serialize_incremental_panel_results(self.panel)

        self.assertEqual(len(aggregate['coordinates']), 1)
        self.assertEqual(aggregate['coordinates'][0]['candidates'], [
            self._aggregate_candidate('baseline', 1234.0, original_case.task_id, label='Baseline'),
            self._aggregate_candidate('trinket', 1300.0, original_case.task_id, label='Trinket'),
        ])

    @override_settings(
        ALLOWED_HOSTS=['testserver'],
        OSS_CONFIG={'base_url': 'https://reports.example.test'},
    )
    def test_incremental_projection_exposes_candidate_oss_raw_report_url(self):
        execution = self._published_success()
        task = execution.cases.get().task
        baseline_run = task.simulation_runs.get(candidate_key='baseline')
        SimcTaskArtifact.objects.create(
            task=task,
            run=baseline_run,
            artifact_type='html_report',
            file_path=f'simc_agent_results/simc_task_{task.id}_run_{baseline_run.id}.html',
            file_size=123,
        )

        coordinate = serialize_incremental_panel_results(self.panel)['coordinates'][0]
        baseline = next(row for row in coordinate['candidates'] if row['key'] == 'baseline')

        self.assertEqual(
            baseline['raw_report_url'],
            f'https://reports.example.test/simc_agent_results/'
            f'simc_task_{task.id}_run_{baseline_run.id}.html',
        )

    def test_incremental_projection_exposes_profile_detail_for_selected_coordinate(self):
        WowItemSnapshot.objects.create(
            item_id=100, name='Helm of Tests', name_zh='测试头盔',
            icon='inv_helmet_01',
        )
        self.profile.player_equipment = (
            'warrior="Profile Player"\nlevel=90\nspec=fury\n'
            'talents=abc123\nhead=,id=100\nmain_hand=,id=200\n'
        )
        self.profile.talent = 'abc123'
        self.profile.use_ptr = True
        self.profile.save(update_fields=['player_equipment', 'talent', 'use_ptr'])
        self._published_success()

        coordinate = serialize_incremental_panel_results(self.panel)['coordinates'][0]
        profile = coordinate['profile_detail']

        self.assertEqual(profile['identity']['name'], 'Profile Player')
        self.assertEqual(profile['identity']['class_name'], 'warrior')
        self.assertEqual(profile['identity']['spec'], '狂怒-战士')
        self.assertEqual(profile['identity']['spec_key'], 'fury')
        self.assertEqual(profile['identity']['level'], 90)
        self.assertEqual(profile['talents']['build_code'], 'abc123')
        self.assertIs(profile['is_ptr'], True)
        self.assertEqual(profile['talent_version'], 'ptr-12.1.0')
        self.assertTrue(coordinate['spec_icon_url'])
        self.assertEqual([item['slot'] for item in profile['equipment']], ['head', 'main_hand'])
        self.assertEqual(profile['equipment'][0]['display_name'], '测试头盔')
        self.assertNotIn('player_equipment', profile)

    def test_spec_icons_cover_underscore_and_new_specialization_keys(self):
        self.assertIn('bestialdiscipline', _spec_icon_url('hunter_beast_mastery'))
        self.assertIn('classicon_demonhunter_void', _spec_icon_url('demonhunter_devourer'))

    def test_incremental_projection_uses_profile_frozen_by_result_source_task(self):
        self.profile.player_equipment = (
            'warrior="Frozen Player"\nlevel=80\nspec=fury\n'
            'talents=frozen-build\nhead=,id=100\n'
        )
        self.profile.talent = 'frozen-build'
        self.profile.save(update_fields=['player_equipment', 'talent'])
        self._published_success()
        self.profile.player_equipment = (
            'warrior="Changed Player"\nlevel=80\nspec=fury\n'
            'talents=changed-build\nhead=,id=999\n'
        )
        self.profile.talent = 'changed-build'
        self.profile.save(update_fields=['player_equipment', 'talent'])

        profile = serialize_incremental_panel_results(
            self.panel,
        )['coordinates'][0]['profile_detail']

        self.assertEqual(profile['identity']['name'], 'Frozen Player')
        self.assertEqual(profile['talents']['build_code'], 'frozen-build')
        self.assertEqual(profile['equipment'][0]['item_id'], 100)

    def test_incremental_projection_uses_profile_frozen_by_failed_source_task(self):
        self.profile.player_equipment = (
            'warrior="Frozen Failure"\nlevel=80\nspec=fury\n'
            'talents=frozen-failure-build\nhead=,id=101\n'
        )
        self.profile.talent = 'frozen-failure-build'
        self.profile.save(update_fields=['player_equipment', 'talent'])
        execution = self._create()
        case = execution.cases.get()
        case.status = SimcBenchmarkExecution.STATUS_FAILED
        case.save(update_fields=['status'])
        self.profile.player_equipment = (
            'warrior="Changed After Failure"\nlevel=80\nspec=fury\n'
            'talents=changed-build\nhead=,id=999\n'
        )
        self.profile.talent = 'changed-build'
        self.profile.save(update_fields=['player_equipment', 'talent'])

        profile = serialize_incremental_panel_results(
            self.panel,
        )['coordinates'][0]['profile_detail']

        self.assertEqual(profile['identity']['name'], 'Frozen Failure')
        self.assertEqual(profile['talents']['build_code'], 'frozen-failure-build')
        self.assertEqual(profile['equipment'][0]['item_id'], 101)

    def test_incremental_projection_exposes_frozen_candidate_item_level(self):
        candidate = self.panel.candidates.get(key='trinket')
        candidate.params = {
            **candidate.params,
            'gear_swap': {
                **candidate.params['gear_swap'],
                'raw_value': ',id=123,ilevel=285,bonus_id=42',
            },
        }
        candidate.save(update_fields=['params'])
        self._published_success()

        candidates = serialize_incremental_panel_results(
            self.panel,
        )['coordinates'][0]['candidates']

        trinket = next(row for row in candidates if row['key'] == 'trinket')
        self.assertEqual(trinket['item_id'], 123)
        self.assertEqual(trinket['item_level'], 285)
        self.assertTrue(trinket['item_variant_key'])

    def test_incremental_result_projection_scans_finalized_cases_once_for_all_coordinates(self):
        self._published_success()
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='single-target', name='Single target',
            simulation_params={'iterations': 1200},
        )

        original_filter = SimcBenchmarkCase.objects.filter
        result_lookup_calls = 0

        def counted_filter(*args, **kwargs):
            nonlocal result_lookup_calls
            if kwargs.get('execution__panel_id') == self.panel.id and kwargs.get('results__isnull') is False:
                result_lookup_calls += 1
            return original_filter(*args, **kwargs)

        with patch.object(SimcBenchmarkCase.objects, 'filter', side_effect=counted_filter):
            result = serialize_incremental_panel_results(self.panel)

        self.assertEqual(len(result['coordinates']), 2)
        self.assertEqual(result_lookup_calls, 1)

    def test_selected_projection_filters_result_query_before_building_coordinate_detail(self):
        self._published_success()
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='single-target', name='Single target',
            simulation_params={'iterations': 1200, 'desired_targets': 1, 'max_time': 300},
        )
        seen_filters = []
        original_filter = SimcBenchmarkCase.objects.filter

        def capture_filter(*args, **kwargs):
            if kwargs.get('execution__panel_id') == self.panel.id and kwargs.get('results__isnull') is False:
                seen_filters.append(kwargs.copy())
            return original_filter(*args, **kwargs)

        with patch.object(SimcBenchmarkCase.objects, 'filter', side_effect=capture_filter):
            result = serialize_incremental_panel_results(
                self.panel,
                coordinate_filter={
                    'spec_key': 'warrior_fury',
                    'profile_key': str(self.profile.id),
                    'scenario_key': 'patchwerk',
                },
                include_coordinate_options=True,
            )

        self.assertEqual(len(result['coordinate_options']), 2)
        self.assertEqual(len(result['coordinates']), 1)
        self.assertEqual(result['coordinates'][0]['scenario_key'], 'patchwerk')
        self.assertEqual(seen_filters, [{
            'execution__panel_id': self.panel.id,
            'results__isnull': False,
            'spec_key': 'warrior_fury',
            'profile_key': str(self.profile.id),
            'scenario_key': 'patchwerk',
        }])
        self.assertNotIn('candidates', result['coordinate_options'][0])
        self.assertNotIn('profile_detail', result['coordinate_options'][0])

    def test_scenario_projection_returns_all_specs_without_loading_other_scenarios(self):
        self._published_success()
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='single-target', name='Single target',
            simulation_params={'iterations': 1200, 'desired_targets': 1, 'max_time': 300},
        )
        seen_filters = []
        original_filter = SimcBenchmarkCase.objects.filter

        def capture_filter(*args, **kwargs):
            if kwargs.get('execution__panel_id') == self.panel.id and kwargs.get('results__isnull') is False:
                seen_filters.append(kwargs.copy())
            return original_filter(*args, **kwargs)

        with patch.object(SimcBenchmarkCase.objects, 'filter', side_effect=capture_filter):
            result = serialize_incremental_panel_results(
                self.panel,
                scenario_filter='patchwerk',
                include_coordinate_options=True,
            )

        self.assertEqual(len(result['coordinate_options']), 2)
        self.assertEqual({row['scenario_key'] for row in result['coordinates']}, {'patchwerk'})
        self.assertEqual(seen_filters, [{
            'execution__panel_id': self.panel.id,
            'results__isnull': False,
            'scenario_key': 'patchwerk',
        }])

    def test_incremental_candidate_creates_only_missing_candidate_task_and_aggregates_old_result(self):
        original = self._published_success()
        original_case = original.cases.get()
        original_task = original_case.task
        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='new-trinket', label='New Trinket',
            candidate_type='gear_swap', params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': {'slot': 'trinket2', 'raw_value': ',id=456',
                              'item_id': 456, 'source': 'manual'},
            },
        )

        incremental = self._create()
        incremental_case = incremental.cases.get()

        self.assertEqual(incremental_case.task.source_task_id, original_task.id)
        self.assertEqual(
            [row['candidate_key'] for row in incremental_case.task.mode_params['initial_candidates']],
            ['new-trinket'],
        )
        self.assertEqual(incremental_case.task.simulation_runs.count(), 0)
        self.assertEqual(SimcBenchmarkCase.objects.filter(execution=original).count(), 1)
        self.assertEqual(original_case.results.count(), 2)

        incremental_task = incremental_case.task
        incremental_task.current_status = 2
        incremental_task.save(update_fields=['current_status'])
        self._run(incremental_task, 1, 'completed', 'new-trinket', dps=1400)
        reconcile_execution(incremental)

        aggregate = serialize_incremental_panel_results(self.panel)
        row = aggregate['coordinates'][0]
        self.assertEqual(row['candidates'], [
            self._aggregate_candidate('baseline', 1234.0, original_task.id, label='Baseline'),
            self._aggregate_candidate('trinket', 1300.0, original_task.id, label='Trinket'),
            self._aggregate_candidate('new-trinket', 1400.0, incremental_task.id, label='New Trinket'),
        ])

    def test_incremental_execution_reuses_complete_coordinate_without_copying_task(self):
        original = self._published_success()
        original_case = original.cases.get()
        incremental = self._create()

        self.assertEqual(incremental.cases.count(), 0)
        self.assertEqual(incremental.status, SimcBenchmarkExecution.STATUS_SUCCESS)
        self.assertIsNotNone(incremental.completed_at)
        self.panel.refresh_from_db()
        self.assertIsNone(self.panel.active_execution_id)
        self.assertEqual(self.panel.published_execution_id, original.id)
        self.assertEqual(SimcTask.objects.count(), 1)
        aggregate = serialize_incremental_panel_results(self.panel)
        self.assertEqual(aggregate['coordinates'][0]['candidates'][0]['task_id'], original_case.task_id)

    def test_reconcile_closes_legacy_empty_supplement_without_publishing_it(self):
        original = self._published_success()
        empty = SimcBenchmarkExecution.objects.create(
            panel=self.panel,
            status=SimcBenchmarkExecution.STATUS_PENDING,
            config_snapshot={
                'version': 2,
                'execution_mode': 'supplement',
                'case_count': 0,
                'run_count': 0,
            },
            config_hash='f' * 64,
        )
        self.panel.active_execution = empty
        self.panel.save(update_fields=['active_execution'])

        reconcile_execution(empty)

        empty.refresh_from_db()
        self.panel.refresh_from_db()
        self.assertEqual(empty.status, SimcBenchmarkExecution.STATUS_SUCCESS)
        self.assertIsNotNone(empty.completed_at)
        self.assertIsNone(self.panel.active_execution_id)
        self.assertEqual(self.panel.published_execution_id, original.id)

    def test_full_rerun_creates_all_runs_and_replaces_current_aggregate_baseline(self):
        original = self._published_success()
        original_task_count = SimcTask.objects.count()

        rerun = self._create(execution_mode='full')

        self.assertNotEqual(rerun.id, original.id)
        self.assertEqual(rerun.cases.count(), 1)
        self.assertEqual(
            [row['candidate_key'] for row in rerun.cases.get().task.mode_params['initial_candidates']],
            ['baseline', 'trinket'],
        )
        self.assertEqual(SimcTask.objects.count(), original_task_count + 1)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.aggregate_baseline_execution_id, rerun.id)
        self.assertEqual(
            serialize_incremental_panel_results(self.panel)['coordinates'][0]['candidates'], [],
        )

    def test_supplement_rerun_only_schedules_missing_candidates_from_current_baseline(self):
        original = self._published_success()
        task = original.cases.get().task
        original.cases.get().results.filter(candidate_key='trinket').delete()
        task.mode_params['initial_candidates'] = [
            {'candidate_key': 'baseline'}, {'candidate_key': 'trinket'},
        ]
        task.save(update_fields=['mode_params'])

        supplement = self._create(execution_mode='supplement')

        self.assertEqual(supplement.cases.count(), 1)
        supplement_case = supplement.cases.get()
        self.assertEqual(
            [row['candidate_key'] for row in supplement_case.task.mode_params['initial_candidates']],
            ['trinket'],
        )
        self.assertEqual(supplement_case.task.simulation_runs.count(), 0)

    def test_adding_another_item_level_supplements_only_the_new_level(self):
        candidate = self.panel.candidates.get()
        original = self._published_success()
        original_case = original.cases.get()
        original_task = original_case.task
        result = original_case.results.get(candidate_key=candidate.key)
        candidate.key = 'item-123-ilvl-700'
        candidate.params['gear_swap']['raw_value'] = ',id=123,ilevel=700'
        candidate.save(update_fields=['key', 'params'])
        old_manifest = original_task.mode_params['request_manifest']['candidates'][1]
        frozen = dict(
            old_manifest,
            candidate_key=candidate.key,
            candidate_params=deepcopy(candidate.params),
        )
        original_task.mode_params['initial_candidates'][1] = deepcopy(frozen)
        original_task.mode_params['request_manifest']['candidates'][1] = deepcopy(frozen)
        original_task.save(update_fields=['mode_params'])
        result.candidate_key = candidate.key
        result.save(update_fields=['candidate_key'])
        SimcBenchmarkCandidate.objects.create(
            panel=self.panel, key='item-123-ilvl-710', label='Trinket 710',
            candidate_type='gear_swap', params={
                'candidate_type': 'gear_swap', 'is_base': False,
                'gear_swap': {
                    'slot': 'trinket1', 'raw_value': ',id=123,ilevel=710',
                    'item_id': 123, 'source': 'manual',
                },
            },
        )

        supplement = self._create(execution_mode='supplement')

        self.assertEqual(supplement.cases.count(), 1)
        self.assertEqual(
            [row['candidate_key'] for row in
             supplement.cases.get().task.mode_params['initial_candidates']],
            ['item-123-ilvl-710'],
        )

    def test_running_execution_persists_complete_case_results_for_incremental_aggregation(self):
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='pending-coordinate', name='Pending coordinate',
            simulation_params={'iterations': 2000},
        )
        execution = self._create()
        cases = {case.scenario_key: case for case in execution.cases.select_related('task')}
        successful_case = cases['patchwerk']
        successful_task = successful_case.task
        successful_task.current_status = 2
        successful_task.save(update_fields=['current_status'])
        self._run(successful_task, 1, 'completed', 'baseline', dps=1234)
        self._run(successful_task, 2, 'completed', 'trinket', dps=1300)

        reconcile_execution(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, SimcBenchmarkExecution.STATUS_RUNNING)
        self.assertEqual(successful_case.results.count(), 2)
        aggregate = serialize_incremental_panel_results(self.panel)
        by_scenario = {row['scenario_key']: row['candidates'] for row in aggregate['coordinates']}
        self.assertEqual(by_scenario['patchwerk'], [
            self._aggregate_candidate('baseline', 1234.0, successful_task.id, label='Baseline'),
            self._aggregate_candidate('trinket', 1300.0, successful_task.id, label='Trinket'),
        ])
        self.assertEqual(by_scenario['pending-coordinate'], [])

    def test_partial_execution_persists_complete_case_results_for_incremental_aggregation(self):
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='failed-coordinate', name='Failed coordinate',
            simulation_params={'iterations': 2000},
        )
        execution = self._create()
        cases = {case.scenario_key: case for case in execution.cases.select_related('task')}
        successful_case = cases['patchwerk']
        failed_case = cases['failed-coordinate']
        successful_task = successful_case.task
        successful_task.current_status = 2
        successful_task.save(update_fields=['current_status'])
        self._run(successful_task, 1, 'completed', 'baseline', dps=1234)
        self._run(successful_task, 2, 'completed', 'trinket', dps=1300)
        failed_task = failed_case.task
        failed_task.current_status = 3
        failed_task.save(update_fields=['current_status'])
        self._run(failed_task, 1, 'failed', 'baseline')

        reconcile_execution(execution)
        execution.refresh_from_db()

        self.assertEqual(execution.status, SimcBenchmarkExecution.STATUS_PARTIAL)
        self.assertEqual(successful_case.results.count(), 2)
        self.assertFalse(failed_case.results.exists())
        aggregate = serialize_incremental_panel_results(self.panel)
        by_scenario = {row['scenario_key']: row['candidates'] for row in aggregate['coordinates']}
        self.assertEqual(by_scenario['patchwerk'], [
            self._aggregate_candidate('baseline', 1234.0, successful_task.id, label='Baseline'),
            self._aggregate_candidate('trinket', 1300.0, successful_task.id, label='Trinket'),
        ])
        self.assertEqual(by_scenario['failed-coordinate'], [])

    def test_success_is_persisted_and_frozen_from_task_run_mutation(self):
        execution = self._published_success()
        execution.refresh_from_db()
        self.assertEqual(execution.status, 'success')
        self.assertEqual(SimcBenchmarkResult.objects.filter(
            case__execution=execution,
        ).count(), 2)
        self.assertEqual(len(execution.result_hash), 64)
        self.assertIsNotNone(execution.results_finalized_at)

        before = summarize_execution(execution)
        task = execution.cases.get().task
        SimulationRun.objects.filter(task=task).delete()
        task.current_status = 3
        task.save(update_fields=['current_status'])
        with CaptureQueriesContext(connection) as captured:
            after = summarize_execution(execution)
            public = serialize_public_execution(execution)
        self.assertEqual(after, before)
        self.assertEqual(public['status'], 'ready')
        sql = ' '.join(query['sql'].lower() for query in captured.captured_queries)
        self.assertNotIn('simulation_run', sql)
        self.assertNotIn('simc_task', sql)

        original_hash = execution.result_hash
        reconcile_execution(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.result_hash, original_hash)
        self.assertEqual(SimcBenchmarkResult.objects.filter(
            case__execution=execution,
        ).count(), 2)

        result = SimcBenchmarkResult.objects.filter(case__execution=execution).first()
        SimcBenchmarkResult.objects.filter(pk=result.pk).update(dps=result.dps + 1)
        self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')

    def test_invalid_dps_finalizes_failed_without_results_or_publication(self):
        invalid_values = (None, True, 0, -1, float('nan'), float('inf'), float('-inf'))
        for value in invalid_values:
            with self.subTest(value=value):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = 2
                task.save(update_fields=['current_status'])
                self._run(task, 1, 'completed', 'baseline', dps=1)
                self._run(task, 2, 'completed', 'trinket', dps=1)
                live = benchmark_execution_service._summarize_live_execution(execution)
                live['cases'][0]['runs'][0]['_raw_dps'] = value
                with patch(
                    'botend.services.simc_benchmark_execution._summarize_live_execution',
                    return_value=live,
                ):
                    reconcile_execution(execution)
                execution.refresh_from_db()
                self.panel.refresh_from_db()
                self.assertEqual(execution.status, 'failed')
                self.assertIsNotNone(execution.completed_at)
                self.assertFalse(execution.result_hash)
                self.assertIsNone(execution.results_finalized_at)
                self.assertFalse(SimcBenchmarkResult.objects.filter(
                    case__execution=execution,
                ).exists())
                self.assertNotEqual(self.panel.published_execution_id, execution.pk)

    def _replace_snapshot(self, execution, mutate, *, update_hash=True):
        snapshot = deepcopy(execution.config_snapshot)
        mutate(snapshot)
        values = {'config_snapshot': snapshot}
        if update_hash:
            values['config_hash'] = hashlib.sha256(json.dumps(
                snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest()
        SimcBenchmarkExecution.objects.filter(pk=execution.pk).update(**values)

    def test_create_execution_uses_real_task_service_and_freezes_safe_plan(self):
        execution = self._create()
        case = execution.cases.select_related('task').get()
        task = case.task
        self.assertEqual(task.mode, 'comparison')
        self.assertEqual(task.user_id, self.user_id)
        self.assertEqual(task.simulation_params, {'iterations': 1000})
        self.assertEqual(task.backend_id, self.backend.id)
        self.assertEqual(
            [row['candidate_key'] for row in task.mode_params['initial_candidates']],
            ['baseline', 'trinket'],
        )
        self.assertEqual(
            task.mode_params['initial_candidates'][1]['candidate_params']['simc_options'],
            ['midnight.crucible_of_erratic_energies_predation=1'],
        )
        self.assertEqual(
            [row['candidate_key'] for row in
             task.mode_params['request_manifest']['candidates']],
            ['baseline', 'trinket'],
        )
        self.assertIn(f'panel-{self.panel.id}', task.name)
        self.assertIn(f'execution-{execution.id}', task.name)
        self.assertIn('warrior_fury', task.name)
        self.assertIn('patchwerk', task.name)
        self.assertIn(str(self.profile.id), task.name)
        frozen = repr(execution.config_snapshot)
        for secret in (self.apl.content, self.template.content, self.backend.simc_path,
                       self.profile.player_equipment):
            if secret:
                self.assertNotIn(secret, frozen)
        self.assertEqual(len(execution.config_hash), 64)
        snapshot = execution.config_snapshot
        self.assertEqual(snapshot['version'], 2)
        self.assertEqual(snapshot['panel']['description'], self.panel.description)
        self.assertEqual(snapshot['specs'][0]['display_label'], 'Fury')
        frozen_case = snapshot['cases'][0]
        self.assertNotIn('resources', frozen_case)
        self.assertNotIn('params', frozen_case)
        self.assertEqual(set(frozen_case), {
            'spec_key', 'scenario_key', 'profile_key', 'resource_key',
            'candidate_keys',
        })
        resources = snapshot['resources'][frozen_case['resource_key']]
        self.assertEqual(resources['apl']['validation_revision'], 'a' * 40)
        self.assertEqual(resources['backend']['current_version'], 'a' * 40)
        self.assertEqual(resources['profile']['name'], 'Profile')
        self.assertEqual(snapshot['candidates'][0]['icon_url'], '')
        self.assertEqual(execution.config_hash, hashlib.sha256(
            __import__('json').dumps(execution.config_snapshot, sort_keys=True,
                                     separators=(',', ':'), ensure_ascii=False).encode()
        ).hexdigest())

    def test_trigger_owner_active_and_slot_contract(self):
        with self.assertRaises(PermissionDenied):
            create_execution(self.panel, requested_by=999)
        with self.assertRaises(ValidationError):
            create_execution(self.panel, requested_by=self.user_id,
                             scheduled_slot=timezone.now())
        with self.assertRaises(ValidationError):
            create_execution(self.panel, trigger='schedule', requested_by=self.user_id)
        with self.assertRaises(ValidationError):
            create_execution(self.panel, trigger='schedule', requested_by=self.user_id,
                             scheduled_slot=timezone.now().replace(tzinfo=None))
        self.panel.is_active = False
        self.panel.save(update_fields=['is_active'])
        with self.assertRaises(ValidationError):
            create_execution(self.panel, requested_by=self.user_id)

    def test_scheduled_slot_is_second_normalized_and_idempotent(self):
        slot = timezone.now().replace(microsecond=987654)
        first = self._create(trigger='schedule', scheduled_slot=slot)
        second = self._create(trigger='schedule', scheduled_slot=slot.replace(microsecond=1))
        self.assertEqual(first.id, second.id)
        self.assertEqual(first.scheduled_slot.microsecond, 0)
        self.assertEqual(SimcBenchmarkExecution.objects.count(), 1)
        self.assertEqual(SimcBenchmarkCase.objects.count(), 1)
        self.assertEqual(SimcTask.objects.count(), 1)

    def test_active_execution_slot_blocks_a_different_scheduled_slot(self):
        slot = timezone.now().replace(microsecond=0)
        winner = self._create(trigger='schedule', scheduled_slot=slot)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.active_execution_id, winner.pk)
        with self.assertRaises(BenchmarkExecutionConflict):
            self._create(trigger='schedule', scheduled_slot=slot + timedelta(seconds=60))

    def test_preflight_deduplicates_resources_and_runs_between_unlocked_and_locked_plans(self):
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='second', name='Second', simulation_params={},
        )
        from botend.services.simc_benchmark_config import build_execution_plan as real_build
        from botend.services.simc_task_service import prepare_task_creation as real_prepare
        events = []

        def record_plan(panel, *args, **kwargs):
            events.append(('plan', kwargs.get('lock', True)))
            return real_build(panel, *args, **kwargs)

        def record_prepare(*args, **kwargs):
            events.append(('validator', None))
            return real_prepare(*args, **kwargs)

        with patch('botend.services.simc_benchmark_execution.build_execution_plan',
                   side_effect=record_plan), patch(
            'botend.services.simc_benchmark_execution.prepare_task_creation',
            side_effect=record_prepare,
        ), patch('botend.services.simc_task_service.current_validation_identity',
                 return_value=('a' * 40, '12.0.1')), patch(
            'botend.services.simc_task_service.validate_apl_for_profile',
            return_value=self.validation,
        ):
            execution = create_execution(self.panel, requested_by=self.user_id)

        self.assertEqual(events, [('plan', False), ('validator', None), ('plan', True)])
        self.assertEqual(execution.cases.count(), 2)

    def test_full_execution_keeps_valid_coordinates_when_another_preflight_fails(self):
        rejected_profile = SimcProfile.objects.create(
            user_id=self.user_id, name='Rejected profile', class_name='warrior',
            spec='warrior_fury', is_active=True,
        )
        SimcBenchmarkProfile.objects.create(
            panel_spec=self.panel.specs.get(), profile=rejected_profile,
            label='Rejected profile',
        )
        from botend.services.simc_task_service import (
            TaskCreationError, prepare_task_creation as real_prepare,
        )

        def prepare(*args, **kwargs):
            if args[1] == rejected_profile.pk:
                raise TaskCreationError(
                    'APL failed authoritative validation for the selected Profile',
                    details={'diagnostics': [{'message': 'unknown action foo_bar'}]},
                )
            return real_prepare(*args, **kwargs)

        with patch(
            'botend.services.simc_benchmark_execution.prepare_task_creation',
            side_effect=prepare,
        ), patch(
            'botend.services.simc_task_service.current_validation_identity',
            return_value=('a' * 40, '12.0.1'),
        ), patch(
            'botend.services.simc_task_service.validate_apl_for_profile',
            return_value=self.validation,
        ):
            execution = create_execution(
                self.panel, requested_by=self.user_id, execution_mode='full',
            )

        cases = list(execution.cases.select_related('task').order_by('profile_key'))
        self.assertEqual(len(cases), 2)
        failed = next(case for case in cases if case.profile_key == str(rejected_profile.pk))
        successful = next(case for case in cases if case.profile_key == str(self.profile.pk))
        self.assertIsNone(failed.task)
        self.assertEqual(failed.status, 'failed')
        self.assertIn('warrior_fury / patchwerk / ' + str(rejected_profile.pk), failed.error_detail)
        self.assertIn(f'Profile #{rejected_profile.pk}', failed.error_detail)
        self.assertIn(f'APL #{self.apl.pk}', failed.error_detail)
        self.assertIn(f'Template #{self.template.pk}', failed.error_detail)
        self.assertIn(f'Backend #{self.backend.pk}', failed.error_detail)
        self.assertIn('unknown action foo_bar', failed.error_detail)
        self.assertIsNotNone(successful.task)
        summary = summarize_execution(execution)
        self.assertEqual(summary['failed'], 1)
        self.assertEqual(summary['run_counts']['failed'], 2)
        self.assertEqual(summary['total_runs'], 2)
        self.assertEqual(next(row for row in summary['cases'] if row['task_id'] is None)['error'],
                         failed.error_detail)

        successful.task.current_status = 2
        successful.task.save(update_fields=['current_status'])
        for sequence, candidate in enumerate(
                successful.task.mode_params['request_manifest']['candidates'], 1):
            self._run(
                successful.task, sequence, 'completed', candidate['candidate_key'],
                dps=100000 + sequence,
            )
        reconcile_execution(execution)
        execution.refresh_from_db()
        terminal = summarize_execution(execution)
        self.assertEqual(execution.status, 'partial')
        self.assertEqual(terminal['run_counts']['failed'], 2)
        self.assertEqual(terminal['total_runs'], 4)
        self.assertEqual(
            next(row for row in terminal['cases'] if row['task_id'] is None)['error'],
            failed.error_detail,
        )

    def test_full_execution_with_every_preflight_rejected_finishes_and_releases_panel(self):
        from botend.services.simc_task_service import TaskCreationError

        with patch(
            'botend.services.simc_benchmark_execution.prepare_task_creation',
            side_effect=TaskCreationError('rejected', details={'error': 'safe validator reason'}),
        ):
            execution = create_execution(
                self.panel, requested_by=self.user_id, execution_mode='full',
            )

        execution.refresh_from_db()
        self.panel.refresh_from_db()
        case = execution.cases.get()
        self.assertEqual(execution.status, 'failed')
        self.assertIsNotNone(execution.completed_at)
        self.assertIsNone(self.panel.active_execution_id)
        self.assertIsNone(case.task_id)
        self.assertEqual(case.status, 'failed')
        self.assertIn('safe validator reason', case.error_detail)
        with self.assertRaisesRegex(ValidationError, '预检失败坐标'):
            rerun_failed_cases(execution, requested_by=self.user_id)

    def test_validator_unavailability_is_conflict_but_content_rejection_is_validation(self):
        retryable_results = [{
            'valid': False, 'content_hash': self.validation['content_hash'],
            'revision': 'a' * 40, 'game_build': '12.0.1', 'error': error,
        } for error in (
            'validation_context_unavailable', 'validation_backend_unavailable',
            'validation_failed',
        )] + [{
            'valid': False, 'content_hash': self.validation['content_hash'],
            'revision': 'a' * 40, 'game_build': '12.0.1',
            'details': {
                'structural_valid': True, 'authoritative_status': 'error',
                'authoritative_error': {'code': code},
            },
        } for code in (
            'stale_binary', 'binary_unavailable', 'temp_directory_error',
            'timeout', 'output_too_large', 'unknown_future_error',
        )] + [{'valid': False, 'malformed': True}]

        for validation in retryable_results:
            with self.subTest(validation=validation), patch(
                'botend.services.simc_task_service.current_validation_identity',
                return_value=('a' * 40, '12.0.1'),
            ), patch(
                'botend.services.simc_task_service.validate_apl_for_profile',
                return_value=validation,
            ):
                with self.assertRaises(BenchmarkExecutionConflict):
                    create_execution(self.panel, requested_by=self.user_id)
                self.assertFalse(SimcBenchmarkExecution.objects.exists())
                self.assertFalse(SimcTask.objects.exists())

        permanent_results = (
            {'valid': False, 'details': {
                'structural_valid': False,
                'authoritative_status': 'skipped_structural_errors',
            }},
            {'valid': False, 'details': {
                'structural_valid': True, 'authoritative_status': 'invalid',
            }},
        )
        for validation in permanent_results:
            with self.subTest(validation=validation), patch(
                'botend.services.simc_task_service.current_validation_identity',
                return_value=('a' * 40, '12.0.1'),
            ), patch(
                'botend.services.simc_task_service.validate_apl_for_profile',
                return_value=validation,
            ):
                with self.assertRaises(ValidationError):
                    create_execution(self.panel, requested_by=self.user_id)
                self.assertFalse(SimcBenchmarkExecution.objects.exists())
                self.assertFalse(SimcTask.objects.exists())

    def test_resource_change_after_preflight_rolls_back_the_whole_execution(self):
        from botend.services.simc_benchmark_config import build_execution_plan as real_build

        def change_resource_before_locked_plan(panel, *args, **kwargs):
            if kwargs.get('lock', True):
                SimcContentTemplate.objects.filter(pk=self.template.pk).update(
                    content='changed after preflight',
                )
            return real_build(panel, *args, **kwargs)

        with patch('botend.services.simc_benchmark_execution.build_execution_plan',
                   side_effect=change_resource_before_locked_plan), patch(
            'botend.services.simc_task_service.current_validation_identity',
            return_value=('a' * 40, '12.0.1'),
        ), patch('botend.services.simc_task_service.validate_apl_for_profile',
                 return_value=self.validation):
            with self.assertRaises(BenchmarkExecutionConflict):
                create_execution(self.panel, requested_by=self.user_id)

        self.assertFalse(SimcBenchmarkExecution.objects.exists())
        self.assertFalse(SimcBenchmarkCase.objects.exists())
        self.assertFalse(SimcTask.objects.exists())

    def test_second_case_failure_rolls_back_execution_cases_tasks_and_runs(self):
        SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='second', name='Second', simulation_params={},
        )
        from botend.services.simc_task_service import create_task as real_create_task
        calls = 0

        def fail_second(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('injected failure')
            return real_create_task(*args, **kwargs)

        with patch('botend.services.simc_benchmark_execution.create_task', side_effect=fail_second), patch(
            'botend.services.simc_task_service.current_validation_identity',
            return_value=('a' * 40, '12.0.1'),
        ), patch('botend.services.simc_task_service.validate_apl_for_profile',
                 return_value=self.validation):
            with self.assertRaises(RuntimeError):
                create_execution(self.panel, requested_by=self.user_id)
        self.assertFalse(SimcBenchmarkExecution.objects.exists())
        self.assertFalse(SimcBenchmarkCase.objects.exists())
        self.assertFalse(SimcTask.objects.exists())
        self.assertFalse(SimulationRun.objects.exists())

    def test_live_summary_derives_status_counts_without_exposing_dps(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 1
        task.error_detail = '/srv/private/profile.simc\n' + ('secret ' * 100)
        task.save(update_fields=['current_status', 'error_detail'])
        SimulationRun.objects.create(
            task=task, sequence=1, candidate_key='baseline',
            candidate_label='Baseline', status='completed', result_summary={'dps': 1234.5},
        )
        SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='trinket',
            candidate_label='Trinket', status='running',
        )
        reconcile_execution(execution)
        result = summarize_execution(execution)
        self.assertEqual(result['status'], 'running')
        self.assertEqual((result['total_cases'], result['total_runs']), (1, 0))
        self.assertEqual(result['running'], 1)
        self.assertEqual(result['cases'][0]['runs'], [])
        self.assertIsNone(result['cases'][0]['error'])
        self.assertNotIn('file_path', repr(result))

    def test_backfill_completed_case_results_restores_only_success_cases_idempotently(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)
        self._run(task, 2, 'completed', 'trinket', dps=1256)
        reconcile_execution(execution)
        execution.refresh_from_db()
        self.assertEqual(execution.status, 'success')
        SimcBenchmarkResult.objects.filter(case__execution=execution).delete()

        original_state = (execution.status, execution.completed_at, execution.result_hash)
        self.assertEqual(backfill_completed_case_results(execution), 2)
        self.assertEqual(
            list(SimcBenchmarkResult.objects.filter(case__execution=execution).values_list(
                'candidate_key', 'dps').order_by('candidate_key')),
            [('baseline', 1234.0), ('trinket', 1256.0)],
        )
        self.assertEqual(backfill_completed_case_results(execution), 0)
        self.assertEqual(SimcBenchmarkResult.objects.filter(case__execution=execution).count(), 2)
        execution.refresh_from_db()
        self.assertEqual((execution.status, execution.completed_at, execution.result_hash), original_state)

    def test_backfill_refuses_existing_conflicting_result(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)
        self._run(task, 2, 'completed', 'trinket', dps=1256)
        reconcile_execution(execution)
        result = execution.cases.get().results.get(candidate_key='baseline')
        result.dps = 9999
        result.save(update_fields=['dps'])

        with self.assertRaises(ValidationError):
            backfill_completed_case_results(execution)
        result.refresh_from_db()
        self.assertEqual(result.dps, 9999)

    def test_partial_case_rerun_reuses_completed_runs_and_only_schedules_failed_candidates(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 3
        task.save(update_fields=['current_status'])
        completed = self._run(task, 1, 'completed', 'baseline', dps=1234)
        failed = self._run(task, 2, 'failed', 'trinket')
        reconcile_execution(execution)

        rerun_execution = rerun_failed_cases(execution, requested_by=self.user_id)
        rerun_case = rerun_execution.cases.get()
        rerun_task = rerun_case.task

        self.assertEqual(rerun_execution.config_snapshot['case_count'], 1)
        self.assertEqual(rerun_execution.cases.count(), execution.cases.count())
        self.assertEqual(rerun_task.source_task_id, task.id)
        self.assertEqual(
            [candidate['candidate_key'] for candidate in rerun_task.mode_params['initial_candidates']],
            ['trinket'],
        )
        self.assertEqual(
            list(rerun_task.simulation_runs.values_list('candidate_key', 'status', 'result_summary')),
            [('trinket', 'pending', None)],
        )
        self.assertEqual(SimulationRun.objects.filter(pk=completed.pk).count(), 1)
        self.assertEqual(SimulationRun.objects.filter(pk=failed.pk).count(), 1)

        rerun_run = rerun_task.simulation_runs.get(candidate_key='trinket')
        rerun_run.status = 'completed'
        rerun_run.result_summary = {'dps': 1260}
        rerun_run.save(update_fields=['status', 'result_summary'])
        rerun_task.current_status = 2
        rerun_task.save(update_fields=['current_status'])
        reconcile_execution(rerun_execution)
        rerun_execution.refresh_from_db()
        self.assertEqual(rerun_execution.status, 'success')
        self.assertEqual(
            list(rerun_case.results.values_list('candidate_key', 'dps').order_by('candidate_key')),
            [('baseline', 1234.0), ('trinket', 1260.0)],
        )

    def test_backfill_restores_completed_source_runs_for_partial_retry_without_promoting_case(self):
        execution = self._create()
        source_task = execution.cases.get().task
        source_task.current_status = 3
        source_task.save(update_fields=['current_status'])
        self._run(source_task, 1, 'completed', 'baseline', dps=1234)
        self._run(source_task, 2, 'failed', 'trinket')
        reconcile_execution(execution)

        retry = rerun_failed_cases(execution, requested_by=self.user_id)
        retry_case = retry.cases.get()
        retry_task = retry_case.task
        retry_task.current_status = 3
        retry_task.save(update_fields=['current_status'])
        reconcile_execution(retry)
        retry.refresh_from_db()
        self.assertEqual(retry.status, 'failed')
        self.assertEqual(retry_case.results.count(), 0)

        self.assertEqual(backfill_completed_case_results(retry), 1)
        self.assertEqual(
            list(retry_case.results.values_list('candidate_key', 'dps')),
            [('baseline', 1234.0)],
        )
        self.assertEqual(backfill_completed_case_results(retry), 0)
        retry.refresh_from_db()
        retry_case.refresh_from_db()
        self.assertEqual(retry.status, 'failed')
        self.assertEqual(retry_case.status, 'failed')
        self.assertEqual(retry_task.simulation_runs.count(), 1)
        self.assertEqual(source_task.simulation_runs.count(), 2)

        aggregate = serialize_incremental_panel_results(self.panel)
        self.assertEqual(aggregate['coordinates'][0]['candidates'], [
            self._aggregate_candidate('baseline', 1234.0, retry_task.id, label='Baseline'),
        ])

    def test_success_task_with_mixed_terminal_runs_is_partial_and_not_published(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline')
        self._run(task, 2, 'failed', 'trinket')

        reconcile_execution(execution)
        summary = summarize_execution(execution)
        self.assertEqual(summary['status'], 'partial')
        self.assertEqual(summary['partial'], 1)
        self.assertEqual(summary['cases'][0]['status'], 'partial')
        reconcile_execution(execution)
        execution.refresh_from_db()
        self.panel.refresh_from_db()
        self.assertIsNotNone(execution.completed_at)
        self.assertIsNone(self.panel.published_execution_id)

        rerun_execution = rerun_failed_cases(execution, requested_by=self.user_id)
        rerun_case = rerun_execution.cases.get()
        self.assertEqual(rerun_case.task.source_task_id, task.id)
        self.assertEqual(rerun_case.task.current_status, 0)

    def test_success_task_with_live_or_missing_runs_never_completes(self):
        for run_status, expected in ((None, 'pending'), ('pending', 'pending'),
                                     ('running', 'running')):
            with self.subTest(run_status=run_status):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = 2
                task.save(update_fields=['current_status'])
                if run_status:
                    self._run(task, 1, run_status, 'baseline')
                reconcile_execution(execution)
                self.assertEqual(summarize_execution(execution)['status'], expected)
                execution.refresh_from_db()
                self.assertIsNone(execution.completed_at)
                execution.delete()

    def test_zero_runs_fold_failed_and_cancelled_task_terminal_status(self):
        for task_status, expected in ((3, 'failed'), (5, 'cancelled')):
            with self.subTest(task_status=task_status):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = task_status
                task.save(update_fields=['current_status'])

                reconcile_execution(execution)
                summary = summarize_execution(execution)
                self.assertEqual(summary['status'], expected)
                self.assertEqual(summary['cases'][0]['status'], expected)
                self.assertEqual(summary['total_runs'], 0)
                reconcile_execution(execution)
                execution.refresh_from_db()
                self.panel.refresh_from_db()
                self.assertIsNotNone(execution.completed_at)
                self.assertIsNone(self.panel.published_execution_id)

    def test_deleted_task_finalizes_case_failed_and_releases_active_slot(self):
        execution = self._create()
        task = execution.cases.get().task
        task.delete()

        reconciled = reconcile_execution(execution)
        execution.refresh_from_db()
        case = execution.cases.get()
        self.panel.refresh_from_db()

        self.assertEqual(reconciled.status, 'failed')
        self.assertEqual(execution.status, 'failed')
        self.assertIsNotNone(execution.completed_at)
        self.assertEqual(case.status, 'failed')
        self.assertIsNone(case.task_id)
        self.assertIsNone(self.panel.active_execution_id)
        self.assertIsNone(self.panel.published_execution_id)

    def test_zero_runs_keep_nonterminal_tasks_nonterminal_and_success_conservative(self):
        for task_status, expected in ((0, 'pending'), (1, 'running'), (2, 'pending')):
            with self.subTest(task_status=task_status):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = task_status
                task.save(update_fields=['current_status'])

                reconcile_execution(execution)
                summary = summarize_execution(execution)
                self.assertEqual(summary['status'], expected)
                self.assertEqual(summary['cases'][0]['status'], expected)
                reconcile_execution(execution)
                execution.refresh_from_db()
                self.panel.refresh_from_db()
                self.assertIsNone(execution.completed_at)
                self.assertIsNone(self.panel.published_execution_id)

    def test_failed_or_cancelled_task_abandons_nonterminal_runs_conservatively(self):
        for task_status, expected in ((3, 'failed'), (5, 'cancelled')):
            with self.subTest(task_status=task_status):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = task_status
                task.save(update_fields=['current_status'])
                self._run(task, 1, 'pending', 'baseline')
                reconcile_execution(execution)
                summary = summarize_execution(execution)
                self.assertEqual(summary['status'], expected)
                self.assertEqual(summary['cases'][0]['status'], expected)
                self.assertEqual(summary['cases'][0]['runs'], [])
                reconcile_execution(execution)
                execution.refresh_from_db()
                self.assertIsNotNone(execution.completed_at)

    def test_complete_candidate_set_and_success_task_are_both_required_to_publish(self):
        execution = self._create()
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)

        reconcile_execution(execution)
        self.assertEqual(summarize_execution(execution)['status'], 'pending')
        self.panel.refresh_from_db()
        execution.refresh_from_db()
        self.assertIsNone(self.panel.published_execution_id)
        self.assertIsNone(execution.completed_at)

        self._run(task, 2, 'completed', 'trinket', dps=1300)
        reconcile_execution(execution)
        self.assertEqual(summarize_execution(execution)['status'], 'success')
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.published_execution_id, execution.id)

    def test_failed_or_cancelled_task_never_publishes_completed_runs(self):
        for task_status, expected in ((3, 'failed'), (5, 'cancelled')):
            with self.subTest(task_status=task_status):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = task_status
                task.save(update_fields=['current_status'])
                self._run(task, 1, 'completed', 'baseline')
                self._run(task, 2, 'completed', 'trinket')

                reconcile_execution(execution)
                self.assertEqual(summarize_execution(execution)['status'], expected)
                self.panel.refresh_from_db()
                self.assertNotEqual(self.panel.published_execution_id, execution.id)

    def test_unknown_duplicate_or_reordered_run_keys_fail_closed(self):
        key_sets = (
            ('baseline', 'unknown'),
            ('baseline', 'baseline'),
            ('trinket', 'baseline'),
        )
        for keys in key_sets:
            with self.subTest(keys=keys):
                execution = self._create()
                task = execution.cases.get().task
                task.current_status = 2
                task.save(update_fields=['current_status'])
                for sequence, key in enumerate(keys, 1):
                    self._run(task, sequence, 'completed', key)

                reconcile_execution(execution)
                self.assertEqual(summarize_execution(execution)['status'], 'failed')
                self.panel.refresh_from_db()
                self.assertNotEqual(self.panel.published_execution_id, execution.id)

    def test_missing_or_malformed_candidate_manifest_fails_closed(self):
        for manifest in (None, {}, {'candidates': []},
                         {'candidates': [{'candidate_key': 'baseline'},
                                         {'candidate_key': 'baseline'}]}):
            with self.subTest(manifest=manifest):
                execution = self._create()
                task = execution.cases.get().task
                mode_params = task.mode_params.copy()
                if manifest is None:
                    mode_params.pop('request_manifest', None)
                else:
                    mode_params['request_manifest'] = manifest
                task.mode_params = mode_params
                task.current_status = 2
                task.save(update_fields=['mode_params', 'current_status'])
                self._run(task, 1, 'completed', 'baseline')
                self._run(task, 2, 'completed', 'trinket')

                reconcile_execution(execution)
                self.assertEqual(summarize_execution(execution)['status'], 'failed')
                self.panel.refresh_from_db()
                self.assertNotEqual(self.panel.published_execution_id, execution.id)

    def test_reconcile_publishes_success_only_is_idempotent_and_never_regresses(self):
        older = self._create()
        old_task = older.cases.get().task
        old_task.current_status = 2
        old_task.save(update_fields=['current_status'])
        self._run(old_task, 1, 'completed', 'baseline', dps=1234)
        self._run(old_task, 2, 'completed', 'trinket', dps=1300)
        reconcile_execution(older)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.published_execution_id, older.id)

        SimcBenchmarkScenario.objects.filter(panel=self.panel, key='patchwerk').update(
            simulation_params={'iterations': 2000},
        )
        newer = self._create()
        new_task = newer.cases.get().task
        new_task.current_status = 2
        new_task.save(update_fields=['current_status'])
        self._run(new_task, 1, 'completed', 'baseline', dps=1234)
        self._run(new_task, 2, 'completed', 'trinket', dps=1300)
        reconcile_execution(newer)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.published_execution_id, newer.id)

        reconcile_execution(older)
        reconcile_execution(older)
        self.panel.refresh_from_db()
        older.refresh_from_db()
        self.assertIsNotNone(older.completed_at)
        self.assertEqual(self.panel.published_execution_id, newer.id)

        SimcBenchmarkScenario.objects.filter(panel=self.panel, key='patchwerk').update(
            simulation_params={'iterations': 3000},
        )
        failed = self._create()
        failed_task = failed.cases.get().task
        failed_task.current_status = 3
        failed_task.save(update_fields=['current_status'])
        self._run(failed_task, 1, 'failed', 'baseline')
        reconcile_execution(failed)
        self.panel.refresh_from_db()
        self.assertEqual(self.panel.published_execution_id, newer.id)

    def test_reconcile_empty_and_cross_panel_cannot_publish(self):
        empty = SimcBenchmarkExecution.objects.create(
            panel=self.panel, trigger='manual', config_hash='0' * 64,
        )
        reconcile_execution(empty)
        empty.refresh_from_db()
        self.panel.refresh_from_db()
        self.assertIsNone(empty.completed_at)
        self.assertIsNone(self.panel.published_execution_id)

        other = SimcBenchmarkPanel.objects.create(
            name='Other', slug='other-execution', created_by_id=self.user_id,
        )
        self.panel.published_execution = SimcBenchmarkExecution.objects.create(
            panel=other, trigger='manual', config_hash='1' * 64,
            completed_at=timezone.now(),
        )
        self.panel.save(update_fields=['published_execution'])
        with self.assertRaises(ValidationError):
            reconcile_execution(empty)

    def test_public_serializer_only_returns_current_publication(self):
        candidate = self.panel.candidates.get(key='trinket')
        candidate.icon_url = 'https://example.com/trinket.png'
        candidate.source_label = 'Raid drop'
        candidate.save(update_fields=['icon_url', 'source_label'])
        execution = self._create()
        self.assertEqual(serialize_public_execution(self.panel), {
            'status': 'not_ready', 'execution': None,
        })
        task = execution.cases.get().task
        task.current_status = 2
        task.save(update_fields=['current_status'])
        self._run(task, 1, 'completed', 'baseline', dps=1234)
        self._run(task, 2, 'completed', 'trinket', dps=1300)
        reconcile_execution(execution)
        private_publication = serialize_public_execution(execution)
        self.assertEqual(private_publication['status'], 'ready')
        self.panel.is_public = True
        self.panel.save(update_fields=['is_public'])
        public = serialize_public_execution(execution)
        self.assertEqual(public['status'], 'ready')
        self.assertEqual(public, private_publication)
        self.assertNotIn('id', public['execution'])
        self.assertNotIn('id', public['panel'])
        self.assertEqual(public['execution']['cases'][0]['candidates'][0]['label'], 'Baseline')
        gear = public['execution']['cases'][0]['candidates'][1]
        self.assertEqual(gear, {
            'key': 'trinket', 'label': 'Trinket', 'type': 'gear_swap',
            'icon_url': 'https://example.com/trinket.png',
            'source_label': 'Raid drop', 'status': 'success', 'dps': 1300,
        })

        def all_keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key
                    yield from all_keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from all_keys(child)

        keys = set(all_keys(public))
        self.assertTrue({'task_id', 'error', 'error_detail', 'config_hash',
                         'config_snapshot', 'path'}.isdisjoint(keys))

        # Finalized aggregate data is immutable product state. Later mutation of the
        # internal execution Task cannot change or invalidate the published result.
        task.current_status = 3
        task.save(update_fields=['current_status'])
        self.assertEqual(serialize_public_execution(execution), public)
        task.current_status = 2
        task.save(update_fields=['current_status'])

        # Public history is rendered from the frozen snapshot, not edited config rows.
        self.panel.name = 'Renamed later'
        self.panel.description = 'Changed later'
        self.panel.save(update_fields=['name', 'description'])
        self.panel.candidates.all().delete()
        historical = serialize_public_execution(execution)
        self.assertEqual(historical['panel']['name'], 'Weekly')
        self.assertEqual(historical['panel']['description'], '')
        self.assertEqual(historical['execution']['cases'][0]['candidates'][0]['label'],
                         'Baseline')

        draft = self._create()
        self.assertEqual(serialize_public_execution(draft), {
            'status': 'not_ready', 'execution': None,
        })

    def test_public_serializer_rejects_mutated_case_axis_labels(self):
        execution = self._published_success()
        SimcBenchmarkCase.objects.filter(execution=execution).update(
            spec_label='EVIL mutable spec',
            scenario_label='EVIL mutable scenario',
            profile_label='EVIL mutable profile',
        )

        self.assertEqual(serialize_public_execution(execution), {
            'status': 'not_ready', 'execution': None,
        })

    def test_public_serializer_rejects_mutated_completed_at(self):
        execution = self._published_success()
        execution.refresh_from_db()
        execution.completed_at = execution.completed_at + timedelta(seconds=1)
        execution.save(update_fields=['completed_at'])

        self.assertEqual(serialize_public_execution(execution), {
            'status': 'not_ready', 'execution': None,
        })

    def test_public_serializer_rejects_snapshot_label_changed_without_matching_hash(self):
        execution = self._published_success()
        self._replace_snapshot(
            execution,
            lambda value: value['specs'][0].__setitem__('display_label', 'Tampered'),
            update_hash=False,
        )
        self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')

    def test_public_serializer_uses_labels_from_new_validly_sealed_snapshot(self):
        execution = self._published_success()

        def mutate(snapshot):
            snapshot['specs'][0]['display_label'] = 'New sealed spec'
            snapshot['scenarios'][0]['label'] = 'New sealed scenario'
            snapshot['profiles'][0]['label'] = 'New sealed profile'

        self._replace_snapshot(execution, mutate)
        public_case = serialize_public_execution(execution)['execution']['cases'][0]
        self.assertEqual(public_case['labels'], {
            'spec': 'New sealed spec',
            'scenario': 'New sealed scenario',
            'profile': 'New sealed profile',
        })

    def test_public_serializer_uses_execution_display_metadata_without_changing_seal(self):
        execution = self._published_success()
        config_snapshot = deepcopy(execution.config_snapshot)
        config_hash = execution.config_hash
        execution.display_metadata = {
            'trinket': {
                'label': '冻结中文饰品',
                'icon_url': '/static/wow_icons/small/inv_trinket_raid_01.jpg',
            },
        }
        execution.save(update_fields=['display_metadata'])

        public = serialize_public_execution(execution)
        trinket = next(
            row for row in public['execution']['cases'][0]['candidates']
            if row['key'] == 'trinket'
        )
        execution.refresh_from_db()
        self.assertEqual(public['status'], 'ready')
        self.assertEqual(trinket['label'], '冻结中文饰品')
        self.assertEqual(trinket['icon_url'], '/static/wow_icons/small/inv_trinket_raid_01.jpg')
        self.assertEqual(execution.config_snapshot, config_snapshot)
        self.assertEqual(execution.config_hash, config_hash)

    def test_public_serializer_rejects_snapshot_changed_without_matching_hash(self):
        execution = self._published_success()
        self._replace_snapshot(
            execution, lambda value: value['panel'].__setitem__('name', 'Tampered'),
            update_hash=False,
        )
        self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')

    def test_public_serializer_rejects_duplicate_snapshot_coordinate_with_valid_hash(self):
        execution = self._published_success()
        self._replace_snapshot(
            execution, lambda value: value['cases'].append(deepcopy(value['cases'][0])),
        )
        self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')

    def test_public_serializer_requires_exact_ordered_snapshot_candidate_keys(self):
        for mutation in ('missing', 'extra', 'reordered'):
            with self.subTest(mutation=mutation):
                execution = self._published_success()

                def mutate(snapshot):
                    if mutation == 'missing':
                        snapshot['cases'][0]['candidate_keys'].pop()
                    elif mutation == 'reordered':
                        snapshot['cases'][0]['candidate_keys'].reverse()
                    else:
                        snapshot['candidates'].append({
                            'key': 'ghost', 'label': 'Ghost', 'candidate_type': 'gear_swap',
                            'icon_url': '', 'source_label': '', 'params': {},
                        })
                        snapshot['cases'][0]['candidate_keys'].append('ghost')

                self._replace_snapshot(execution, mutate)
                self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')

    def test_public_serializer_requires_exact_snapshot_case_set(self):
        for mutation in ('missing', 'extra'):
            with self.subTest(mutation=mutation):
                execution = self._published_success()

                def mutate(snapshot):
                    if mutation == 'missing':
                        snapshot['cases'].clear()
                    else:
                        profile = deepcopy(snapshot['profiles'][0])
                        profile['key'] = 'extra-profile'
                        snapshot['profiles'].append(profile)
                        case = deepcopy(snapshot['cases'][0])
                        case['profile_key'] = profile['key']
                        snapshot['cases'].append(case)

                self._replace_snapshot(execution, mutate)
                self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')

    def test_public_serializer_rejects_invalid_snapshot_counts_and_definitions(self):
        mutations = {
            'run_count': lambda value: value.__setitem__('run_count', 999),
            'bool_case_count': lambda value: value.__setitem__('case_count', True),
            'duplicate_spec': lambda value: value['specs'].append(deepcopy(value['specs'][0])),
            'empty_scenario_key': lambda value: value['scenarios'][0].__setitem__('key', ''),
            'incomplete_profile': lambda value: value['profiles'][0].pop('label'),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                execution = self._published_success()
                self._replace_snapshot(execution, mutate)
                self.assertEqual(serialize_public_execution(execution)['status'], 'not_ready')
