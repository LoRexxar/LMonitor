"""Local audit integration: immutable real resources + saved combat evidence.

Requires the separately audited resource export (override with
CONDITIONAL_RESOURCE_EXPORT). Deliberately does NOT retrofit a claim digest to
old prepared evidence. The old fixture cannot pass today's Composer context.
"""
import gzip
import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase, override_settings
from botend import models
from botend.tests import test_simc_conditional_api_wiring as wiring
from botend.services.simc_conditional_execution import CONTEXT_KEY, input_context_digest
from botend.services.simc_conditional_store import import_reviewed_contract, approve_executable
from botend.services.wow_item_effect_activation_store import META_KEY as ACTIVATION_KEY


@override_settings(SIMC_AGENT_REQUIRED_REVISION='a' * 40, ALLOWED_HOSTS=['testserver'],
                   OSS_CONFIG={'base_url': 'https://reports.example'})
class ConditionalRealResourceAPITests(TestCase):
    backend_row = wiring.ConditionalEvidenceAPITests.backend_row
    agent_row = wiring.ConditionalEvidenceAPITests.agent_row
    post_json = wiring.ConditionalEvidenceAPITests.post_json
    claim = wiring.ConditionalEvidenceAPITests.claim
    metadata = wiring.ConditionalEvidenceAPITests.metadata
    pointer = wiring.ConditionalEvidenceAPITests.pointer

    def setUp(self):
        wiring.ConditionalEvidenceAPITests.setUp(self)
        self.sides = json.loads(gzip.decompress((Path(__file__).with_name('fixtures') /
            'conditional_run2_evidence.json.gz').read_bytes()))['sides']
        source = Path(os.environ.get('CONDITIONAL_RESOURCE_EXPORT',
            Path(__file__).with_name('fixtures') / 'conditional_real_resources_mage_fire.json'))
        self.resources = json.loads(source.read_text())
        # Original resource export and actual report are Live. Do not reuse the
        # generic wiring fixture's PTR choice or its invented warrior resource.
        for item in models.WowItemSnapshot.objects.all():
            for fact in item.metadata[ACTIVATION_KEY].values():
                fact['is_ptr'] = False
            item.save(update_fields=['metadata'])
        import_reviewed_contract(policy=self.policy, expectation=self.expectation,
                                 is_ptr=False, review=self.review)
        approve_executable(**{**self.selector, 'is_ptr': False}, approval=self.approval)
        self.objects, self.versions = {}, {}
        for kind, cls in [('profile', models.SimcProfile), ('apl', models.SimcApl),
                          ('template', models.SimcContentTemplate), ('talent', models.SimcTalentString)]:
            payload = self.resources['payloads'][kind]
            fields = {field.name for field in cls._meta.concrete_fields}
            values = {k: deepcopy(v) for k, v in payload.items() if k in fields}
            if 'user_id' in fields:
                values['user_id'] = 1
            if 'owner_user_id' in fields:
                values['owner_user_id'] = 1
            obj = cls.objects.create(**values)
            self.objects[kind] = obj
            self.versions[kind] = models.SimcResourceVersion.objects.create(
                resource_type=kind, resource_id=obj.pk,
                content_hash=self.resources['versions'][kind]['content_hash'],
                payload=deepcopy(payload))

    def real_job(self, side='normal'):
        params = deepcopy(self.sides[side]['params'])
        # Task candidate schema represents normal by absence, unlike the old
        # standalone helper fixture which explicitly wrote False.
        if params.get('equipment_effect_control') is False:
            params.pop('equipment_effect_control')
        # This is central activation identity, established from independent DBC
        # facts in setUp, not a digest learned from the prepared/report pair.
        params['equipment_effect_expectation'] = deepcopy(self.expectation)
        task = models.SimcTask.objects.create(user_id=1, name='original-resource-audit',
            simc_profile_id=self.objects['profile'].pk, backend=self.backend,
            profile=self.objects['profile'], apl=self.objects['apl'],
            template=self.objects['template'], talent_string=self.objects['talent'],
            profile_version=self.versions['profile'], apl_version=self.versions['apl'],
            template_version=self.versions['template'], talent_version=self.versions['talent'],
            mode='comparison', simulation_params={'iterations': 100, 'threads': 1,
                'max_time': 120, 'desired_targets': 1, 'fight_style': 'Patchwerk', 'seed': 41719},
            mode_params={'initial_candidates': [{'candidate_key': side, 'candidate_params': params}]})
        response = self.claim()
        self.assertEqual(response.status_code, 200, response.content)
        job = response.json()
        self.assertEqual(job['task_id'], task.pk)
        return job

    def submit_saved(self, job, side, *, mutate=None, during_download=None, token=None):
        evidence = deepcopy(self.sides[side])
        if mutate:
            evidence = mutate(evidence)
        payload = {**self.metadata(job), 'conditional_evidence': self.pointer(job)}
        def download(*args, **kwargs):
            if during_download:
                during_download()
            return {'prepared_input': evidence['prepared'], 'report_json': evidence['report']}
        # Only storage I/O is replaced. Strict validator, semantic parser,
        # Composer, auth and claim-context functions stay real.
        with patch('botend.services.simc_conditional_evidence_storage.download_evidence', side_effect=download) as private, \
             patch('botend.services.simc_agent_oss.verify_uploaded_report') as verify, \
             patch('botend.services.simc_agent_oss.download_report_html', return_value=(evidence['html'], hashlib.sha256(evidence['html'].encode()).hexdigest())) as html:
            response = self.post_json(f"/api/simc-agent/v1/jobs/{job['run_id']}/complete/", payload, token=token)
        return response, private, verify, html

    def test_original_resources_real_claim_rejects_saved_both_sides_without_rebinding(self):
        for side in ('normal', 'control'):
            with self.subTest(side=side):
                job = self.real_job(side)
                run = models.SimulationRun.objects.get(pk=job['run_id'])
                self.assertEqual(run.resource_manifest[CONTEXT_KEY], input_context_digest(job['input']))
                self.assertIn('html=', job['input'])
                self.assertFalse(any(line.startswith('html=') for line in self.sides[side]['prepared'].splitlines()))
                self.assertNotEqual(input_context_digest(job['input']), input_context_digest(self.sides[side]['prepared']))
                print('REAL_CLAIM_CONTEXT', side, json.dumps({
                    'claim_digest': input_context_digest(job['input']),
                    'prepared_digest': input_context_digest(self.sides[side]['prepared']),
                    'claim_first_lines': job['input'].splitlines()[:14],
                    'prepared_first_lines': self.sides[side]['prepared'].splitlines()[:10],
                    'claim_output': [line for line in job['input'].splitlines() if line.startswith('html=')],
                }))
                before = deepcopy(run.resource_manifest)
                response, private, verify, html = self.submit_saved(job, side)
                self.assertEqual(response.status_code, 422, response.content)
                self.assertIn('Conditional evidence validation failed', response.content.decode())
                private.assert_called_once()
                verify.assert_not_called()
                html.assert_not_called()
                run.refresh_from_db()
                self.assertEqual(run.resource_manifest, before)
                self.assertEqual(run.status, 'running')
                self.assertFalse(models.SimcTaskArtifact.objects.exists())
                # End this test lease through the real failed callback, so the
                # next side is independently claimed by the same real Agent.
                failed = {**self.metadata(job), 'status': 'failed', 'report': None,
                          'stderr': 'saved fixture cannot match current claim'}
                self.assertEqual(self.post_json(f"/api/simc-agent/v1/jobs/{run.pk}/complete/", failed).status_code, 200)

    def test_download_drift_is_rejected_but_old_context_masks_commit_fence(self):
        from django.db import transaction
        for drift in ('candidate', 'freeze', 'purge', 'lease'):
            with self.subTest(drift=drift), transaction.atomic():
                job = self.real_job()
                def change():
                    run = models.SimulationRun.objects.get(pk=job['run_id'])
                    if drift == 'candidate':
                        params = deepcopy(run.candidate_params)
                        params['gear_swaps'][0]['raw_value'] += ',ilevel=1'
                        models.SimulationRun.objects.filter(pk=run.pk).update(candidate_params=params)
                    elif drift == 'freeze':
                        manifest = deepcopy(run.resource_manifest)
                        manifest['conditional_execution']['conditional_authorization'] = {}
                        models.SimulationRun.objects.filter(pk=run.pk).update(resource_manifest=manifest)
                    elif drift == 'lease':
                        models.SimulationRun.objects.filter(pk=run.pk).update(lease_token_hash='0' * 64)
                    else:
                        models.SimcBenchmarkPurgeTask.objects.create(panel_id_snapshot=1,
                            panel_name='audit', requested_by_id=1, fingerprint='a' * 64,
                            batch_id='race-purge', status='pending', plan={'task_ids': [run.task_id]})
                response, private, verify, html = self.submit_saved(job, 'normal', during_download=change)
                self.assertEqual(response.status_code, 422, response.content)
                private.assert_called_once()
                verify.assert_not_called()
                html.assert_not_called()
                self.assertFalse(models.SimcTaskArtifact.objects.exists())
                self.assertEqual(models.SimulationRun.objects.get(pk=job['run_id']).status, 'running')
                transaction.set_rollback(True)

    def test_paired_substitution_rejected_but_old_context_is_not_positive_control(self):
        from django.db import transaction
        # Both saved sides receive the same substitution, across all serialized
        # evidence representations. These are malicious copies, not combat facts.
        changes = [('actor', 'MID2_Mage_Fire_Sunfury', 'WrongActor'),
                   ('apl', 'arcane_intellect', 'wrong_action'),
                   ('options', 'iterations=100', 'iterations=101')]
        for kind, old, new in changes:
            for side in ('normal', 'control'):
                with self.subTest(kind=kind, side=side), transaction.atomic():
                    job = self.real_job(side)
                    def mutate(data):
                        return json.loads(json.dumps(data).replace(old, new))
                    response, private, verify, html = self.submit_saved(job, side, mutate=mutate)
                    self.assertEqual(response.status_code, 422, response.content)
                    private.assert_called_once()
                    verify.assert_not_called()
                    html.assert_not_called()
                    self.assertFalse(models.SimcTaskArtifact.objects.exists())
                    transaction.set_rollback(True)

    def test_wrong_authenticated_owner_cannot_complete(self):
        job = self.real_job()
        response, private, verify, html = self.submit_saved(job, 'normal', token=self.other_token)
        self.assertEqual(response.status_code, 403, response.content)
        private.assert_not_called()
        verify.assert_not_called()
        html.assert_not_called()
        self.assertFalse(models.SimcTaskArtifact.objects.exists())

    def test_wrong_authenticated_owner_cannot_upload(self):
        job = self.real_job()
        import base64
        payload = dict(lease_token=job['lease_token'], instance_id='instance-a', size=100,
                       sha256='a'*64, content_md5=base64.b64encode(b'a'*16).decode())
        with patch('botend.services.simc_conditional_evidence_storage.issue_upload_ticket', return_value={'protocol_version': 1}) as ticket:
            response = self.post_json(f"/api/simc-agent/v1/jobs/{job['run_id']}/evidence-upload/", payload, token=self.other_token)
        self.assertEqual(response.status_code, 403, response.content)
        ticket.assert_not_called()
        self.assertFalse(models.SimcTaskArtifact.objects.exists())
