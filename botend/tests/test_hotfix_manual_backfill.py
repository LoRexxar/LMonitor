import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings
from botend.services.wago_hotfix_source import source_ids_sha256


class HotfixManualBackfillTests(SimpleTestCase):
    def test_exact_interval_uses_existing_monitor_and_does_not_save_live_cursor(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'static' / 'portal' / 'reports' / 'manual.html'
            path.parent.mkdir(parents=True)
            path.write_text('verified', encoding='utf-8')
            live = SimpleNamespace(branch='wow', locale='enUS', build='12.1.0.69933',
                                   hotfix_region_id=3, hotfix_push_id=112236,
                                   refresh_from_db=lambda: None)
            sources = [{'id': 91}, {'id': 92}]
            digest = source_ids_sha256(sources)
            report = SimpleNamespace(pk=17, from_push=111863, to_push=112236, collection_complete=True,
                                     entry_count=2, source_facts_json=json.dumps([{'source': row} for row in sources]),
                                     content_html_path='portal/reports/manual.html',
                                     class_spell_count=0, class_content_html_path='')
            with override_settings(BASE_DIR=root), \
                 patch('botend.management.commands.backfill_wow_hotfix_interval.WowWagoMonitorState.objects.get', return_value=live), \
                 patch('botend.management.commands.backfill_wow_hotfix_interval.WowHotfixReport.objects.filter') as reports, \
                 patch('botend.management.commands.backfill_wow_hotfix_interval.WagoSkillDiffMonitor') as monitor_class:
                reports.return_value.first.side_effect = [None, report]
                monitor_class.return_value._scan_hotfix_if_needed.return_value = True
                call_command('backfill_wow_hotfix_interval', from_push=111863, to_push=112236, region_id=3,
                             expected_count=2, expected_id_sha256=digest)
                replay = monitor_class.return_value._scan_hotfix_if_needed.call_args
                self.assertEqual(replay.args[1:], ('wow', '12.1.0.69933'))
                self.assertEqual(replay.kwargs['backfill_interval'], (111863, 112236))
                self.assertEqual(replay.kwargs['expected_source_count'], 2)
                self.assertEqual(replay.kwargs['expected_source_sha256'], digest)
                self.assertEqual(replay.args[0].hotfix_push_id, 111863)
                self.assertTrue(monitor_class.return_value._hotfix_report_only)
                self.assertEqual(live.hotfix_push_id, 112236)
