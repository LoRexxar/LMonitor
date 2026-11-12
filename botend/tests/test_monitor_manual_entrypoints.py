"""Manual API admission/argument contracts; collectors and network never run.

SQLite proves deterministic admission/state only, not concurrent row locking.
"""
from datetime import timedelta
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from LMonitor.config import Monitor_Type_BaseObject_List
from botend.controller.plugins.portal.PortalPeakSpecRankMonitor import PortalPeakSpecRankMonitor
from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
from botend.models import MonitorTask, MonitorTaskLease, MonitorTaskLeaseLost


class MonitorManualEntrypointTests(TestCase):
    def setUp(self):
        MonitorTask.objects.all().delete()
        self.enterContext(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.client.force_login(get_user_model().objects.create_superuser(
            username='manual-entrypoint-admin', email='manual@example.invalid'))
        self.now = timezone.now()
        self.peak = self.task(PortalPeakSpecRankMonitor, target='exact-target?region=us', flag='old-peak')
        self.wago = self.task(WagoSkillDiffMonitor, target='wow', flag='old-wago')

    def task(self, plugin, **overrides):
        values = dict(name=plugin.__name__, type=Monitor_Type_BaseObject_List.index(plugin),
                      target='', env_limit=0, is_active=False,
                      last_scan_time=self.now - timedelta(days=2))
        values.update(overrides)
        return MonitorTask.objects.create(**values)

    def lease(self, task, owner='existing-owner'):
        return MonitorTaskLease.objects.create(task=task, owner=owner,
            expires_at=self.now + timedelta(hours=1), claimed_at=self.now)

    def post(self, path, payload=None):
        return self.client.post(path, data=json.dumps(payload or {}), content_type='application/json')

    def assert_rejected(self, response, code):
        self.assertEqual(response.status_code, 409)
        self.assertFalse(response.json()['success'])
        self.assertEqual(response.json()['code'], code)
        self.assertFalse(response.json()['executed'])

    def test_busy_same_scope_never_calls_either_api_executor(self):
        for task, path, payload, plugin, method in (
            (self.peak, '/api/portal/peak/refresh/', {}, PortalPeakSpecRankMonitor, 'scan'),
            (self.wago, '/api/wago-skill-diff/rerun/', {'event_id': '00123'}, WagoSkillDiffMonitor, 'rerun_build_event'),
        ):
            with self.subTest(task=task.name), patch.object(plugin, method, autospec=True) as execute:
                self.lease(task)
                response = self.post(path, payload)
                self.assert_rejected(response, 'monitor_task_busy')
                execute.assert_not_called()
                self.assertEqual(MonitorTaskLease.objects.get(task=task).owner, 'existing-owner')
                MonitorTaskLease.objects.filter(task=task).delete()

    def test_full_three_slot_pool_rejects_both_apis_without_execution(self):
        for index in range(3):
            task = MonitorTask.objects.create(name='busy-{}'.format(index), type=1000 + index,
                target='', last_scan_time=self.now)
            self.lease(task)
        with patch.object(PortalPeakSpecRankMonitor, 'scan', autospec=True) as scan, \
                patch.object(WagoSkillDiffMonitor, 'rerun_build_diff', autospec=True) as rerun:
            self.assert_rejected(self.post('/api/portal/peak/refresh/'), 'monitor_task_busy')
            self.assert_rejected(self.post('/api/wago-skill-diff/rerun/', {
                'from_build': '11.0.1.1', 'to_build': '11.0.1.2'}), 'monitor_task_busy')
            scan.assert_not_called()
            rerun.assert_not_called()
        self.assertEqual(MonitorTaskLease.objects.count(), 3)

    def test_peak_passes_claimed_task_exact_target_and_fences_only_real_flag_save(self):
        old_time = self.peak.last_scan_time

        def scan(plugin, target):
            self.assertEqual(target, 'exact-target?region=us')
            task = plugin.task
            self.assertEqual(task.pk, self.peak.pk)
            lease = MonitorTaskLease.objects.get(task=task)
            self.assertEqual(task._monitor_task_lease_owner, lease.owner)
            self.assertTrue(lease.is_manual)
            self.assertEqual(task.last_scan_time, old_time)
            task.flag = 'saved-by-real-scan'
            task.save()
            return True

        with patch.object(PortalPeakSpecRankMonitor, 'scan', autospec=True, side_effect=scan) as execute:
            response = self.post('/api/portal/peak/refresh/')
        self.assertEqual(response.json(), {'success': True, 'ok': True, 'total': 0})
        execute.assert_called_once()
        self.peak.refresh_from_db()
        self.assertEqual(self.peak.flag, 'saved-by-real-scan')
        self.assertEqual(self.peak.last_scan_time, old_time)
        self.assertFalse(MonitorTaskLease.objects.exists())

    def test_wago_preserves_event_and_build_parameters_and_response(self):
        original_time = self.wago.last_scan_time
        result = {'success': True, 'report_id': 67, 'custom': ['unchanged']}
        for payload, method, expected in (
            ({'event_id': '00123', 'from_build': 'ignored', 'to_build': 'ignored'},
             'rerun_build_event', {'event_id': '00123'}),
            ({'branch': ' wow_classic ', 'from_build': ' 1.2.3.004 ',
              'to_build': ' 1.2.3.005 ', 'locale': ' zhCN '},
             'rerun_build_diff', {'branch': 'wow_classic', 'from_build': '1.2.3.004',
                                  'to_build': '1.2.3.005', 'locale': 'zhCN'}),
            ({'from_build': '1', 'to_build': '2'}, 'rerun_build_diff',
             {'branch': 'wow', 'from_build': '1', 'to_build': '2', 'locale': 'enUS'}),
        ):
            def execute(plugin, **kwargs):
                self.assertEqual(kwargs, expected)
                self.assertTrue(MonitorTaskLease.objects.filter(task=plugin.task,
                    owner=plugin.task._monitor_task_lease_owner).exists())
                return result

            with self.subTest(method=method, payload=payload), patch.object(
                    WagoSkillDiffMonitor, method, autospec=True, side_effect=execute) as run:
                response = self.post('/api/wago-skill-diff/rerun/', payload)
                self.assertEqual(response.json(), result)
                run.assert_called_once()
                self.assertFalse(MonitorTaskLease.objects.exists())
        self.wago.refresh_from_db()
        self.assertEqual(self.wago.last_scan_time, original_time)
        self.assertEqual(self.wago.flag, 'old-wago')

    def test_environment_blocked_for_both_apis_never_executes(self):
        with patch('botend.monitor_env.is_windows', return_value=False):
            for task, path, payload, plugin, method in (
                (self.peak, '/api/portal/peak/refresh/', {}, PortalPeakSpecRankMonitor, 'scan'),
                (self.wago, '/api/wago-skill-diff/rerun/', {'event_id': 123}, WagoSkillDiffMonitor, 'rerun_build_event'),
            ):
                task.env_limit = 1
                task.save(update_fields=['env_limit'])
                with self.subTest(task=task.name), patch.object(plugin, method, autospec=True) as execute:
                    response = self.post(path, payload)
                    self.assertFalse(response.json()['success'])
                    self.assertEqual(response.json()['code'], 'env_limit_blocked')
                    self.assertEqual(response.json()['env_limit'], 1)
                    execute.assert_not_called()
                    self.assertFalse(MonitorTaskLease.objects.exists())

    def test_registry_type_is_authoritative_and_legacy_name_alias_is_supported(self):
        from botend.services.monitor_task_execution import monitor_task_execution, MonitorTaskAdmissionBusy
        self.peak.name = 'legacy-label'
        self.peak.save(update_fields=['name'])
        with monitor_task_execution('PortalPeakSpecRankMonitor') as task:
            self.assertEqual(task.pk, self.peak.pk)
        self.peak.delete()
        self.wago.name = 'PortalPeakSpecRankMonitor'
        self.wago.save(update_fields=['name'])
        with self.assertRaises(MonitorTaskAdmissionBusy) as raised:
            with monitor_task_execution('PortalPeakSpecRankMonitor'):
                self.fail('incorrect plugin type must not enter execution')
        self.assertEqual(raised.exception.code, 'monitor_task_not_configured')

    def test_missing_configuration_rejects_without_creating_a_task(self):
        self.peak.delete()
        with patch.object(PortalPeakSpecRankMonitor, 'scan', autospec=True) as execute:
            response = self.post('/api/portal/peak/refresh/')
        self.assert_rejected(response, 'monitor_task_not_configured')
        execute.assert_not_called()
        self.assertEqual(MonitorTask.objects.count(), 1)

    def test_helper_releases_on_executor_exception_without_saving_control_state(self):
        from botend.services.monitor_task_execution import monitor_task_execution
        original_time = self.peak.last_scan_time
        with self.assertRaisesRegex(RuntimeError, 'executor failed'):
            with monitor_task_execution('PortalPeakSpecRankMonitor') as task:
                task.flag = 'unsaved'
                raise RuntimeError('executor failed')
        self.peak.refresh_from_db()
        self.assertEqual(self.peak.flag, 'old-peak')
        self.assertEqual(self.peak.last_scan_time, original_time)
        self.assertFalse(MonitorTaskLease.objects.exists())

    def test_helper_rejects_normal_exit_after_lease_loss_and_keeps_replacement(self):
        from botend.services.monitor_task_execution import monitor_task_execution
        with self.assertRaises(MonitorTaskLeaseLost):
            with monitor_task_execution('PortalPeakSpecRankMonitor'):
                MonitorTaskLease.objects.filter(task=self.peak).update(owner='replacement')
        self.assertEqual(MonitorTaskLease.objects.get(task=self.peak).owner, 'replacement')

    def test_helper_rejects_expired_lease_even_before_first_heartbeat(self):
        from botend.services.monitor_task_execution import monitor_task_execution
        with self.assertRaises(MonitorTaskLeaseLost):
            with monitor_task_execution('PortalPeakSpecRankMonitor'):
                MonitorTaskLease.objects.filter(task=self.peak).update(
                    expires_at=timezone.now() - timedelta(seconds=1))
        self.assertFalse(MonitorTaskLease.objects.exists())

    def test_helper_releases_when_heartbeat_start_fails(self):
        from botend.services.monitor_task_execution import monitor_task_execution
        with patch('botend.views._MonitorTaskLeaseHeartbeat.start', side_effect=RuntimeError('start failed')):
            with self.assertRaisesRegex(RuntimeError, 'start failed'):
                with monitor_task_execution('PortalPeakSpecRankMonitor'):
                    self.fail('must not execute without heartbeat')
        self.assertFalse(MonitorTaskLease.objects.exists())

    def test_authentication_and_wago_permission_still_precede_execution(self):
        self.client.logout()
        with patch.object(PortalPeakSpecRankMonitor, 'scan', autospec=True) as scan, \
                patch.object(WagoSkillDiffMonitor, 'rerun_build_event', autospec=True) as rerun:
            self.assertEqual(self.post('/api/portal/peak/refresh/').status_code, 302)
            self.assertEqual(self.post('/api/wago-skill-diff/rerun/', {'event_id': 1}).status_code, 401)
            user = get_user_model().objects.create_user(username='no-wago-permission')
            self.client.force_login(user)
            self.assertEqual(self.post('/api/wago-skill-diff/rerun/', {'event_id': 1}).status_code, 403)
            scan.assert_not_called()
            rerun.assert_not_called()
        self.assertFalse(MonitorTaskLease.objects.exists())
