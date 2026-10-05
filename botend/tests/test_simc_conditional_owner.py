"""Owner fences with real ORM/auth; transport and semantic parsing isolated."""
import base64
from contextlib import ExitStack
from unittest.mock import patch
from django.test import TestCase, override_settings
from botend.models import SimcAgent, SimcTask, SimulationRun, SimcTaskArtifact
from botend.tests import test_simc_conditional_api_wiring as fixtures


@override_settings(SIMC_AGENT_REQUIRED_REVISION='a' * 40, ALLOWED_HOSTS=['testserver'],
                   OSS_CONFIG={'base_url': 'https://reports.example'})
class ConditionalOwnerTests(TestCase):
    for _name in ('setUp', 'import_source', 'approve', 'backend_row', 'agent_row', 'task',
                  'claim', 'post_json', 'conditional_task', 'job', 'metadata', 'pointer'):
        locals()[_name] = getattr(fixtures.ConditionalEvidenceAPITests, _name)

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
