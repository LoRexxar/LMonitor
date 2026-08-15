import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from botend.models import (
    DashboardUserGroup,
    DashboardUserGroupMembership,
    SimcBackendBinary,
    SimcBenchmarkCase,
    SimcBenchmarkExecution,
    SimcBenchmarkPanel,
    SimcBenchmarkPurgeTask,
    SimcBenchmarkResult,
    SimcTask,
    SimcTaskArtifact,
    SimcTaskFavorite,
    SimulationRun,
)
from botend.services import simc_benchmark_purge as purge_service
from botend.services.simc_benchmark_purge import process_next_purge, queue_panel_purge


class SimcBenchmarkPanelPurgeTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username='benchmark-purge-staff', password='password', is_staff=True,
        )
        group = DashboardUserGroup.objects.create(
            name='Benchmark purge group', permission_codes=['simc.benchmarks'],
        )
        self.group = group
        DashboardUserGroupMembership.objects.create(user=self.staff, group=group)
        self.backend = SimcBackendBinary.objects.create(
            identifier='benchmark-purge', name='Benchmark purge', is_active=True,
        )
        self.client.force_login(self.staff)

    @staticmethod
    def _json(client, method, path, payload):
        return getattr(client, method)(
            path, data=json.dumps(payload), content_type='application/json',
        )

    def _graph(self, *, task_status=2):
        panel = SimcBenchmarkPanel.objects.create(
            name='Purge me', slug=f'purge-me-{SimcBenchmarkPanel.objects.count()}', created_by_id=self.staff.id,
            is_active=True, schedule_enabled=True,
        )
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='a' * 64,
            status=(SimcBenchmarkExecution.STATUS_RUNNING if task_status in (0, 1)
                    else SimcBenchmarkExecution.STATUS_SUCCESS),
        )
        task = SimcTask.objects.create(
            user_id=self.staff.id, name='owned benchmark task', simc_profile_id=1,
            backend=self.backend, mode='comparison', current_status=task_status,
        )
        run = SimulationRun.objects.create(
            task=task, sequence=1,
            status=('running' if task_status == 1 else 'completed'),
            result_summary={'dps': 100},
        )
        unique_artifact = SimcTaskArtifact.objects.create(
            task=task, run=run, artifact_type='html_report',
            file_path=f'simc_agent_results/simc_task_{task.id}_run_{run.id}.html',
            file_size=100,
        )
        shared_artifact = SimcTaskArtifact.objects.create(
            task=task, artifact_type='html_report',
            file_path='simc_results/shared-report.html', file_size=200,
        )
        case = SimcBenchmarkCase.objects.create(
            execution=execution, task=task, status=execution.status,
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='1',
            spec_label='Fury', scenario_label='Patchwerk', profile_label='Raid',
            coordinate_hash='b' * 64,
        )
        SimcBenchmarkResult.objects.create(case=case, candidate_key='baseline', dps=100)
        SimcTaskFavorite.objects.create(user=self.staff, task=task)

        retained = SimcTask.objects.create(
            user_id=self.staff.id, name='ordinary retained rerun', simc_profile_id=1,
            backend=self.backend, mode='normal', current_status=2, source_task=task,
        )
        retained_run = SimulationRun.objects.create(
            task=retained, sequence=1, status='completed', result_summary={'dps': 99},
        )
        SimcTaskArtifact.objects.create(
            task=retained, run=retained_run, artifact_type='html_report',
            file_path=shared_artifact.file_path, file_size=200,
        )
        return panel, execution, task, run, case, unique_artifact, retained

    def test_preview_fingerprint_confirmation_and_complete_background_purge(self):
        panel, execution, task, run, case, unique_artifact, retained = self._graph()
        preview = self.client.get(f'/api/simc-benchmarks/panels/{panel.id}/purge/')
        self.assertEqual(preview.status_code, 200, preview.content)
        data = preview.json()['data']
        self.assertRegex(data['fingerprint'], r'^[0-9a-f]{64}$')
        self.assertEqual(data['counts'], {
            'panels': 1, 'executions': 1, 'cases': 1, 'results': 1,
            'tasks': 1, 'runs': 1, 'artifacts': 2, 'favorites': 1,
            'oss_objects': 1, 'retained_reruns_detached': 1,
        })

        stale = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': '0' * 64},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertTrue(SimcBenchmarkPanel.objects.filter(pk=panel.id).exists())

        queued = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': data['fingerprint']},
        )
        self.assertEqual(queued.status_code, 202, queued.content)
        job = SimcBenchmarkPurgeTask.objects.get(panel_id=panel.id)
        panel.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_PENDING)
        self.assertFalse(panel.is_active)
        self.assertFalse(panel.schedule_enabled)

        oss = Mock()
        quarantine_map = {
            unique_artifact.file_path: {
                'quarantine_key': f'simc_benchmark_cleanup/quarantine/{job.batch_id}/{unique_artifact.file_path}',
                'source': {}, 'quarantine': {},
            },
        }
        oss._quarantine_objects.return_value = quarantine_map
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss):
            self.assertTrue(process_next_purge())
            job.refresh_from_db()
            self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING)
            self.assertTrue(process_next_purge())

        job.refresh_from_db()
        retained.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_SUCCEEDED)
        self.assertIsNone(job.panel_id)
        self.assertFalse(SimcBenchmarkPanel.objects.filter(pk=panel.id).exists())
        self.assertFalse(SimcBenchmarkExecution.objects.filter(pk=execution.id).exists())
        self.assertFalse(SimcBenchmarkCase.objects.filter(pk=case.id).exists())
        self.assertFalse(SimcTask.objects.filter(pk=task.id).exists())
        self.assertFalse(SimulationRun.objects.filter(pk=run.id).exists())
        self.assertFalse(SimcTaskArtifact.objects.filter(pk=unique_artifact.id).exists())
        self.assertTrue(SimcTask.objects.filter(pk=retained.id).exists())
        self.assertIsNone(retained.source_task_id)
        self.assertEqual(
            SimcTaskArtifact.objects.get(task=retained).file_path,
            'simc_results/shared-report.html',
        )
        oss._quarantine_objects.assert_called_once_with(
            (unique_artifact.file_path,), job.batch_id,
        )
        oss._delete_objects.assert_called_once_with(quarantine_map)
        oss._purge_quarantine_objects.assert_called_once_with(quarantine_map)
        oss._restore_objects.assert_not_called()

    def test_preview_blocks_while_panel_has_pending_or_running_work(self):
        for status in (0, 1):
            with self.subTest(status=status):
                panel, *_ = self._graph(task_status=status)
                response = self.client.get(
                    f'/api/simc-benchmarks/panels/{panel.id}/purge/',
                )
                self.assertEqual(response.status_code, 409)
                self.assertEqual(response.json()['error'], 'purge_blocked')
                self.assertTrue(response.json()['data']['active_task_ids'])
                self.assertTrue(response.json()['data']['active_execution_ids'])

    def test_stale_running_job_recovers_quarantine_evidence_and_resumes(self):
        panel, _execution, _task, _run, _case, unique_artifact, _retained = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        job = SimcBenchmarkPurgeTask.objects.get(panel_id=panel.id)
        SimcBenchmarkPurgeTask.objects.filter(pk=job.id).update(
            status=SimcBenchmarkPurgeTask.STATUS_RUNNING,
            attempts=1,
            updated_at=timezone.now() - timedelta(hours=7),
        )
        quarantine_map = {
            unique_artifact.file_path: {
                'quarantine_key': f'simc_benchmark_cleanup/quarantine/{job.batch_id}/{unique_artifact.file_path}',
                'source': {}, 'quarantine': {},
            },
        }
        oss = Mock()
        oss._recover_quarantine_objects.return_value = quarantine_map
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss):
            self.assertTrue(process_next_purge())
            self.assertTrue(process_next_purge())

        job.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_SUCCEEDED)
        self.assertEqual(job.attempts, 2)
        oss._recover_quarantine_objects.assert_called_once_with(
            (unique_artifact.file_path,), job.batch_id,
        )
        oss._quarantine_objects.assert_not_called()
        oss._delete_objects.assert_called_once_with(quarantine_map)

    def test_reference_created_after_database_commit_retains_recovery_copy(self):
        panel, _execution, _task, _run, _case, unique_artifact, _retained = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        job = SimcBenchmarkPurgeTask.objects.get(panel_id=panel.id)
        quarantine_map = {
            unique_artifact.file_path: {
                'quarantine_key': f'simc_benchmark_cleanup/quarantine/{job.batch_id}/{unique_artifact.file_path}',
                'source': {}, 'quarantine': {},
            },
        }
        oss = Mock()
        oss._quarantine_objects.return_value = quarantine_map
        real_delete_database = purge_service._delete_database

        def delete_then_reference(job_id, claim_token):
            real_delete_database(job_id, claim_token)
            late_task = SimcTask.objects.create(
                user_id=self.staff.id, name='late report reference', simc_profile_id=1,
                backend=self.backend, mode='normal', current_status=2,
            )
            SimcTaskArtifact.objects.create(
                task=late_task, artifact_type='html_report',
                file_path=unique_artifact.file_path, file_size=100,
            )

        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss), \
                patch('botend.services.simc_benchmark_purge._delete_database',
                      side_effect=delete_then_reference):
            self.assertTrue(process_next_purge())
            self.assertTrue(process_next_purge())

        job.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING)
        self.assertEqual(job.quarantine_map, quarantine_map)
        oss._restore_objects.assert_called_once_with(quarantine_map)
        oss._delete_objects.assert_not_called()
        oss._purge_quarantine_objects.assert_not_called()

    def test_cleanup_losing_claim_after_source_delete_preserves_quarantine(self):
        panel, _execution, _task, _run, _case, unique_artifact, _retained = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        job = queue_panel_purge(panel.id, preview['fingerprint'], self.staff.id)
        quarantine_map = {
            unique_artifact.file_path: {
                'quarantine_key': f'simc_benchmark_cleanup/quarantine/{job.batch_id}/report.html',
                'source': {}, 'quarantine': {},
            },
        }
        oss = Mock()
        oss._quarantine_objects.return_value = quarantine_map
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss):
            self.assertTrue(process_next_purge())

        def lose_claim_after_delete(_quarantine_map):
            late_task = SimcTask.objects.create(
                user_id=self.staff.id, name='late cleanup owner', simc_profile_id=1,
                backend=self.backend, mode='normal', current_status=2,
            )
            SimcTaskArtifact.objects.create(
                task=late_task, artifact_type='html_report',
                file_path=unique_artifact.file_path, file_size=100,
            )
            SimcBenchmarkPurgeTask.objects.filter(pk=job.id).update(
                status=SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING,
                claim_token='new-worker-token',
                claimed_at=timezone.now(),
            )

        oss._delete_objects.side_effect = lose_claim_after_delete
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss):
            self.assertTrue(process_next_purge())

        job.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_CLEANUP_PENDING)
        self.assertEqual(job.claim_token, 'new-worker-token')
        self.assertEqual(job.quarantine_map, quarantine_map)
        oss._purge_quarantine_objects.assert_not_called()

    def test_dashboard_member_without_staff_privilege_cannot_preview_or_queue_purge(self):
        panel, *_ = self._graph()
        member = User.objects.create_user('benchmark-viewer', password='password')
        DashboardUserGroupMembership.objects.create(user=member, group=self.group)
        self.client.force_login(member)
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        )
        self.assertEqual(preview.status_code, 403)
        queued = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': '0' * 64},
        )
        self.assertEqual(queued.status_code, 403)

    def test_confirmation_rejects_config_drift_and_malformed_fingerprint(self):
        panel, *_ = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        panel.description = 'mutated after preview'
        panel.save(update_fields=['description', 'updated_at'])

        drift = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        self.assertEqual(drift.status_code, 409)
        malformed = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': 'not-a-sha256'},
        )
        self.assertEqual(malformed.status_code, 400)
        self.assertFalse(SimcBenchmarkPurgeTask.objects.filter(panel=panel).exists())

    def test_preview_blocks_active_simulation_run_even_when_task_is_terminal(self):
        panel, _execution, _task, run, *_ = self._graph()
        run.status = 'running'
        run.save(update_fields=['status'])
        response = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['data']['active_run_ids'], [run.id])

    def test_non_comparison_source_ancestor_is_not_owned_by_panel(self):
        panel, _execution, task, *_ = self._graph()
        source = SimcTask.objects.create(
            user_id=self.staff.id, name='ordinary source', simc_profile_id=1,
            backend=self.backend, mode='normal', current_status=2,
        )
        task.source_task = source
        task.save(update_fields=['source_task'])

        response = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        )
        self.assertEqual(response.status_code, 200, response.content)
        preview = response.json()['data']
        self.assertEqual(preview['counts']['tasks'], 1)
        job = queue_panel_purge(panel.id, preview['fingerprint'], self.staff.id)
        oss = Mock()
        oss._quarantine_objects.side_effect = (
            lambda keys, batch_id: {key: {'quarantine_key': f'q/{key}'} for key in keys}
        )
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss):
            process_next_purge()
            process_next_purge()
        job.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_SUCCEEDED)
        self.assertTrue(SimcTask.objects.filter(pk=source.id).exists())

    def test_source_task_comparison_chain_is_owned_by_panel(self):
        panel, _execution, task, *_ = self._graph()
        source = SimcTask.objects.create(
            user_id=self.staff.id, name='recovered source', simc_profile_id=1,
            backend=self.backend, mode='comparison', current_status=2,
        )
        task.source_task = source
        task.save(update_fields=['source_task'])
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        self.assertEqual(preview['counts']['tasks'], 2)

    def test_active_purge_fence_covers_frozen_task_chain_and_object_keys(self):
        panel, _execution, task, _run, _case, unique_artifact, _retained = self._graph()
        source = SimcTask.objects.create(
            user_id=self.staff.id, name='frozen comparison source', simc_profile_id=1,
            backend=self.backend, mode='comparison', current_status=2,
        )
        task.source_task = source
        task.save(update_fields=['source_task'])
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        queue_panel_purge(panel.id, preview['fingerprint'], self.staff.id)

        self.assertTrue(purge_service.task_has_active_panel_purge(source.id))
        self.assertTrue(
            purge_service.artifact_key_has_active_panel_purge(unique_artifact.file_path)
        )

    def test_stale_claim_cannot_replace_owner_while_execution_lock_is_held(self):
        panel, *_ = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        job = queue_panel_purge(panel.id, preview['fingerprint'], self.staff.id)
        SimcBenchmarkPurgeTask.objects.filter(pk=job.id).update(
            status=SimcBenchmarkPurgeTask.STATUS_RUNNING,
            claim_token='old-worker-token',
            claimed_at=timezone.now() - timedelta(hours=7),
        )

        with patch.object(
            purge_service, '_try_acquire_execution_lock', return_value=False, create=True,
        ):
            self.assertIsNone(purge_service._claim_next_job())
        job.refresh_from_db()
        self.assertEqual(job.claim_token, 'old-worker-token')
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_RUNNING)

    def test_failed_attempt_is_preserved_when_a_new_attempt_is_queued(self):
        panel, *_ = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        first = SimcBenchmarkPurgeTask.objects.get(panel=panel)
        oss = Mock()
        oss._quarantine_objects.return_value = {}
        oss._recover_quarantine_objects.return_value = {}
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss), \
                patch('botend.services.simc_benchmark_purge._delete_database',
                      side_effect=RuntimeError('first attempt failed')):
            self.assertTrue(process_next_purge())
        first.refresh_from_db()
        self.assertEqual(first.status, SimcBenchmarkPurgeTask.STATUS_FAILED)

        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        queued = self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        self.assertEqual(queued.status_code, 202)
        self.assertEqual(SimcBenchmarkPurgeTask.objects.filter(panel=panel).count(), 2)
        first.refresh_from_db()
        self.assertEqual(first.status, SimcBenchmarkPurgeTask.STATUS_FAILED)
        self.assertIn('first attempt failed', first.error_detail)

    def test_restore_failure_keeps_panel_disabled_until_retry_succeeds(self):
        panel, *_ = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        job = SimcBenchmarkPurgeTask.objects.get(panel=panel)
        quarantine_map = {
            job.plan['object_keys'][0]: {
                'quarantine_key': 'simc_benchmark_cleanup/quarantine/restore/report.html',
                'source': {}, 'quarantine': {},
            },
        }
        oss = Mock()
        oss._quarantine_objects.return_value = quarantine_map
        oss._recover_quarantine_objects.return_value = {}
        oss._restore_objects.side_effect = [RuntimeError('restore unavailable'), None]
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss), \
                patch('botend.services.simc_benchmark_purge._delete_database',
                      side_effect=RuntimeError('database failed')):
            self.assertTrue(process_next_purge())
        job.refresh_from_db()
        panel.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_RESTORE_PENDING)
        self.assertFalse(panel.is_active)
        self.assertFalse(panel.schedule_enabled)

        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss):
            self.assertTrue(process_next_purge())
        job.refresh_from_db()
        panel.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_FAILED)
        self.assertTrue(panel.is_active)
        self.assertTrue(panel.schedule_enabled)

    def test_restore_is_idempotent_when_original_exists_after_partial_quarantine_cleanup(self):
        from botend.management.commands.cleanup_simc_benchmark_history import (
            OBJECT_STATE_FIELDS,
            Command,
        )
        command = Command()
        original = 'simc_task_1.html'
        batch_id = 'panel-20260815T000000Z-abcdef123456'
        state = {field: None for field in OBJECT_STATE_FIELDS}
        state.update({
            'content_length': 123,
            'etag': 'same-etag',
            'last_modified': '2026-08-15T00:00:00+00:00',
            'storage_class': 'Standard',
            'acl': 'private',
        })
        descriptor = {
            'quarantine_key': command._quarantine_key(batch_id, original),
            'source': dict(state),
            'quarantine': dict(state),
        }
        with patch(
            'botend.management.commands.cleanup_simc_benchmark_history._client',
            return_value=(Mock(), Mock(), 'bucket'),
        ), patch.object(command, '_object_snapshot', return_value=dict(state)) as snapshot:
            command._restore_objects({original: descriptor})
        snapshot.assert_called_once()
        self.assertEqual(snapshot.call_args.args[-1], original)

    def test_database_failure_restores_oss_and_panel_operational_state(self):
        panel, _execution, task, *_rest = self._graph()
        preview = self.client.get(
            f'/api/simc-benchmarks/panels/{panel.id}/purge/',
        ).json()['data']
        self._json(
            self.client, 'post', f'/api/simc-benchmarks/panels/{panel.id}/purge/',
            {'fingerprint': preview['fingerprint']},
        )
        job = SimcBenchmarkPurgeTask.objects.get(panel_id=panel.id)
        oss = Mock()
        quarantine_map = {
            job.plan['object_keys'][0]: {
                'quarantine_key': 'simc_benchmark_cleanup/quarantine/failure/report.html',
                'source': {}, 'quarantine': {},
            },
        }
        oss._quarantine_objects.return_value = quarantine_map
        oss._recover_quarantine_objects.return_value = {}
        with patch('botend.services.simc_benchmark_purge._oss_command', return_value=oss), \
                patch('botend.services.simc_benchmark_purge._delete_database',
                      side_effect=RuntimeError('database exploded')):
            self.assertTrue(process_next_purge())

        job.refresh_from_db()
        panel.refresh_from_db()
        self.assertEqual(job.status, SimcBenchmarkPurgeTask.STATUS_FAILED)
        self.assertIn('database exploded', job.error_detail)
        self.assertTrue(panel.is_active)
        self.assertTrue(panel.schedule_enabled)
        self.assertTrue(SimcTask.objects.filter(pk=task.id).exists())
        oss._delete_objects.assert_not_called()
        oss._restore_objects.assert_called_once_with(quarantine_map)
        oss._purge_quarantine_objects.assert_called_once_with(quarantine_map)
