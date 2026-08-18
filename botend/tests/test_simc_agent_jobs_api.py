import hashlib
import json
import subprocess
from datetime import timedelta
from unittest.mock import patch

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from botend.models import (
    SimcAgent, SimcApl, SimcBackendBinary, SimcBenchmarkCase, SimcBenchmarkExecution,
    SimcBenchmarkPanel, SimcContentTemplate, SimcProfile,
    SimcResourceVersion, SimcTalentString, SimcTask, SimcTaskArtifact, SimulationRun,
)
from botend.services.simc_agent_control import _issue_token
from botend.services.simc_benchmark_execution import cancel_execution


CLAIM = '/api/simc-agent/v1/jobs/claim/'


@override_settings(
    SIMC_AGENT_ONLINE_TIMEOUT_SECONDS=90,
    SIMC_AGENT_LEASE_SECONDS=60,
    SIMC_AGENT_REQUIRED_VERSION='1.4.0',
    OSS_CONFIG={'base_url': 'https://reports.example'},
    ALLOWED_HOSTS=['testserver'],
)
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
        revision = settings.SIMC_AGENT_REQUIRED_REVISION or ('a' * 40)
        agent = SimcAgent.objects.create(
            backend=backend, host_identifier=(name.encode().hex() * 64)[:64], name=name,
            is_active=True, binary_available=True, status=SimcAgent.STATUS_ONLINE,
            agent_version='1.4.0', protocol_version=1,
            agent_revision=revision,
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
        return self.post_json(CLAIM, {
                        'instance_id': instance, 'agent_version': '1.4.0',
                        'agent_revision': settings.SIMC_AGENT_REQUIRED_REVISION or ('a' * 40), 'protocol_version': 1,
        }, token)

    def test_claim_keeps_regular_simulation_above_high_priority_benchmark(self):
        regular = self.task(name='regular')
        benchmark = self.task(name='benchmark')
        benchmark.queue_priority = SimcTask.QUEUE_PRIORITY_BENCHMARK_HIGH
        benchmark.is_benchmark_task = True
        benchmark.save(update_fields=['queue_priority', 'is_benchmark_task'])
        panel = SimcBenchmarkPanel.objects.create(
            name='Priority', slug='priority-regular-first', created_by_id=1,
            queue_priority=SimcTask.QUEUE_PRIORITY_BENCHMARK_HIGH,
        )
        execution = SimcBenchmarkExecution.objects.create(panel=panel, config_hash='p' * 64)
        SimcBenchmarkCase.objects.create(
            execution=execution, task=benchmark, spec_key='warrior_fury',
            scenario_key='patchwerk', profile_key='default', spec_label='Fury',
            scenario_label='Patchwerk', profile_label='Default', coordinate_hash='q' * 64,
        )

        response = self.claim()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], regular.pk)

    def test_claim_orders_benchmark_tasks_by_frozen_priority(self):
        low = self.task(name='low')
        high = self.task(name='high')
        low.queue_priority = SimcTask.QUEUE_PRIORITY_BENCHMARK_LOW
        high.queue_priority = SimcTask.QUEUE_PRIORITY_BENCHMARK_HIGH
        low.is_benchmark_task = high.is_benchmark_task = True
        SimcTask.objects.bulk_update([low, high], ['queue_priority', 'is_benchmark_task'])
        panel = SimcBenchmarkPanel.objects.create(name='Priority', slug='priority-benchmark-order', created_by_id=1)
        execution = SimcBenchmarkExecution.objects.create(panel=panel, config_hash='r' * 64)
        for task, key in ((low, 'low'), (high, 'high')):
            SimcBenchmarkCase.objects.create(
                execution=execution, task=task, spec_key='warrior_fury',
                scenario_key='patchwerk', profile_key=key, spec_label='Fury',
                scenario_label='Patchwerk', profile_label=key, coordinate_hash=key * 64,
            )

        response = self.claim()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], high.pk)

    def test_control_plane_settings_pin_current_repository_revision(self):
        expected = subprocess.check_output(
            ['git', '-C', settings.BASE_DIR, 'rev-parse', 'HEAD'], text=True, timeout=5,
        ).strip().lower()
        self.assertEqual(settings.SIMC_AGENT_REQUIRED_REVISION, expected)

    @override_settings(SIMC_AGENT_REQUIRED_REVISION='')
    @patch('botend.services.simc_run_control.subprocess.check_output')
    def test_claim_requires_checkout_revision_when_setting_is_omitted(self, check_output):
        check_output.return_value = ('b' * 40) + '\n'

        response = self.claim()

        self.assertEqual(response.status_code, 426, response.content)
        self.assertEqual(response.json()['code'], 'agent_update_required')
        self.assertEqual(response.json()['required_revision'], 'b' * 40)
        check_output.assert_called_once_with(
            ['git', '-C', settings.BASE_DIR, 'rev-parse', 'HEAD'], text=True, timeout=5,
        )

    def test_claim_allows_agent_when_simc_revision_differs_from_worker_backend(self):
        task = self.task()
        self.backend.current_version = 'b' * 40
        self.backend.save(update_fields=['current_version'])
        self.agent.current_version = 'a' * 40
        self.agent.save(update_fields=['current_version'])

        response = self.claim()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], task.pk)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 1)
        self.assertTrue(SimulationRun.objects.filter(task=task, status='running').exists())

    @override_settings(SIMC_AGENT_REQUIRED_REVISION='b' * 40)
    def test_claim_rejects_agent_when_lmonitor_revision_does_not_match(self):
        task = self.task()
        response = self.claim()
        self.assertEqual(response.status_code, 426, response.content)
        self.assertEqual(response.json()['code'], 'agent_update_required')
        self.assertEqual(response.json()['required_revision'], 'b' * 40)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)

    @override_settings(SIMC_AGENT_REQUIRED_VERSION='1.1.0', SIMC_AGENT_PROTOCOL_VERSION=1)
    def test_claim_rejects_outdated_agent_before_mutating_task(self):
        task = self.task()

        response = self.post_json(CLAIM, {
            'instance_id': 'old-instance', 'agent_version': '1.0.0', 'protocol_version': 1,
        })

        self.assertEqual(response.status_code, 426, response.content)
        self.assertEqual(response.json()['code'], 'agent_update_required')
        self.assertEqual(response.json()['required_version'], '1.1.0')
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)
        self.assertFalse(SimulationRun.objects.filter(task=task).exists())

    @override_settings(SIMC_AGENT_REQUIRED_VERSION='1.1.0', SIMC_AGENT_PROTOCOL_VERSION=2)
    def test_claim_rejects_incompatible_protocol_before_mutating_task(self):
        task = self.task()

        response = self.post_json(CLAIM, {
            'instance_id': 'old-protocol', 'agent_version': '1.1.0',
            'agent_revision': settings.SIMC_AGENT_REQUIRED_REVISION, 'protocol_version': 1,
        })

        self.assertEqual(response.status_code, 426, response.content)
        self.assertEqual(response.json()['code'], 'agent_protocol_mismatch')
        self.assertEqual(response.json()['required_protocol_version'], 2)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)

    @override_settings(SIMC_AGENT_REQUIRED_VERSION='1.4.0', SIMC_AGENT_PROTOCOL_VERSION=1)
    def test_outdated_agent_with_live_lease_is_not_told_to_reexec(self):
        self.task()
        first = self.claim(instance='lease-owner')
        self.assertEqual(first.status_code, 200, first.content)
        run = SimulationRun.objects.get(pk=first.json()['run_id'])

        response = self.post_json(CLAIM, {
            'instance_id': 'lease-owner', 'agent_version': '1.0.0', 'protocol_version': 1,
        })

        self.assertEqual(response.status_code, 204, response.content)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertEqual(run.lease_agent, self.agent)

    def test_legacy_claim_without_version_fields_remains_bootstrap_compatible(self):
        task = self.task()

        response = self.post_json(CLAIM, {
            'instance_id': 'legacy-instance',
            'agent_revision': settings.SIMC_AGENT_REQUIRED_REVISION,
        })

        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], task.pk)

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

    def test_claim_uses_frozen_task_talent_instead_of_profile_talent(self):
        task = self.task()
        profile_payload = {**task.profile_version.payload, 'talent': 'PROFILE_BUILD'}
        profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=task.profile_id,
            content_hash='profile-build', payload=profile_payload,
        )
        talent = SimcTalentString.objects.create(
            name='Selected build', spec='fury', talent='SELECTED_BUILD',
            owner_user_id=task.user_id,
        )
        talent_version = SimcResourceVersion.objects.create(
            resource_type='talent', resource_id=talent.pk,
            content_hash='selected-build',
            payload={'name': talent.name, 'spec': talent.spec, 'talent': talent.talent},
        )
        task.profile_version = profile_version
        task.talent_string = talent
        task.talent_version = talent_version
        task.save(update_fields=['profile_version', 'talent_string', 'talent_version'])

        response = self.claim()

        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn('talents=SELECTED_BUILD', response.json()['input'])
        self.assertNotIn('talents=PROFILE_BUILD', response.json()['input'])

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

    def test_same_agent_can_claim_up_to_advertised_capacity(self):
        self.agent.capabilities = {'max_concurrent_runs': 2}
        self.agent.save(update_fields=['capabilities'])
        task = self.task(mode='comparison', candidates=[
            {'candidate_key': 'one', 'candidate_params': {'candidate_type': 'base'}},
            {'candidate_key': 'two', 'candidate_params': {'candidate_type': 'base'}},
            {'candidate_key': 'three', 'candidate_params': {'candidate_type': 'base'}},
        ])

        first = self.claim()
        second = self.claim()
        third = self.claim()

        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual(second.status_code, 200, second.content)
        self.assertEqual(first.json()['task_id'], task.pk)
        self.assertEqual(second.json()['task_id'], task.pk)
        self.assertNotEqual(first.json()['run_id'], second.json()['run_id'])
        self.assertEqual(third.status_code, 204, third.content)

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
        # The original lease token remains the authority for terminal submission:
        # an Agent row rotation must not strand its durable completion outbox.
        self.assertEqual(self.complete(first.json(), token=token_b, instance='instance-a').status_code, 200)
        first_run = SimulationRun.objects.get(pk=first.json()['run_id'])
        self.assertEqual(first_run.status, 'completed')
        self.assertEqual(first_run.lease_agent, self.agent)

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

    def test_attribute_agent_finalization_preserves_converged_recommendation(self):
        from botend.services.simc_attribute_search import attribute_variants
        from botend.services.simc_run_control import _finalize_task

        task = self.task(mode='attribute_sweep', name='attribute finalization')
        claimed_at = timezone.now()
        task.current_status = 1
        task.execution_owner = SimcTask.EXECUTION_OWNER_AGENT
        task.started_at = claimed_at
        task.save(update_fields=['current_status', 'execution_owner', 'started_at'])
        ratings = {'crit': 1077, 'haste': 928, 'mastery': 947, 'versatility': 0}
        for sequence, (label, candidate_ratings, is_base, search) in enumerate(
            attribute_variants(ratings, step=20), 1
        ):
            SimulationRun.objects.create(
                task=task,
                sequence=sequence,
                round_number=1,
                candidate_key=f'round-1-candidate-{sequence}',
                candidate_label=label,
                candidate_params={
                    'candidate_type': 'attribute_ratings',
                    'is_base': is_base,
                    'attribute_ratings': candidate_ratings,
                    'search': search,
                },
                status='completed',
                result_summary={
                    'dps': 100000 if is_base else 99900,
                    'dps_error': 100,
                },
                started_at=claimed_at,
                completed_at=timezone.now(),
            )

        _finalize_task(task, timezone.now())

        task.refresh_from_db()
        self.assertEqual(task.current_status, 2)
        self.assertTrue(task.analysis_result['attribute_search']['converged'])
        self.assertEqual(
            task.analysis_result['attribute_search']['stop_reason'],
            'local_optimum_20_pairwise',
        )

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

    def test_cancelled_execution_rejects_late_heartbeat_and_completion(self):
        owner = get_user_model().objects.create_user(username='benchmark-owner')
        panel = SimcBenchmarkPanel.objects.create(
            name='Agent cancellation', slug='agent-cancellation', created_by_id=owner.id,
        )
        task = self.task()
        execution = SimcBenchmarkExecution.objects.create(
            panel=panel, status='running', config_snapshot={}, config_hash='a' * 64,
        )
        SimcBenchmarkCase.objects.create(
            execution=execution, task=task, status='running',
            spec_key='warrior_fury', scenario_key='single', profile_key='profile',
            spec_label='Fury', scenario_label='Single', profile_label='Profile',
            coordinate_hash='b' * 64,
        )
        panel.active_execution = execution
        panel.save(update_fields=['active_execution'])
        job = self.claim().json()

        cancel_execution(execution, requested_by=owner)

        heartbeat = self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/heartbeat/",
            {'lease_token': job['lease_token'], 'instance_id': 'instance-a'},
        )
        completion = self.complete(job, verify_report=False)
        self.assertEqual(heartbeat.status_code, 409, heartbeat.content)
        self.assertEqual(completion.status_code, 409, completion.content)
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(run.status, 'cancelled')
        self.assertIsNone(run.result_summary)
        self.assertFalse(SimcTaskArtifact.objects.filter(run=run).exists())

    def claim_after_task(self):
        self.task()
        return self.claim().json()

    def complete(self, job, status='completed', completion_id='completion-1',
                 report=b'<html>DPS=1234</html>', token=None, instance='instance-a',
                 verify_report=True):
        report_identity = None
        if status == 'completed' and report is not None:
            report_identity = {
                'object_key': (
                    f"simc_agent_results/simc_task_{job['task_id']}_run_{job['run_id']}.html"
                ),
                'size': len(report),
                'sha256': hashlib.sha256(report).hexdigest(),
            }
        metadata = {
            'lease_token': job['lease_token'], 'instance_id': instance,
            'completion_id': completion_id, 'status': status,
            'stdout': 'Player: Agent\nDPS=1234 DPS-Error=1/0.1%\n  bloodthirst Count=10 pDPS= 1234',
            'stderr': '', 'report': report_identity,
        }
        path = f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/"
        if not verify_report:
            return self.post_json(path, metadata, token)
        downloaded_report = report.decode('utf-8', errors='replace') if report is not None else ''
        downloaded_sha256 = report_identity['sha256'] if report_identity else ''
        with patch('botend.services.simc_agent_oss.verify_uploaded_report'), \
             patch(
                 'botend.services.simc_agent_oss.download_report_html',
                 return_value=(downloaded_report, downloaded_sha256),
             ):
            return self.post_json(path, metadata, token)

    def test_complete_marks_report_invalid_weapon_actions_semantically_invalid(self):
        job = self.claim_after_task()
        report = b'''<html><body><div><h2>Trivial</h2><ul>
<li>Player 'MID2_Rogue_Outlaw' attempting to use Action 'dispatch' (2098) with invalid main-hand weapon type 'Dagger'.</li>
</ul></div></body></html>'''
        response = self.complete(job, report=report)
        self.assertEqual(response.status_code, 200, response.content)
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(run.status, 'failed')
        self.assertFalse(run.result_summary['valid'])
        self.assertEqual(run.result_summary['failure_type'], 'invalid_weapon_action')
        self.assertIn('dispatch', run.result_summary['reason'])
        self.assertIn('dispatch', run.error_detail)
        run.task.refresh_from_db()
        self.assertEqual(run.task.current_status, 3)

    def test_complete_success_artifact_dps_and_idempotency(self):
        job = self.claim_after_task()
        first = self.complete(job)
        self.assertEqual(first.status_code, 200, first.content)
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(run.status, 'completed')
        self.assertEqual(run.result_summary['dps'], 1234.0)
        artifact = SimcTaskArtifact.objects.get(run=run, artifact_type='html_report')
        self.assertEqual(artifact.content_hash, hashlib.sha256(b'<html>DPS=1234</html>').hexdigest())
        self.assertEqual(
            artifact.file_path,
            f"simc_agent_results/simc_task_{job['task_id']}_run_{job['run_id']}.html",
        )
        self.assertEqual(self.complete(job).status_code, 200)
        self.assertEqual(SimcTaskArtifact.objects.filter(run=run).count(), 1)
        duplicate = self.complete(job, status='failed', completion_id='different', token=self.other_token)
        self.assertEqual(duplicate.status_code, 200, duplicate.content)
        self.assertEqual(duplicate.json(), {
            'run_id': job['run_id'], 'status': 'completed', 'idempotent': True,
        })
        upload = self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/",
            {
                'lease_token': job['lease_token'], 'instance_id': 'instance-a',
                'size': 16, 'sha256': 'a' * 64,
                'content_md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
            }, self.other_token,
        )
        self.assertEqual(upload.status_code, 200, upload.content)
        self.assertEqual(upload.json(), {
            'run_id': job['run_id'], 'status': 'completed', 'already_completed': True,
        })
        self.assertEqual(SimcTaskArtifact.objects.filter(run=run).count(), 1)

        # Re-enrollment changes the Agent row but not the cryptographic lease.
        # The replacement identity can commit the original unique report once.
        replacement_agent, replacement_token = self.agent_row(self.backend, 'replacement')
        recovered_job = self.claim_after_task()
        recovered = self.complete(recovered_job, completion_id='recovered', token=replacement_token)
        self.assertEqual(recovered.status_code, 200, recovered.content)
        recovered_run = SimulationRun.objects.get(pk=recovered_job['run_id'])
        self.assertEqual(recovered_run.status, 'completed')
        self.assertEqual(recovered_run.lease_agent_id, self.agent.pk)

        from botend.dashboard.api import SimcWorkbenchAPIView
        with override_settings(OSS_CONFIG={'base_url': 'https://reports.example'}):
            row = SimcWorkbenchAPIView._artifact_row(artifact)
            task_row = SimcWorkbenchAPIView._task_row(run.task)
        expected_url = 'https://reports.example/' + artifact.file_path
        self.assertEqual(row['preview_url'], expected_url)
        self.assertTrue(row['is_external'])
        self.assertTrue(task_row['has_report'])
        self.assertEqual(task_row['report_preview_url'], expected_url)
        owner = get_user_model().objects.create_user(
            id=run.task.user_id, username='artifact-owner', password='x',
        )
        self.client.force_login(owner)
        with override_settings(
            OSS_CONFIG={'base_url': 'https://reports.example'},
            ALLOWED_HOSTS=['testserver'],
        ):
            preview = self.client.get(
                f'/api/simc-workbench/artifacts/{artifact.id}/preview/',
            )
        self.assertEqual(preview.status_code, 302)
        self.assertEqual(preview['Location'], expected_url)
        from botend.services.simc_result_analysis import analyze_run_artifact
        report_html = b'<html><body><div class="player"><h2>Agent: 1,234 dps</h2></div></body></html>'
        with patch(
            'botend.services.simc_result_analysis.simc_agent_oss.download_report_html',
            return_value=(
                report_html.decode('utf-8'),
                artifact.content_hash,
            ),
        ) as download, patch(
            'botend.services.simc_result_analysis.simc_artifacts._validated_result',
        ) as validated:
            summary = analyze_run_artifact(run.task, artifact)
        self.assertEqual(summary['dps'], 1234)
        download.assert_called_once_with(
            artifact.file_path,
            expected_size=artifact.file_size,
            expected_sha256=artifact.content_hash,
            expected_lease_fence=run.lease_token_hash,
        )
        validated.assert_not_called()

    def test_agent_report_analysis_backfills_verified_hash_for_existing_artifact(self):
        job = self.claim_after_task()
        self.complete(job)
        run = SimulationRun.objects.get(pk=job['run_id'])
        artifact = SimcTaskArtifact.objects.get(run=run, artifact_type='html_report')
        verified_sha256 = artifact.content_hash
        artifact.content_hash = ''
        artifact.save(update_fields=['content_hash'])
        report_html = '<html><body><div class="player"><h2>Agent: 1,234 dps</h2></div></body></html>'
        from botend.services.simc_result_analysis import analyze_run_artifact

        with patch(
            'botend.services.simc_result_analysis.simc_agent_oss.download_report_html',
            return_value=(report_html, verified_sha256),
        ) as download:
            summary = analyze_run_artifact(run.task, artifact)
        self.assertEqual(summary['dps'], 1234)
        artifact.refresh_from_db()
        self.assertEqual(artifact.content_hash, verified_sha256)
        download.assert_called_once_with(
            artifact.file_path,
            expected_size=artifact.file_size,
            expected_sha256='',
            expected_lease_fence=run.lease_token_hash,
        )

    def test_agent_report_analysis_requires_completed_run_bound_to_same_task(self):
        job = self.claim_after_task()
        self.complete(job)
        run = SimulationRun.objects.get(pk=job['run_id'])
        artifact = SimcTaskArtifact.objects.get(run=run, artifact_type='html_report')
        other_task = self.task(name='other')
        from botend.services.simc_result_analysis import analyze_run_artifact

        artifact.task = other_task
        artifact.save(update_fields=['task'])
        with patch(
            'botend.services.simc_result_analysis.simc_agent_oss.download_report_html',
        ) as download:
            self.assertIsNone(analyze_run_artifact(other_task, artifact))
        download.assert_not_called()

        artifact.task = run.task
        artifact.save(update_fields=['task'])
        run.status = 'running'
        run.save(update_fields=['status'])
        artifact.run.refresh_from_db()
        with patch(
            'botend.services.simc_result_analysis.simc_agent_oss.download_report_html',
        ) as download:
            self.assertIsNone(analyze_run_artifact(run.task, artifact))
        download.assert_not_called()

    def test_report_upload_ticket_is_bound_to_run_and_lease(self):
        job = self.claim_after_task()
        payload = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'size': 16, 'sha256': 'a' * 64,
            'content_md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
        }
        ticket = {
            'object_key': f"simc_agent_results/simc_task_{job['task_id']}_run_{job['run_id']}.html",
            'url': 'https://bucket.example/signed', 'method': 'PUT', 'headers': {},
        }
        with patch('botend.services.simc_agent_oss.issue_upload_ticket', return_value=ticket) as issue:
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/", payload,
            )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), ticket)
        issue.assert_called_once()
        issue_kwargs = issue.call_args.kwargs
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(issue_kwargs['lease_fence'], run.lease_token_hash)
        self.assertEqual(issue_kwargs['lease_expires_at'], run.lease_expires_at)
        bad = self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/",
            {**payload, 'lease_token': 'wrong'},
        )
        self.assertEqual(bad.status_code, 409)

    def test_stale_lease_completion_is_acknowledged_and_discarded(self):
        job = self.claim_after_task()
        stale_payload = {
            'lease_token': 'x' * 43, 'instance_id': 'instance-a',
            'size': 16, 'sha256': 'a' * 64,
            'content_md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
        }
        upload = self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/", stale_payload,
        )
        self.assertEqual(upload.status_code, 200, upload.content)
        self.assertEqual(upload.json(), {
            'run_id': job['run_id'], 'status': 'running', 'already_completed': True,
        })
        completion_body = {
            'lease_token': stale_payload['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'stale-lease', 'status': 'failed',
            'stdout': '', 'stderr': 'obsolete result', 'report': None,
        }
        response = self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", completion_body,
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json(), {
            'run_id': job['run_id'], 'status': 'running', 'idempotent': True,
        })
        self.assertEqual(SimulationRun.objects.get(pk=job['run_id']).status, 'running')

    def test_report_upload_returns_conflict_if_lease_expires_before_presign(self):
        from botend.services.simc_agent_oss import ReportLeaseExpiredError

        job = self.claim_after_task()
        payload = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'size': 16, 'sha256': 'a' * 64,
            'content_md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
        }
        with patch(
            'botend.services.simc_agent_oss.issue_upload_ticket',
            side_effect=ReportLeaseExpiredError('Run lease expired before upload signing'),
        ):
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/", payload,
            )
        self.assertEqual(response.status_code, 409, response.content)

    @override_settings(
        OSS_CONFIG={'base_url': 'https://testserver'},
        ALLOWED_HOSTS=['testserver'],
    )
    def test_report_upload_rejects_same_origin_report_configuration(self):
        job = self.claim_after_task()
        payload = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'size': 16, 'sha256': 'a' * 64,
            'content_md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
        }
        with patch('botend.services.simc_agent_oss.issue_upload_ticket') as issue:
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/",
                payload, self.token,
            )
        self.assertEqual(response.status_code, 503, response.content)
        issue.assert_not_called()

    def test_report_upload_rejects_oversized_identity_before_signing(self):
        job = self.claim_after_task()
        payload = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'size': 20 * 1024 * 1024 + 1, 'sha256': 'a' * 64,
            'content_md5': 'MDEyMzQ1Njc4OUFCQ0RFRg==',
        }
        with patch('botend.services.simc_agent_oss.issue_upload_ticket') as issue:
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/report-upload/", payload,
            )
        self.assertEqual(response.status_code, 413, response.content)
        issue.assert_not_called()

    def test_completion_validation_mismatch_is_422_and_does_not_mark_successful(self):
        from botend.services.simc_agent_oss import ReportValidationError
        job = self.claim_after_task()
        with patch('botend.services.simc_agent_oss.verify_uploaded_report',
                   side_effect=ReportValidationError('OSS report size mismatch')):
            response = self.complete(job, verify_report=False)
        self.assertEqual(response.status_code, 422, response.content)
        self.assertEqual(SimulationRun.objects.get(pk=job['run_id']).status, 'running')

    def test_completion_head_failure_does_not_mark_run_successful(self):
        from botend.services.simc_agent_oss import ReportStorageError
        job = self.claim_after_task()
        with patch('botend.services.simc_agent_oss.verify_uploaded_report',
                   side_effect=ReportStorageError('OSS report size mismatch')):
            response = self.complete(job, verify_report=False)
        self.assertEqual(response.status_code, 503, response.content)
        self.assertEqual(SimulationRun.objects.get(pk=job['run_id']).status, 'running')

    def test_completion_rechecks_expiry_after_locked_agent_authentication(self):
        from botend.services.simc_run_control import authenticate_bearer as real_authenticate

        job = self.claim_after_task()
        run = SimulationRun.objects.get(pk=job['run_id'])
        before_expiry = run.lease_expires_at - timedelta(seconds=1)
        after_expiry = run.lease_expires_at + timedelta(seconds=1)
        clock = {'now': before_expiry}

        def authenticate_then_advance(authorization, lock=False):
            agent = real_authenticate(authorization, lock=lock)
            if lock:
                clock['now'] = after_expiry
            return agent

        with patch('botend.services.simc_run_control.timezone.now',
                   side_effect=lambda: clock['now']), patch(
                       'botend.services.simc_run_control.authenticate_bearer',
                       side_effect=authenticate_then_advance,
                   ), patch('botend.services.simc_agent_oss.verify_uploaded_report'), patch(
                       'botend.services.simc_agent_oss.download_report_html',
                       return_value=('<html></html>', 'a' * 64),
                   ):
            response = self.complete(job, verify_report=False)

        self.assertEqual(response.status_code, 409, response.content)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')

    def test_failed_does_not_require_report(self):
        job = self.claim_after_task()
        response = self.complete(job, status='failed', report=None)
        self.assertEqual(response.status_code, 200, response.content)
        run = SimulationRun.objects.get(pk=job['run_id'])
        self.assertEqual(run.status, 'failed')
        self.assertTrue(run.error_detail)

    def test_completed_requires_bound_report_identity(self):
        job = self.claim_after_task()
        self.assertEqual(self.complete(job, report=None).status_code, 400)
        metadata = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'wrong-key', 'status': 'completed',
            'stdout': 'DPS=1234', 'stderr': '',
            'report': {
                'object_key': 'simc_agent_results/other-run.html',
                'size': 16, 'sha256': 'a' * 64,
            },
        }
        response = self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", metadata,
        )
        self.assertEqual(response.status_code, 409)

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

    def test_expired_upload_ticket_cannot_become_authoritative(self):
        job = self.claim_after_task()
        run = SimulationRun.objects.get(pk=job['run_id'])
        SimulationRun.objects.filter(pk=run.pk).update(
            lease_expires_at=timezone.now() - timedelta(seconds=1),
        )
        report = b'<html>DPS=1234</html>'
        metadata = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'expired-ticket', 'status': 'completed',
            'stdout': 'DPS=1234', 'stderr': '', 'report': {
                'object_key': f"simc_agent_results/simc_task_{job['task_id']}_run_{job['run_id']}.html",
                'size': len(report), 'sha256': hashlib.sha256(report).hexdigest(),
            },
        }
        with patch('botend.services.simc_agent_oss.verify_uploaded_report') as verify:
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", metadata,
            )
        self.assertEqual(response.status_code, 409, response.content)
        verify.assert_not_called()
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')
        self.assertFalse(SimcTaskArtifact.objects.filter(run=run).exists())

    def test_completed_rejects_report_for_another_run(self):
        job = self.claim_after_task()
        metadata = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'forged', 'status': 'completed',
            'stdout': 'DPS=1234', 'stderr': '',
            'report': {
                'object_key': 'simc_agent_results/simc_task_999_run_999.html',
                'size': 16, 'sha256': 'b' * 64,
            },
        }
        self.assertEqual(self.post_json(
            f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", metadata,
        ).status_code, 409)

    def test_completion_metadata_larger_than_64k_is_accepted(self):
        job = self.claim_after_task()
        report = b'<!doctype html><html></html>'
        metadata = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'large-output', 'status': 'completed',
            'stdout': ('x' * 70000) + '\nPlayer: Agent\nDPS=1234 DPS-Error=1/0.1%',
            'stderr': '',
            'report': {
                'object_key': f"simc_agent_results/simc_task_{job['task_id']}_run_{job['run_id']}.html",
                'size': len(report), 'sha256': hashlib.sha256(report).hexdigest(),
            },
        }
        with patch('botend.services.simc_agent_oss.verify_uploaded_report'), patch(
            'botend.services.simc_agent_oss.download_report_html',
            return_value=(report.decode(), hashlib.sha256(report).hexdigest()),
        ):
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", metadata,
            )
        self.assertEqual(response.status_code, 200, response.content)

    def test_unauthenticated_completion_is_rejected_before_oss_head(self):
        job = self.claim_after_task()
        report = b'<html>DPS=1234</html>'
        metadata = {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'unauth', 'status': 'completed', 'stdout': 'DPS=1234',
            'stderr': '', 'report': {
                'object_key': f"simc_agent_results/simc_task_{job['task_id']}_run_{job['run_id']}.html",
                'size': len(report), 'sha256': hashlib.sha256(report).hexdigest(),
            },
        }
        with patch('botend.services.simc_agent_oss.verify_uploaded_report') as verify:
            response = self.post_json(
                f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/",
                metadata, token='invalid.invalid',
            )
        self.assertEqual(response.status_code, 401)
        verify.assert_not_called()

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
