"""失败原因可从普通日志复盘，诊断不得改变原有采集、退避和发布结果。"""
import json
from contextlib import ExitStack
from types import SimpleNamespace
from datetime import timedelta
from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor, WagoDiffUnavailable
from botend.models import WowWagoBuildEvent, WowWagoHotfixEvent, WowWagoMonitorState
from botend.services.wago_diagnostics import safe_message
from utils.log import logger


def stages(capture):
    prefix = '[WagoSkillDiff Stage] '
    return [json.loads(record.getMessage()[len(prefix):]) for record in capture.records
            if record.getMessage().startswith(prefix)]


class WagoDiagnosticTests(SimpleTestCase):
    def setUp(self):
        self.monitor = WagoSkillDiffMonitor(None, None)
        self.event = WowWagoBuildEvent(pk=34, branch='wow', locale='enUS',
                                      from_build='12.0.7.68887', to_build='12.1.5.69594')

    def test_failure_keeps_original_exception_and_records_phase_cause_and_location(self):
        cause = ValueError('分页元数据不完整')
        failure = WagoDiffUnavailable('Hotfix prefix search limit push=113800')
        def operation():
            raise failure from cause
        with self.assertLogs(logger, level='INFO') as captured:
            with self.assertRaises(WagoDiffUnavailable) as raised:
                self.monitor._run_stage('hotfix_collect', operation, event=self.event)
        self.assertIs(raised.exception, failure)
        failed = stages(captured)[-1]
        self.assertEqual(failed['phase'], 'hotfix_collect')
        self.assertEqual(failed['event'], 'failed')
        self.assertEqual(failed['event_id'], 34)
        self.assertEqual(failed['from_build'], '12.0.7.68887')
        self.assertEqual(failed['error_message'], str(failure))
        self.assertEqual(failed['error_type'], 'WagoDiffUnavailable')
        self.assertEqual(failed['cause_message'], str(cause))
        self.assertTrue(any('test_wago_diagnostics.py:' in frame for frame in failed['frames']))
        self.assertGreaterEqual(failed['seconds'], 0)
        self.assertEqual(failed['http_attempts'], 0)
        self.assertTrue(all(record.levelname in ('INFO', 'WARNING') for record in captured.records))

    def test_nested_stages_inherit_event_and_context_does_not_leak(self):
        result = {'value': 1}
        with self.assertLogs(logger, level='INFO') as captured:
            actual = self.monitor._run_stage('build_generate', lambda: self.monitor._run_stage(
                'build_diff_rows', lambda: result, table='spell'), event=self.event)
            self.monitor._run_stage('next_run', lambda: None)
        self.assertIs(actual, result)
        records = stages(captured)
        child = next(row for row in records if row['phase'] == 'build_diff_rows')
        self.assertEqual(child['event_id'], 34)
        self.assertEqual(child['table'], 'spell')
        self.assertNotIn('event_id', records[-1])
        self.assertIsNone(self.monitor._wago_log_context)

    def test_returned_unavailable_is_failed_without_changing_return_value(self):
        result = {'diff_unavailable': True, 'error': '分页总数发生变化'}
        with self.assertLogs(logger, level='INFO') as captured:
            actual = self.monitor._run_stage('build_generate', lambda: result, event=self.event)
        self.assertIs(actual, result)
        self.assertEqual(stages(captured)[-1]['event'], 'failed')
        self.assertEqual(stages(captured)[-1]['error_message'], result['error'])

    def test_interruption_is_not_logged_as_success_and_restores_context(self):
        def interrupted():
            raise KeyboardInterrupt()
        with self.assertLogs(logger, level='INFO') as captured, self.assertRaises(KeyboardInterrupt):
            self.monitor._run_stage('build_generate', interrupted, event=self.event)
        self.assertEqual(stages(captured)[-1]['event'], 'failed')
        self.assertEqual(stages(captured)[-1]['error_type'], 'KeyboardInterrupt')
        self.assertIsNone(self.monitor._wago_log_context)

    def test_http_retry_metrics_cover_failures_and_success(self):
        responses = [requests.Timeout('等待超时'), Mock(status_code=503),
                     Mock(status_code=200, content=b'ok', text='ok')]
        with patch.object(self.monitor._http_session, 'get', side_effect=responses) as get, \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.time.sleep') as sleep, \
             self.assertLogs(logger, level='INFO') as captured:
            actual = self.monitor._run_stage('hotfix_collect', lambda: self.monitor._http_get_text(
                'https://wago.tools/hotfixes'), event=self.event)
        self.assertEqual(actual, 'ok')
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        summary = stages(captured)[-1]
        self.assertEqual(summary['http_attempts'], 3)
        self.assertEqual(summary['http_failed_attempts'], 2)
        self.assertGreaterEqual(summary['http_seconds'], 0)

    def test_event_error_is_logged_without_new_database_alert_or_changed_error(self):
        error = RuntimeError('mapping failed token=private-value\nnext line')
        event = SimpleNamespace(pk=164, status='detected', error_message='', save=Mock())
        with self.assertLogs(logger, level='INFO') as captured:
            self.monitor._mark_event(event, status='generate_failed', error_message=error)
        self.assertEqual(event.error_message, str(error))
        event.save.assert_called_once()
        message = next(row.getMessage() for row in captured.records
                       if row.getMessage().startswith('[WagoSkillDiff Error] '))
        self.assertIn('mapping failed', message)
        self.assertNotIn('private-value', message)
        self.assertNotIn('\n', message)
        self.assertTrue(all(row.levelname != 'ERROR' for row in captured.records))

    def test_error_message_redacts_credentials_and_is_bounded(self):
        text = ('url=https://name:private-pass@example.org/?token=private-token '
                'Authorization: Bearer private-bearer "api_key": "private-key" '
                "{'password': 'private-password'} prefix=326272")
        cleaned = safe_message(text)
        self.assertNotIn('private-', cleaned)
        self.assertIn('prefix=326272', cleaned)
        self.assertEqual(len(safe_message('x' * 3000)), 2000)


class WagoDiagnosticFlowTests(TestCase):
    def setUp(self):
        self.monitor = WagoSkillDiffMonitor(None, None)
        self.state = WowWagoMonitorState.objects.create(branch='wow', locale='enUS',
            build='12.1.5.70575', hotfix_region_id=3, hotfix_push_id=99)

    def test_retry_wait_exposes_existing_error_without_rescheduling_or_writing(self):
        event = WowWagoBuildEvent.objects.create(branch='wow', locale='enUS',
            from_build=self.state.build, to_build='12.1.5.70576', status='generate_failed',
            error_message='既有失败原因：源资料不完整')
        before = event.updated_at
        with self.assertNumQueries(0), self.assertLogs(logger, level='INFO') as captured:
            self.assertTrue(self.monitor._event_retry_deferred(event, now=before + timedelta(minutes=5)))
        message = next(row.getMessage() for row in captured.records
                       if row.getMessage().startswith('[WagoSkillDiff Error] '))
        details = json.loads(message.removeprefix('[WagoSkillDiff Error] '))
        self.assertEqual(details['phase'], 'retry_wait')
        self.assertEqual(details['error_message'], event.error_message)
        event.refresh_from_db()
        self.assertEqual(event.updated_at, before)

    def test_hotfix_collection_failure_is_not_misreported_as_render_failure(self):
        reason = 'Hotfix prefix search limit push=101'
        with patch.object(self.monitor, '_fetch_latest_hotfix_push_id', return_value=101), \
             patch.object(self.monitor, '_collect_hotfix_interval_rows', side_effect=WagoDiffUnavailable(reason)), \
             patch.object(self.monitor, '_generate_hotfix_class_report') as render, \
             patch.object(self.monitor, '_build_hotfix_fallback_report', return_value={'entry_count': 0}), \
             self.assertLogs(logger, level='INFO') as captured:
            result = self.monitor._scan_hotfix_locked(self.state, 'wow', self.state.build)
        self.assertFalse(result)
        render.assert_not_called()
        event = WowWagoHotfixEvent.objects.get(to_push=101)
        self.assertEqual(event.error_message, reason)
        self.assertEqual(event.status, 'generate_failed_fallback_report')
        self.state.refresh_from_db()
        self.assertEqual(self.state.hotfix_push_id, 99)
        failed = [row for row in stages(captured) if row['event'] == 'failed']
        self.assertEqual([row['phase'] for row in failed], ['hotfix_collect'])
        self.assertEqual(failed[0]['event_id'], event.pk)
        self.assertEqual(failed[0]['region_id'], 3)

    def test_build_unavailable_preserves_status_and_cursor_with_error_detail(self):
        event = WowWagoBuildEvent.objects.create(branch='wow', locale='enUS',
            from_build=self.state.build, to_build='12.1.5.70576')
        reason = 'Wago DB2 diff pagination loop for spell'
        with patch.object(self.monitor, '_fetch_changed_db2_tables', side_effect=WagoDiffUnavailable(reason)), \
             self.assertLogs(logger, level='INFO') as captured:
            result = self.monitor._process_build_event(self.state, event)
        self.assertFalse(result)
        event.refresh_from_db()
        self.state.refresh_from_db()
        self.assertEqual(event.status, 'diff_unavailable')
        self.assertEqual(event.error_message, reason)
        self.assertEqual(self.state.build, '12.1.5.70575')
        failed = [row for row in stages(captured) if row['event'] == 'failed']
        self.assertEqual({row['phase'] for row in failed}, {'build_generate', 'build_table_discovery'})
        self.assertTrue(all(row['event_id'] == event.pk for row in failed))

    def test_hotfix_resolve_render_save_and_publish_failures_are_distinguished(self):
        from botend.models import WowHotfixReport
        self.monitor.task = SimpleNamespace(_monitor_task_is_manual=True)
        source = {'id': 1, 'push_id': 101, 'region_id': 3, 'locale': 'enUS'}
        for failed_phase, method in (
            ('hotfix_resolve', '_resolve_hotfix_facts'),
            ('hotfix_class_report', '_generate_hotfix_class_report'),
            ('hotfix_full_report', '_generate_hotfix_full_report'),
            ('hotfix_save_report', 'update_or_create'),
            ('hotfix_publish', '_publish_staged_hotfix_reports'),
        ):
            with self.subTest(phase=failed_phase), ExitStack() as stack:
                values = {
                    '_fetch_latest_hotfix_push_id': 101,
                    '_collect_hotfix_interval_rows': [source],
                    '_resolve_hotfix_facts': [{'source': source}],
                    '_generate_hotfix_class_report': {'content_html_path': 'class.html',
                        'spell_count': 1, 'class_count': 1, 'unresolved_count': 0},
                    '_generate_hotfix_full_report': {'content_html_path': 'full.html', 'entry_count': 1},
                    '_publish_staged_hotfix_reports': [],
                    '_build_hotfix_fallback_report': {'entry_count': 0},
                }
                reason = f'{failed_phase}测试失败'
                for name, value in values.items():
                    stack.enter_context(patch.object(self.monitor, name, **(
                        {'side_effect': RuntimeError(reason)} if name == method else {'return_value': value})))
                if method == 'update_or_create':
                    stack.enter_context(patch.object(WowHotfixReport.objects, method,
                                                    side_effect=RuntimeError(reason)))
                captured = stack.enter_context(self.assertLogs(logger, level='INFO'))
                self.assertFalse(self.monitor._scan_hotfix_locked(self.state, 'wow', self.state.build))
                event = WowWagoHotfixEvent.objects.get(to_push=101)
                self.assertEqual(event.error_message, reason)
                self.state.refresh_from_db()
                self.assertEqual(self.state.hotfix_push_id, 99)
                failed = [row for row in stages(captured) if row['event'] == 'failed']
                self.assertEqual([row['phase'] for row in failed], [failed_phase])
                self.assertEqual(failed[0]['error_type'], 'RuntimeError')
