import base64
from copy import deepcopy
from unittest.mock import patch
from django.test import TestCase
from django.test import override_settings
from botend.models import SimulationRun
from botend.models import SimcTaskArtifact
from botend.tests import test_simc_conditional_execution as fixtures
from contextlib import ExitStack
from botend.models import SimcAgent
from botend.models import SimcTask

@override_settings(SIMC_AGENT_REQUIRED_REVISION='a' * 40, ALLOWED_HOSTS=['testserver'],
                   OSS_CONFIG={'base_url': 'https://reports.example'})
class ConditionalEvidenceAPITests(TestCase):
    setUp = fixtures.ConditionalExecutionTests.setUp
    import_source = fixtures.ConditionalExecutionTests.import_source
    approve = fixtures.ConditionalExecutionTests.approve
    backend_row = fixtures.ConditionalExecutionTests.backend_row
    agent_row = fixtures.ConditionalExecutionTests.agent_row
    task = fixtures.ConditionalExecutionTests.task
    claim = fixtures.ConditionalExecutionTests.claim
    post_json = fixtures.ConditionalExecutionTests.post_json
    conditional_task = fixtures.ConditionalExecutionTests.conditional_task

    def job(self):
        self.approve()
        self.conditional_task()
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def metadata(self, job):
        from botend.services.simc_agent_oss import object_key_for_run
        run = SimulationRun.objects.get(pk=job['run_id'])
        return dict(lease_token=job['lease_token'], instance_id='instance-a',
            completion_id='conditional-api', status='completed', stdout='DPS=100', stderr='',
            report=dict(object_key=object_key_for_run(run), size=100, sha256='a'*64))

    def pointer(self, job):
        from botend.services.simc_conditional_evidence_storage import object_key_for_run
        run = SimulationRun.objects.get(pk=job['run_id'])
        return dict(protocol_version=1, object_key=object_key_for_run(run, lease_fence=run.lease_token_hash), size=100, sha256='a'*64)

    def test_missing_evidence_rejected_before_network(self):
        job = self.job()
        with patch('botend.services.simc_agent_oss.verify_uploaded_report') as network:
            response = self.post_json(f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", self.metadata(job))
        self.assertEqual(response.status_code, 400, response.content)
        network.assert_not_called()
        self.assertFalse(SimcTaskArtifact.objects.exists())

    def test_upload_route_server_digest_and_stale_ack(self):
        job = self.job()
        run = SimulationRun.objects.get(pk=job['run_id'])
        payload = dict(lease_token=job['lease_token'], instance_id='instance-a', size=100,
                       sha256='a'*64, content_md5=base64.b64encode(b'a'*16).decode())
        path = f"/api/simc-agent/v1/jobs/{run.pk}/evidence-upload/"
        with patch('botend.services.simc_conditional_evidence_storage.issue_upload_ticket', return_value={'protocol_version':1}) as issue:
            response = self.post_json(path, payload)
            self.assertEqual(response.status_code, 200, response.content)
            self.assertEqual(issue.call_args.kwargs['lease_fence'], run.lease_token_hash)
            self.assertEqual(response['Cache-Control'], 'no-store')
            issue.reset_mock()
            response = self.post_json(path, {**payload, 'lease_token':'x'*43})
            self.assertTrue(response.json()['already_completed'])
            issue.assert_not_called()
            self.assertEqual(self.post_json(path, {**payload, 'authorization':{}}).status_code,400)

    def test_metadata_strict_optional_pointer(self):
        from botend.services.simc_run_control import validate_completion_metadata
        from botend.services.simc_agent_control import AgentAPIError
        job = self.job()
        metadata = self.metadata(job)
        pointer = self.pointer(job)
        self.assertEqual(validate_completion_metadata({**metadata, 'conditional_evidence':pointer})['conditional_evidence'],pointer)
        for bad in ({**pointer,'protocol_version':True}, {**pointer,'auth':{}}, {**pointer,'size':0}):
            with self.assertRaises(AgentAPIError):
                validate_completion_metadata({**metadata,'conditional_evidence':bad})
        with self.assertRaises(AgentAPIError):
            validate_completion_metadata({**metadata,'status':'failed','report':None,'conditional_evidence':pointer})

    def test_wrong_task_run_fence_pointer_never_downloads(self):
        job = self.job()
        pointer = self.pointer(job)
        path = f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/"
        for key in (pointer['object_key'].replace('task_', 'task_9'),
                    pointer['object_key'].replace('run_', 'run_9'),
                    pointer['object_key'].rsplit('/',1)[0] + '/wrong.json.gz'):
            with patch('botend.services.simc_conditional_evidence_storage.download_evidence') as download:
                response = self.post_json(path, {**self.metadata(job), 'conditional_evidence':{**pointer,'object_key':key}})
            self.assertEqual(response.status_code, 409, response.content)
            download.assert_not_called()
        self.assertFalse(SimcTaskArtifact.objects.exists())

    def test_completion_pair_pending_and_lease_replacement(self):
        from contextlib import ExitStack
        from botend.services.simc_run_control import _lease_digest
        job = self.job()
        payload = {**self.metadata(job), 'conditional_evidence':self.pointer(job)}
        path = f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/"
        body = {'prepared_input':job['input'], 'report_json':'{}'}
        for replace_lease in (True, False):
            with ExitStack() as stack:
                def download(*args, **kwargs):
                    self.assertEqual(kwargs['expected_lease_fence'], _lease_digest(job['lease_token']))
                    if replace_lease:
                        SimulationRun.objects.filter(pk=job['run_id']).update(lease_token_hash=_lease_digest('replacement'))
                    return body
                stack.enter_context(patch('botend.services.simc_conditional_evidence_storage.download_evidence',side_effect=download))
                stack.enter_context(patch('botend.services.simc_agent_oss.verify_uploaded_report'))
                stack.enter_context(patch('botend.services.simc_agent_oss.download_report_html',return_value=('<html/>','a'*64)))
                stack.enter_context(patch('botend.controller.plugins.simc.SimcMonitor.SimcMonitor.validate_simulation_semantics',return_value={'valid':True,'dps':100}))
                validator = stack.enter_context(patch('botend.services.simc_equipment_effect_validation.validate_equipment_effect_report',return_value={'status':'pair_pending','valid':None}))
                response = self.post_json(path,payload)
            if replace_lease:
                self.assertEqual(response.status_code,409,response.content)
                self.assertFalse(SimcTaskArtifact.objects.exists())
                SimulationRun.objects.filter(pk=job['run_id']).update(lease_token_hash=_lease_digest(job['lease_token']))
            else:
                self.assertEqual(response.status_code,200,response.content)
                run = SimulationRun.objects.get(pk=job['run_id'])
                self.assertEqual(run.result_summary['equipment_effect_validation']['status'],'pair_pending')
                self.assertEqual(validator.call_args.kwargs['prepared_input'],body['prepared_input'])
                self.assertEqual(validator.call_args.kwargs['report_json'],'{}')
                self.assertEqual(validator.call_args.kwargs['conditional_authorization'],job['conditional_authorization'])
                self.assertNotIn('conditional_evidence',run.result_summary)

    def test_claim_context_is_authority_not_two_matching_agent_inputs(self):
        from botend.services.simc_conditional_execution import validate_prepared_context
        from django.core.exceptions import ValidationError
        job = self.job()
        run = SimulationRun.objects.get(pk=job['run_id'])
        validate_prepared_context(run, job['input'])
        for wrong in (job['input'].replace('warrior=Agent','warrior=Wrong'), job['input']+'\nactions=/wrong\n', job['input']+'\niterations=1\n'):
            with self.subTest(wrong=wrong[-40:]), self.assertRaises(ValidationError):
                validate_prepared_context(run, wrong)

"""Owner fences with real ORM/auth; transport and semantic parsing isolated."""


@override_settings(SIMC_AGENT_REQUIRED_REVISION='a' * 40, ALLOWED_HOSTS=['testserver'],
                   OSS_CONFIG={'base_url': 'https://reports.example'})
class ConditionalOwnerTests(TestCase):
    for _name in ('setUp', 'import_source', 'approve', 'backend_row', 'agent_row', 'task',
                  'claim', 'post_json', 'conditional_task', 'job', 'metadata', 'pointer'):
        locals()[_name] = getattr(ConditionalEvidenceAPITests, _name)

    def upload(self, job):
        return dict(lease_token=job['lease_token'], instance_id='instance-a', size=100,
                    sha256='a'*64, content_md5=base64.b64encode(b'a'*16).decode())

    def test_cross_backend_all_entries_rejected_before_io_including_terminal(self):
        job = self.job()
        payload = {**self.metadata(job), 'conditional_evidence': self.pointer(job)}
        with ExitStack() as stack:
            networks = [stack.enter_context(patch(name, return_value={})) for name in (
                'botend.services.simc_conditional_evidence_storage.issue_upload_ticket',
                'botend.services.simc_agent_oss.issue_upload_ticket',
                'botend.services.simc_conditional_evidence_storage.download_evidence',
                'botend.services.simc_agent_oss.verify_uploaded_report',
                'botend.services.simc_agent_oss.download_report_html')]
            from botend.services.simc_agent_oss import ReportValidationError
            networks[2].side_effect = ReportValidationError('unauthorized download reached')
            for status in ('running', 'completed'):
                SimulationRun.objects.filter(pk=job['run_id']).update(status=status)
                for entry in ('evidence-upload', 'report-upload', 'complete'):
                    with self.subTest(status=status, entry=entry):
                        response = self.post_json(f"/api/simc-agent/v1/jobs/{job['run_id']}/{entry}/",
                            payload if entry == 'complete' else self.upload(job), self.other_token)
                        self.assertEqual(response.status_code, 403, response.content)
            for network in networks:
                network.assert_not_called()
        self.assertFalse(SimcTaskArtifact.objects.exists())

    def test_same_backend_rotation_upload_keeps_original_lease(self):
        job = self.job()
        agent, token = self.agent_row(self.backend, 'rotated')
        for entry, storage in (('evidence-upload', 'simc_conditional_evidence_storage'),
                               ('report-upload', 'simc_agent_oss')):
            with patch(f'botend.services.{storage}.issue_upload_ticket', return_value={}) as issue:
                response = self.post_json(f"/api/simc-agent/v1/jobs/{job['run_id']}/{entry}/", self.upload(job), token)
            self.assertEqual(response.status_code, 200, response.content)
            issue.assert_called_once()
        self.assertEqual(SimulationRun.objects.get(pk=job['run_id']).lease_agent_id, self.agent.pk)

    def test_commit_rechecks_backend_task_agent_and_positive_rotation(self):
        job = self.job()
        original = SimulationRun.objects.get(pk=job['run_id'])
        _, token = self.agent_row(self.backend, 'rotated')
        rotated = SimcAgent.objects.get(name='rotated')
        other_task = self.task(name='other-owned-task')
        payload = {**self.metadata(job), 'conditional_evidence': self.pointer(job)}
        for drift in ('task_backend', 'agent_backend', 'both_backends', 'task', 'terminal_backend', None):
            SimcTask.objects.filter(pk=job['task_id']).update(backend=self.backend)
            SimcAgent.objects.filter(pk=rotated.pk).update(backend=self.backend)
            SimulationRun.objects.filter(pk=original.pk).update(task_id=job['task_id'], status='running')
            SimcTaskArtifact.objects.all().delete()
            with self.subTest(drift=drift), ExitStack() as stack:
                def download(*args, **kwargs):
                    if drift in ('task_backend', 'both_backends', 'terminal_backend'):
                        SimcTask.objects.filter(pk=job['task_id']).update(backend=self.other)
                    if drift in ('agent_backend', 'both_backends'):
                        SimcAgent.objects.filter(pk=rotated.pk).update(backend=self.other)
                    if drift == 'task':
                        SimulationRun.objects.filter(pk=original.pk).update(task=other_task)
                    if drift == 'terminal_backend':
                        SimulationRun.objects.filter(pk=original.pk).update(status='completed')
                    return {'prepared_input': job['input'], 'report_json': '{}'}
                stack.enter_context(patch('botend.services.simc_conditional_evidence_storage.download_evidence', side_effect=download))
                stack.enter_context(patch('botend.services.simc_agent_oss.verify_uploaded_report'))
                stack.enter_context(patch('botend.services.simc_agent_oss.download_report_html', return_value=('<html/>', 'a'*64)))
                stack.enter_context(patch('botend.controller.plugins.simc.SimcMonitor.SimcMonitor.validate_simulation_semantics', return_value={'valid': True, 'dps': 100}))
                stack.enter_context(patch('botend.services.simc_equipment_effect_validation.validate_equipment_effect_report', return_value={'status': 'pair_pending', 'valid': None}))
                response = self.post_json(f"/api/simc-agent/v1/jobs/{original.pk}/complete/", payload, token)
                if drift is None:
                    self.assertEqual(response.status_code, 200, response.content)
                    self.assertTrue(SimcTaskArtifact.objects.filter(run_id=original.pk).exists())
                    original.refresh_from_db()
                    self.assertEqual(original.lease_agent_id, self.agent.pk)
                else:
                    self.assertIn(response.status_code, (403, 404, 409), response.content)
                    self.assertFalse(SimcTaskArtifact.objects.exists())
                SimcTask.objects.filter(pk=job['task_id']).update(backend=self.backend)
                SimcAgent.objects.filter(pk=rotated.pk).update(backend=self.backend)
                SimulationRun.objects.filter(pk=original.pk).update(task_id=job['task_id'], status='running')
