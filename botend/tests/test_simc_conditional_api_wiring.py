import base64
from copy import deepcopy
from unittest.mock import patch
from django.test import TestCase, override_settings
from botend.models import SimulationRun, SimcTaskArtifact
from botend.tests import test_simc_conditional_execution as fixtures


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
