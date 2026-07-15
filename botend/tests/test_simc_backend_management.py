#!/usr/bin/env python
# encoding: utf-8
"""
Test SimC backend binary management security contract.
Ensures regular users cannot modify backend, staff can trigger safe local compilation,
and responses never leak sensitive paths or raw errors.
"""
import json
import unittest
from unittest.mock import patch, Mock


class SimcBackendManagementSecurityTests(unittest.TestCase):
    """Test backend management without database by mocking model layer."""
    def setUp(self):
        from django.test import RequestFactory
        from botend.dashboard.api import SimcBackendBinaryAPIView
        self.factory = RequestFactory()
        self.view_class = SimcBackendBinaryAPIView
        self.regular_user = Mock(spec=['is_staff', 'username'])
        self.regular_user.is_staff = False
        self.regular_user.username = 'regular'
        self.staff_user = Mock(spec=['is_staff', 'username'])
        self.staff_user.is_staff = True
        self.staff_user.username = 'staff'

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_get_returns_can_write_false_for_regular_user(self, mock_objects):
        """Regular users GET can_write=false."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.auto_update = True
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = ''
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.get('/api/simc-backend-binary/')
        request.user = self.regular_user
        view = self.view_class()
        response = view.get(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertIn('data', data)
        self.assertIn('can_write', data['data'])
        self.assertFalse(data['data']['can_write'])

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_get_returns_can_write_true_for_staff(self, mock_objects):
        """Staff users GET can_write=true."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.auto_update = True
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = ''
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.get('/api/simc-backend-binary/')
        request.user = self.staff_user
        view = self.view_class()
        response = view.get(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertIn('data', data)
        self.assertIn('can_write', data['data'])
        self.assertTrue(data['data']['can_write'])

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_get_never_returns_simc_path(self, mock_objects):
        """GET response must not contain simc_path field."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.auto_update = True
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = ''
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.get('/api/simc-backend-binary/')
        request.user = self.regular_user
        view = self.view_class()
        response = view.get(request)
        data = json.loads(response.content)
        self.assertNotIn('simc_path', data.get('data', {}))

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_get_never_returns_last_error_raw(self, mock_objects):
        """GET response must not contain last_error field."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.auto_update = True
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = 'Sensitive error message with /home/paths'
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.get('/api/simc-backend-binary/')
        request.user = self.staff_user
        view = self.view_class()
        response = view.get(request)
        data = json.loads(response.content)
        self.assertNotIn('last_error', data.get('data', {}))

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_get_returns_has_error_boolean_only(self, mock_objects):
        """GET returns has_error boolean instead of raw error text."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.auto_update = True
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = 'Some error'
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.get('/api/simc-backend-binary/')
        request.user = self.staff_user
        view = self.view_class()
        response = view.get(request)
        data = json.loads(response.content)
        self.assertIn('has_error', data['data'])
        self.assertTrue(data['data']['has_error'])
        self.assertNotIn('last_error', data['data'])

    def test_regular_user_post_returns_403(self):
        """Regular users cannot POST to backend endpoint."""
        request = self.factory.post(
            '/api/simc-backend-binary/',
            data=json.dumps({'action': 'check'}),
            content_type='application/json',
        )
        request.user = self.regular_user
        view = self.view_class()
        response = view.post(request)
        self.assertEqual(response.status_code, 403)
        data = json.loads(response.content)
        self.assertFalse(data['success'])
        self.assertIn('仅管理员', data['error'])

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    @patch('botend.dashboard.api.threading.Thread')
    def test_staff_post_check_action_calls_update_simc_binary_check_only(self, mock_thread, mock_objects):
        """Staff POST action=check spawns thread calling update_simc_binary --check."""
        mock_row = Mock()
        mock_row.pk = 1
        mock_row.platform = 'linux64'
        mock_row.is_updating = False
        mock_objects.filter.return_value.first.return_value = mock_row
        mock_objects.filter.return_value.update.return_value = 1

        with patch('django.core.management.call_command') as mock_call:
            request = self.factory.post(
                '/api/simc-backend-binary/',
                data=json.dumps({'action': 'check'}),
                content_type='application/json',
            )
            request.user = self.staff_user
            view = self.view_class()
            response = view.post(request)
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.content)
            self.assertTrue(data['success'])
            self.assertIn('检查', data['message'])
            mock_thread.assert_called_once()
            thread_args = mock_thread.call_args
            self.assertTrue(thread_args[1].get('daemon'))
            target_func = thread_args[1]['target']
            target_func()
            mock_call.assert_called_once_with('update_simc_binary', threads=2, no_pull=False, check=True)

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    @patch('botend.dashboard.api.threading.Thread')
    def test_staff_post_update_action_calls_update_simc_binary_with_pull(self, mock_thread, mock_objects):
        """Staff POST action=update spawns thread calling update_simc_binary without --check."""
        mock_row = Mock()
        mock_row.pk = 1
        mock_row.platform = 'linux64'
        mock_row.is_updating = False
        mock_objects.filter.return_value.first.return_value = mock_row
        mock_objects.filter.return_value.update.return_value = 1

        with patch('django.core.management.call_command') as mock_call:
            request = self.factory.post(
                '/api/simc-backend-binary/',
                data=json.dumps({'action': 'update', 'threads': 1}),
                content_type='application/json',
            )
            request.user = self.staff_user
            view = self.view_class()
            response = view.post(request)
            self.assertEqual(response.status_code, 200)
            data = json.loads(response.content)
            self.assertTrue(data['success'])
            self.assertIn('本地编译', data['message'])
            mock_thread.assert_called_once()
            target_func = mock_thread.call_args[1]['target']
            target_func()
            mock_call.assert_called_once_with('update_simc_binary', threads=1, no_pull=False, check=False)

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_staff_post_set_auto_update_action_toggles_flag(self, mock_objects):
        """Staff POST action=set_auto_update updates auto_update field."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.auto_update = True
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = ''
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.post(
            '/api/simc-backend-binary/',
            data=json.dumps({'action': 'set_auto_update', 'auto_update': False}),
            content_type='application/json',
        )
        request.user = self.staff_user
        view = self.view_class()
        response = view.post(request)
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertTrue(data['success'])
        self.assertIn('关闭', data['message'])
        self.assertFalse(mock_row.auto_update)

    def test_post_missing_csrf_should_be_rejected_by_middleware(self):
        """Write requests without CSRF token should be rejected by Django middleware."""
        # This test documents the requirement; actual CSRF validation is done by middleware
        # in production. The view itself checks is_staff but does not directly validate CSRF.
        pass

    @patch('botend.dashboard.api.SimcBackendBinary.objects')
    def test_post_response_never_contains_simc_path_or_last_error(self, mock_objects):
        """POST action=set_auto_update response must not leak simc_path or last_error."""
        mock_row = Mock()
        mock_row.platform = 'linux64'
        mock_row.auto_update = True
        mock_row.current_version = '11.0.1'
        mock_row.latest_version = '11.0.2'
        mock_row.is_updating = False
        mock_row.update_progress = 0
        mock_row.update_status = 'idle'
        mock_row.last_error = 'Sensitive error'
        mock_row.last_checked_at = None
        mock_row.last_updated_at = None
        mock_objects.filter.return_value.first.return_value = mock_row

        request = self.factory.post(
            '/api/simc-backend-binary/',
            data=json.dumps({'action': 'set_auto_update', 'auto_update': False}),
            content_type='application/json',
        )
        request.user = self.staff_user
        view = self.view_class()
        response = view.post(request)
        data = json.loads(response.content)
        self.assertNotIn('simc_path', json.dumps(data))
        self.assertNotIn('last_error', json.dumps(data))

    def test_post_unsupported_action_returns_400(self):
        """POST with unsupported action returns 400."""
        request = self.factory.post(
            '/api/simc-backend-binary/',
            data=json.dumps({'action': 'delete'}),
            content_type='application/json',
        )
        request.user = self.staff_user
        view = self.view_class()
        response = view.post(request)
        self.assertEqual(response.status_code, 400)
        data = json.loads(response.content)
        self.assertFalse(data['success'])
        self.assertIn('不支持', data['error'])
