"""Hotfix failure reporting contracts; no live collection or publication."""

import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import TestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import (
    WagoDiffUnavailable,
    WagoSkillDiffMonitor,
)
from botend.models import WowHotfixReport, WowWagoHotfixEvent, WowWagoMonitorState


class HotfixFallbackStatusTests(TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.enterContext(override_settings(BASE_DIR=self.root))
        self.http = self.enterContext(patch(
            'requests.sessions.Session.request',
            side_effect=AssertionError('regression tests must not collect live data'),
        ))
        self.monitor = WagoSkillDiffMonitor(None, None)
        self.state = WowWagoMonitorState.objects.create(
            branch='wow', locale='enUS', build='12.1.0.69933',
            hotfix_region_id=3, hotfix_push_id=112181,
            hotfix_last_run_status='success', hotfix_summary_title='last good summary',
        )
        self.enterContext(patch.object(
            self.monitor, '_fetch_latest_hotfix_push_id', return_value=112185,
        ))

    def tearDown(self):
        self.http.assert_not_called()

    def test_collection_failure_preserves_original_reason_and_reports_failure(self):
        reason = 'Hotfix partition incomplete: parent=17 children=16'
        def collection_failure(*args, **kwargs):
            self.assertEqual(WowWagoMonitorState.objects.get(pk=self.state.pk).hotfix_last_run_status, 'running')
            raise WagoDiffUnavailable(reason)
        with patch.object(self.monitor, '_collect_hotfix_interval_rows',
                          side_effect=collection_failure), \
             patch.object(self.monitor, '_build_hotfix_fallback_report',
                          wraps=self.monitor._build_hotfix_fallback_report) as fallback:
            result = self.monitor._scan_hotfix_if_needed(self.state, 'wow', self.state.build)

        report = WowHotfixReport.objects.get(region_id=3, to_push=112185)
        event = WowWagoHotfixEvent.objects.get(region_id=3, to_push=112185)
        self.state.refresh_from_db()
        # Separate subtests expose both the lost diagnostic and false success.
        with self.subTest(contract='original reason in persisted report and HTML'):
            self.assertIn(reason, report.content_md)
            self.assertIn(reason, (self.root / 'static' / report.content_html_path).read_text())
            fallback.assert_called_once()
        with self.subTest(contract='failed scan is not reported as success'):
            self.assertFalse(result)
        self.assertEqual(event.error_message, reason)
        self.assertEqual(event.status, 'generate_failed_fallback_report')
        self.assertEqual(event.report_id, report.pk)
        self.assertEqual(self.state.hotfix_last_run_status, 'failed')
        self.assertEqual(self.state.hotfix_push_id, 112181)
        self.assertFalse(report.collection_complete)
        self.assertEqual(report.entry_count, 0)

    def test_fallback_save_failure_persists_failed_run_status(self):
        with patch.object(self.monitor, '_collect_hotfix_interval_rows',
                          side_effect=WagoDiffUnavailable('source incomplete')), \
             patch.object(WowHotfixReport.objects, 'update_or_create',
                          side_effect=RuntimeError('report persistence failed')):
            result = self.monitor._scan_hotfix_if_needed(self.state, 'wow', self.state.build)
        self.state.refresh_from_db()
        event = WowWagoHotfixEvent.objects.get(region_id=3, to_push=112185)
        self.assertFalse(result)
        self.assertEqual(self.state.hotfix_last_run_status, 'failed')
        self.assertEqual(self.state.hotfix_push_id, 112181)
        self.assertEqual(event.status, 'save_report_failed')
        self.assertEqual(event.error_message, 'report persistence failed')
        self.assertFalse(WowHotfixReport.objects.exists())

    def test_failed_manual_replay_is_failed_without_fallback_or_live_state_write(self):
        replay = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=112181,
                                 save=lambda **kwargs: None)
        with patch.object(self.monitor, '_collect_hotfix_interval_rows',
                          side_effect=WagoDiffUnavailable('manual source incomplete')), \
             patch.object(self.monitor, '_build_hotfix_fallback_report') as fallback:
            result = self.monitor._scan_hotfix_if_needed(
                replay, 'wow', self.state.build, backfill_interval=(112181, 112185),
            )
        self.state.refresh_from_db()
        self.assertFalse(result)
        fallback.assert_not_called()
        self.assertFalse(WowHotfixReport.objects.exists())
        self.assertEqual(self.state.hotfix_last_run_status, 'success')
        self.assertEqual(self.state.hotfix_summary_title, 'last good summary')
        self.assertEqual(self.state.hotfix_push_id, 112181)
        self.assertEqual(replay.hotfix_push_id, 112181)
        self.assertEqual(replay.hotfix_last_run_status, 'failed')

    def test_completed_manual_replay_is_successful_without_advancing_live_state(self):
        replay = SimpleNamespace(hotfix_region_id=3, hotfix_push_id=112181,
                                 save=lambda **kwargs: None)
        source = {'id': 1, 'push_id': 112185, 'region_id': 3, 'locale': 'enUS',
                  'table_name': 'SpellEffect', 'record_id': 1}
        full = {'entry_count': 1, 'content_html_path': 'portal/reports/full.html'}
        cls = {'spell_count': 1, 'class_count': 1, 'unresolved_count': 0,
               'content_html_path': 'portal/reports/class.html'}
        with patch.object(self.monitor, '_collect_hotfix_interval_rows', return_value=[source]), \
             patch.object(self.monitor, '_resolve_hotfix_facts', return_value=[{'source': source}]), \
             patch.object(self.monitor, '_generate_hotfix_full_report', return_value=full), \
             patch.object(self.monitor, '_generate_hotfix_class_report', return_value=cls), \
             patch.object(self.monitor, '_publish_staged_hotfix_reports', return_value=[]):
            result = self.monitor._scan_hotfix_if_needed(
                replay, 'wow', self.state.build, backfill_interval=(112181, 112185),
            )
        self.state.refresh_from_db()
        self.assertTrue(result)
        self.assertTrue(WowHotfixReport.objects.get(region_id=3, to_push=112185).collection_complete)
        self.assertEqual(self.state.hotfix_last_run_status, 'success')
        self.assertEqual(self.state.hotfix_summary_title, 'last good summary')
        self.assertEqual(self.state.hotfix_push_id, 112181)
        self.assertEqual(replay.hotfix_push_id, 112181)
        self.assertEqual(replay.hotfix_last_run_status, 'success')
