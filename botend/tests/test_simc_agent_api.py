import hashlib
import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection
from django.test import TestCase, override_settings
from django.utils import timezone

from botend.models import SimcAgent, SimcAgentMaintenanceTask, SimcBackendBinary
from botend.services.simc_agent_control import create_enrollment_code


REGISTER_URL = '/api/simc-agent/v1/register/'
HEARTBEAT_URL = '/api/simc-agent/v1/heartbeat/'
MANAGEMENT_URL = '/api/simc-workbench/agents/'
ENROLLMENT = 'test-enrollment-token'
HOST_A = 'a' * 64
HOST_B = 'b' * 64


@override_settings(
    SIMC_AGENT_HEARTBEAT_INTERVAL_SECONDS=30,
    SIMC_AGENT_LEASE_SECONDS=90,
    SIMC_AGENT_REQUIRED_REVISION='a' * 40,
)
class SimcAgentAPITests(TestCase):
    def setUp(self):
        SimcBackendBinary.objects.all().delete()

    def register_payload(self, **overrides):
        payload = {
            'host_identifier': HOST_A,
            'backend_identifier': 'production',
            'name': 'Production Agent',
            'platform': 'linux64',
            'agent_version': '1.2.3',
            'agent_revision': 'a' * 40,
            'protocol_version': 1,
            'capabilities': {'claim': False, 'cores': 8},
            'instance_id': 'instance-a',
            'current_version': 'simc-1234',
            'binary_available': True,
        }
        payload.update(overrides)
        return payload

    def post_json(self, url, payload, authorization=''):
        return self.client.post(
            url, data=json.dumps(payload), content_type='application/json',
            HTTP_AUTHORIZATION=authorization,
        )

    def enrollment_code(self, backend_identifier='production'):
        backend, _ = SimcBackendBinary.objects.get_or_create(
            identifier=backend_identifier,
            defaults={
                'name': backend_identifier, 'simc_path': '',
                'auto_update': False, 'is_active': True,
            },
        )
        _, plaintext = create_enrollment_code(backend=backend, created_by=None)
        return plaintext

    def enroll(self, **overrides):
        backend_identifier = overrides.get('backend_identifier', 'production')
        return self.post_json(
            REGISTER_URL, self.register_payload(**overrides),
            f'Enrollment {self.enrollment_code(backend_identifier)}',
        )

    def test_first_registration_creates_logical_backend_and_agent_returns_token_once(self):
        response = self.enroll()
        self.assertEqual(response.status_code, 201, response.content)
        body = response.json()
        backend = SimcBackendBinary.objects.get(identifier='production')
        agent = SimcAgent.objects.get(backend=backend, host_identifier=HOST_A)

        self.assertEqual(backend.name, 'production')
        self.assertEqual(backend.simc_path, '')
        self.assertFalse(backend.auto_update)
        self.assertTrue(backend.is_active)
        self.assertEqual(agent.name, 'Production Agent')
        self.assertEqual(agent.status, 'online')
        self.assertIsNotNone(agent.registered_at)
        self.assertIsNotNone(agent.last_seen_at)
        self.assertEqual(agent.agent_version, '1.2.3')
        self.assertEqual(agent.protocol_version, 1)
        self.assertEqual(agent.capabilities, {'claim': False, 'cores': 8})
        self.assertEqual(agent.instance_id, 'instance-a')
        self.assertTrue(agent.binary_available)
        self.assertEqual(agent.current_version, 'simc-1234')

        token = body['agent_token']
        token_id, secret = token.split('.', 1)
        self.assertEqual(token_id, agent.token_id)
        self.assertGreaterEqual(len(secret), 43)
        self.assertNotEqual(agent.token_hash, secret)
        self.assertEqual(agent.token_hash, 'sha256$' + hashlib.sha256(secret.encode('ascii')).hexdigest())
        self.assertEqual(body['agent']['id'], agent.pk)
        self.assertEqual(body['agent']['backend']['id'], backend.pk)
        self.assertEqual(body['heartbeat_interval_seconds'], 30)
        self.assertEqual(body['lease_seconds'], 90)
        serialized = json.dumps(body)
        for sensitive in ('simc_path', 'token_hash', 'token_id'):
            self.assertNotIn(sensitive, serialized)
        self.assertEqual(response['Cache-Control'], 'no-store')
        self.assertEqual(response['Pragma'], 'no-cache')

    def test_superuser_can_read_agent_management_projection(self):
        self.enroll()
        superuser = get_user_model().objects.create_user(
            username='simc-superuser', password='x', is_superuser=True,
        )
        self.client.force_login(superuser)
        response = self.client.get(MANAGEMENT_URL)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(len(response.json()['data']), 1)

    def test_registration_uses_precreated_backend_without_overwriting_its_name(self):
        backend = SimcBackendBinary.objects.create(
            identifier='production', name='Existing Backend', simc_path='/legacy/path'
        )
        response = self.enroll(name='Independent Node')
        self.assertEqual(response.status_code, 201, response.content)
        backend.refresh_from_db()
        agent = backend.agents.get()
        self.assertEqual(backend.name, 'Existing Backend')
        self.assertEqual(backend.simc_path, '/legacy/path')
        self.assertEqual(agent.name, 'Independent Node')
        self.assertEqual(SimcBackendBinary.objects.count(), 1)

    def test_enrollment_code_selects_backend_without_payload_identifier(self):
        code = self.enrollment_code('production')
        payload = self.register_payload()
        payload.pop('backend_identifier')

        response = self.post_json(REGISTER_URL, payload, f'Enrollment {code}')

        self.assertEqual(response.status_code, 201, response.content)
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        self.assertEqual(agent.backend.identifier, 'production')

    def test_bearer_registration_does_not_require_backend_identifier(self):
        token = self.enroll().json()['agent_token']
        payload = self.register_payload(name='Renamed')
        payload.pop('backend_identifier')

        response = self.post_json(REGISTER_URL, payload, f'Bearer {token}')

        self.assertEqual(response.status_code, 200, response.content)
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        self.assertEqual(agent.backend.identifier, 'production')
        self.assertEqual(agent.name, 'Renamed')

    def test_two_hosts_can_enroll_same_backend_with_independent_tokens(self):
        first = self.enroll(host_identifier=HOST_A, name='Node A')
        second = self.enroll(host_identifier=HOST_B, name='Node B')
        self.assertEqual(first.status_code, 201, first.content)
        self.assertEqual(second.status_code, 201, second.content)
        self.assertNotEqual(first.json()['agent_token'], second.json()['agent_token'])
        backend = SimcBackendBinary.objects.get(identifier='production')
        self.assertEqual(backend.agents.count(), 2)
        self.assertSetEqual(set(backend.agents.values_list('name', flat=True)), {'Node A', 'Node B'})

    def test_bearer_register_updates_only_its_own_agent(self):
        first = self.enroll(host_identifier=HOST_A, name='Node A')
        second = self.enroll(host_identifier=HOST_B, name='Node B')
        token_a = first.json()['agent_token']
        agent_a = SimcAgent.objects.get(host_identifier=HOST_A)
        agent_b = SimcAgent.objects.get(host_identifier=HOST_B)
        old_hash = agent_a.token_hash
        old_b_seen = agent_b.last_seen_at

        response = self.post_json(
            REGISTER_URL,
            self.register_payload(host_identifier=HOST_A, name='Node A2', agent_version='1.2.4',
                                  instance_id='instance-new'),
            f'Bearer {token_a}',
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('agent_token', response.json())
        agent_a.refresh_from_db()
        agent_b.refresh_from_db()
        self.assertEqual(agent_a.name, 'Node A2')
        self.assertEqual(agent_a.agent_version, '1.2.4')
        self.assertEqual(agent_a.instance_id, 'instance-new')
        self.assertEqual(agent_a.token_hash, old_hash)
        self.assertEqual(agent_b.name, 'Node B')
        self.assertEqual(agent_b.last_seen_at, old_b_seen)
        self.assertTrue(second.json()['agent_token'])

    def test_fresh_enrollment_recovers_lost_bearer_token_for_same_host(self):
        first = self.enroll()
        issued = first.json()['agent_token']
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        old_hash = agent.token_hash

        response = self.enroll()

        self.assertEqual(response.status_code, 200, response.content)
        replacement = response.json()['agent_token']
        self.assertNotEqual(replacement, issued)
        agent.refresh_from_db()
        self.assertNotEqual(agent.token_hash, old_hash)
        stale = self.post_json(
            HEARTBEAT_URL, {'status': 'online'}, f'Bearer {issued}',
        )
        self.assertEqual(stale.status_code, 401, stale.content)
        current = self.post_json(
            HEARTBEAT_URL, {'status': 'online'}, f'Bearer {replacement}',
        )
        self.assertEqual(current.status_code, 200, current.content)

    def test_same_host_cannot_switch_backend(self):
        self.assertEqual(self.enroll(backend_identifier='production').status_code, 201)
        response = self.enroll(backend_identifier='ptr')
        self.assertEqual(response.status_code, 409, response.content)
        self.assertTrue(SimcBackendBinary.objects.filter(identifier='ptr').exists())
        self.assertEqual(SimcAgent.objects.count(), 1)

    def test_invalid_or_legacy_enrollment_is_rejected(self):
        wrong = self.post_json(REGISTER_URL, self.register_payload(), 'Enrollment wrong-token')
        self.assertEqual(wrong.status_code, 401)
        legacy = self.post_json(REGISTER_URL, self.register_payload(), f'Enrollment {ENROLLMENT}')
        self.assertEqual(legacy.status_code, 401)
        self.assertFalse(SimcAgent.objects.exists())

    def test_heartbeat_delivers_only_own_pending_maintenance_when_idle_and_completion_is_authenticated(self):
        token_a = self.enroll(host_identifier=HOST_A).json()['agent_token']
        token_b = self.enroll(host_identifier=HOST_B).json()['agent_token']
        agent_a = SimcAgent.objects.get(host_identifier=HOST_A)
        agent_b = SimcAgent.objects.get(host_identifier=HOST_B)
        task_a = SimcAgentMaintenanceTask.objects.create(agent=agent_a)
        SimcAgentMaintenanceTask.objects.create(agent=agent_b)

        response = self.post_json(HEARTBEAT_URL, {'status': 'online'}, f'Bearer {token_a}')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()['agent_maintenance_task'], {'id': task_a.pk, 'action': 'update_simc'})
        started = self.post_json(f'/api/simc-agent/v1/maintenance-tasks/{task_a.pk}/', {'status': 'running'}, f'Bearer {token_a}')
        self.assertEqual(started.status_code, 200, started.content)
        forbidden = self.post_json(f'/api/simc-agent/v1/maintenance-tasks/{task_a.pk}/', {'status': 'success'}, f'Bearer {token_b}')
        self.assertEqual(forbidden.status_code, 404, forbidden.content)
        complete = self.post_json(f'/api/simc-agent/v1/maintenance-tasks/{task_a.pk}/', {'status': 'success'}, f'Bearer {token_a}')
        self.assertEqual(complete.status_code, 200, complete.content)
        task_a.refresh_from_db()
        self.assertEqual(task_a.status, 'success')
        self.assertIsNotNone(task_a.completed_at)

    def test_heartbeat_does_not_deliver_maintenance_with_live_or_uncertain_lease(self):
        token = self.enroll().json()['agent_token']
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        SimcAgentMaintenanceTask.objects.create(agent=agent)
        from botend.models import SimcTask, SimulationRun
        task = SimcTask.objects.create(user_id=1, name='lease', simc_profile_id=1, backend=agent.backend)
        SimulationRun.objects.create(task=task, status='running', lease_agent=agent, lease_expires_at=timezone.now() + timedelta(minutes=1))
        response = self.post_json(HEARTBEAT_URL, {'status': 'busy'}, f'Bearer {token}')
        self.assertEqual(response.status_code, 200, response.content)
        self.assertNotIn('agent_maintenance_task', response.json())

    def test_heartbeat_updates_only_authenticated_agent_report(self):
        token_a = self.enroll(host_identifier=HOST_A, name='Node A').json()['agent_token']
        self.enroll(host_identifier=HOST_B, name='Node B')
        agent_a = SimcAgent.objects.get(host_identifier=HOST_A)
        agent_b = SimcAgent.objects.get(host_identifier=HOST_B)
        old_b_seen = agent_b.last_seen_at
        response = self.post_json(HEARTBEAT_URL, {
            'status': 'busy', 'platform': 'linuxarm64', 'agent_version': '1.3.0',
            'protocol_version': 2, 'capabilities': {'cores': 16},
            'instance_id': 'instance-new', 'current_version': 'simc-5678',
            'binary_available': False,
        }, f'Bearer {token_a}')
        self.assertEqual(response.status_code, 200, response.content)
        agent_a.refresh_from_db()
        agent_b.refresh_from_db()
        self.assertEqual(agent_a.status, 'busy')
        self.assertEqual(agent_a.platform, 'linuxarm64')
        self.assertEqual(agent_a.protocol_version, 2)
        self.assertEqual(agent_a.capabilities, {'cores': 16})
        self.assertFalse(agent_a.binary_available)
        self.assertEqual(agent_b.status, 'online')
        self.assertEqual(agent_b.last_seen_at, old_b_seen)

    @override_settings(SIMC_AGENT_REQUIRED_REVISION='b' * 40)
    def test_heartbeat_defers_stale_agent_update_while_simc_maintenance_is_active(self):
        token = self.enroll(agent_revision='a' * 40).json()['agent_token']
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        agent.capabilities = {'cores': 1, 'maintenance': 'simc_compile'}
        agent.save(update_fields=['capabilities'])

        compiling = self.post_json(HEARTBEAT_URL, {'status': 'degraded'}, f'Bearer {token}')

        self.assertEqual(compiling.status_code, 200, compiling.content)
        agent.refresh_from_db()
        self.assertEqual(agent.status, 'degraded')
        self.assertEqual(agent.capabilities['maintenance'], 'simc_compile')

        ready = self.post_json(HEARTBEAT_URL, {'status': 'online'}, f'Bearer {token}')
        self.assertEqual(ready.status_code, 426, ready.content)
        self.assertEqual(ready.json()['code'], 'agent_update_required')
        self.assertEqual(ready.json()['current_revision'], 'a' * 40)
        self.assertEqual(ready.json()['required_revision'], 'b' * 40)

    @override_settings(SIMC_AGENT_REQUIRED_REVISION='b' * 40)
    def test_heartbeat_requires_agent_update_when_revision_is_stale(self):
        token = self.enroll(agent_revision='a' * 40).json()['agent_token']

        response = self.post_json(HEARTBEAT_URL, {'status': 'online'}, f'Bearer {token}')

        self.assertEqual(response.status_code, 426, response.content)
        self.assertEqual(response.json()['code'], 'agent_update_required')
        self.assertEqual(response.json()['current_revision'], 'a' * 40)
        self.assertEqual(response.json()['required_revision'], 'b' * 40)

    def test_heartbeat_allows_inactive_agent(self):
        token = self.enroll().json()['agent_token']
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        previous_seen = agent.last_seen_at
        SimcAgent.objects.filter(pk=agent.pk).update(is_active=False)
        response = self.post_json(HEARTBEAT_URL, {'status': 'online'}, f'Bearer {token}')
        self.assertEqual(response.status_code, 200, response.content)
        agent.refresh_from_db()
        self.assertFalse(agent.is_active)
        self.assertGreater(agent.last_seen_at, previous_seen)

    def test_heartbeat_rejects_wrong_token(self):
        self.enroll()
        response = self.post_json(HEARTBEAT_URL, {'status': 'online'}, 'Bearer bogus.' + ('x' * 43))
        self.assertEqual(response.status_code, 401)

    def _damage_token_state(self, agent, *, token_id, token_hash):
        table = connection.ops.quote_name(SimcAgent._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute('PRAGMA ignore_check_constraints = ON')
            try:
                cursor.execute(
                    f'UPDATE {table} SET token_id = %s, token_hash = %s WHERE id = %s',
                    [token_id, token_hash, agent.pk],
                )
            finally:
                cursor.execute('PRAGMA ignore_check_constraints = OFF')

    def damaged_agent(self):
        backend = SimcBackendBinary.objects.create(identifier='production', name='Backend')
        return SimcAgent.objects.create(backend=backend, host_identifier=HOST_A, name='Damaged')

    def test_enrollment_refuses_token_id_without_hash(self):
        agent = self.damaged_agent()
        self._damage_token_state(agent, token_id='x' * 24, token_hash='')
        response = self.enroll()
        self.assertEqual(response.status_code, 409, response.content)
        agent.refresh_from_db()
        self.assertEqual(agent.token_id, 'x' * 24)
        self.assertEqual(agent.token_hash, '')

    def test_enrollment_refuses_hash_without_token_id(self):
        agent = self.damaged_agent()
        damaged_hash = 'sha256$' + ('a' * 64)
        self._damage_token_state(agent, token_id=None, token_hash=damaged_hash)
        response = self.enroll()
        self.assertEqual(response.status_code, 409, response.content)
        agent.refresh_from_db()
        self.assertIsNone(agent.token_id)
        self.assertEqual(agent.token_hash, damaged_hash)

    def test_agent_model_token_credentials_must_be_a_pair(self):
        backend = SimcBackendBinary.objects.create(identifier='production', name='Backend')
        for index, (token_id, token_hash) in enumerate((('x' * 24, ''), (None, 'sha256$' + 'a' * 64))):
            with self.subTest(token_id=token_id):
                agent = SimcAgent(backend=backend, host_identifier=str(index) * 64,
                                  token_id=token_id, token_hash=token_hash)
                with self.assertRaises(ValidationError):
                    agent.validate_constraints()

    def test_registration_accepts_html_locale_patch_version_as_telemetry(self):
        response = self.enroll(html_locale_patch_version=1)

        self.assertEqual(response.status_code, 201, response.content)

    def test_heartbeat_accepts_html_locale_patch_version_as_telemetry(self):
        token = self.enroll().json()['agent_token']

        response = self.post_json(
            HEARTBEAT_URL,
            {'status': 'online', 'html_locale_patch_version': 1},
            f'Bearer {token}',
        )

        self.assertEqual(response.status_code, 200, response.content)
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        self.assertEqual(agent.backend.identifier, 'production')

    def test_payload_cannot_set_backend_or_host_identity_during_heartbeat(self):
        token = self.enroll().json()['agent_token']
        for forbidden in ('simc_path', 'host_identifier', 'backend_identifier'):
            response = self.post_json(
                HEARTBEAT_URL, {'status': 'online', forbidden: 'attacker-controlled'},
                f'Bearer {token}',
            )
            self.assertEqual(response.status_code, 400, response.content)
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        self.assertEqual(agent.backend.identifier, 'production')

    def test_registration_rejects_unknown_and_invalid_payloads(self):
        invalid_payloads = [
            self.register_payload(simc_path='/tmp/evil'),
            self.register_payload(host_identifier='A' * 64),
            self.register_payload(host_identifier='a' * 31),
            self.register_payload(agent_version='v' * 65),
            self.register_payload(status='online'), [],
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = self.post_json(REGISTER_URL, payload, f'Enrollment {ENROLLMENT}')
                self.assertEqual(response.status_code, 400, response.content)
        malformed = self.client.post(REGISTER_URL, data='{', content_type='application/json',
                                     HTTP_AUTHORIZATION=f'Enrollment {ENROLLMENT}')
        self.assertEqual(malformed.status_code, 400)
        self.assertFalse(SimcAgent.objects.exists())

    def test_registration_requires_json_and_enforces_body_limit(self):
        plain = self.client.post(REGISTER_URL, data=json.dumps(self.register_payload()),
                                 content_type='text/plain',
                                 HTTP_AUTHORIZATION=f'Enrollment {ENROLLMENT}')
        self.assertEqual(plain.status_code, 400)
        declared = self.client.post(REGISTER_URL, data=b'{}', content_type='application/json',
                                    CONTENT_LENGTH='65537',
                                    HTTP_AUTHORIZATION=f'Enrollment {ENROLLMENT}')
        self.assertEqual(declared.status_code, 413)
        actual = self.client.post(REGISTER_URL, data=b'{' + b' ' * 65536 + b'}',
                                  content_type='application/json',
                                  HTTP_AUTHORIZATION=f'Enrollment {ENROLLMENT}')
        self.assertEqual(actual.status_code, 413)

    def test_json_rejects_non_finite_constants_and_excessive_nesting(self):
        for raw in (b'{"capabilities":{"value":NaN}}',
                    ('[' * 1100 + '0' + ']' * 1100).encode()):
            response = self.client.post(REGISTER_URL, data=raw, content_type='application/json',
                                        HTTP_AUTHORIZATION=f'Enrollment {ENROLLMENT}')
            self.assertEqual(response.status_code, 400, response.content)

    def test_authorization_scheme_case_and_strict_whitespace(self):
        enrollment_code = self.enrollment_code()
        enrolled = self.post_json(
            REGISTER_URL, self.register_payload(), f'eNrOlLmEnT {enrollment_code}',
        )
        self.assertEqual(enrolled.status_code, 201, enrolled.content)
        token = enrolled.json()['agent_token']
        valid = self.post_json(HEARTBEAT_URL, {'status': 'online'}, f'bEaReR {token}')
        self.assertEqual(valid.status_code, 200)
        for authorization in (f' Bearer {token}', f'Bearer {token} ', f'Bearer  {token}',
                              'Bearer ', 'Bearer', 'Bearer\t' + token, 'Bearer ' + 'x' * 506):
            response = self.post_json(HEARTBEAT_URL, {'status': 'online'}, authorization)
            self.assertEqual(response.status_code, 401, response.content)

    def test_backend_bound_enrollment_code_cannot_register_another_backend(self):
        production_code = self.enrollment_code('production')
        rejected = self.post_json(
            REGISTER_URL, self.register_payload(backend_identifier='ptr'),
            f'Enrollment {production_code}',
        )
        self.assertEqual(rejected.status_code, 401)
        ptr_code = self.enrollment_code('ptr')
        accepted = self.post_json(
            REGISTER_URL, self.register_payload(backend_identifier='ptr'),
            f'Enrollment {ptr_code}',
        )
        self.assertEqual(accepted.status_code, 201)

    def test_agent_status_defaults_and_online_is_derived(self):
        backend = SimcBackendBinary.objects.create(identifier='legacy', name='Legacy')
        agent = SimcAgent.objects.create(backend=backend, host_identifier=HOST_A)
        self.assertEqual(agent.status, 'unregistered')
        self.assertFalse(agent.binary_available)
        self.assertEqual(agent.protocol_version, 1)
        self.assertEqual(agent.capabilities, {})
        self.assertFalse(agent.is_online(timeout_seconds=90))
        agent.status = 'online'
        agent.last_seen_at = timezone.now() - timedelta(seconds=89)
        self.assertTrue(agent.is_online(timeout_seconds=90))
        agent.last_seen_at = timezone.now() - timedelta(seconds=91)
        self.assertFalse(agent.is_online(timeout_seconds=90))

    def test_management_list_exposes_all_live_leases_for_a_multi_run_agent(self):
        from botend.models import SimcTask, SimulationRun

        self.enroll(host_identifier=HOST_A, name='Node A')
        agent = SimcAgent.objects.get(host_identifier=HOST_A)
        backend = agent.backend
        task = SimcTask.objects.create(
            user_id=1, simc_profile_id=1, backend=backend, mode='comparison', name='parallel', current_status=1,
            execution_owner=SimcTask.EXECUTION_OWNER_AGENT, started_at=timezone.now(),
        )
        for sequence in (1, 2):
            SimulationRun.objects.create(
                task=task, sequence=sequence, status='running', lease_agent=agent,
                lease_instance_id='instance-a', lease_expires_at=timezone.now() + timedelta(seconds=60),
            )
        staff = get_user_model().objects.create_user(username='staff', password='x', is_staff=True)
        self.client.force_login(staff)

        response = self.client.get(MANAGEMENT_URL)

        self.assertEqual(response.status_code, 200, response.content)
        row = response.json()['data'][0]
        self.assertEqual([lease['run_id'] for lease in row['leases']], [
            run.pk for run in SimulationRun.objects.filter(task=task).order_by('id')
        ])

    def test_management_list_requires_staff_and_shows_agents_and_leases(self):
        self.enroll(host_identifier=HOST_A, name='Node A')
        self.enroll(host_identifier=HOST_B, name='Node B')
        user_model = get_user_model()
        normal = user_model.objects.create_user(username='normal', password='x')
        staff = user_model.objects.create_user(username='staff', password='x', is_staff=True)
        self.assertEqual(self.client.get(MANAGEMENT_URL).status_code, 403)
        self.client.force_login(normal)
        self.assertEqual(self.client.get(MANAGEMENT_URL).status_code, 403)
        self.client.force_login(staff)
        response = self.client.get(MANAGEMENT_URL)
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()['data']
        self.assertEqual([row['name'] for row in rows], ['Node A', 'Node B'])
        self.assertTrue(all(row['online'] for row in rows))
        self.assertTrue(all(row['lease'] is None for row in rows))

    def test_management_active_requires_exact_bool_and_only_toggles_target(self):
        self.enroll(host_identifier=HOST_A, name='Node A')
        self.enroll(host_identifier=HOST_B, name='Node B')
        first, second = list(SimcAgent.objects.order_by('id'))
        staff = get_user_model().objects.create_user(username='staff', password='x', is_staff=True)
        self.client.force_login(staff)
        path = f'/api/simc-workbench/agents/{first.pk}/active/'
        for payload in ({'is_active': 0}, {'is_active': 'false'},
                        {'is_active': False, 'extra': 1}, {}):
            self.assertEqual(self.post_json(path, payload).status_code, 400)
        response = self.post_json(path, {'is_active': False})
        self.assertEqual(response.status_code, 200, response.content)
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertFalse(first.is_active)
        self.assertTrue(second.is_active)
