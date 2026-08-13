"""TDD API contracts for the staff-only SimC Benchmark Dashboard."""
import hashlib
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.utils import timezone

from botend.models import (
    DashboardUserGroup,
    DashboardUserGroupMembership,
    SimcApl, SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcBenchmarkResult, SimcContentTemplate, SimcProfile, SimcTask,
    SimcTaskArtifact, SimulationRun,
)
from botend.services.simc_benchmark_config import build_execution_plan
from botend.services.simc_benchmark_execution import BenchmarkExecutionConflict


class SimcBenchmarkDashboardApiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='benchmark-staff', password='password', is_staff=True,
        )
        self.other_staff = User.objects.create_user(
            username='benchmark-other-staff', password='password', is_staff=True,
        )
        self.regular = User.objects.create_user(
            username='benchmark-regular', password='password',
        )
        self.authorized = User.objects.create_user(
            username='benchmark-authorized', password='password',
        )
        group = DashboardUserGroup.objects.create(
            name='SimC benchmark API group', permission_codes=['simc.benchmarks'],
        )
        DashboardUserGroupMembership.objects.create(user=self.staff, group=group)
        DashboardUserGroupMembership.objects.create(user=self.other_staff, group=group)
        DashboardUserGroupMembership.objects.create(user=self.authorized, group=group)
        self.backend = SimcBackendBinary.objects.create(
            identifier='dashboard-api', name='Dashboard API', is_active=True,
        )
        self.apl = SimcApl.objects.create(
            name='Fury APL', spec='warrior_fury', content='actions=/auto_attack',
            owner_user_id=self.staff.id, is_active=True, is_selectable=True,
        )
        self.template = SimcContentTemplate.objects.create(
            name='Fury template', spec='warrior_fury', content='iterations=1000',
            owner_user_id=self.staff.id, is_active=True, is_selectable=True,
        )
        self.profile = SimcProfile.objects.create(
            user_id=self.staff.id, name='Fury profile', class_name='warrior',
            spec='warrior_fury', is_active=True,
        )
        self.payload = {
            'name': 'Weekly benchmark', 'slug': 'weekly-dashboard-api',
            'description': 'Dashboard API fixture', 'is_active': True,
            'is_public': False, 'schedule_enabled': False,
            'interval_seconds': 3600, 'next_run_at': None,
            'specs': [{
                'class_name': 'warrior', 'spec_key': 'warrior_fury',
                'label': 'Fury', 'apl_id': self.apl.id,
                'template_id': self.template.id, 'backend_id': self.backend.id,
                'profiles': [{'profile_id': self.profile.id, 'label': 'Raid'}],
            }],
            'scenarios': [{
                'key': 'patchwerk', 'name': 'Patchwerk',
                'simulation_params': {'iterations': 1000},
            }],
            'candidates': [],
        }
        self.client.force_login(self.staff)

    @staticmethod
    def _json(client, method, path, payload):
        return getattr(client, method)(
            path, data=json.dumps(payload), content_type='application/json',
        )

    def _create_panel(self):
        response = self._json(self.client, 'post', '/api/simc-benchmarks/panels/', self.payload)
        self.assertEqual(response.status_code, 201, response.content)
        return SimcBenchmarkPanel.objects.get(pk=response.json()['data']['id'])

    def test_full_run_response_exposes_preflight_failure_coordinate_and_reason(self):
        panel = self._create_panel()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status='running',
            config_snapshot={'version': 2, 'case_count': 1, 'run_count': 1},
            config_hash='a' * 64,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=None, status='failed',
            error_detail=(
                'rogue_subtlety / patchwerk / 313 '
                '(Profile #313, APL #29, Template #3, Backend #1): '
                'sim_signal_handler: Segmentation fault'
            ),
            spec_key='rogue_subtlety', scenario_key='patchwerk', profile_key='313',
            spec_label='敏锐潜行者', scenario_label='Patchwerk', profile_label='PTR baseline',
            coordinate_hash='b' * 64,
        )

        with patch('botend.dashboard.api.create_execution', return_value=execution):
            response = self._json(
                self.client, 'post', f'/api/simc-benchmarks/panels/{panel.pk}/run/',
                {'mode': 'full'},
            )

        self.assertEqual(response.status_code, 202, response.content)
        failure = response.json()['data']['preflight_failures'][0]
        self.assertEqual(failure['coordinate'], {
            'spec_key': 'rogue_subtlety', 'scenario_key': 'patchwerk', 'profile_key': '313',
        })
        self.assertEqual(failure['labels']['spec'], '敏锐-潜行者')
        self.assertIn('Profile #313', failure['error'])
        self.assertIn('Segmentation fault', failure['error'])

    def test_create_generates_stable_unique_slug_when_client_omits_it(self):
        payload = dict(self.payload)
        payload.pop('slug')
        payload['name'] = '午夜饰品 基准面板'

        first = self._json(self.client, 'post', '/api/simc-benchmarks/panels/', payload)
        second = self._json(self.client, 'post', '/api/simc-benchmarks/panels/', payload)

        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        first_slug = first.json()['data']['slug']
        second_slug = second.json()['data']['slug']
        self.assertRegex(first_slug, r'^[a-z0-9][a-z0-9_-]*$')
        self.assertRegex(second_slug, r'^[a-z0-9][a-z0-9_-]*$')
        self.assertNotEqual(first_slug, second_slug)

    def test_metadata_patch_renames_panel_without_changing_generated_slug(self):
        payload = dict(self.payload)
        payload.pop('slug')
        created = self._json(
            self.client, 'post', '/api/simc-benchmarks/panels/', payload,
        ).json()['data']

        response = self._json(
            self.client, 'patch',
            f"/api/simc-benchmarks/panels/{created['id']}/",
            {'name': 'Renamed benchmark'},
        )

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['name'], 'Renamed benchmark')
        self.assertEqual(response.json()['data']['slug'], created['slug'])

        replacement = dict(self.payload)
        replacement['name'] = 'Reconfigured benchmark'
        replacement['slug'] = 'client-attempted-slug-change'
        replaced = self._json(
            self.client, 'put',
            f"/api/simc-benchmarks/panels/{created['id']}/", replacement,
        )
        self.assertEqual(replaced.status_code, 200, replaced.content)
        self.assertEqual(replaced.json()['data']['slug'], created['slug'])

    def test_login_and_admin_permissions_are_consistent_json(self):
        anonymous = Client().get('/api/simc-benchmarks/panels/')
        self.assertEqual(anonymous.status_code, 302)
        regular = Client()
        regular.force_login(self.regular)
        for method, path, payload in (
            ('get', '/api/simc-benchmarks/panels/', None),
            ('post', '/api/simc-benchmarks/panels/', {}),
            ('get', '/api/simc-benchmarks/panels/1/', None),
            ('put', '/api/simc-benchmarks/panels/1/', {}),
            ('delete', '/api/simc-benchmarks/panels/1/', None),
            ('post', '/api/simc-benchmarks/panels/1/run/', {}),
            ('get', '/api/simc-benchmarks/panels/1/executions/', None),
            ('post', '/api/simc-benchmarks/executions/1/reconcile/', {}),
            ('post', '/api/simc-benchmarks/executions/1/cancel/', {}),
        ):
            with self.subTest(method=method, path=path):
                kwargs = {}
                if payload is not None:
                    kwargs = {'data': json.dumps(payload), 'content_type': 'application/json'}
                response = getattr(regular, method)(path, **kwargs)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(response.json(), {'success': False, 'error': 'forbidden'})

    def test_crud_list_counts_and_creator_cannot_be_reassigned(self):
        panel = self._create_panel()
        response = self.client.get('/api/simc-benchmarks/panels/')
        self.assertEqual(response.status_code, 200)
        row = response.json()['data'][0]
        self.assertEqual(row['counts'], {
            'specs': 1, 'scenarios': 1, 'profiles': 1, 'candidates': 0,
        })
        self.assertEqual(row['published_execution_id'], None)

        other = Client()
        other.force_login(self.other_staff)
        updated = dict(self.payload, name='Maintained by another admin')
        response = self._json(
            other, 'put', f'/api/simc-benchmarks/panels/{panel.id}/', updated,
        )
        self.assertEqual(response.status_code, 200, response.content)
        panel.refresh_from_db()
        self.assertEqual(panel.created_by_id, self.staff.id)
        self.assertEqual(panel.name, 'Maintained by another admin')

        reassignment = dict(updated, created_by_id=self.other_staff.id)
        response = self._json(
            other, 'put', f'/api/simc-benchmarks/panels/{panel.id}/', reassignment,
        )
        self.assertEqual(response.status_code, 400)
        panel.refresh_from_db()
        self.assertEqual(panel.created_by_id, self.staff.id)
        self.assertEqual(response.json()['error'], 'validation_error')

    def test_completed_execution_rerun_endpoint_delegates_only_to_benchmark_service(self):
        panel = self._create_panel()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={'version': 2, 'case_count': 0, 'run_count': 0},
            config_hash='e' * 64, status='failed', completed_at=timezone.now(),
        )
        with patch('botend.dashboard.api.rerun_failed_cases', return_value=execution) as rerun:
            response = self.client.post(
                f'/api/simc-benchmarks/executions/{execution.id}/rerun-failed/',
                data='{}', content_type='application/json',
            )
        self.assertEqual(response.status_code, 202, response.content)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['data']['id'], execution.id)
        rerun.assert_called_once_with(execution, requested_by=self.staff)

    def test_active_execution_cancel_endpoint_delegates_to_benchmark_service(self):
        panel = self._create_panel()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={'version': 2, 'case_count': 0, 'run_count': 0},
            config_hash='c' * 64, status='running',
        )
        def persist_cancel(target, requested_by):
            target.status = SimcBenchmarkExecution.STATUS_CANCELLED
            target.completed_at = timezone.now()
            target.save(update_fields=['status', 'completed_at'])
            return target

        cancelled_summary = {
            'id': execution.id, 'status': 'cancelled', 'cases': [],
            'total_cases': 0, 'total_runs': 0, 'run_counts': {},
            'created_at': execution.created_at, 'completed_at': timezone.now(),
        }
        with patch('botend.dashboard.api.cancel_execution', side_effect=persist_cancel) as cancel, \
                patch('botend.dashboard.api.summarize_execution',
                      return_value=cancelled_summary):
            response = self.client.post(
                f'/api/simc-benchmarks/executions/{execution.id}/cancel/',
                data='{}', content_type='application/json',
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.json()['success'])
        self.assertEqual(response.json()['data']['status'], 'cancelled')
        called_execution = cancel.call_args.args[0]
        self.assertEqual(called_execution.pk, execution.pk)
        self.assertEqual(cancel.call_args.kwargs, {'requested_by': self.staff})

    def test_execution_detail_exposes_full_panel_result_coverage_separately_from_execution_scale(self):
        """Execution #6/#7 的局部快照不能遮蔽 Panel 的完整 96×5031 聚合面。"""
        panel = self._create_panel()
        snapshot = {'version': 2, 'case_count': 65, 'run_count': 3407}
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot=snapshot,
            config_hash=hashlib.sha256(json.dumps(
                snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest(),
            status='partial', completed_at=timezone.now(),
        )
        coverage = {
            'coordinates': 96,
            'candidate_runs': 5031,
            'available_results': 4342,
            'missing_results': 689,
            'source_executions': [
                {'execution_id': 4, 'results': 1511},
                {'execution_id': 6, 'results': 2831},
            ],
        }
        with patch(
            'botend.dashboard.api.summarize_incremental_panel_coverage',
            return_value=coverage,
        ) as summarize_coverage:
            response = self.client.get(f'/api/simc-benchmarks/executions/{execution.id}/')

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['data']['total_cases'], 65)
        self.assertEqual(response.json()['data']['panel_coverage'], coverage)
        summarize_coverage.assert_called_once_with(panel)

    def test_panel_list_does_not_project_results_for_the_configuration_page(self):
        panel = self._create_panel()
        aggregate = {
            'panel_id': panel.id,
            'coordinates': [{
                'spec_key': 'warrior_fury', 'scenario_key': 'patchwerk',
                'profile_key': str(self.profile.id),
                'labels': {'spec': 'Fury', 'scenario': 'Patchwerk', 'profile': 'Raid'},
                'candidates': [
                    {'key': 'baseline', 'dps': 1234.5, 'task_id': 101},
                    {'key': 'new-trinket', 'dps': 1400.0, 'task_id': 102},
                ],
            }],
        }
        with patch(
            'botend.dashboard.api.serialize_incremental_panel_results',
            return_value=aggregate,
        ) as serialize:
            response = self.client.get('/api/simc-benchmarks/panels/')
        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['data'][0]
        self.assertNotIn('aggregated_results', row)
        serialize.assert_not_called()

    def test_panel_lists_count_only_results_reusable_by_current_config(self):
        panel = self._create_panel()
        panel.is_public = True
        panel.save(update_fields=['is_public'])
        coordinate = build_execution_plan(
            panel, validate_for_execution=False, lock=False,
        )['cases'][0]
        stale_candidate = {
            'candidate_key': 'removed-candidate',
            'candidate_label': 'Removed candidate',
            'candidate_type': 'gear_swap',
            'candidate_params': {
                'candidate_type': 'gear_swap',
                'gear_swap': {
                    'slot': 'trinket1', 'item_id': 999999,
                    'raw_value': 'id=999999,ilevel=289',
                },
            },
        }
        task = SimcTask.objects.create(
            user_id=self.staff.id, name='historical-result-count',
            simc_profile_id=self.profile.id, profile=self.profile,
            apl=self.apl, template=self.template, backend=self.backend,
            mode='comparison', current_status=2,
            simulation_params=coordinate['simulation_params'],
            mode_params={'request_manifest': {
                'candidates': coordinate['candidates'] + [stale_candidate],
            }},
            ext='{}',
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status='success',
            config_snapshot={'version': 2, 'case_count': 1, 'run_count': 1},
            config_hash='f' * 64, completed_at=timezone.now(),
        )
        case = SimcBenchmarkCase.objects.create(
            execution=execution, task=task, status='success',
            spec_key=coordinate['spec_key'], scenario_key=coordinate['scenario_key'],
            profile_key=coordinate['profile_key'], spec_label=coordinate['spec_label'],
            scenario_label=coordinate['scenario_label'],
            profile_label=coordinate['profile_label'], coordinate_hash='f' * 64,
        )
        SimcBenchmarkResult.objects.create(
            case=case, candidate_key='baseline', dps=1000,
        )
        SimcBenchmarkResult.objects.create(
            case=case, candidate_key='removed-candidate', dps=1100,
        )
        panel.aggregate_baseline_execution = execution
        panel.save(update_fields=['aggregate_baseline_execution'])

        dashboard_response = self.client.get('/api/simc-benchmarks/panels/')
        portal_response = self.client.get('/portal/api/simc-benchmarks/panels/')

        self.assertEqual(dashboard_response.status_code, 200, dashboard_response.content)
        self.assertEqual(portal_response.status_code, 200, portal_response.content)
        dashboard_panel = dashboard_response.json()['data'][0]
        portal_panel = next(
            row for row in portal_response.json()['panels'] if row['id'] == panel.id
        )
        self.assertEqual(dashboard_panel['panel_coverage']['available_results'], 1)
        self.assertEqual(portal_panel['result_count'], 1)

    def test_panel_list_exposes_current_plan_growth_separately_from_aggregate_baseline(self):
        panel = self._create_panel()
        SimcBenchmarkExecution.objects.create(
            panel=panel,
            config_snapshot={'version': 2, 'case_count': 1, 'run_count': 1},
            config_hash='a' * 64,
            status='partial',
            completed_at=timezone.now(),
        )
        updated = dict(self.payload)
        updated['candidates'] = [{
            'key': 'new-item-level',
            'label': 'New item level',
            'candidate_type': 'gear_swap',
            'params': {'slot': 'trinket1', 'raw_value': 'trinket1=id=249343,ilevel=289'},
            'spec_keys': ['warrior_fury'],
        }]
        response = self._json(
            self.client, 'put', f'/api/simc-benchmarks/panels/{panel.id}/', updated,
        )
        self.assertEqual(response.status_code, 200, response.content)

        response = self.client.get('/api/simc-benchmarks/panels/')
        self.assertEqual(response.status_code, 200, response.content)
        coverage = response.json()['data'][0]['panel_coverage']
        self.assertEqual(coverage['candidate_runs'], 1)
        self.assertEqual(coverage['current_plan_runs'], 2)
        self.assertEqual(coverage['plan_delta_runs'], 1)

    def test_panel_list_keeps_full_coverage_when_active_supplement_owns_every_coordinate(self):
        """A supplement can contain 96 Case Tasks but only the missing candidate Runs."""
        panel = self._create_panel()
        full_snapshot = {'version': 2, 'case_count': 2, 'run_count': 4}
        full = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot=full_snapshot, config_hash='a' * 64,
            status='partial', completed_at=timezone.now(),
        )
        for index, coordinate_hash in enumerate(('a' * 64, 'b' * 64)):
            case = SimcBenchmarkCase.objects.create(
                execution=full, status='success', spec_key='warrior_fury',
                scenario_key=f'full-{index}', profile_key=str(index), spec_label='Fury',
                scenario_label=f'Full {index}', profile_label='Raid',
                coordinate_hash=coordinate_hash,
            )
            for key in ('baseline', 'candidate'):
                SimcBenchmarkResult.objects.create(
                    case=case, candidate_key=key, dps=1000 + index,
                )

        supplement_snapshot = {'version': 2, 'case_count': 2, 'run_count': 2, 'execution_mode': 'supplement'}
        supplement = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot=supplement_snapshot, config_hash='b' * 64,
            status='running',
        )
        panel.active_execution = supplement
        panel.save(update_fields=['active_execution'])
        for index, coordinate_hash in enumerate(('a' * 64, 'b' * 64)):
            task = SimcTask.objects.create(
                user_id=self.staff.id, name=f'supplement-{index}', mode='comparison',
                simc_profile_id=self.profile.id, backend=self.backend, current_status=0,
                ext='{}',
            )
            SimulationRun.objects.create(task=task, sequence=1, status='pending')
            SimcBenchmarkCase.objects.create(
                execution=supplement, task=task, status='pending', spec_key='warrior_fury',
                scenario_key=f'supplement-{index}', profile_key=str(index), spec_label='Fury',
                scenario_label=f'Supplement {index}', profile_label='Raid',
                coordinate_hash=coordinate_hash,
            )

        with patch('botend.dashboard.api.summarize_incremental_panel_coverage') as summarize:
            response = self.client.get('/api/simc-benchmarks/panels/')
        self.assertEqual(response.status_code, 200)
        row = response.json()['data'][0]
        self.assertEqual(row['aggregate_baseline_execution_id'], None)
        self.assertEqual(row['execution']['case_count'], 2)
        self.assertEqual(row['execution']['total_runs'], 2)
        self.assertEqual(row['panel_coverage'], {
            'aggregate_baseline_execution_id': full.id,
            'coordinates': 2,
            'candidate_runs': 4,
            'current_plan_runs': 1,
            'plan_delta_runs': -3,
            'available_results': 4,
            'missing_results': 0,
            'source_executions': [],
        })
        summarize.assert_not_called()

    def test_panel_list_and_history_expose_execution_progress_and_metadata_readiness(self):
        panel = self._create_panel()
        # Execution owns four Case Tasks, but the Worker materializes each
        # comparison candidate Run lazily.  The frozen supplement workload must
        # remain visible before all planned Runs have database rows.
        snapshot = {'version': 2, 'case_count': 4, 'run_count': 9}
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel,
            config_snapshot=snapshot,
            config_hash=hashlib.sha256(json.dumps(
                snapshot, sort_keys=True, separators=(',', ':'), ensure_ascii=False,
            ).encode()).hexdigest(),
            status='running',
        )
        panel.active_execution = execution
        panel.save(update_fields=['active_execution'])
        fixtures = (
            ('success', 2, '{}'),
            ('failed', 3, '{}'),
            ('running', 1, json.dumps({'progress': 37})),
            ('pending', 0, '{}'),
        )
        task_ids = []
        for index, (case_status, task_status, ext) in enumerate(fixtures):
            task = SimcTask.objects.create(
                user_id=self.staff.id,
                name=f'benchmark-progress-{index}',
                mode='comparison',
                simc_profile_id=self.profile.id,
                backend=self.backend,
                current_status=task_status,
                ext=ext,
            )
            task_ids.append(task.id)
            SimcBenchmarkCase.objects.create(
                execution=execution,
                task=task,
                status=case_status,
                error_detail=(
                    "Player 'MID2_Rogue_Outlaw' attempting to use Action 'dispatch' "
                    "with invalid main-hand weapon type 'Dagger'."
                    if case_status == 'failed' else ''
                ),
                spec_key='warrior_fury',
                scenario_key=f'scenario-{index}',
                profile_key=str(index),
                spec_label='Fury',
                scenario_label=f'Scenario {index}',
                profile_label=f'Profile {index}',
                coordinate_hash=f'{index + 1:064x}',
            )
            SimulationRun.objects.create(
                task=task, sequence=1,
                status=('completed' if index == 0 else ('failed' if index == 1 else ('running' if index == 2 else 'pending'))),
            )

        report_artifact = SimcTaskArtifact.objects.create(
            task_id=task_ids[1],
            run=SimulationRun.objects.get(task_id=task_ids[1]),
            artifact_type='html_report',
            file_path='simc_reports/benchmark-progress-failed.html',
        )

        response = self.client.get('/api/simc-benchmarks/panels/')
        self.assertEqual(response.status_code, 200)
        progress = response.json()['data'][0]['execution']
        self.assertEqual(progress['id'], execution.id)
        self.assertTrue(progress['is_active'])
        self.assertEqual(progress['progress'], 59)
        self.assertEqual(progress['counts'], {
            'pending': 1, 'running': 1, 'success': 1,
            'partial': 0, 'failed': 1, 'cancelled': 0,
        })
        self.assertEqual(progress['current_cases'], [{
            'task_id': task_ids[2],
            'spec': 'Fury', 'scenario': 'Scenario 2', 'profile': 'Profile 2',
            'progress': 37,
        }])
        self.assertEqual(progress['run_counts'], {
            'pending': 1, 'running': 1, 'success': 1,
            'failed': 1, 'cancelled': 0,
        })
        self.assertEqual(progress['total_runs'], 9)
        self.assertEqual(progress['materialized_runs'], 4)
        self.assertEqual(progress['failures'], [{
            'case_id': execution.cases.get(status='failed').id,
            'task_id': task_ids[1],
            'labels': {
                'spec': 'Fury', 'scenario': 'Scenario 1', 'profile': 'Profile 1',
            },
            'error': (
                "Player 'MID2_Rogue_Outlaw' attempting to use Action 'dispatch' "
                "with invalid main-hand weapon type 'Dagger'."
            ),
            'report_url': f'/api/simc-workbench/artifacts/{report_artifact.id}/preview/',
            'detail_url': f'/dashboard/simc/benchmarks/executions/{execution.id}/',
        }])
        coverage = response.json()['data'][0]['panel_coverage']
        self.assertEqual(coverage, {
            'aggregate_baseline_execution_id': execution.id,
            'coordinates': 4,
            'candidate_runs': 9,
            'current_plan_runs': 1,
            'plan_delta_runs': -8,
            'available_results': 0,
            'missing_results': 9,
            'source_executions': [],
        })
        self.assertEqual(progress['metadata'], {
            'config_frozen': True,
            'task_bindings': 4,
            'task_total': 4,
            'results_available': False,
        })

        response = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/executions/',
        )
        history = response.json()['data']['items'][0]
        self.assertEqual(history['progress'], 59)
        self.assertEqual(history['counts']['running'], 1)
        self.assertEqual(history['metadata']['task_bindings'], 4)
        self.assertEqual(history['failures'], progress['failures'])

    def test_execution_progress_counts_missing_cases_without_claiming_aggregate_available(self):
        panel = self._create_panel()
        now = timezone.now()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel,
            config_snapshot={'version': 2, 'case_count': 3, 'run_count': 1},
            config_hash='c' * 64,
            status='success',
            completed_at=now,
            results_finalized_at=now,
            result_hash='d' * 64,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution,
            status='success',
            spec_key='warrior_fury',
            scenario_key='patchwerk',
            profile_key=str(self.profile.id),
            spec_label='Fury',
            scenario_label='Patchwerk',
            profile_label='Raid',
            coordinate_hash='e' * 64,
        )

        panel_response = self.client.get('/api/simc-benchmarks/panels/')
        panel_progress = panel_response.json()['data'][0]['execution']
        self.assertEqual(panel_progress['case_count'], 3)
        self.assertEqual(panel_progress['progress'], 33)
        self.assertEqual(panel_progress['counts'], {
            'pending': 2, 'running': 0, 'success': 1,
            'partial': 0, 'failed': 0, 'cancelled': 0,
        })
        self.assertEqual(panel_progress['metadata'], {
            'config_frozen': False,
            'task_bindings': 0,
            'task_total': 3,
            'results_available': False,
        })

        history_response = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/executions/',
        )
        history = history_response.json()['data']['items'][0]
        self.assertEqual(history['case_count'], 3)
        self.assertEqual(history['progress'], 33)
        self.assertEqual(history['counts']['pending'], 2)
        self.assertFalse(history['metadata']['config_frozen'])
        self.assertFalse(history['metadata']['results_available'])

    def test_body_must_be_valid_json_object_and_errors_are_stable(self):
        for body in (' ', '[]', '{broken'):
            with self.subTest(body=body):
                response = self.client.post(
                    '/api/simc-benchmarks/panels/', data=body,
                    content_type='application/json',
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(response.json()['error'], 'validation_error')
                self.assertIn('body', response.json()['fields'])

        with patch(
            'botend.dashboard.api.replace_panel_config',
            side_effect=RuntimeError('/srv/private/config traceback secret'),
        ):
            response = self._json(
                self.client, 'post', '/api/simc-benchmarks/panels/', self.payload,
            )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {'success': False, 'error': 'internal_error'})
        self.assertNotIn('/srv/private', response.content.decode())

    def test_json_writes_require_json_media_type_and_empty_command_object(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='JSON contract', slug='json-contract', created_by_id=self.staff.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={}, config_hash='8' * 64,
        )
        writes = (
            ('post', '/api/simc-benchmarks/panels/', json.dumps(self.payload)),
            ('put', f'/api/simc-benchmarks/panels/{panel.id}/', json.dumps(self.payload)),
            ('post', f'/api/simc-benchmarks/panels/{panel.id}/run/', '{}'),
            ('post', f'/api/simc-benchmarks/executions/{execution.id}/reconcile/', '{}'),
        )
        for method, path, body in writes:
            with self.subTest(method=method, path=path):
                response = getattr(self.client, method)(
                    path, data=body, content_type='text/plain',
                )
                self.assertEqual(response.status_code, 415)
                self.assertEqual(response.json(), {
                    'success': False, 'error': 'unsupported_media_type',
                })

        # Media type parameters are accepted.
        with patch('botend.dashboard.api.create_execution', return_value=execution) as create:
            response = self.client.post(
                f'/api/simc-benchmarks/panels/{panel.id}/run/', data='{}',
                content_type='application/json; charset=utf-8',
            )
        self.assertNotEqual(response.status_code, 415)
        create.assert_called_once()

        for path in (
            f'/api/simc-benchmarks/panels/{panel.id}/run/',
            f'/api/simc-benchmarks/executions/{execution.id}/reconcile/',
        ):
            for body in (' ', '{"unexpected": true}'):
                with self.subTest(path=path, body=body):
                    response = self.client.post(
                        path, data=body, content_type='application/json',
                    )
                    self.assertEqual(response.status_code, 400)
                    expected = ('unknown_fields' if body.startswith('{') else 'validation_error')
                    self.assertEqual(response.json()['error'], expected)

    def test_panel_metadata_patch_does_not_accept_task_matrix_fields(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='Metadata', slug='metadata-contract', created_by_id=self.staff.id,
        )
        response = self.client.patch(
            f'/api/simc-benchmarks/panels/{panel.id}/',
            data='{"name":"Renamed","specs":[]}', content_type='application/json',
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['error'], 'unknown_fields')
        panel.refresh_from_db()
        self.assertEqual(panel.name, 'Metadata')

    def test_method_not_allowed_is_json_and_preserves_allow_header(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='Methods', slug='method-contract', created_by_id=self.staff.id,
        )
        detail = self.client.patch(
            f'/api/simc-benchmarks/panels/{panel.id}/', data='{}',
            content_type='application/json',
        )
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()['success'], True)

        run = self.client.get(f'/api/simc-benchmarks/panels/{panel.id}/run/')
        self.assertEqual(run.status_code, 405)
        self.assertEqual(run.json(), {
            'success': False, 'error': 'method_not_allowed',
        })
        self.assertEqual(set(run['Allow'].split(', ')), {'POST', 'OPTIONS'})

    def test_physical_delete_removes_config_execution_and_case_but_keeps_task(self):
        panel = self._create_panel()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={}, config_hash='a' * 64,
        )
        task = SimcTask.objects.create(
            user_id=self.staff.id, name='surviving task', mode='comparison',
            simc_profile_id=self.profile.id, backend=self.backend,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=task, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key=str(self.profile.id),
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Raid',
            coordinate_hash='b' * 64,
        )
        response = self.client.delete(f'/api/simc-benchmarks/panels/{panel.id}/')
        self.assertEqual(response.status_code, 204)
        self.assertFalse(SimcBenchmarkPanel.objects.filter(pk=panel.id).exists())
        self.assertFalse(SimcBenchmarkExecution.objects.filter(pk=execution.id).exists())
        self.assertTrue(SimcTask.objects.filter(pk=task.id).exists())

    def test_manual_run_is_async_active_only_and_maps_conflict(self):
        panel = self._create_panel()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={'case_count': 2, 'run_count': 3},
            config_hash='c' * 64,
        )
        with patch('botend.dashboard.api.create_execution', return_value=execution) as mocked:
            response = self._json(
                self.client, 'post',
                f'/api/simc-benchmarks/panels/{panel.id}/run/', {},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['data']['case_count'], 2)
        self.assertEqual(response.json()['data']['run_count'], 3)
        mocked.assert_called_once_with(
            panel, requested_by=self.staff, execution_mode='supplement',
        )

        with patch('botend.dashboard.api.create_execution', return_value=execution) as mocked:
            response = self._json(
                self.client, 'post',
                f'/api/simc-benchmarks/panels/{panel.id}/run/', {'mode': 'full'},
            )
        self.assertEqual(response.status_code, 202)
        mocked.assert_called_once_with(
            panel, requested_by=self.staff, execution_mode='full',
        )

        with patch('botend.dashboard.api.create_execution',
                   side_effect=BenchmarkExecutionConflict('private drift')):
            response = self._json(
                self.client, 'post',
                f'/api/simc-benchmarks/panels/{panel.id}/run/', {},
            )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['error'], 'execution_conflict')

        panel.is_active = False
        panel.save(update_fields=['is_active'])
        response = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/run/', {},
        )
        self.assertEqual(response.status_code, 400)

    def test_execution_pagination_is_panel_scoped_ordered_and_capped(self):
        panel = self._create_panel()
        other = SimcBenchmarkPanel.objects.create(
            name='Other', slug='other-dashboard-panel', created_by_id=self.staff.id,
        )
        SimcBenchmarkExecution.objects.create(
            panel=other, config_snapshot={}, config_hash='e' * 64,
        )
        for index in range(55):
            SimcBenchmarkExecution.objects.create(
                panel=panel,
                config_snapshot={'case_count': index, 'run_count': index + 1},
                config_hash=f'{index:064x}',
                status=('failed' if index % 2 else 'cancelled'),
                completed_at=timezone.now(),
            )
        response = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/executions/?page=1&size=100',
        )
        data = response.json()['data']
        self.assertEqual(response.status_code, 200)
        self.assertEqual(data['pagination']['size'], 50)
        self.assertEqual(data['pagination']['total'], 55)
        self.assertEqual(len(data['items']), 50)
        ids = [row['id'] for row in data['items']]
        self.assertEqual(ids, sorted(ids, reverse=True))
        self.assertTrue(all(row['panel_id'] == panel.id for row in data['items']))
        self.assertEqual({row['status'] for row in data['items']}, {'failed', 'cancelled'})

    def test_detail_and_reconcile_use_safe_summary_projection(self):
        panel = self._create_panel()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={'secret': '/tmp/raw.simc'},
            config_hash='f' * 64,
        )
        summary = {
            'id': execution.id, 'status': 'failed',
            'created_at': execution.created_at, 'completed_at': timezone.now(),
            'total_cases': 1, 'total_runs': 1, 'pending': 0, 'running': 0,
            'success': 0, 'partial': 0, 'failed': 1, 'cancelled': 0,
            'run_counts': {'pending': 0, 'failed': 1, 'secret': 999},
            'top_secret': '/srv/top-secret', 'cases': [{
                'spec_key': 'warrior_fury', 'scenario_key': 'patchwerk',
                'profile_key': '1',
                'labels': {'spec': 'Fury', 'scenario': 'Patchwerk', 'profile': 'Raid',
                           'secret': '/srv/label-secret'},
                'status': 'failed', 'task_id': 123, 'task_status': 'failed',
                'error': '/srv/private/input.simc traceback secret',
                'runs': [{
                    'key': 'baseline', 'label': 'Baseline', 'status': 'failed',
                    'dps': None, 'error': '/tmp/result.simc traceback secret',
                    'secret': '/srv/run-secret',
                }], 'mode_params': {'secret': True},
            }],
        }
        with patch('botend.dashboard.api.summarize_execution', return_value=summary):
            response = self.client.get(
                f'/api/simc-benchmarks/executions/{execution.id}/',
            )
        serialized = response.content.decode()
        self.assertEqual(response.status_code, 200)
        data = response.json()['data']
        case = data['cases'][0]
        self.assertEqual(set(case), {
            'coordinate', 'labels', 'status', 'task_id', 'task_status',
            'task_status_label', 'task_progress', 'error', 'runs',
        })
        self.assertEqual(set(case['coordinate']), {
            'spec_key', 'scenario_key', 'profile_key',
        })
        self.assertEqual(set(case['labels']), {'spec', 'scenario', 'profile'})
        self.assertEqual(set(case['runs'][0]), {
            'key', 'label', 'status', 'dps', 'error',
        })
        self.assertEqual(set(data['run_counts']), {
            'pending', 'running', 'success', 'failed', 'cancelled',
        })
        self.assertEqual(case['task_status'], 'failed')
        self.assertIsNone(case['task_status_label'])
        self.assertIsNone(case['task_progress'])
        for forbidden in (
            'config_snapshot', 'mode_params', 'top_secret',
            '/tmp/raw.simc', '/srv/', '/tmp/', 'traceback', 'secret',
        ):
            self.assertNotIn(forbidden, serialized)

        with patch('botend.dashboard.api.reconcile_execution', return_value=execution) as reconcile, \
                patch('botend.dashboard.api.summarize_execution', return_value=summary) as summarize:
            response = self._json(
                self.client, 'post',
                f'/api/simc-benchmarks/executions/{execution.id}/reconcile/', {},
            )
        self.assertEqual(response.status_code, 200)
        reconcile.assert_called_once()
        summarize.assert_called_once()

    def test_all_benchmark_writes_require_csrf(self):
        panel = SimcBenchmarkPanel.objects.create(
            name='CSRF', slug='csrf-benchmark', created_by_id=self.staff.id,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_snapshot={}, config_hash='9' * 64,
        )
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.staff)
        for method, path in (
            ('post', '/api/simc-benchmarks/panels/'),
            ('put', f'/api/simc-benchmarks/panels/{panel.id}/'),
            ('delete', f'/api/simc-benchmarks/panels/{panel.id}/'),
            ('post', f'/api/simc-benchmarks/panels/{panel.id}/run/'),
            ('post', f'/api/simc-benchmarks/executions/{execution.id}/reconcile/'),
        ):
            with self.subTest(method=method):
                response = getattr(client, method)(
                    path, data=json.dumps({}), content_type='application/json',
                )
                self.assertEqual(response.status_code, 403)
        self.assertTrue(SimcBenchmarkPanel.objects.filter(pk=panel.id).exists())
