"""Re-render an existing complete Hotfix report only from its frozen facts."""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase, override_settings

from botend.services.wago_hotfix_source import source_ids_sha256


class HotfixReportRerenderTests(SimpleTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.reports = self.root / 'static' / 'portal' / 'reports'
        self.reports.mkdir(parents=True)
        self.private = self.root / 'tmp' / 'wago-hotfix-staging'
        self.private.mkdir(parents=True)
        self.full = self.reports / 'full.html'
        self.cls = self.reports / 'class.html'
        self.full.write_text('old full', encoding='utf-8')
        self.cls.write_text('old class', encoding='utf-8')
        self.source = {'id': 13, 'push_id': 112185, 'region_id': 3, 'locale': 'enUS',
                       'table_name': 'SpellEffect', 'record_id': 99, 'build': 69933, 'status': 1,
                       'data': [99]}
        self.digest = source_ids_sha256([self.source])
        self.row = SimpleNamespace(
            pk=126, branch='wow', locale='enUS', region_id=3, from_push=111863, to_push=112236,
            build_str='12.1.0.69933', entry_count=1, table_count=1,
            build_num='69933', summary_title='Verified Hotfix',
            wago_url='https://wago.tools/hotfixes?search=enUS+112236',
            class_spell_count=1, class_class_count=1, class_unresolved_count=0,
            collection_complete=True,
            source_facts_json=json.dumps([{'source': self.source, 'after_verified': True, 'after': {'ID': '99'},
                                           'before_verified': False, 'changes': []}]),
            content_html_path='portal/reports/full.html', class_content_html_path='portal/reports/class.html',
            save=Mock(side_effect=AssertionError('must not update report facts')),
        )
        self.staged_full = self.private / 'new-full.html'
        self.staged_class = self.private / 'new-class.html'
        self.staged_full.write_text("<article class='record'></article>可核实旧→新 0 个字段", encoding='utf-8')
        self.staged_class.write_text("<article class='spell'></article>", encoding='utf-8')
        self.monitor = Mock()
        self.monitor._generate_hotfix_class_report.return_value = {
            'content_html_path': self.row.class_content_html_path, 'staging_path': str(self.staged_class),
            'spell_count': 1, 'class_count': 1, 'unresolved_count': 0,
        }
        self.monitor._write_hotfix_full_html.return_value = (str(self.staged_full), self.row.content_html_path)

    def invoke(self, digest=None, facts_digest=None, **options):
        return call_command('rerender_wow_hotfix_report', report_id=126,
                            expected_count=1, expected_id_sha256=digest or self.digest,
                            expected_facts_sha256=facts_digest or hashlib.sha256(
                                self.row.source_facts_json.encode('utf-8')).hexdigest(), **options)

    def test_full_only_replaces_normal_report_without_touching_class_report(self):
        old_class_sha256 = hashlib.sha256(self.cls.read_bytes()).hexdigest()
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            self.invoke(full_only=True)
        self.assertIn("class='record'", self.full.read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(self.cls.read_bytes()).hexdigest(), old_class_sha256)
        self.monitor._generate_hotfix_class_report.assert_not_called()
        self.row.save.assert_not_called()
        self.assertFalse(self.staged_full.exists())
        self.assertTrue(self.staged_class.exists())

    def test_full_only_failed_publication_preserves_both_originals(self):
        before_class = hashlib.sha256(self.cls.read_bytes()).hexdigest()
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor), \
             patch('botend.management.commands.rerender_wow_hotfix_report.os.replace',
                   side_effect=OSError('publish blocked')):
            with self.assertRaises(CommandError):
                self.invoke(full_only=True)
        self.assertEqual(self.full.read_text(encoding='utf-8'), 'old full')
        self.assertEqual(hashlib.sha256(self.cls.read_bytes()).hexdigest(), before_class)
        self.row.save.assert_not_called()

    def test_full_only_refuses_to_erase_verified_client_comparisons(self):
        marker = "data-baseline-key='spelleffect:12.1.0.69933:enUS:1284426:112236'"
        verified_name = "<article class='reader-db2-name-card'>Reuse</article>"
        old = "<article class='reader-impact-card' " + marker + "></article>" + verified_name
        self.full.write_text(old, encoding='utf-8')
        old_class_sha256 = hashlib.sha256(self.cls.read_bytes()).hexdigest()
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            with self.assertRaisesRegex(CommandError, 'verified client DB2'):
                self.invoke(full_only=True)
        self.assertEqual(self.full.read_text(encoding='utf-8'), old)
        self.assertEqual(hashlib.sha256(self.cls.read_bytes()).hexdigest(), old_class_sha256)
        self.row.save.assert_not_called()
        self.staged_full.write_text(
            "<article class='record'></article>" + old, encoding='utf-8')
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            self.invoke(full_only=True)
        self.assertIn(marker, self.full.read_text(encoding='utf-8'))
        self.assertIn(verified_name, self.full.read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(self.cls.read_bytes()).hexdigest(), old_class_sha256)

    def test_full_only_refuses_to_erase_verified_range_reference(self):
        marker = "data-range-context='spellmisc:12.1.0.69814:enUS:865878:112186:6:13'"
        old = "<article class='reader-confirmed-card' " + marker + "></article>"
        self.full.write_text(old, encoding='utf-8')
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            with self.assertRaisesRegex(CommandError, 'range references'):
                self.invoke(full_only=True)
        self.assertEqual(self.full.read_text(encoding='utf-8'), old)
        self.staged_full.write_text("<article class='record'></article>" + old, encoding='utf-8')
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            self.invoke(full_only=True)
        self.assertIn(marker, self.full.read_text(encoding='utf-8'))

    def test_full_only_rejects_shared_class_and_full_file(self):
        self.row.class_content_html_path = self.row.content_html_path
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            with self.assertRaisesRegex(CommandError, 'share its path'):
                self.invoke(full_only=True)
        self.monitor._write_hotfix_full_html.assert_not_called()
        self.assertEqual(self.full.read_text(encoding='utf-8'), 'old full')

    def test_only_verified_saved_facts_replace_both_files_not_database(self):
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            self.invoke()
        self.assertIn('可核实旧→新 0 个字段', self.full.read_text(encoding='utf-8'))
        self.assertIn("class='spell'", self.cls.read_text(encoding='utf-8'))
        self.row.save.assert_not_called()
        self.monitor._generate_hotfix_full_report.assert_not_called()
        kwargs = self.monitor._write_hotfix_full_html.call_args.kwargs
        self.assertTrue(kwargs['stage_for_publication'])
        self.assertEqual(kwargs['facts'][0]['source'], self.source)
        self.assertEqual(kwargs['table_stats'], [('SpellEffect', 1)])
        self.assertEqual(kwargs['db2_build'], self.row.build_str)
        self.assertFalse(self.staged_full.exists())
        self.assertFalse(self.staged_class.exists())

    def test_wrong_source_fingerprint_never_replaces_or_renders(self):
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            with self.assertRaises(CommandError):
                self.invoke('0' * 64)
        self.monitor._write_hotfix_full_html.assert_not_called()
        self.assertEqual(self.full.read_text(encoding='utf-8'), 'old full')

    def test_wrong_frozen_fact_payload_fingerprint_never_renders(self):
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor):
            with self.assertRaises(CommandError):
                self.invoke(facts_digest='0' * 64)
        self.monitor._write_hotfix_full_html.assert_not_called()
        self.assertEqual(self.full.read_text(encoding='utf-8'), 'old full')

    def test_failed_second_publication_restores_first_and_leaves_facts_untouched(self):
        real_replace = os.replace
        def interrupted(source, destination):
            if str(source) == str(self.staged_class):
                raise OSError('second file failed')
            return real_replace(source, destination)
        with override_settings(BASE_DIR=str(self.root)), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WowHotfixReport.objects.get',
                   return_value=self.row), \
             patch('botend.management.commands.rerender_wow_hotfix_report.WagoSkillDiffMonitor',
                   return_value=self.monitor), \
             patch('botend.management.commands.rerender_wow_hotfix_report.os.replace', side_effect=interrupted):
            with self.assertRaises(CommandError):
                self.invoke()
        self.assertEqual(self.full.read_text(encoding='utf-8'), 'old full')
        self.assertEqual(self.cls.read_text(encoding='utf-8'), 'old class')
        self.row.save.assert_not_called()
