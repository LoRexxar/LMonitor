"""Real ORM regressions for stale recovery's attempt/provenance boundary."""
import hashlib
import json
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from botend import models
from botend.services.simc_worker import SimcWorker
from botend.services.task_rerun import create_rerun


@override_settings(SIMC_WORKER_STALE_SECONDS=60, SIMC_WORKER_MAX_ATTEMPTS=3)
class StaleWorkerRetryBudgetTests(TestCase):
    def setUp(self):
        self.backend = models.SimcBackendBinary.objects.create(
            identifier='retry-budget', name='Retry budget',
        )
        self.references = {}
        resources = (
            ('profile', models.SimcProfile, {'user_id': 1, 'name': 'Profile',
             'class_name': 'warrior', 'spec': 'warrior_fury'}),
            ('template', models.SimcContentTemplate, {'name': 'Template',
             'spec': 'warrior_fury', 'content': 'iterations=1000', 'owner_user_id': 1}),
            ('apl', models.SimcApl, {'name': 'APL', 'spec': 'warrior_fury',
             'content': 'actions=/auto_attack', 'owner_user_id': 1}),
            ('talent', models.SimcTalentString, {'name': 'Talents',
             'spec': 'warrior_fury', 'talent': 'CYEAoonA', 'owner_user_id': 1}),
        )
        for kind, model, payload in resources:
            resource = model.objects.create(**payload)
            self.references['talent_string' if kind == 'talent' else kind] = resource
            self.references[f'{kind}_version'] = models.SimcResourceVersion.objects.create(
                resource_type=kind, resource_id=resource.pk, payload=payload,
                content_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
            )
        self.worker = SimcWorker()

    def task(self, *, source=None, status=0):
        return models.SimcTask.objects.create(
            user_id=1, name='Frozen request', simc_profile_id=0, backend=self.backend,
            source_task=source, current_status=status, mode='comparison',
            simulation_params={'iterations': 1000},
            mode_params={'initial_candidates': [
                {'candidate_key': 'baseline', 'candidate_params': {'is_base': True}},
                {'candidate_key': 'candidate', 'candidate_params': {'is_base': False}},
            ]}, **self.references,
        )

    def expire(self, task, *, agent=None):
        now = timezone.now()
        models.SimcTask.objects.filter(pk=task.pk).update(
            current_status=1, started_at=now,
            modified_time=now if agent else now - timedelta(minutes=5),
        )
        if agent is not None:
            return models.SimulationRun.objects.create(
                task=task, sequence=1, candidate_key='baseline', status='running',
                lease_agent=agent, lease_token_hash='old-lease', lease_instance_id='old-instance',
                lease_heartbeat_at=now - timedelta(minutes=5),
                lease_expires_at=now - timedelta(seconds=1),
            )

    def assert_retry(self, source):
        source.refresh_from_db()
        self.assertEqual(source.current_status, 3)
        self.assertIn('已复制 Task 重试', source.error_detail)
        retry = source.reruns.get()
        self.assertEqual(retry.current_status, 0)
        self.assertEqual(retry.mode_params, source.mode_params)
        self.assertEqual(retry.simulation_params, source.simulation_params)
        self.assertEqual(retry.backend_id, source.backend_id)
        for field in self.references:
            self.assertEqual(getattr(retry, f'{field}_id'), getattr(source, f'{field}_id'))
        self.assertFalse(retry.simulation_runs.exists())
        return retry

    def assert_exhausted(self, task):
        task.refresh_from_db()
        self.assertEqual(task.current_status, 3)
        self.assertEqual(task.error_detail, f'Worker 重试次数上限（{self.worker.max_attempts}）')
        self.assertFalse(task.reruns.exists())
        self.assertEqual(self.worker.recover_stale_tasks(), 0)

    def test_successful_supplement_history_gets_full_budget_and_preserves_fences(self):
        oldest = self.task(status=2)
        successful = self.task(source=oldest, status=2)
        historical_ids = [oldest.pk, successful.pk]
        historical = list(models.SimcTask.objects.filter(pk__in=historical_ids).order_by('pk').values())
        task = self.task(source=successful)
        panel = models.SimcBenchmarkPanel.objects.create(
            name='Budget', slug='retry-budget', created_by_id=1,
        )
        execution = models.SimcBenchmarkExecution.objects.create(
            panel=panel, config_hash='frozen', status='running',
        )
        case = models.SimcBenchmarkCase.objects.create(
            execution=execution, task=task, status='running', error_detail='old projection',
            spec_key='warrior_fury', scenario_key='patchwerk', profile_key='profile',
            coordinate_hash='frozen',
        )
        models.SimcBenchmarkResult.objects.create(case=case, candidate_key='baseline', dps=100)
        agent = models.SimcAgent.objects.create(
            backend=self.backend, host_identifier='budget-agent', platform='linux64',
            status=models.SimcAgent.STATUS_BUSY, last_seen_at=timezone.now(),
        )
        expired = self.expire(task, agent=agent)
        sibling = models.SimulationRun.objects.create(
            task=task, sequence=2, candidate_key='candidate', status='pending',
        )
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        retry = self.assert_retry(task)
        case.refresh_from_db()
        self.assertEqual((case.task_id, case.status, case.error_detail), (retry.pk, 'pending', ''))
        self.assertFalse(case.results.exists())
        expired.refresh_from_db()
        sibling.refresh_from_db()
        self.assertEqual((expired.status, sibling.status), ('failed', 'failed'))
        self.assertEqual(expired.error_detail, 'Agent 租约过期')
        self.assertEqual(sibling.error_detail, 'Agent 同级租约已失效')
        self.assertIsNone(expired.lease_agent_id)
        self.assertIsNone(expired.lease_expires_at)
        self.assertIsNone(expired.lease_heartbeat_at)
        self.assertEqual((expired.lease_token_hash, expired.lease_instance_id), ('', ''))
        agent.refresh_from_db()
        self.assertEqual(agent.status, models.SimcAgent.STATUS_ONLINE)
        self.assertEqual(self.worker.recover_stale_tasks(), 0)
        self.expire(retry, agent=agent)
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        last = self.assert_retry(retry)
        self.expire(last, agent=agent)
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        self.assert_exhausted(last)
        case.refresh_from_db()
        self.assertEqual(case.task_id, last.pk)
        self.assertEqual(
            list(models.SimcTask.objects.filter(pk__in=historical_ids).order_by('pk').values()),
            historical,
        )

    def test_local_stale_recovery_without_provenance_stops_after_three_attempts(self):
        task = self.task()
        for _ in range(2):
            self.expire(task)
            self.assertEqual(self.worker.recover_stale_tasks(), 1)
            task = self.assert_retry(task)
        self.expire(task)
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        self.assert_exhausted(task)
        self.assertEqual(models.SimcTask.objects.count(), 3)

    def test_success_boundary_excludes_failures_from_older_attempt_series(self):
        older_failure = self.task(status=3)
        old_retry = create_rerun(older_failure.pk, 1)
        models.SimcTask.objects.filter(pk=old_retry.pk).update(current_status=3)
        successful = create_rerun(old_retry.pk, 1)
        models.SimcTask.objects.filter(pk=successful.pk).update(current_status=2)
        failed = self.task(source=successful, status=3)
        current = create_rerun(failed.pk, 1)
        self.expire(current)
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        last = self.assert_retry(current)
        self.expire(last)
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        self.assert_exhausted(last)

    def test_cycles_fail_closed_even_below_attempt_limit(self):
        for length in (1, 2):
            with self.subTest(length=length):
                task = self.task()
                ancestor = task if length == 1 else self.task(source=task, status=3)
                models.SimcTask.objects.filter(pk=task.pk).update(source_task=ancestor)
                self.expire(task)
                before = models.SimcTask.objects.count()
                self.assertEqual(self.worker.recover_stale_tasks(), 1)
                task.refresh_from_db()
                self.assertEqual(task.current_status, 3)
                self.assertEqual(task.error_detail, 'Worker 重试次数上限（3）')
                self.assertEqual(models.SimcTask.objects.count(), before)
                self.assertEqual(self.worker.recover_stale_tasks(), 0)

    @override_settings(SIMC_WORKER_MAX_ATTEMPTS=1)
    def test_success_boundary_does_not_bypass_single_attempt_limit(self):
        self.worker = SimcWorker()
        task = self.task(source=self.task(status=2))
        self.expire(task)
        self.assertEqual(self.worker.recover_stale_tasks(), 1)
        self.assert_exhausted(task)
