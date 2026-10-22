"""Backend management security and persistence, using real isolated ORM rows."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from django.conf import settings
from django.http import HttpResponse
from django.middleware.csrf import CsrfViewMiddleware, get_token
from django.test import RequestFactory, TestCase, override_settings
from django.urls import resolve, reverse

from botend.dashboard.api import SimcBackendBinaryAPIView
from botend.models import SimcBackendBinary


class SimcBackendManagementSecurityTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.view_class = SimcBackendBinaryAPIView
        self.url = reverse('simc_backend_binary')
        self.regular_user = SimpleNamespace(is_staff=False, username='regular')
        self.staff_user = SimpleNamespace(is_staff=True, username='staff')
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        config = override_settings(SIMC_CONFIG={
            'simc_source_dir': str(root / 'source'),
            'simc_build_dir': str(root / 'build'),
            'simc_path': str(root / 'build' / 'simc'),
        })
        config.enable()
        self.addCleanup(config.disable)
        # Binary/catalog identity is an external resource boundary. ORM queries,
        # response serialization and persisted settings remain real.
        identity = patch('botend.dashboard.api.current_validation_identity', return_value=None)
        identity.start()
        self.addCleanup(identity.stop)
        self.backend, _ = SimcBackendBinary.objects.update_or_create(
            identifier='production', defaults=dict(
                name='正式服', platform='linux64',
                simc_path=str(root / 'build' / 'simc'),
                current_version='a' * 40, latest_version='b' * 40,
                local_worker_enabled=True, auto_update=True, is_active=True,
                is_updating=False, update_progress=0, update_status='idle', last_error='',
                maintenance_enabled=True, maintenance_daily_time='03:00',
                maintenance_window_minutes=60, maintenance_policy_revision=1,
            ),
        )

    def _get(self, user=None):
        request = self.factory.get(self.url)
        request.user = user or self.staff_user
        response = self.view_class().get(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        return data['data']

    def _post(self, payload, user=None, status=200):
        request = self.factory.post(self.url, data=json.dumps(payload), content_type='application/json')
        request.user = user or self.staff_user
        response = self.view_class().post(request)
        self.assertEqual(response.status_code, status)
        data = json.loads(response.content)
        self.assertEqual(data['success'], status == 200)
        return data

    def test_get_returns_can_write_false_for_regular_user(self):
        self.assertFalse(self._get(self.regular_user)['can_write'])

    @patch('botend.dashboard.api.current_validation_identity', return_value=('a' * 40, '12.0.1.70000'))
    def test_get_returns_supported_wow_version(self, _identity):
        self.assertEqual(self._get()['game_version'], '12.0.1.70000')

    @patch('botend.dashboard.api.current_validation_identity', return_value=('a' * 40, '12.0.1.70000'))
    def test_supported_wow_version_uses_backend_catalog_identity(self, identity):
        self.assertEqual(self.view_class()._get_game_version(self.backend), '12.0.1.70000')
        identity.assert_called_once_with(backend=self.backend)

    def test_get_returns_can_write_true_for_staff(self):
        self.assertTrue(self._get()['can_write'])

    def test_get_never_returns_simc_path(self):
        payload = json.dumps(self._get(self.regular_user))
        self.assertNotIn('simc_path', payload)
        self.assertNotIn(self.backend.simc_path, payload)

    def test_get_never_returns_last_error_raw(self):
        self.backend.last_error = 'Sensitive error with /private/path'
        self.backend.save(update_fields=['last_error'])
        payload = json.dumps(self._get())
        self.assertNotIn('last_error', payload)
        self.assertNotIn(self.backend.last_error, payload)

    def test_get_returns_has_error_boolean_only(self):
        self.assertIs(self._get()['has_error'], False)
        self.backend.last_error = 'Some error'
        self.backend.save(update_fields=['last_error'])
        data = self._get()
        self.assertIs(data['has_error'], True)
        self.assertNotIn('last_error', data)

    def test_regular_user_post_returns_403(self):
        for action in ('check', 'update', 'set_auto_update', 'set_maintenance_schedule',
                       'set_local_worker_enabled', 'dispatch_agent_maintenance'):
            with self.subTest(action=action):
                data = self._post({'action': action}, self.regular_user, status=403)
                self.assertIn('仅管理员', data['error'])
        self.backend.refresh_from_db()
        self.assertFalse(self.backend.is_updating)
        self.assertTrue(self.backend.auto_update)

    def _assert_compilation_dispatch(self, action, check_only):
        # Never start a compiler/thread; exercise its real captured callback.
        with patch('botend.dashboard.api.threading.Thread') as thread, \
                patch('django.core.management.call_command') as command:
            data = self._post({'action': action, 'threads': 1})
            self.assertIn('检查' if check_only else '本地编译', data['message'])
            self.backend.refresh_from_db()
            self.assertTrue(self.backend.is_updating)
            thread.assert_called_once()
            self.assertTrue(thread.call_args.kwargs['daemon'])
            thread.return_value.start.assert_called_once_with()
            thread.call_args.kwargs['target']()
            command.assert_called_once_with('update_simc_binary', threads=1, no_pull=False, check=check_only)

    def test_staff_post_check_action_calls_update_simc_binary_check_only(self):
        self._assert_compilation_dispatch('check', True)

    def test_staff_post_update_action_calls_update_simc_binary_with_pull(self):
        self._assert_compilation_dispatch('update', False)

    def test_staff_post_set_auto_update_action_toggles_flag(self):
        data = self._post({'action': 'set_auto_update', 'auto_update': False})
        self.assertIn('关闭', data['message'])
        self.backend.refresh_from_db()
        self.assertFalse(self.backend.auto_update)
        self.assertIs(data['data']['auto_update'], False)

    def test_post_missing_csrf_should_be_rejected_by_middleware(self):
        self.assertIn('django.middleware.csrf.CsrfViewMiddleware', settings.MIDDLEWARE)
        callback = resolve(self.url).func
        self.assertIs(callback.view_class, self.view_class)
        middleware = CsrfViewMiddleware(lambda request: HttpResponse())
        for has_token in (False, True):
            with self.subTest(has_token=has_token):
                request = self.factory.post(self.url, {'action': 'check'})
                request.user = self.staff_user
                if has_token:
                    request.META[settings.CSRF_HEADER_NAME] = get_token(request)
                    request.COOKIES[settings.CSRF_COOKIE_NAME] = request.META['CSRF_COOKIE']
                middleware.process_request(request)
                response = middleware.process_view(request, callback, (), {})
                if has_token:
                    self.assertIsNone(response)
                else:
                    self.assertIsInstance(response, HttpResponse)
                    self.assertEqual(response.status_code, 403)

    def test_post_response_never_contains_simc_path_or_last_error(self):
        self.backend.last_error = 'Sensitive error'
        self.backend.save(update_fields=['last_error'])
        payload = json.dumps(self._post({'action': 'set_auto_update', 'auto_update': False}))
        for secret in ('simc_path', 'last_error', self.backend.simc_path, self.backend.last_error):
            self.assertNotIn(secret, payload)

    def test_post_unsupported_action_returns_400(self):
        self.assertIn('不支持', self._post({'action': 'delete'}, status=400)['error'])

    def test_staff_can_configure_daily_maintenance_window(self):
        data = self._post({'action': 'set_maintenance_schedule', 'enabled': True,
                           'daily_time': '03:30', 'window_minutes': 45})
        self.backend.refresh_from_db()
        self.assertEqual(self.backend.maintenance_daily_time, '03:30')
        self.assertEqual(self.backend.maintenance_window_minutes, 45)
        self.assertEqual(self.backend.maintenance_policy_revision, 2)
        self.assertEqual(data['data']['maintenance_policy']['daily_time'], '03:30')

    def test_maintenance_schedule_rejects_invalid_clock_or_window(self):
        for clock, window in (('25:00', 45), ('03:00', 0)):
            with self.subTest(clock=clock, window=window):
                self._post({'action': 'set_maintenance_schedule', 'enabled': True,
                            'daily_time': clock, 'window_minutes': window}, status=400)
        self.backend.refresh_from_db()
        self.assertEqual(self.backend.maintenance_policy_revision, 1)

    def test_dispatch_agent_maintenance_rejects_empty_agent_selection(self):
        data = self._post({'action': 'dispatch_agent_maintenance',
                           'backend_id': self.backend.pk}, status=404)
        self.assertIn('没有可下发', data['error'])
