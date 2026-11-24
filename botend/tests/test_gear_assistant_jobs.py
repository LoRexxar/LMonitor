from datetime import timedelta
import os
import signal
import subprocess
import sys
from unittest import skipUnless
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth import get_user_model
from django.test import Client, SimpleTestCase, TestCase
from django.utils import timezone

from botend.models import GearAssistantJob
from botend.services.gear_assistant_jobs import _maintain, run_job, work_once


class GearAssistantDeadlineTests(SimpleTestCase):
    @skipUnless(hasattr(signal, 'setitimer') and os.name == 'posix', 'POSIX worker deadline')
    def test_child_terminates_without_launcher_timeout(self):
        code = '''
import django
import time
django.setup()
from botend.management.commands import run_gear_assistant_job as command
command.run_job = lambda _pk: time.sleep(60)
command.run_with_deadline(1, seconds=.25)
'''
        child = subprocess.run([sys.executable, '-B', '-c', code], timeout=15, capture_output=True)
        self.assertEqual(child.returncode, -signal.SIGALRM)


class GearAssistantJobTests(TestCase):
    url = '/portal/api/gear-assistant/optimize/'

    def setUp(self):
        self.owner = get_user_model().objects.create_user(username='gear-job-owner')
        self.client.force_login(self.owner)
        self.other = Client()
        self.other.force_login(get_user_model().objects.create_user(username='gear-job-other'))

    def submit(self, request_id=None):
        return self.client.post(self.url, {'request_id': str(request_id or uuid4()), 'target': {'crit': 23}},
                                content_type='application/json')

    def test_short_submit_idempotent_poll_private_and_releases_slot(self):
        nonce = uuid4()
        with patch('botend.services.gear_assistant_jobs.optimize_loadouts', return_value={'plans': ['QA']}) as solve:
            reply = self.submit(nonce)
            self.assertEqual(reply.status_code, 202)
            self.assertEqual(reply.json()['job']['id'], str(nonce))
            self.assertEqual(self.submit(nonce)['Location'], reply['Location'])
            self.assertEqual(self.submit().status_code, 409)
            self.assertEqual(GearAssistantJob.objects.count(), 1)
            solve.assert_not_called()
            location = reply['Location']
            pending = self.client.get(location)
            self.assertIn('no-store', pending['Cache-Control'])
            self.assertEqual(pending.json()['job']['state'], 'queued')
            self.assertNotIn('result', pending.json()['job'])
            self.assertEqual(self.other.get(location).status_code, 404)
            self.assertEqual(Client().get(location).status_code, 401)
            pk = GearAssistantJob.objects.get().pk
            self.assertTrue(run_job(pk))
            self.assertFalse(run_job(pk))
            solve.assert_called_once()
            self.assertEqual(solve.call_args.args[0], self.owner)
            self.assertEqual(solve.call_args.args[1]['target']['crit'], 23)
        result = self.client.get(location).json()['job']
        self.assertEqual((result['state'], result['result']), ('done', {'plans': ['QA']}))
        self.assertEqual(self.other.get(location).status_code, 404)
        self.assertEqual(self.submit(nonce).json()['job']['state'], 'done')
        self.assertEqual(self.submit().status_code, 202)

    def test_worker_slot_and_interrupted_run_fence(self):
        reply = self.submit()
        first = GearAssistantJob.objects.get()
        other_id = uuid4()
        second = GearAssistantJob.objects.create(owner_id=self.other.session['_auth_user_id'], request_id=other_id,
                                                 class_name='Warrior', spec_name='Fury')
        active = GearAssistantJob.objects.filter(pk=first.pk)
        active.update(state='running', worker_slot=1, updated_at=timezone.now() - timedelta(minutes=6))
        with patch('botend.services.gear_assistant_jobs.optimize_loadouts') as solve:
            self.assertFalse(run_job(second.pk))
            solve.assert_not_called()
        _maintain(timezone.now())
        self.assertEqual(self.client.get(reply['Location']).json()['job']['state'], 'failed')
        self.assertFalse(run_job(first.pk))
        with patch('botend.services.gear_assistant_jobs.subprocess.run') as spawn:
            spawn.return_value.returncode = 0
            work_once()
            self.assertEqual(spawn.call_args.args[0][-2:], ['run_gear_assistant_job', str(second.pk)])
            self.assertLess(spawn.call_args.kwargs['timeout'], 300)
