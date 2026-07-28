import hashlib
import json
import os
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, TestCase, override_settings
from django.utils import timezone

from botend.models import (
    SimcAgent, SimcApl, SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcContentTemplate, SimcProfile,
    SimcResourceVersion, SimcTask, SimcTaskArtifact, SimulationRun,
)
from botend.services.simc_agent_control import _issue_token


CLAIM = '/api/simc-agent/v1/jobs/claim/'


@override_settings(SIMC_AGENT_ONLINE_TIMEOUT_SECONDS=90, SIMC_AGENT_LEASE_SECONDS=60)
class SimcAgentJobAPITests(TestCase):
    def setUp(self):
        self.backend, self.agent, self.token = self.backend_row('primary')
        self.other, self.other_agent, self.other_token = self.backend_row('other')

    def backend_row(self, identifier):
        backend = SimcBackendBinary.objects.create(
            identifier=identifier, name=identifier, is_active=True,
        )
        agent, token = self.agent_row(backend, identifier)
        return backend, agent, token

    def agent_row(self, backend, name):
        agent = SimcAgent.objects.create(
            backend=backend, host_identifier=(name.encode().hex() * 64)[:64], name=name,
            is_active=True, binary_available=True, status=SimcAgent.STATUS_ONLINE,
            last_seen_at=timezone.now(),
        )
        token = _issue_token(agent)
        agent.save(update_fields=['token_id', 'token_hash'])
        return agent, token

    def task(self, backend=None, mode='normal', candidates=None, name='task'):
        backend = backend or self.backend
        owner_id = 1 if backend == self.backend else 2
        cached = getattr(self, '_task_resources', {}).get(owner_id)
        if cached is None:
            profile = SimcProfile.objects.create(
                user_id=owner_id, name=name, spec='fury', player_config_mode='manual_equipment',
                player_equipment='warrior="Agent"\nspec=fury\nhead=,id=1', talent='',
            )
            template = SimcContentTemplate.objects.create(
                name=name, spec='fury', content='{player_config}\niterations={iterations}',
                owner_user_id=owner_id,
            )
            apl = SimcApl.objects.create(
                name=name, spec='fury', content='actions=/bloodthirst', owner_user_id=owner_id,
            )
            versions = []
            for kind, obj, payload in (
                ('profile', profile, {'name': name, 'spec': 'fury', 'player_config_mode': 'manual_equipment',
                                      'player_equipment': profile.player_equipment, 'talent': ''}),
                ('template', template, {'name': name, 'spec': 'fury', 'content': template.content}),
                ('apl', apl, {'name': name, 'spec': 'fury', 'content': apl.content, 'is_system': False}),
            ):
                versions.append(SimcResourceVersion.objects.create(
                    resource_type=kind, resource_id=obj.pk, content_hash=kind + str(owner_id),
                    payload=payload,
                ))
            self._task_resources = {**getattr(self, '_task_resources', {}),
                                    owner_id: (profile, template, apl, versions)}
        else:
            profile, template, apl, versions = cached
        return SimcTask.objects.create(
            user_id=owner_id, name=name, simc_profile_id=profile.pk, backend=backend,
            profile=profile, template=template, apl=apl,
            profile_version=versions[0], template_version=versions[1], apl_version=versions[2],
            mode=mode, simulation_params={'iterations': 123, 'max_time': 30},
            mode_params={'initial_candidates': candidates} if candidates else {},
        )

    def post_json(self, path, payload, token=None):
        return self.client.post(path, data=json.dumps(payload), content_type='application/json',
                                HTTP_AUTHORIZATION='Bearer ' + (token or self.token))

    def claim(self, token=None, instance='instance-a'):
        return self.post_json(CLAIM, {'instance_id': instance}, token)

    def test_claim_initializes_and_freezes_safe_input(self):
        task = self.task()
        task.result_file = ('a' * 32) + '.html'
        task.save(update_fields=['result_file'])
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body['task_id'], task.pk)
        self.assertEqual(body['sequence'], 1)
        self.assertEqual(body['input_hash'], hashlib.sha256(body['input'].encode()).hexdigest())
        self.assertEqual(
            body['output_filename'],
            'simc_task_%s_run_%s.html' % (task.pk, body['run_id']),
        )
        self.assertIn('bloodthirst', body['input'])
        self.assertIn('html=' + body['output_filename'], body['input'])
        self.assertNotIn('simc_path', body)
        self.assertNotIn('command', body)
        run = SimulationRun.objects.get(pk=body['run_id'])
        self.assertTrue(run.lease_token_hash.startswith('sha256$'))
        self.assertNotIn(body['lease_token'], run.lease_token_hash)
        self.assertEqual(run.input_hash, body['input_hash'])
        task.refresh_from_db()
        self.assertEqual(task.execution_owner, SimcTask.EXECUTION_OWNER_AGENT)

    def test_agent_registered_after_pending_task_can_claim_it(self):
        self.agent.delete()
        task = self.task()
        self.agent, self.token = self.agent_row(self.backend, 'registered-later')

        response = self.claim()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], task.pk)

    def test_agent_never_claims_local_owned_task(self):
        task = self.task()
        task.execution_owner = SimcTask.EXECUTION_OWNER_LOCAL
        task.save(update_fields=['execution_owner'])

        self.assertEqual(self.claim().status_code, 204)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)

    def test_expired_sibling_run_blocks_whole_task_until_stale_recovery(self):
        blocked = self.task(mode='comparison', candidates=[
            {'candidate_key': 'one', 'candidate_params': {'candidate_type': 'base'}},
            {'candidate_key': 'two', 'candidate_params': {'candidate_type': 'base'}},
        ], name='blocked')
        blocked.current_status = 1
        blocked.execution_owner = SimcTask.EXECUTION_OWNER_AGENT
        blocked.started_at = timezone.now()
        blocked.save(update_fields=['current_status', 'execution_owner', 'started_at'])
        SimulationRun.objects.create(
            task=blocked, sequence=1, status='running', lease_agent=self.other_agent,
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        SimulationRun.objects.create(task=blocked, sequence=2, status='pending')
        available = self.task(name='available')

        response = self.claim()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], available.pk)
        self.assertEqual(SimulationRun.objects.get(task=blocked, sequence=2).status, 'pending')

    def test_backend_isolation_and_active_lease_204(self):
        other_task = self.task(backend=self.other, name='other-task')
        self.assertEqual(self.claim().status_code, 204)
        first = self.claim(self.other_token, 'other-instance')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()['task_id'], other_task.pk)
        self.task(backend=self.other, name='later')
        self.assertEqual(self.claim(self.other_token, 'other-instance').status_code, 204)

    def test_two_agents_same_backend_claim_different_runs_concurrently(self):
        agent_b, token_b = self.agent_row(self.backend, 'primary-b')
        task = self.task(mode='comparison', candidates=[
            {'candidate_key': 'one', 'candidate_params': {'candidate_type': 'base'}},
            {'candidate_key': 'two', 'candidate_params': {'candidate_type': 'base'}},
        ])
        first = self.claim(self.token, 'instance-a')
        second = self.claim(token_b, 'instance-b')
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertNotEqual(first.json()['run_id'], second.json()['run_id'])
        self.assertEqual(SimulationRun.objects.get(pk=first.json()['run_id']).lease_agent, self.agent)
        self.assertEqual(SimulationRun.objects.get(pk=second.json()['run_id']).lease_agent, agent_b)
        self.assertEqual(self.claim(self.token, 'instance-a').status_code, 204)

        staff = get_user_model().objects.create_user(username='staff', password='x', is_staff=True)
        self.client.force_login(staff)
        rows = self.client.get('/api/simc-workbench/agents/').json()['data']
        by_id = {row['id']: row for row in rows}
        self.assertEqual(by_id[self.agent.pk]['status'], 'busy')
        self.assertEqual(by_id[agent_b.pk]['status'], 'busy')
        self.assertEqual(by_id[self.agent.pk]['lease']['run_id'], first.json()['run_id'])
        self.assertEqual(by_id[agent_b.pk]['lease']['run_id'], second.json()['run_id'])

        first_path = f"/api/simc-agent/v1/jobs/{first.json()['run_id']}/heartbeat/"
        self.assertEqual(self.post_json(first_path, {
            'lease_token': first.json()['lease_token'], 'instance_id': 'instance-a',
        }, token_b).status_code, 403)
        self.assertEqual(self.complete(first.json(), token=token_b, instance='instance-a').status_code, 403)
        self.assertEqual(self.complete(second.json(), token=token_b, instance='instance-b',
                                       completion_id='agent-b').status_code, 200)

    def test_disabled_agent_cannot_claim_but_can_finish_existing_lease(self):
        job = self.claim_after_task()
        self.agent.is_active = False
        self.agent.save(update_fields=['is_active'])
        self.assertEqual(self.claim().status_code, 403)
        path = f"/api/simc-agent/v1/jobs/{job['run_id']}/heartbeat/"
        self.assertEqual(self.post_json(path, {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
        }).status_code, 200)
        self.assertEqual(self.complete(job).status_code, 200)

    def test_running_task_is_drained_one_run_at_a_time(self):
        task = self.task(mode='comparison', candidates=[
            {'candidate_key': 'base', 'candidate_params': {'candidate_type': 'base'}},
            {'candidate_key': 'apl', 'candidate_params': {'candidate_type': 'apl_override',
                                                          'apl_override': 'actions=/execute'}},
        ])
        first = self.claim().json()
        self.complete(first)
        second = self.claim().json()
        self.assertEqual(second['task_id'], task.pk)
        self.assertEqual(second['sequence'], 2)
        self.assertIn('execute', second['input'])

    def test_disabled_unavailable_and_offline_rejected(self):
        for field, value in (('is_active', False), ('binary_available', False),
                             ('last_seen_at', timezone.now() - timedelta(seconds=91))):
            setattr(self.agent, field, value)
            self.agent.save(update_fields=[field])
            response = self.claim()
            self.assertEqual(response.status_code, 403)
            setattr(self.agent, field, True if field != 'last_seen_at' else timezone.now())
            self.agent.save(update_fields=[field])

    def test_disabled_backend_rejects_claim(self):
        self.backend.is_active = False
        self.backend.save(update_fields=['is_active'])
        self.task()
        self.assertEqual(self.claim().status_code, 403)

    def test_heartbeat_renews_only_matching_fence(self):
        job = self.claim_after_task()
        path = f"/api/simc-agent/v1/jobs/{job['run_id']}/heartbeat/"
        old = SimulationRun.objects.get(pk=job['run_id']).lease_expires_at
        response = self.post_json(path, {'lease_token': job['lease_token'], 'instance_id': 'instance-a'})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertGreater(SimulationRun.objects.get(pk=job['run_id']).lease_expires_at, old)
        self.assertEqual(self.post_json(path, {'lease_token': 'x' * 43, 'instance_id': 'instance-a'}).status_code, 409)
        self.assertEqual(self.post_json(path, {'lease_token': job['lease_token'], 'instance_id': 'instance-a'},
                                        self.other_token).status_code, 403)

    def claim_after_task(self):
        self.task()
        return self.claim().json()

    def complete(self, job, status='completed', completion_id='completion-1',
                 report=b'<html>DPS=1234</html>', token=None, instance='instance-a'):
        metadata = {'lease_token': job['lease_token'], 'instance_id': instance,
                    'completion_id': completion_id, 'status': status,
                    'stdout': 'Player: Agent\nDPS=1234 DPS-Error=1/0.1%\n  bloodthirst Count=10 pDPS= 1234',
                    'stderr': ''}
        data = {'metadata': json.dumps(metadata)}
        if report is not None:
            data['report'] = SimpleUploadedFile('../../evil.html', report, content_type='text/html')
        return self.client.post(f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", data,
                                HTTP_AUTHORIZATION='Bearer ' + (token or self.token))

    def test_complete_success_artifact_dps_and_idempotency(self):
        job = self.claim_after_task()
        response = self.complete(job)
        self.assertEqual(response.status_code, 200, response.content)
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.result_summary['dps'], 1234.0)
        artifact = SimcTaskArtifact.objects.get(run=run, artifact_type='html_report')
        self.assertTrue(artifact.file_path.startswith('simc_agent_results/'))
        self.assertNotIn('evil', artifact.file_path)
        self.assertNotIn('/task-', artifact.file_path)
        from botend.services.simc_artifacts import _validated_result
        validated = _validated_result(run.task, os.path.basename(artifact.file_path), run=run)
        self.assertIsNotNone(validated)
        self.assertNotIn('/static/', str(validated[0]).replace('\\\\', '/'))
        self.assertEqual(validated[1], artifact.file_path)
        self.assertIn('var/simc_agent_results', str(validated[0]))
        self.assertEqual(self.complete(job).status_code, 200)
        self.assertEqual(SimcTaskArtifact.objects.filter(run=run).count(), 1)
        self.assertEqual(self.complete(job, completion_id='different').status_code, 409)

        run.lease_agent = None
        run.save(update_fields=['lease_agent'])
        validated_after_recovery = _validated_result(
            run.task, os.path.basename(artifact.file_path), run=run)
        self.assertIsNotNone(validated_after_recovery)
        self.assertEqual(validated_after_recovery[1], artifact.file_path)
        self.assertIn('var/simc_agent_results', str(validated_after_recovery[0]))

        owner = get_user_model().objects.create_user(username='artifact-owner')
        self.assertEqual(owner.pk, run.task.user_id)
        self.client.force_login(owner)
        preview = self.client.get(f'/api/simc-workbench/artifacts/{artifact.pk}/preview/')
        self.assertEqual(preview.status_code, 200)
        self.assertIn(b'DPS=1234', b''.join(preview.streaming_content))
        self.assertIn('sandbox', preview['Content-Security-Policy'])

    def test_completion_rejects_obviously_oversized_request_before_multipart_parse(self):
        job = self.claim_after_task()
        request = RequestFactory().generic(
            'POST', f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/",
            '', content_type='multipart/form-data; boundary=unused',
        )
        request.META['CONTENT_TYPE'] = 'multipart/form-data; boundary=unused'
        request.content_type = 'multipart/form-data'
        request.content_params = {'boundary': 'unused'}
        request.META['CONTENT_LENGTH'] = str(24 * 1024 * 1024)
        from botend.simc_agent_api import SimcAgentJobCompleteAPIView
        with patch('botend.simc_agent_api.authenticate_bearer', return_value=self.agent), \
                patch('django.http.multipartparser.MultiPartParser.parse',
                      side_effect=AssertionError('multipart parser must not run')):
            response = SimcAgentJobCompleteAPIView.as_view()(request, run_id=job['run_id'])
        self.assertEqual(response.status_code, 413, response.content)

    def test_completion_stream_aborts_report_over_20_mib(self):
        job = self.claim_after_task()
        with patch('botend.simc_agent_api.complete_run',
                   side_effect=AssertionError('service must not receive oversized report')):
            response = self.complete(job, report=b'x' * (20 * 1024 * 1024 + 1))
        self.assertEqual(response.status_code, 413, response.content)
        self.assertEqual(SimulationRun.objects.get(pk=job['run_id']).status, 'running')

    def test_completion_stream_aborts_cumulative_multiple_file_parts(self):
        job = self.claim_after_task()
        metadata = {
            'instance_id': 'instance-a', 'lease_token': job['lease_token'],
            'completion_id': 'multi-part-limit', 'status': 'completed',
            'stdout': 'DPS=1234', 'stderr': '',
        }
        reports = [
            SimpleUploadedFile(
                f'report-{index}.html', b'<html>' + (b'x' * (11 * 1024 * 1024)),
                content_type='text/html',
            )
            for index in range(2)
        ]
        with patch('botend.simc_agent_api.complete_run',
                   side_effect=AssertionError('service must not receive cumulative oversized files')):
            response = self.client.post(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/",
                data={'metadata': json.dumps(metadata), 'report': reports},
                HTTP_AUTHORIZATION='Bea' + 'rer ' + self.token,
            )
        self.assertEqual(response.status_code, 413, response.content)
        self.assertEqual(SimulationRun.objects.get(pk=job['run_id']).status, 'running')

    def test_failed_does_not_require_report(self):
        job = self.claim_after_task()
        response = self.complete(job, status='failed', report=None)
        self.assertEqual(response.status_code, 200, response.content)
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(run.status, 'failed')
        self.assertTrue(run.error_detail)

    def test_completed_requires_html_report(self):
        job = self.claim_after_task()
        self.assertEqual(self.complete(job, report=None).status_code, 400)
        bad = SimpleUploadedFile('x.txt', b'bad', content_type='text/plain')
        metadata = json.dumps({'lease_token': job['lease_token'], 'instance_id': 'instance-a',
                               'completion_id': 'x', 'status': 'completed', 'stdout': '', 'stderr': ''})
        response = self.client.post(f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/",
                                    {'metadata': metadata, 'report': bad},
                                    HTTP_AUTHORIZATION='Bearer ' + self.token)
        self.assertEqual(response.status_code, 400)

    def test_benchmark_reconcile_called_after_completion(self):
        job = self.claim_after_task()
        with patch('botend.services.simc_run_control.reconcile_execution_for_task') as reconcile:
            self.assertEqual(self.complete(job).status_code, 200)
        reconcile.assert_called_once_with(job['task_id'])

    def benchmark_task(self, task, slug):
        panel = SimcBenchmarkPanel.objects.create(name=slug, slug=slug, created_by_id=1)
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash=hashlib.sha256(slug.encode()).hexdigest(),
        )
        return SimcBenchmarkCase.objects.create(
            execution=execution, task=task, spec_key='fury', scenario_key='patchwerk',
            profile_key=str(task.pk), spec_label='Fury', scenario_label='Patchwerk',
            profile_label=task.name, coordinate_hash=hashlib.sha256(str(task.pk).encode()).hexdigest(),
        )

    def test_claim_prefers_normal_then_fifo_and_ignores_other_backend(self):
        benchmark = self.task(mode='comparison', name='benchmark')
        self.benchmark_task(benchmark, 'benchmark-order')
        normal_first = self.task(name='normal-first')
        normal_second = self.task(name='normal-second')
        self.task(backend=self.other, name='other-pending')

        first = self.claim().json()
        self.assertEqual(first['task_id'], normal_first.pk)
        self.assertEqual(self.complete(first).status_code, 200)
        second = self.claim().json()
        self.assertEqual(second['task_id'], normal_second.pk)
        self.assertEqual(self.complete(second, completion_id='second').status_code, 200)
        third = self.claim().json()
        self.assertEqual(third['task_id'], benchmark.pk)

    def test_heartbeat_rejects_expired_instance_and_token_without_mutating_expiry(self):
        job = self.claim_after_task()
        path = f"/api/simc-agent/v1/jobs/{job['run_id']}/heartbeat/"
        run = SimulationRun.objects.get(pk=job['run_id'])
        original = run.lease_expires_at
        for payload in (
            {'lease_token': job['lease_token'], 'instance_id': 'wrong'},
            {'lease_token': 'x' * 43, 'instance_id': 'instance-a'},
        ):
            self.assertEqual(self.post_json(path, payload).status_code, 409)
            self.assertEqual(SimulationRun.objects.get(pk=run.pk).lease_expires_at, original)
        SimulationRun.objects.filter(pk=run.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        expired = SimulationRun.objects.get(pk=run.pk).lease_expires_at
        self.assertEqual(self.post_json(path, {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
        }).status_code, 409)
        self.assertEqual(SimulationRun.objects.get(pk=run.pk).lease_expires_at, expired)

    def test_completed_rejects_forged_html_content_type(self):
        job = self.claim_after_task()
        self.assertEqual(self.complete(job, report=b'not html at all').status_code, 400)

    def test_completion_metadata_larger_than_64k_is_accepted(self):
        job = self.claim_after_task()
        metadata = {'lease_token': job['lease_token'], 'instance_id': 'instance-a',
                    'completion_id': 'large-output', 'status': 'completed',
                    'stdout': ('x' * 70000) + '\nPlayer: Agent\nDPS=1234 DPS-Error=1/0.1%',
                    'stderr': ''}
        response = self.client.post(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/",
            {'metadata': json.dumps(metadata), 'report': SimpleUploadedFile(
                'report.html', b'<!doctype html><html></html>', content_type='text/html')},
            HTTP_AUTHORIZATION='Bearer ' + self.token,
        )
        self.assertEqual(response.status_code, 200, response.content)

    def test_unauthenticated_upload_is_rejected_before_service_tempfile(self):
        job = self.claim_after_task()
        metadata = {'lease_token': job['lease_token'], 'instance_id': 'instance-a',
                    'completion_id': 'unauth', 'status': 'completed', 'stdout': '', 'stderr': ''}
        with patch('botend.services.simc_run_control.tempfile.mkstemp') as mkstemp:
            response = self.client.post(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/",
                {'metadata': json.dumps(metadata), 'report': SimpleUploadedFile(
                    'large.html', b'<html>' + b'x' * 70000 + b'</html>', content_type='text/html')},
                HTTP_AUTHORIZATION='Bearer invalid.invalid',
            )
        self.assertEqual(response.status_code, 401)
        mkstemp.assert_not_called()

    def test_claim_composition_failure_rolls_back_task_and_runs(self):
        task = self.task()
        with patch('botend.services.simc_run_control.build_frozen_run_input',
                   side_effect=ValueError('boom')):
            response = self.claim()
        self.assertEqual(response.status_code, 409, response.content)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)
        self.assertFalse(SimulationRun.objects.filter(task=task).exists())

    def test_all_responses_are_no_store_including_errors(self):
        self.assertEqual(self.claim().headers['Cache-Control'], 'no-store')
        response = self.client.post(CLAIM, data='{}', content_type='application/json')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
