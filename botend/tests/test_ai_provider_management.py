import json
import ssl
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

from django.utils import timezone

from django.contrib.auth import get_user_model
from django.db import OperationalError
from django.test import Client, TestCase, override_settings

from botend.dashboard.permissions import effective_dashboard_permissions
from botend.models import DashboardUserGroup


BASELINE = {
    'ZHIYU_API_CONFIG': {'api_key': 'fixture-glm-secret', 'model': 'GLM-fixture',
                         'base_url': 'https://glm.example/v1', 'coding_base_url': 'https://coding.example/v1',
                         'fallback_models': ['GLM-fallback'], 'request_timeout_seconds': 12},
    'MIMO_API_CONFIG': {'api_key': 'fixture-mimo-secret', 'model': 'mimo-fixture',
                        'base_url': 'https://mimo.example/v1', 'request_timeout_seconds': 15},
    'CODEX_API_CONFIG': {},
}

CODEX_BASELINE = {'enabled': True, 'api_key': 'fixture-codex-secret', 'model': 'gpt-5.5',
                  'base_url': 'https://codex.example', 'request_timeout_seconds': 18}


@override_settings(**BASELINE)
class AIProviderManagementTests(TestCase):
    def setUp(self):
        from botend.models import AIProviderConfiguration
        from botend.services import ai_provider_service
        self.model = AIProviderConfiguration
        self.service = ai_provider_service
        self.superuser = get_user_model().objects.create_user('ai-root', is_superuser=True, is_staff=True)
        self.member = get_user_model().objects.create_user('ai-member')
        self.staff = get_user_model().objects.create_user('ai-staff', is_staff=True)
        self.client.force_login(self.superuser)

    def get_data(self):
        response = self.client.get('/api/dashboard/ai-providers/')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'success')
        return response.json()['data']

    def provider(self, key='glm'):
        return next(p for p in self.get_data()['providers'] if p['provider_key'] == key)

    def update(self, payload, key='glm'):
        return self.client.patch('/api/dashboard/ai-providers/' + key + '/',
                                 data=json.dumps(payload), content_type='application/json')

    def test_settings_get_is_read_only_and_secret_free(self):
        data = self.get_data()
        self.assertEqual(len(data['providers']), 3)
        self.assertEqual([p['provider_key'] for p in data['providers']], ['glm', 'mimo', 'codex'])
        codex = self.provider('codex')
        self.assertTrue(codex['enabled'])
        self.assertEqual(codex['priority'], 30)
        self.assertFalse(codex['has_api_key'])
        self.assertFalse(codex['effective'])
        self.assertEqual(codex['state']['code'], 'incomplete')
        self.assertEqual(codex['scopes'], ['translation'])
        self.assertEqual(codex['scope_label'], '文章翻译专用')
        glm = self.provider()
        self.assertEqual(glm['scopes'], ['general', 'translation'])
        self.assertEqual(glm['scope_label'], '通用')
        self.assertTrue(glm['enabled'])
        self.assertEqual(glm['version'], 0)
        self.assertEqual(glm['base_url'], 'https://coding.example/v1')
        self.assertEqual(glm['api_key_source'], 'settings')
        self.assertEqual(glm['state']['code'], 'unverified')
        self.assertEqual(data['preferred_provider'], 'glm')
        self.assertEqual(data['preferred_translation_provider'], 'glm')
        self.assertIsNone(data['last_successful_provider'])
        self.assertFalse(self.model.objects.exists())
        serialized = json.dumps(data)
        for secret in ('fixture-glm-secret', 'api_key_encrypted', '_runtime_signature'):
            self.assertNotIn(secret, serialized)

    def test_superuser_only_even_with_group_permission(self):
        group = DashboardUserGroup.objects.create(name='ai-group', permission_codes=['system.ai-management'])
        for user in (self.member, self.staff):
            group.users.add(user)
            self.client.force_login(user)
            self.assertNotIn('system.ai-management', effective_dashboard_permissions(user))
            self.assertEqual(self.client.get('/api/dashboard/ai-providers/').status_code, 403)
            self.assertEqual(self.update({'version': 0, 'enabled': False}).status_code, 403)
        self.client.logout()
        self.assertEqual(self.client.get('/api/dashboard/ai-providers/').status_code, 403)
        self.assertEqual(self.update({'version': 0}).status_code, 403)
        self.assertFalse(self.model.objects.exists())

    def test_unconfigured_codex_sparse_updates_preserve_inheritance(self):
        for payload in ({'version': 0, 'enabled': False}, {'version': 1, 'priority': 0},
                        {'version': 2, 'clear_api_key': True}):
            self.assertEqual(self.update(payload, key='codex').status_code, 200)
            row = self.model.objects.get(pk='codex')
            self.assertIsNone(row.model)
            self.assertIsNone(row.base_url)
            self.assertIsNone(row.request_timeout_seconds)
        provider = self.provider('codex')
        self.assertFalse(provider['enabled'])
        self.assertEqual(provider['priority'], 0)
        self.assertEqual(provider['state']['code'], 'disabled')
        self.assertEqual(provider['scopes'], ['translation'])
        for field in ('model', 'base_url'):
            self.assertEqual(self.update({'version': 3, field: ''}, key='codex').status_code, 400)
        self.assertEqual(self.provider('codex')['version'], 3)

    def test_csrf_is_retained(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.superuser)
        self.assertEqual(client.patch('/api/dashboard/ai-providers/glm/', data='{"version":0}',
                                     content_type='application/json').status_code, 403)

    def test_encrypted_save_and_effective_config(self):
        result = self.update({'version': 0, 'api_key': 'fixture-database-secret', 'model': 'Exact.Model-V1',
                              'enabled': False, 'priority': 3, 'base_url': 'https://override.example/v2',
                              'request_timeout_seconds': 7.5})
        self.assertEqual(result.status_code, 200)
        self.assertIn('providers', result.json()['data'])
        row = self.model.objects.get(pk='glm')
        self.assertNotIn('fixture-database-secret', row.api_key_encrypted)
        self.assertEqual(row.updated_by_id, self.superuser.pk)
        config = self.service.resolve_ai_configs()['glm']
        self.assertEqual(config['api_key'], 'fixture-database-secret')
        self.assertEqual(config['model'], 'Exact.Model-V1')
        self.assertEqual(config['base_url'], config['coding_base_url'])
        self.assertEqual(config['fallback_models'], ['GLM-fallback'])
        self.assertFalse(config['enabled'])
        self.assertEqual(config['_management_version'], 1)
        self.assertEqual(self.provider()['api_key_source'], 'database')
        self.assertNotIn('fixture-database-secret', result.content.decode())

    def test_empty_key_preserve_clear_reset(self):
        self.assertEqual(self.update({'version': 0, 'api_key': 'fixture-db-key'}).status_code, 200)
        encrypted = self.model.objects.get(pk='glm').api_key_encrypted
        self.assertEqual(self.update({'version': 1, 'api_key': '', 'priority': 5}).status_code, 200)
        self.assertEqual(self.model.objects.get(pk='glm').api_key_encrypted, encrypted)
        self.assertEqual(self.update({'version': 2, 'clear_api_key': True}).status_code, 200)
        self.assertEqual(self.provider()['api_key_source'], 'cleared')
        self.assertFalse(self.provider()['has_api_key'])
        self.assertFalse(self.provider()['effective'])
        self.assertEqual(self.update({'version': 3, 'reset': True}).status_code, 200)
        self.assertEqual(self.provider()['api_key_source'], 'settings')
        self.assertEqual(self.provider()['config_source'], 'settings')
        self.assertEqual(self.provider()['priority'], 10)
        self.assertIsNone(self.model.objects.get(pk='glm').api_key_encrypted)

    def test_validation_is_atomic(self):
        bad = [ {'enabled': 'false'}, {'priority': True}, {'priority': -1}, {'request_timeout_seconds': 0},
                {'request_timeout_seconds': True}, {'request_timeout_seconds': float('nan')},
                {'model': ' spaced '}, {'model': ''}, {'model': 'a\nb'}, {'base_url': 'http://a.example'},
                {'base_url': 'https://user:pass@a.example'}, {'base_url': 'https://a.example?token=secret'},
                {'base_url': 'https://a.example#x'}, {'base_url': 'https://a.example:bad'},
                {'api_key': 123}, {'clear_api_key': 'true'}, {'reset': 'true'}, {'unknown': 1},
                {'version': True}, {'version': -1} ]
        for fields in bad:
            with self.subTest(fields=fields):
                payload = {'version': 0, 'enabled': False, **fields}
                self.assertEqual(self.update(payload).status_code, 400)
                self.assertFalse(self.model.objects.exists())
        self.assertEqual(self.update({'version': 0}, key='unknown').status_code, 404)
        self.assertEqual(self.update({'enabled': False}).status_code, 400)

    def test_version_conflict_preserves_current_configuration(self):
        self.assertEqual(self.update({'version': 0, 'enabled': False}).status_code, 200)
        self.assertEqual(self.update({'version': 0, 'enabled': True}).status_code, 409)
        self.assertFalse(self.provider()['enabled'])
        self.assertEqual(self.provider()['version'], 1)

    def test_priority_and_preferred_are_actual_enabled_configs(self):
        self.update({'version': 0, 'enabled': True, 'priority': 1}, key='mimo')
        self.assertEqual([p['provider_key'] for p in self.get_data()['providers']], ['mimo', 'glm', 'codex'])
        self.assertEqual(self.get_data()['preferred_provider'], 'mimo')
        self.assertEqual(self.get_data()['preferred_translation_provider'], 'mimo')
        self.update({'version': 1, 'clear_api_key': True}, key='mimo')
        self.assertEqual(self.get_data()['preferred_provider'], 'glm')
        self.update({'version': 0, 'enabled': False})
        self.assertIsNone(self.get_data()['preferred_provider'])
        self.assertIsNone(self.get_data()['preferred_translation_provider'])

    @override_settings(CODEX_API_CONFIG=CODEX_BASELINE)
    def test_configured_codex_preserves_settings_and_read_only_scopes(self):
        configs = self.service.resolve_ai_configs()
        codex = configs['codex']
        self.assertEqual(codex['model'], 'gpt-5.5')
        self.assertEqual(codex['base_url'], 'https://codex.example')
        self.assertEqual(codex['api_key'], 'fixture-codex-secret')
        self.assertEqual(codex['_scopes'], ('translation',))
        for key in ('glm', 'mimo'):
            self.assertEqual(configs[key]['_scopes'], ('general', 'translation'))
        self.assertNotIn('_scopes', CODEX_BASELINE)
        self.assertTrue(self.provider('codex')['effective'])
        self.assertEqual(self.provider('codex')['state']['code'], 'unverified')
        self.assertNotIn('fixture-codex-secret', json.dumps(self.get_data()))
        with self.assertRaises(TypeError):
            self.service.PROVIDER_SCOPES['codex'] = ('general',)
        self.assertEqual(self.update({'version': 0, 'scopes': ['general']}, key='codex').status_code, 400)
        self.assertFalse(self.model.objects.exists())
        with override_settings(CODEX_API_CONFIG={**CODEX_BASELINE, 'model': 'Exact.Custom-V2',
                                               'base_url': 'https://another.example/prefix'}):
            fresh = self.service.resolve_ai_configs()['codex']
            self.assertEqual(fresh['model'], 'Exact.Custom-V2')
            self.assertEqual(fresh['base_url'], 'https://another.example/prefix')
            self.assertEqual(fresh['_scopes'], ('translation',))

    @override_settings(CODEX_API_CONFIG=CODEX_BASELINE)
    def test_codex_priority_key_and_disable_are_translation_only(self):
        response = self.update({'version': 0, 'api_key': 'fixture-codex-database-secret', 'priority': 0}, key='codex')
        self.assertEqual(response.status_code, 200)
        row = self.model.objects.get(pk='codex')
        self.assertNotIn('fixture-codex-database-secret', row.api_key_encrypted)
        row.full_clean()
        config = self.service.resolve_ai_configs()['codex']
        self.assertEqual(config['api_key'], 'fixture-codex-database-secret')
        self.assertEqual(config['_scopes'], ('translation',))
        with self.assertRaises(ValueError):
            self.service._decrypt_key(row.api_key_encrypted, 'glm')
        data = self.get_data()
        self.assertEqual([p['provider_key'] for p in data['providers']], ['codex', 'glm', 'mimo'])
        self.assertEqual(data['preferred_provider'], 'glm')
        self.assertEqual(data['preferred_translation_provider'], 'codex')
        self.assertEqual(self.provider('codex')['api_key_source'], 'database')
        for secret in ('fixture-codex-secret', 'fixture-codex-database-secret', 'api_key_encrypted', '_runtime_signature'):
            self.assertNotIn(secret, response.content.decode())
        self.service.record_ai_result('codex', config, response={'choices': [{'message': {'content': 'ok'}}]},
                                      latency_ms=8, model='gpt-5.5')
        self.assertEqual(self.provider('codex')['state']['code'], 'healthy')
        self.assertEqual(self.get_data()['last_successful_provider'], 'codex')
        self.assertEqual(self.update({'version': 1, 'enabled': False}, key='codex').status_code, 200)
        self.assertFalse(self.service.resolve_ai_configs()['codex']['enabled'])
        self.assertEqual(self.provider('codex')['state']['code'], 'disabled')
        self.assertEqual(self.get_data()['preferred_provider'], 'glm')
        self.assertEqual(self.get_data()['preferred_translation_provider'], 'glm')
        self.assertEqual(self.get_data()['last_successful_provider'], 'codex')

    @override_settings(CODEX_API_CONFIG={key: value for key, value in CODEX_BASELINE.items() if key != 'model'})
    def test_codex_requires_explicit_model_and_never_becomes_general_preferred(self):
        self.assertEqual(self.update({'version': 0, 'priority': 0}, key='codex').status_code, 200)
        self.assertNotIn('model', self.service.resolve_ai_configs()['codex'])
        self.assertEqual(self.provider('codex')['state']['code'], 'incomplete')
        self.assertEqual(self.get_data()['preferred_translation_provider'], 'glm')
        self.assertEqual(self.update({'version': 1, 'model': 'gpt-5.5'}, key='codex').status_code, 200)
        self.assertEqual(self.get_data()['preferred_translation_provider'], 'codex')
        self.assertEqual(self.update({'version': 0, 'enabled': False}).status_code, 200)
        self.assertIsNone(self.get_data()['preferred_provider'])
        self.assertEqual(self.get_data()['preferred_translation_provider'], 'codex')
        self.assertEqual(self.update({'version': 2, 'clear_api_key': True}, key='codex').status_code, 200)
        self.assertFalse(self.provider('codex')['has_api_key'])
        self.assertEqual(self.provider('codex')['state']['code'], 'incomplete')
        self.assertIsNone(self.get_data()['preferred_translation_provider'])

    def test_record_success_is_real_and_preserves_nullable_overrides(self):
        config = self.service.resolve_ai_configs()['glm']
        self.service.record_ai_result('glm', config, response={'choices': [{'message': {'content': 'ok'}}]},
                                      latency_ms=12.5, model='GLM-fallback')
        row = self.model.objects.get(pk='glm')
        self.assertIsNone(row.enabled)
        self.assertIsNone(row.priority)
        self.assertEqual(row.version, 0)
        self.assertEqual(self.provider()['state']['code'], 'healthy')
        self.assertEqual(self.provider()['state']['last_model'], 'GLM-fallback')
        self.assertEqual(self.get_data()['last_successful_provider'], 'glm')
        self.assertIsNotNone(row.last_success_at)

    def test_empty_response_and_tool_calls(self):
        config = self.service.resolve_ai_configs()['glm']
        self.service.record_ai_result('glm', config, response={'choices': [{'message': {'content': ''}}]})
        self.assertEqual(self.provider()['state']['code'], 'response_error')
        valid = {'id': 'x', 'function': {'name': 'fixture_tool', 'arguments': '{"value": 1}'}}
        self.service.record_ai_result('glm', config, response={'choices': [{'message': {'tool_calls': [valid]}}]})
        self.assertEqual(self.provider()['state']['code'], 'healthy')
        success_at = self.model.objects.get(pk='glm').last_success_at
        self.service.record_ai_result('glm', config, response={'choices': [{'message': {'tool_calls': [{'id': 'x'}]}}]})
        self.assertEqual(self.provider()['state']['code'], 'response_error')
        self.assertEqual(self.model.objects.get(pk='glm').last_success_at, success_at)

    def test_tool_response_structure_matrix(self):
        valid = {'id': 'x', 'function': {'name': 'fixture_tool', 'arguments': '{}'}}
        invalid = [
            {'id': 'x'}, {'function': valid['function']},
            {'id': '', 'function': valid['function']}, {'id': 1, 'function': valid['function']},
            {'id': 'x', 'function': None}, {'id': 'x', 'function': {}},
            {'id': 'x', 'function': {'name': '', 'arguments': '{}'}},
            {'id': 'x', 'function': {'name': 1, 'arguments': '{}'}},
            {'id': 'x', 'function': {'name': 'fixture_tool'}},
        ]
        invalid += [{'id': 'x', 'function': {'name': 'fixture_tool', 'arguments': args}}
                    for args in ('invalid-json', '[]', 'null', '1', '{', None, {}, [])]
        for index, tool in enumerate(invalid):
            for sdk in (False, True):
                with self.subTest(index=index, sdk=sdk):
                    tools = [valid, tool]
                    if sdk:
                        tools = [SimpleNamespace(**{**item, 'function': SimpleNamespace(**item['function'])})
                                 if isinstance(item.get('function'), dict) else SimpleNamespace(**item)
                                 for item in tools]
                        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                            content='content must not bypass invalid tools', tool_calls=tools))])
                    else:
                        response = {'choices': [{'message': {'content': 'content must not bypass invalid tools',
                                                            'tool_calls': tools}}]}
                    self.assertFalse(self.service._response_is_healthy(response))
        for args in ('{}', b'{}', bytearray(b'{}')):
            tool = SimpleNamespace(id='x', function=SimpleNamespace(name='fixture_tool', arguments=args))
            response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[tool]))])
            self.assertTrue(self.service._response_is_healthy(response))

    def test_same_config_older_finish_cannot_overwrite_new_success(self):
        config = self.service.resolve_ai_configs()['glm']
        newer = timezone.now()
        older = newer - timedelta(seconds=1)
        with patch.object(self.service.timezone, 'now', return_value=newer):
            self.service.record_ai_result('glm', config, response={'choices': [{'message': {'content': 'ok'}}]},
                                          latency_ms=7.5, model='new-success-model')
        before = self.model.objects.get(pk='glm')
        # Deterministically simulate reversed lock acquisition, not MySQL concurrency.
        for outcome in ({'error': ValueError('fixture failure')},
                        {'response': {'choices': [{'message': {'content': 'older success'}}]}}):
            with self.subTest(outcome=list(outcome)):
                with patch.object(self.service.timezone, 'now', return_value=older):
                    self.service.record_ai_result('glm', config, **outcome, latency_ms=999, model='old-model')
                after = self.model.objects.get(pk='glm')
                for field in self.model._meta.concrete_fields:
                    self.assertEqual(getattr(after, field.attname), getattr(before, field.attname), field.attname)
                self.assertEqual(self.provider()['state']['code'], 'healthy')

    def test_new_signature_evidence_is_not_blocked_by_old_timestamp(self):
        old = self.service.resolve_ai_configs()['glm']
        newer = timezone.now()
        with patch.object(self.service.timezone, 'now', return_value=newer):
            self.service.record_ai_result('glm', old, response={'choices': [{'message': {'content': 'ok'}}]})
        with override_settings(ZHIYU_API_CONFIG={**BASELINE['ZHIYU_API_CONFIG'], 'model': 'new-config'}):
            fresh = self.service.resolve_ai_configs()['glm']
            earlier = newer - timedelta(seconds=1)
            with patch.object(self.service.timezone, 'now', return_value=earlier):
                self.service.record_ai_result('glm', fresh, response={'choices': [{'message': {'content': 'ok'}}]},
                                              model='new-config')
            row = self.model.objects.get(pk='glm')
            self.assertEqual(row.last_config_signature, fresh['_runtime_signature'])
            self.assertEqual(row.last_finished_at, earlier)
            self.assertEqual(row.last_model, 'new-config')

    def test_stale_version_and_settings_signature_fencing(self):
        config = self.service.resolve_ai_configs()['glm']
        self.update({'version': 0, 'model': 'new-model'})
        self.service.record_ai_result('glm', config, response={'choices': [{'message': {'content': 'ok'}}]})
        self.assertIsNone(self.model.objects.get(pk='glm').last_finished_at)
        fresh = self.service.resolve_ai_configs()['glm']
        self.service.record_ai_result('glm', fresh, response={'choices': [{'message': {'content': 'ok'}}]})
        with override_settings(ZHIYU_API_CONFIG={**BASELINE['ZHIYU_API_CONFIG'], 'api_key': 'changed-fixture'}):
            self.assertEqual(self.provider()['state']['code'], 'unverified')

    def test_late_settings_result_cannot_overwrite_new_success(self):
        old = self.service.resolve_ai_configs()['glm']
        with override_settings(ZHIYU_API_CONFIG={**BASELINE['ZHIYU_API_CONFIG'], 'model': 'new-settings-model'}):
            fresh = self.service.resolve_ai_configs()['glm']
            self.assertEqual(fresh['_management_version'], old['_management_version'])
            self.service.record_ai_result('glm', fresh, response={'choices': [{'message': {'content': 'ok'}}]},
                                          latency_ms=7.5, model='new-settings-model')
            before = self.model.objects.get(pk='glm')
            for outcome in ({'response': {'choices': [{'message': {'content': 'late'}}]}},
                            {'error': ValueError('late fixture failure')}):
                with self.subTest(outcome=list(outcome)):
                    self.service.record_ai_result('glm', old, **outcome, latency_ms=999, model='GLM-fixture')
                    after = self.model.objects.get(pk='glm')
                    for field in ('last_state', 'last_http_status', 'last_error_code', 'last_finished_at',
                                  'last_latency_ms', 'last_model', 'last_config_signature',
                                  'last_success_at', 'last_failure_at'):
                        self.assertEqual(getattr(after, field), getattr(before, field), field)
                    self.assertEqual(self.provider()['state']['code'], 'healthy')
                    self.assertEqual(self.provider()['state']['last_model'], 'new-settings-model')
            self.assertNotIn('_runtime_signature', json.dumps(self.get_data()))

    def test_structured_error_classification_not_arbitrary_error_text(self):
        from core.ai_errors import AIHTTPError
        config = self.service.resolve_ai_configs()['glm']
        for error, code in [(AIHTTPError('GLM', 401, {'error': {'code': 'invalid_api_key'}}), 'authentication_error'),
                            (AIHTTPError('GLM', 429), 'rate_limited'),
                            (ssl.SSLCertVerificationError('certificate verify failed'), 'certificate_error'),
                            (ValueError('text 429 fixture-secret'), 'response_error')]:
            self.service.record_ai_result('glm', config, error=error)
            self.assertEqual(self.provider()['state']['code'], code)
            self.assertNotIn('fixture-secret', json.dumps(self.get_data()))

    def test_startup_and_missing_table_only_fallback(self):
        with patch('django.apps.apps.ready', False):
            with self.assertNumQueries(0):
                self.assertTrue(self.service.resolve_ai_configs()['glm']['enabled'])
        with patch.object(self.model.objects, 'filter', side_effect=OperationalError('database unavailable')):
            with self.assertRaises(OperationalError):
                self.service.resolve_ai_configs()
        with patch.object(self.model.objects, 'filter', side_effect=OperationalError('no such table: botend_aiproviderconfiguration')):
            self.assertTrue(self.service.resolve_ai_configs()['glm']['enabled'])

    def test_status_write_failure_does_not_break_completion(self):
        config = self.service.resolve_ai_configs()['glm']
        with patch.object(self.model.objects, 'get_or_create', side_effect=OperationalError('unavailable')):
            self.assertIsNone(self.service.record_ai_result('glm', config, response={'choices': []}))

    def test_cipher_nonce_authentication_and_fail_closed(self):
        import base64
        self.assertEqual(self.update({'version': 0, 'api_key': 'nonce-fixture'}).status_code, 200)
        first = self.model.objects.get(pk='glm').api_key_encrypted
        self.assertEqual(self.update({'version': 1, 'api_key': 'nonce-fixture'}).status_code, 200)
        second = self.model.objects.get(pk='glm').api_key_encrypted
        self.assertNotEqual(first, second)
        payload = bytearray(base64.b64decode(second.split(':', 2)[2]))
        self.assertEqual(len(payload), 16 + 16 + len('nonce-fixture'))
        payload[-1] ^= 1
        self.model.objects.filter(pk='glm').update(api_key_encrypted='aesgcm:v1:' + base64.b64encode(payload).decode())
        with self.assertRaises(ValueError):
            self.service.resolve_ai_configs()
        response = self.client.get('/api/dashboard/ai-providers/')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn(first, response.content.decode())
        self.assertNotIn('nonce-fixture', response.content.decode())
        self.assertEqual(self.update({'version': 2, 'reset': True}).status_code, 200)

    def test_invalid_update_preserves_existing_row(self):
        self.update({'version': 0, 'enabled': False, 'api_key': 'existing-fixture'})
        before = self.model.objects.get(pk='glm')
        response = self.update({'version': 1, 'enabled': True, 'api_key': 'new-fixture', 'priority': '1'})
        self.assertEqual(response.status_code, 400)
        after = self.model.objects.get(pk='glm')
        self.assertEqual(after.version, before.version)
        self.assertEqual(after.api_key_encrypted, before.api_key_encrypted)
        self.assertFalse(after.enabled)

    def test_no_installed_app_and_startup_never_record_or_read_db(self):
        for kwargs in ({'ready': False}, {'is_installed': lambda app: False}):
            with patch.multiple('django.apps.apps', **kwargs):
                with self.assertNumQueries(0):
                    configs = self.service.resolve_ai_configs()
                    self.assertEqual(configs['glm']['priority'], 10)
                    self.assertEqual(configs['mimo']['priority'], 20)
                    self.assertEqual(configs['codex']['priority'], 30)
                    self.assertEqual(configs['codex']['_scopes'], ('translation',))
                    self.assertEqual(configs['glm']['_management_version'], 0)
                    self.assertTrue(configs['glm']['_runtime_signature'])
                    self.service.record_ai_result('glm', configs['glm'], response={'choices': []})
        self.assertFalse(self.model.objects.exists())

    def test_record_does_not_invent_actual_model(self):
        config = self.service.resolve_ai_configs()['glm']
        self.service.record_ai_result('glm', config, response={'choices': [{'message': {'content': 'ok'}}]})
        self.assertEqual(self.provider()['state']['last_model'], '')

    def test_model_is_not_in_generic_crud_allowlist(self):
        from botend.dashboard.dashboard import MODEL_DESCRIPTIONS
        self.assertNotIn('AIProviderConfiguration', MODEL_DESCRIPTIONS)
