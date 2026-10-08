"""Real ORM claim and execute_job boundary; process doubles are not combat evidence."""
import hashlib
import tempfile
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock, patch
from django.db import transaction
from django.test import TestCase, SimpleTestCase, override_settings
from botend import models
from botend.tests import test_simc_conditional_api_real_integration as fixtures
import simc_agent_consumer as agent


@override_settings(SIMC_AGENT_REQUIRED_REVISION='a'*40, ALLOWED_HOSTS=['testserver'], SIMC_CONFIG={'runtime_max_threads': 2})
class RuntimeClaimTests(TestCase):
    setUp = fixtures.ConditionalRealResourceAPITests.setUp
    backend_row = fixtures.ConditionalRealResourceAPITests.backend_row
    agent_row = fixtures.ConditionalRealResourceAPITests.agent_row
    post_json = fixtures.ConditionalRealResourceAPITests.post_json
    claim = fixtures.ConditionalRealResourceAPITests.claim

    def pair_task(self):
        from botend.services.simc_benchmark_config import _normalize_simulation_params
        candidates = []
        for side in ('normal', 'control'):
            params = deepcopy(self.sides[side]['params'])
            if params.get('equipment_effect_control') is False:
                params.pop('equipment_effect_control')
            params['equipment_effect_expectation'] = deepcopy(self.expectation)
            candidates.append({'candidate_key': side, 'candidate_params': params})
        return models.SimcTask.objects.create(user_id=1, name='same-task-runtime',
            simc_profile_id=self.objects['profile'].pk, backend=self.backend,
            profile=self.objects['profile'], apl=self.objects['apl'], template=self.objects['template'],
            talent_string=self.objects['talent'], profile_version=self.versions['profile'],
            apl_version=self.versions['apl'], template_version=self.versions['template'],
            talent_version=self.versions['talent'], mode='comparison',
            simulation_params=_normalize_simulation_params({'iterations': 100, 'max_time': 120,
                'desired_targets': 1, 'fight_style': 'Patchwerk', 'additional_simc_input': 'seed=41719'}),
            mode_params={'initial_candidates': candidates})

    def first_pair_claim(self):
        task = self.pair_task()
        with patch('os.sched_getaffinity', return_value={0, 1}):
            response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        job = response.json()
        # Real failed callback frees capacity without deleting the frozen input.
        response = self.post_json(f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", {
            'lease_token': job['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'runtime-test-no-combat', 'status': 'failed',
            'stdout': '', 'stderr': 'test releases lease without combat', 'report': None})
        self.assertEqual(response.status_code, 200, response.content)
        return task, job

    @override_settings(SIMC_CONFIG={'runtime_max_threads': 8})
    def test_same_task_sibling_and_reclaim_keep_first_cpu_policy(self):
        task, first = self.first_pair_claim()
        with patch('os.sched_getaffinity', return_value={0, 1, 2, 3}) as affinity:
            response = self.claim()
            self.assertEqual(response.status_code, 200, response.content)
            sibling = response.json()
            self.assertEqual(sibling['task_id'], task.pk)
            self.assertNotEqual(sibling['run_id'], first['run_id'])
            self.assertEqual([first['threads'], sibling['threads']], [1, 1])
            run = models.SimulationRun.objects.get(pk=sibling['run_id'])
            self.assertEqual(run.resource_manifest['runtime_threads'], 1)
            self.assertEqual([line for line in sibling['input'].splitlines()
                              if line.startswith('threads=')], ['threads=1'])
            models.SimulationRun.objects.filter(pk=run.pk).update(status='pending')
            retry = self.claim()
            self.assertEqual(retry.status_code, 200, retry.content)
            self.assertEqual(retry.json()['run_id'], run.pk)
            self.assertEqual(retry.json()['input'], sibling['input'])
            self.assertEqual(retry.json()['threads'], 1)
            affinity.assert_not_called()

    def test_missing_or_invalid_frozen_sibling_never_uses_live_cpu(self):
        for value in ('missing', True, 0, '1'):
            with self.subTest(value=value), transaction.atomic():
                task, first = self.first_pair_claim()
                run = models.SimulationRun.objects.get(pk=first['run_id'])
                manifest = deepcopy(run.resource_manifest)
                if value == 'missing':
                    manifest.pop('runtime_threads')
                else:
                    manifest['runtime_threads'] = value
                models.SimulationRun.objects.filter(pk=run.pk).update(resource_manifest=manifest)
                with patch('os.sched_getaffinity', side_effect=AssertionError('no live backfill')):
                    response = self.claim()
                self.assertEqual(response.status_code, 204, response.content)
                sibling = task.simulation_runs.exclude(pk=run.pk).get()
                self.assertEqual(sibling.status, 'failed')
                self.assertIn('Conditional', sibling.error_detail)
                self.assertIn('runtime threads', sibling.error_detail)
                run.refresh_from_db()
                self.assertEqual(run.resource_manifest, manifest)
                self.assertEqual(run.input_hash, first['input_hash'])
                transaction.set_rollback(True)

    def test_old_frozen_run_missing_threads_fails_closed_on_reclaim(self):
        task, first = self.first_pair_claim()
        run = models.SimulationRun.objects.get(pk=first['run_id'])
        manifest = deepcopy(run.resource_manifest)
        manifest.pop('runtime_threads')
        models.SimulationRun.objects.filter(pk=run.pk).update(status='pending', resource_manifest=manifest)
        with patch('os.sched_getaffinity', side_effect=AssertionError('no live backfill')):
            response = self.claim()
        self.assertEqual(response.status_code, 204, response.content)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('Conditional frozen runtime threads missing', run.error_detail)
        self.assertEqual(run.input_hash, first['input_hash'])
        self.assertEqual(run.resource_manifest, manifest)

    @override_settings(SIMC_CONFIG={'runtime_max_threads': 8})
    def test_sibling_claim_during_unlocked_enrichment_sees_reserved_policy(self):
        from botend.services import simc_run_control
        task = self.pair_task()
        self.agent.capabilities['max_concurrent_runs'] = 2
        self.agent.save(update_fields=['capabilities'])
        original = simc_run_control._enrich_frozen_manifest
        siblings = []
        entered = False
        def enrich(resolved, code, manifest):
            nonlocal entered
            if not entered:
                entered = True
                with patch('os.sched_getaffinity', return_value={0, 1, 2, 3}) as later_cpu:
                    sibling = self.claim()
                self.assertEqual(sibling.status_code, 200, sibling.content)
                siblings.append(sibling.json())
                later_cpu.assert_not_called()
            return original(resolved, code, manifest)
        with patch('os.sched_getaffinity', return_value={0, 1}) as initial_cpu, \
                patch.object(simc_run_control, '_enrich_frozen_manifest', side_effect=enrich):
            first = self.claim()
        self.assertEqual(first.status_code, 200, first.content)
        self.assertEqual([first.json()['threads'], siblings[0]['threads']], [1, 1])
        self.assertEqual(task.simulation_runs.filter(status='running').count(), 2)
        initial_cpu.assert_called_once()

    def test_conflicting_historical_threads_fail_closed(self):
        task, first = self.first_pair_claim()
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        run = models.SimulationRun.objects.get(pk=response.json()['run_id'])
        manifest = {**run.resource_manifest, 'runtime_threads': 2}
        models.SimulationRun.objects.filter(pk=run.pk).update(status='pending', resource_manifest=manifest)
        with patch('os.sched_getaffinity', side_effect=AssertionError('no live repair')):
            retry = self.claim()
        self.assertEqual(retry.status_code, 204, retry.content)
        run.refresh_from_db()
        self.assertEqual(run.status, 'failed')
        self.assertIn('Conditional frozen runtime threads conflict', run.error_detail)
        self.assertEqual(run.resource_manifest, manifest)
        self.assertEqual(run.input_hash, response.json()['input_hash'])

    def test_readonly_builder_uses_sibling_without_any_write(self):
        from django.db import connection
        from botend.services.simc_run_control import build_frozen_run_input
        task, first = self.first_pair_claim()
        run = task.simulation_runs.exclude(pk=first['run_id']).get()
        before = list(task.simulation_runs.values())
        def readonly(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(readonly), patch('os.sched_getaffinity',
                side_effect=AssertionError('must use frozen sibling')):
            code, manifest = build_frozen_run_input(task, run)
        self.assertEqual(manifest['runtime_threads'], 1)
        self.assertIn('threads=1\n', code)
        self.assertEqual(list(task.simulation_runs.values()), before)

    def test_unfrozen_preview_and_client_fields_cannot_freeze_policy(self):
        from django.db import connection
        from botend.services.simc_run_control import build_frozen_run_input
        from botend.services.simc_task_service import initialize_task_runs
        task = self.pair_task()
        run = initialize_task_runs(task)[0]
        before = list(task.simulation_runs.values())
        def readonly(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with connection.execute_wrapper(readonly), patch('os.sched_getaffinity', return_value={0, 1}):
            code, manifest = build_frozen_run_input(task, run)
        self.assertEqual(manifest['runtime_threads'], 1)
        self.assertEqual(list(task.simulation_runs.values()), before)
        # Neither public claim fields nor a manifest-like pending value is authority.
        rejected = self.post_json('/api/simc-agent/v1/jobs/claim/', {
            'instance_id': 'instance-a', 'runtime_threads': 99})
        self.assertEqual(rejected.status_code, 400, rejected.content)
        models.SimulationRun.objects.filter(pk=run.pk).update(resource_manifest={'runtime_threads': 99})
        with patch('os.sched_getaffinity', return_value={0, 1, 2}):
            response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['threads'], 2)
        run.refresh_from_db()
        self.assertEqual(run.resource_manifest['runtime_threads'], 2)

    def test_real_claim_freezes_threads_and_retry_ignores_cpu_drift(self):
        from botend.services.simc_run_control import runtime_threads, build_frozen_run_input
        from botend.services.simc_conditional_execution import CONTEXT_KEY, input_context_digest
        from botend.services.simc_benchmark_config import _normalize_simulation_params
        values = []
        for side in ('normal', 'control'):
            with transaction.atomic():
                params = deepcopy(self.sides[side]['params'])
                if params.get('equipment_effect_control') is False:
                    params.pop('equipment_effect_control')
                params['equipment_effect_expectation'] = deepcopy(self.expectation)
                task = models.SimcTask.objects.create(user_id=1, name='runtime-threads',
                    simc_profile_id=self.objects['profile'].pk, backend=self.backend,
                    profile=self.objects['profile'], apl=self.objects['apl'], template=self.objects['template'],
                    talent_string=self.objects['talent'], profile_version=self.versions['profile'],
                    apl_version=self.versions['apl'], template_version=self.versions['template'], talent_version=self.versions['talent'],
                    mode='comparison', simulation_params=_normalize_simulation_params({'iterations':100,
                        'max_time':120, 'desired_targets':1, 'fight_style':'Patchwerk', 'additional_simc_input':'seed=41719'}),
                    mode_params={'initial_candidates':[{'candidate_key':side, 'candidate_params':params}]})
                expected = runtime_threads(task)
                response = self.claim()
                self.assertEqual(response.status_code, 200, response.content)
                job = response.json()
                run = models.SimulationRun.objects.get(pk=job['run_id'])
                self.assertEqual(job['threads'], expected)
                self.assertEqual([x for x in job['input'].splitlines() if x.startswith('threads=')], [f'threads={expected}'])
                self.assertEqual(run.resource_manifest['runtime_threads'], expected)
                self.assertEqual(run.resource_manifest[CONTEXT_KEY], input_context_digest(job['input']))
                captured = capture_execute(self, job)
                self.assertIn(f'threads={expected}', captured)
                import os
                if os.environ.get('SIMC_RUNTIME_REAL_BINARY'):
                    execute_real(self, job, side, os.environ['SIMC_RUNTIME_REAL_BINARY'])
                values.append(expected)
                with patch('botend.services.simc_run_control.runtime_threads', side_effect=AssertionError('must use frozen policy')):
                    code, manifest = build_frozen_run_input(task, run, job['output_filename'])
                    self.assertEqual(code, job['input'])
                    models.SimulationRun.objects.filter(pk=run.pk).update(status='pending')
                    retry = self.claim()
                    self.assertEqual(retry.status_code, 200, retry.content)
                    self.assertEqual(retry.json()['input'], job['input'])
                    self.assertEqual(retry.json()['threads'], expected)
                transaction.set_rollback(True)
        self.assertEqual(values[0], values[1])


def execute_real(case, job, side, binary):
    import json
    import subprocess
    from simc_conditional_evidence import decode
    from botend.services.simc_conditional_execution import validate_prepared_context
    original_popen = subprocess.Popen
    evidence, commands = {}, []
    with tempfile.TemporaryDirectory() as root:
        c = agent.SimcAgentConsumer(agent.AgentConfig(simc_path=binary, token_path=str(Path(root)/'token')), transport=MagicMock())
        c.agent_token = 'test-token'
        c.transport.json.return_value = {'lease_expires_at':job['lease_expires_at']}
        def spawn(argv, **kwargs):
            commands.append(argv)
            return original_popen(argv, **kwargs)
        def save(*args, **kwargs):
            evidence.update(decode(kwargs['evidence_bytes']))
        with patch('simc_agent_consumer.subprocess.Popen', side_effect=spawn), patch.object(c, '_save_completion_outbox', side_effect=save), patch.object(c, 'flush_completion_outbox'), patch.object(c, '_complete') as complete:
            c.execute_job(job)
        case.assertTrue(evidence, str(complete.call_args))
        run = models.SimulationRun.objects.get(pk=job['run_id'])
        validate_prepared_context(run, evidence['prepared_input'])
        report = json.loads(evidence['report_json'])
        case.assertEqual(report['sim']['options']['threads'], job['threads'])
        case.assertIn(f"threads={job['threads']}", commands[-1])
        output = Path('/tmp/lmonitor-embellishment-audit')
        (output/f'conditional-runtime-threads-{side}.json').write_text(json.dumps({'commands':commands, 'threads':job['threads'], 'evidence':evidence}))


def capture_execute(case, job):
    with tempfile.TemporaryDirectory() as root:
        consumer = agent.SimcAgentConsumer(agent.AgentConfig(simc_path='/bin/true', token_path=str(Path(root)/'token')), transport=MagicMock())
        consumer.agent_token = 'test-token'
        consumer.transport.json.return_value = {'lease_expires_at':job['lease_expires_at']}
        command = []
        def spawn(argv, **kw):
            command.extend(argv)
            process = MagicMock(returncode=1)
            process.communicate.return_value = (b'', b'')
            return process
        with patch('simc_equipment_control.prepare_control_input', side_effect=lambda text,*a,**kw:text), patch('simc_agent_consumer.subprocess.Popen', side_effect=spawn), patch.object(consumer, '_complete') as complete:
            consumer.execute_job(job)
        return command


class RuntimeConsumerTests(SimpleTestCase):
    def job(self):
        text = 'threads=4\n'
        return dict(run_id=7, lease_token='lease', input=text, input_hash=hashlib.sha256(text.encode()).hexdigest(),
            output_filename='simc_task_1_run_7.html', timeout_seconds=10, lease_expires_at='2999-01-01T00:00:00+00:00')

    def test_cli_controlled_threads_and_legacy_missing(self):
        job = self.job()
        self.assertFalse(any(x.startswith('threads=') for x in capture_execute(self, job)))
        job['threads'] = 2
        self.assertEqual([x for x in capture_execute(self, job) if x.startswith('threads=')], ['threads=2'])

    def test_invalid_threads_never_spawn(self):
        for value in (True, False, 0, -1, '2', '2\ninput=evil', None, 1.5):
            with self.subTest(value=value):
                self.assertEqual(capture_execute(self, {**self.job(), 'threads':value}), [])

    def test_conditional_mismatch_never_spawn(self):
        self.assertEqual(capture_execute(self, {**self.job(), 'threads':2, 'conditional_authorization':{}}), [])
