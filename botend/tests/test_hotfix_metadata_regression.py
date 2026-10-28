"""Report 149: a verified effect need not have a rendered effect marker."""
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, RequestFactory
from django.template.loader import render_to_string

from botend.portal.views import PortalWowHotfixClassMetadataAPIView


class HotfixMetadataRegressionTests(SimpleTestCase):
    def test_verified_unmarked_effect_retains_build_identity_and_fail_closed(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/hotfix149_metadata.json').read_text())
        original = copy.deepcopy(fixture)
        row = SimpleNamespace(id=149, branch='wow', collection_complete=True,
                              class_spell_count=1, class_unresolved_count=0,
                              class_content_html_path='portal/reports/report149.html')
        def metadata(ids, branch, build, **kwargs):
            self.assertIn(build, ('12.1.5.70529', '12.1.5.70575'))
            return {sid: {'name': '', 'icon': '', 'icon_source': ''} for sid in ids}
        with patch('botend.portal.views.WowHotfixReport.objects') as manager, \
             patch('botend.portal.views._resolve_portal_report_html_path') as path, \
             patch('botend.services.wow_skill_report_metadata.database_spell_metadata', side_effect=metadata), \
             patch('botend.services.wow_skill_report_metadata.requests.get', side_effect=AssertionError('remote lookup')):
            manager.filter.return_value.first.return_value = row
            path.return_value.read_text.return_value = fixture['html']
            cases = [('verified', fixture['facts'], 200),
                     ('unverified', [{**f, 'after_verified': False} for f in fixture['facts']], 503),
                     ('invalid-build', [{**f, 'source_build': ''} for f in fixture['facts']], 503),
                     ('missing-spell', [], 503)]
            for name, facts, status in cases:
                with self.subTest(name=name):
                    row.source_facts_json = json.dumps(facts)
                    response = PortalWowHotfixClassMetadataAPIView.as_view()(
                        RequestFactory().get('/portal/api/wow-hotfix-class/149/metadata/'), report_id=149)
                    self.assertEqual(response.status_code, status)
                    if status == 200:
                        spell = json.loads(response.content)['spells']['470057']
                        self.assertEqual(spell['source_builds'], ['12.1.5.70529', '12.1.5.70575'])
                        self.assertEqual(spell['name'], '流电炽焰')
                        self.assertEqual(spell['effects'], [])
                        self.assertEqual(spell['direct_indices'], [])
        self.assertEqual(fixture, original)

    def test_real_effect_attribute_change_is_rendered_with_source_identity(self):
        import tempfile
        from bs4 import BeautifulSoup
        from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
        from botend.services.wago_hotfix_facts import project_class_spell_changes
        from botend.services.wow_skill_report_metadata import report_spell_entries
        fixture = json.loads((Path(__file__).parent / 'fixtures/hotfix149_metadata.json').read_text())
        changes = project_class_spell_changes(fixture['facts'], is_class_spell=lambda *args: True)
        monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        with tempfile.TemporaryDirectory() as folder, self.settings(BASE_DIR=folder), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.database_spell_metadata', return_value={}), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.WowSpellSnapshot.objects.filter') as snapshots, \
             patch.object(monitor, '_http_get_text', side_effect=AssertionError('unexpected remote request')), \
             patch.object(monitor, '_load_chr_classes', return_value={7: '萨满祭司'}), \
             patch.object(monitor, '_load_chr_specialization_meta', return_value={-7: {'name': '通用', 'class_id': 7}}):
            snapshots.return_value.values.return_value = []
            result = monitor._write_html_report(
                branch='wow', server_title='Hotfix', from_build='12.1.5.70575', to_build='12.1.5.70575',
                display_from_build='push 112881', display_to_build='push 113268',
                class_names={7: 'Shaman'}, spec_meta={-7: {'name': 'General', 'class_id': 7}},
                spell_to_specs={470057: {-7}}, spec_to_class={-7: 7}, spell_changes=changes,
                data_build='12.1.5.70575', effect_record_ids=True, assess_tone=False,
                source_names_override={470057: '流电炽焰'}, report_key='hotfix_r3_p113268')
            html = (Path(folder) / 'static' / result['path']).read_text()
        doc = BeautifulSoup(html, 'html.parser')
        rows = doc.select('.impact-block [data-effect-index]')
        self.assertEqual(len(rows), 2)
        self.assertEqual({r['data-source-build'] for r in rows}, {'12.1.5.70529', '12.1.5.70575'})
        for row in rows:
            text = row.get_text(' ', strip=True)
            self.assertIn('EffectAttributes', text)
            self.assertIn('2048', text)
            self.assertIn('133120', text)
            self.assertIn('非前次热修值', text)
        self.assertEqual(report_spell_entries(html)[470057]['indices'], {0})

    def test_first_generation_and_latest_rebuild_are_distinct(self):
        row = SimpleNamespace(branch='wow', created_at=datetime(2026, 10, 24, 1, 52, 44, tzinfo=timezone.utc),
                              updated_at=datetime(2026, 10, 27, 10, 49, 20, tzinfo=timezone.utc))
        with self.settings(TIME_ZONE='UTC'):
            html = render_to_string('portal/wow_skill_diff_report.html', {'report': row, 'is_hotfix': True})
        self.assertIn('<b>首次生成</b>2026-10-24 01:52:44', html)
        self.assertIn('<b>最近重建</b>2026-10-27 10:49:20', html)
        self.assertEqual(row.created_at.day, 24)
        self.assertEqual(row.updated_at.day, 27)
