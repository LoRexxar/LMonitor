import json
from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db.models.query import QuerySet
from django.test import Client, TestCase, override_settings
from django.utils import timezone

from botend.models import SimcAgent, SimcAgentEnrollmentCode, SimcBackendBinary


REGISTER_URL = '/api/simc-agent/v1/register/'
CODES_URL = '/api/simc-workbench/agent-enrollment-codes/'
HOST_A = 'a' * 64
HOST_B = 'b' * 64


@override_settings(
    SIMC_AGENT_ENROLLMENT_TOKEN='legacy-shared-secret',
    SIMC_AGENT_ENROLLMENT_TOKENS={'production': 'legacy-scoped-secret'},
)
class SimcAgentEnrollmentCodeAPITests(TestCase):
    def setUp(self):
        SimcAgentEnrollmentCode.objects.all().delete()
        SimcAgent.objects.all().delete()
        SimcBackendBinary.objects.all().delete()
        self.backend = SimcBackendBinary.objects.create(
            identifier='production', name='Production', platform='linux64',
        )
        self.other_backend = SimcBackendBinary.objects.create(
            identifier='ptr', name='PTR', platform='linux64',
        )
        self.staff = get_user_model().objects.create_user(
            username='staff-enrollment', password='x', is_staff=True,
        )
        self.client.force_login(self.staff)

    def post_json(self, url, payload, authorization=''):
        return self.client.post(
            url, data=json.dumps(payload), content_type='application/json',
            HTTP_AUTHORIZATION=authorization,
        )

    def register_payload(self, host=HOST_A, backend='production'):
        return {
            'host_identifier': host,
            'backend_identifier': backend,
            'name': 'Compute Node',
            'platform': 'linux64',
            'agent_version': '1.0.0',
            'agent_revision': 'a' * 40,
            'protocol_version': 1,
            'capabilities': {'max_concurrent_runs': 1},
            'instance_id': 'instance-a',
            'current_version': 'simc-1',
            'binary_available': True,
        }

    def create_code(self, backend='production', expires_in_seconds=1800):
        response = self.post_json(CODES_URL, {
            'backend_identifier': backend,
            'expires_in_seconds': expires_in_seconds,
        })
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()['data']

    def test_staff_generates_single_use_backend_scoped_code_and_list_never_reveals_secret(self):
        created = self.create_code()
        self.assertEqual(created['backend']['identifier'], 'production')
        self.assertEqual(created['status'], 'active')
        self.assertIn('.', created['enrollment_code'])

        listing = self.client.get(CODES_URL)
        self.assertEqual(listing.status_code, 200, listing.content)
        serialized = json.dumps(listing.json())
        self.assertNotIn(created['enrollment_code'], serialized)
        self.assertNotIn('secret_hash', serialized)
        self.assertNotIn('enrollment_code', serialized)
        self.assertEqual(listing.json()['data'][0]['status'], 'active')

        accepted = self.post_json(
            REGISTER_URL, self.register_payload(),
            f"Enrollment {created['enrollment_code']}",
        )
        self.assertEqual(accepted.status_code, 201, accepted.content)
        self.assertTrue(accepted.json()['agent_token'])
        self.assertEqual(SimcAgent.objects.count(), 1)

        reused = self.post_json(
            REGISTER_URL, self.register_payload(host=HOST_B),
            f"Enrollment {created['enrollment_code']}",
        )
        self.assertEqual(reused.status_code, 401, reused.content)
        self.assertEqual(SimcAgent.objects.count(), 1)

        consumed = self.client.get(CODES_URL).json()['data'][0]
        self.assertEqual(consumed['status'], 'consumed')
        self.assertEqual(consumed['consumed_by_agent_id'], SimcAgent.objects.get().pk)

    def test_code_is_backend_bound_and_failed_attempt_does_not_consume_it(self):
        created = self.create_code('production')
        mismatch = self.post_json(
            REGISTER_URL, self.register_payload(backend='ptr'),
            f"Enrollment {created['enrollment_code']}",
        )
        self.assertEqual(mismatch.status_code, 401, mismatch.content)
        self.assertEqual(self.client.get(CODES_URL).json()['data'][0]['status'], 'active')

        accepted = self.post_json(
            REGISTER_URL, self.register_payload(),
            f"Enrollment {created['enrollment_code']}",
        )
        self.assertEqual(accepted.status_code, 201, accepted.content)

    def test_staff_can_revoke_unused_code_and_revoked_code_cannot_register(self):
        created = self.create_code()
        revoke_url = f"{CODES_URL}{created['id']}/revoke/"
        revoked = self.post_json(revoke_url, {})
        self.assertEqual(revoked.status_code, 200, revoked.content)
        self.assertEqual(revoked.json()['data']['status'], 'revoked')

        response = self.post_json(
            REGISTER_URL, self.register_payload(),
            f"Enrollment {created['enrollment_code']}",
        )
        self.assertEqual(response.status_code, 401, response.content)
        self.assertFalse(SimcAgent.objects.exists())

    def test_management_requires_staff_and_validates_backend_and_ttl(self):
        self.client.logout()
        self.assertEqual(self.client.get(CODES_URL).status_code, 403)
        self.assertEqual(self.post_json(CODES_URL, {
            'backend_identifier': 'production', 'expires_in_seconds': 1800,
        }).status_code, 403)

        normal = get_user_model().objects.create_user(username='normal-enrollment', password='x')
        self.client.force_login(normal)
        self.assertEqual(self.client.get(CODES_URL).status_code, 403)

        self.client.force_login(self.staff)
        for payload in (
            {'backend_identifier': 'missing', 'expires_in_seconds': 1800},
            {'backend_identifier': 'production', 'expires_in_seconds': 299},
            {'backend_identifier': 'production', 'expires_in_seconds': 86401},
            {'backend_identifier': 'production', 'expires_in_seconds': True},
            {'backend_identifier': 'production', 'expires_in_seconds': 1800, 'extra': 1},
        ):
            with self.subTest(payload=payload):
                self.assertEqual(self.post_json(CODES_URL, payload).status_code, 400)

    def test_management_writes_require_csrf(self):
        csrf_client = Client(enforce_csrf_checks=True)
        csrf_client.force_login(self.staff)
        create_response = csrf_client.post(
            CODES_URL,
            data=json.dumps({'backend_identifier': 'production', 'expires_in_seconds': 1800}),
            content_type='application/json',
        )
        self.assertEqual(create_response.status_code, 403)

        created = self.create_code()
        revoke_response = csrf_client.post(
            f"{CODES_URL}{created['id']}/revoke/",
            data='{}', content_type='application/json',
        )
        self.assertEqual(revoke_response.status_code, 403)
        self.assertEqual(self.client.get(CODES_URL).json()['data'][0]['status'], 'active')

    def test_expired_code_cannot_register_and_is_reported_expired(self):
        created = self.create_code()
        SimcAgentEnrollmentCode.objects.filter(pk=created['id']).update(
            expires_at=timezone.now() - timedelta(seconds=1),
        )
        response = self.post_json(
            REGISTER_URL, self.register_payload(),
            f"Enrollment {created['enrollment_code']}",
        )
        self.assertEqual(response.status_code, 401, response.content)
        self.assertFalse(SimcAgent.objects.exists())
        self.assertEqual(self.client.get(CODES_URL).json()['data'][0]['status'], 'expired')

    def test_consumption_takes_row_lock_and_rolls_back_if_agent_registration_conflicts(self):
        first = self.create_code()
        accepted = self.post_json(
            REGISTER_URL, self.register_payload(),
            f"Enrollment {first['enrollment_code']}",
        )
        self.assertEqual(accepted.status_code, 201, accepted.content)

        second = self.create_code('ptr')
        original = QuerySet.select_for_update
        with patch.object(QuerySet, 'select_for_update', autospec=True, side_effect=original) as locked:
            conflict = self.post_json(
                REGISTER_URL, self.register_payload(backend='ptr'),
                f"Enrollment {second['enrollment_code']}",
            )
        self.assertEqual(conflict.status_code, 409, conflict.content)
        self.assertTrue(any(
            getattr(call.args[0], 'model', None) is SimcAgentEnrollmentCode
            for call in locked.call_args_list
        ))
        row = SimcAgentEnrollmentCode.objects.get(pk=second['id'])
        self.assertIsNone(row.consumed_at)
        self.assertIsNone(row.consumed_by_agent_id)

    def test_legacy_environment_secret_is_not_an_enrollment_bypass(self):
        for secret in ('legacy-shared-secret', 'legacy-scoped-secret'):
            response = self.post_json(
                REGISTER_URL, self.register_payload(), f'Enrollment {secret}',
            )
            self.assertEqual(response.status_code, 401, response.content)
        self.assertFalse(SimcAgent.objects.exists())
