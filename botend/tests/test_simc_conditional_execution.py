from copy import deepcopy
from django.test import TestCase, override_settings
from botend.models import SimulationRun, WowItemSnapshot
from botend.tests.test_simc_conditional_store import ConditionalStoreTests
from botend.tests.test_simc_agent_jobs_api import SimcAgentJobAPITests


@override_settings(SIMC_AGENT_REQUIRED_REVISION='a' * 40, ALLOWED_HOSTS=['testserver'])
class ConditionalExecutionTests(TestCase):
    import_source = ConditionalStoreTests.import_source
    approve = ConditionalStoreTests.approve
    backend_row = SimcAgentJobAPITests.backend_row
    agent_row = SimcAgentJobAPITests.agent_row
    task = SimcAgentJobAPITests.task
    claim = SimcAgentJobAPITests.claim
    post_json = SimcAgentJobAPITests.post_json
    complete = SimcAgentJobAPITests.complete

    def setUp(self):
        ConditionalStoreTests.setUp(self)
        SimcAgentJobAPITests.setUp(self)
        identity = self.expectation['identity']
        self.agent.platform = 'linux'
        self.agent.capabilities = {
            'conditional_evidence_protocol_version': 1, 'binary_identity_status': 'ok',
            'binary_sha256': identity['binary_sha256'], 'binary_revision': identity['revision'],
            'dbc_build': identity['dbc_build'],
        }
        self.agent.save(update_fields=['platform', 'capabilities'])

    def conditional_task(self):
        swaps = [{'slot': t['slot'], 'item_id': t['item_id'],
                  'raw_value': ',id=' + str(t['item_id'])} for t in self.expectation['targets']]
        swaps.sort(key=lambda s: self.policy['target_slots'].index(s['slot']))
        params = {'candidate_type': 'gear_swap', 'gear_swaps': swaps,
                  'equipment_effect_policy': deepcopy(self.policy),
                  'equipment_effect_expectation': deepcopy(self.expectation)}
        task = self.task(mode='comparison', candidates=[{'candidate_params': params}])
        version = task.profile_version
        version.payload = {**version.payload, 'use_ptr': True, 'player_equipment':
            'warrior=Agent\nspec=fury\n' + '\n'.join(s['slot'] + '=' + s['raw_value'] for s in swaps)}
        from botend.models import SimcResourceVersion
        task.profile_version = SimcResourceVersion.objects.create(
            resource_type='profile', resource_id=task.profile_id,
            content_hash='conditional-profile-' + str(task.pk), payload=version.payload)
        # Conditional composition must expose the server-owned threads directive.
        # Do not change the shared ordinary fixture or mutate its immutable version.
        task.template_version = SimcResourceVersion.objects.create(
            resource_type='template', resource_id=task.template_id,
            content_hash='conditional-template-' + str(task.pk),
            payload={**task.template_version.payload,
                     'content': '{simulation_options}\n{player_config}\n{output_options}'})
        task.save(update_fields=['profile_version', 'template_version'])
        return task

    def test_real_claim_freezes_central_authorization_and_reader_is_detached(self):
        self.approve()
        task = self.conditional_task()
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body.get('conditional_authorization'), self.fixture['authorization'])
        from botend.services.simc_conditional_execution import read_frozen_conditional_execution
        run = SimulationRun.objects.get(pk=body['run_id'])
        self.assertEqual(run.task_id, task.pk)
        self.assertIn('conditional_execution', run.resource_manifest)
        WowItemSnapshot.objects.all().delete()
        frozen = read_frozen_conditional_execution(run.pk)
        self.assertEqual(frozen['conditional_authorization'], body['conditional_authorization'])
        frozen['conditional_authorization'].clear()
        self.assertEqual(read_frozen_conditional_execution(run.pk)['conditional_authorization'], body['conditional_authorization'])

    def test_protocol_zero_rejects_conditional_without_mutation(self):
        self.approve()
        task = self.conditional_task()
        self.agent.capabilities['conditional_evidence_protocol_version'] = 0
        self.agent.save(update_fields=['capabilities'])
        response = self.claim()
        self.assertEqual(response.status_code, 409, response.content)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)
        self.assertFalse(task.simulation_runs.exists())

    def test_candidate_cannot_self_approve(self):
        self.import_source()
        self.conditional_task()
        response = self.claim()
        self.assertEqual(response.status_code, 409, response.content)
        self.assertFalse(SimulationRun.objects.exists())

    def test_enrichment_cannot_replace_authority(self):
        from unittest.mock import patch
        from botend.services import simc_run_control
        original = simc_run_control._enrich_frozen_manifest
        self.approve()
        self.conditional_task()
        def corrupt(resolved, code, manifest):
            result = original(resolved, code, manifest)
            result['conditional_execution'] = {'conditional_authorization': {'forged': True}}
            return result
        with patch.object(simc_run_control, '_enrich_frozen_manifest', side_effect=corrupt):
            response = self.claim()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['conditional_authorization'], self.fixture['authorization'])
        run = SimulationRun.objects.get(pk=response.json()['run_id'])
        self.assertEqual(run.resource_manifest['conditional_execution']['conditional_authorization'], self.fixture['authorization'])

    def test_exact_agent_identity_and_branch_mismatches_fail_closed(self):
        self.approve()
        task = self.conditional_task()
        original = deepcopy(self.agent.capabilities)
        for key, value in [('binary_sha256', '0' * 64), ('binary_revision', '0' * 40),
                           ('dbc_build', '12.1.0.69934'), ('binary_identity_status', 'failed')]:
            with self.subTest(key=key):
                self.agent.capabilities = {**original, key: value}
                self.agent.save(update_fields=['capabilities'])
                self.assertEqual(self.claim().status_code, 409)
        self.agent.capabilities = original
        self.agent.platform = 'windows'
        self.agent.save(update_fields=['capabilities', 'platform'])
        self.assertEqual(self.claim().status_code, 409)
        task.refresh_from_db()
        self.assertEqual(task.current_status, 0)
        self.assertFalse(SimulationRun.objects.exists())

    def test_completion_missing_freeze_rejected_before_report_storage(self):
        from botend.services.simc_conditional_execution import read_frozen_conditional_execution
        from django.core.exceptions import ValidationError
        self.approve()
        self.conditional_task()
        body = self.claim().json()
        run = SimulationRun.objects.get(pk=body['run_id'])
        run.resource_manifest = {}
        run.save(update_fields=['resource_manifest'])
        with self.assertRaises(ValidationError):
            read_frozen_conditional_execution(run.pk)
        response = self.post_json('/api/simc-agent/v1/jobs/%s/complete/' % run.pk, {
            'lease_token': body['lease_token'], 'instance_id': 'instance-a',
            'completion_id': 'missing-freeze', 'status': 'failed',
            'stdout': '', 'stderr': 'failed', 'report': None,
        })
        self.assertEqual(response.status_code, 409, response.content)
        run.refresh_from_db()
        self.assertEqual(run.status, 'running')

    @override_settings(OSS_CONFIG={'base_url': 'https://reports.example'})
    def test_protocol_zero_keeps_v2_equipment_claim_compatible(self):
        self.agent.capabilities['conditional_evidence_protocol_version'] = 0
        self.agent.save(update_fields=['capabilities'])
        task = self.conditional_task()
        params = task.mode_params['initial_candidates'][0]['candidate_params']
        params['equipment_effect_policy'] = {'version': 2, 'target_slots': self.policy['target_slots'],
                                             'rules': self.policy['rules']}
        params.pop('equipment_effect_expectation')
        task.save(update_fields=['mode_params'])
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('conditional_authorization', response.json())
        # Real ORM reader/fence; only external report I/O is mocked by helper.
        completed = self.complete(response.json())
        self.assertEqual(completed.status_code, 200, completed.content)
        run = SimulationRun.objects.get(pk=response.json()['run_id'])
        self.assertEqual(run.status, 'completed')
        self.assertNotIn('conditional_execution', run.resource_manifest)

    def test_missing_measured_revision_uses_unique_approval_not_marker(self):
        self.approve()
        self.conditional_task()
        self.agent.capabilities.pop('binary_revision')
        self.agent.capabilities['current_version'] = '0' * 40
        self.agent.save(update_fields=['capabilities'])
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        frozen = SimulationRun.objects.get(pk=response.json()['run_id']).resource_manifest['conditional_execution']
        self.assertIsNone(frozen['actual_observation']['revision'])
        self.assertEqual(frozen['expectation']['identity'], self.expectation['identity'])
        from botend.services.simc_conditional_execution import freeze_for_agent
        from django.core.exceptions import ValidationError
        run = SimulationRun.objects.get(pk=response.json()['run_id'])
        with self.assertRaises(ValidationError):
            freeze_for_agent(run, agent=self.agent, is_ptr=False)
        self.agent.capabilities['binary_revision'] = '0' * 40
        with self.assertRaises(ValidationError):
            freeze_for_agent(run, agent=self.agent, is_ptr=True)

    def test_mixed_queue_progress_pending_and_running(self):
        from botend.services.simc_task_service import initialize_task_runs
        from django.utils import timezone
        from botend.models import SimcTask
        self.approve()
        original = deepcopy(self.agent.capabilities)
        for running in (False, True):
            for mismatch in ('protocol', 'platform', 'approval', 'identity', 'branch'):
                with self.subTest(running=running, mismatch=mismatch):
                    SimcTask.objects.all().delete()
                    self.agent.capabilities = deepcopy(original)
                    self.agent.platform = 'linux'
                    if mismatch == 'protocol':
                        self.agent.capabilities['conditional_evidence_protocol_version'] = 0
                    if mismatch == 'platform':
                        self.agent.platform = 'windows'
                    if mismatch == 'identity':
                        self.agent.capabilities['binary_sha256'] = '0' * 64
                    self.agent.save(update_fields=['capabilities', 'platform'])
                    conditional = self.conditional_task()
                    conditional.queue_priority = 100
                    if mismatch == 'branch':
                        from botend.models import SimcResourceVersion
                        version = conditional.profile_version
                        conditional.profile_version = SimcResourceVersion.objects.create(
                            resource_type='profile', resource_id=conditional.profile_id,
                            content_hash='live-' + str(conditional.pk),
                            payload={**version.payload, 'use_ptr': False})
                    if mismatch == 'approval':
                        from botend.services.simc_conditional_store import META_KEY
                        item = WowItemSnapshot.objects.get(item_id=self.owner)
                        before = deepcopy(item.metadata)
                        next(iter(item.metadata[META_KEY].values()))['executables'] = {}
                        item.save(update_fields=['metadata'])
                    if running:
                        conditional.current_status = 1
                        conditional.execution_owner = conditional.EXECUTION_OWNER_AGENT
                        conditional.started_at = timezone.now()
                    conditional.save()
                    if running:
                        initialize_task_runs(conditional, expected_started_at=conditional.started_at)
                    ordinary = self.task(name='ordinary-behind-conditional')
                    response = self.claim()
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertEqual(response.json()['task_id'], ordinary.pk)
                    conditional.refresh_from_db()
                    self.assertEqual(conditional.current_status, int(running))
                    self.assertEqual(list(conditional.simulation_runs.values_list('status', flat=True)),
                                     ['pending'] if running else [])
                    if mismatch == 'approval':
                        item.metadata = before
                        item.save(update_fields=['metadata'])

    def test_progress_beyond_first_discovery_page(self):
        from unittest.mock import patch
        from botend.services import simc_run_control
        self.approve()
        first = self.conditional_task()
        # Clone Tasks, not resources; all incompatible tasks precede ordinary.
        for unused in range(6):
            clone = deepcopy(first)
            clone.pk = None
            clone.save()
        ordinary = self.task(name='ordinary-after-pages')
        self.agent.capabilities['conditional_evidence_protocol_version'] = 0
        self.agent.save(update_fields=['capabilities'])
        with patch.object(simc_run_control, 'CLAIM_DISCOVERY_PAGE_SIZE', 2, create=True):
            from django.db import connection
            from django.test.utils import CaptureQueriesContext
            from django.utils import timezone
            with CaptureQueriesContext(connection) as queries:
                ids = list(simc_run_control._remaining_claim_task_ids(self.agent, timezone.now(), first.pk))
            self.assertEqual(len(ids), 7)
            self.assertEqual(len(queries), 6)  # MAX + empty running + four pending pages
            for query in queries.captured_queries[1:]:
                self.assertIn('LIMIT 2', query['sql'])
                self.assertNotIn('OFFSET', query['sql'])
                self.assertNotIn('mode_params', query['sql'])
            response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['task_id'], ordinary.pk)
        self.assertEqual(SimulationRun.objects.count(), 1)

    def test_protocol_zero_keeps_ordinary_claim_compatible(self):
        self.agent.capabilities['conditional_evidence_protocol_version'] = 0
        self.agent.save(update_fields=['capabilities'])
        self.task()
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('conditional_authorization', response.json())
