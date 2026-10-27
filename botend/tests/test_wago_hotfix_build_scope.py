"""Frozen public Wago response: one push/record legitimately has two builds."""
import copy
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, override_settings

from botend.services.wago_hotfix_source import (
    collect_hotfix_push_rows, collect_hotfix_record_history,
    collect_hotfix_build_rows, HotfixSourceIncomplete,
)
from botend.services.wago_hotfix_facts import previous_hotfix_row


class HotfixBuildScopeTests(SimpleTestCase):
    def setUp(self):
        self.props = json.loads((Path(__file__).parent / 'fixtures' / 'wago_hotfix_113268.json').read_text())

    def fetch(self, url, **kwargs):
        props = copy.deepcopy(self.props)
        props['filters']['search'] = parse_qs(urlsplit(url).query)['search'][0]
        return props

    def test_real_four_row_push_preserves_both_build_identities(self):
        rows = collect_hotfix_push_rows(self.fetch, lambda p: p, 113268, region_id=3, locale='enUS')
        self.assertEqual([(r['id'], r['build']) for r in rows],
                         [(28177750651, 70575), (28177776356, 70529)])

    def test_explicit_build_scope_filters_only_after_complete_source_collection(self):
        for build, sid in ((70575, 28177750651), (70529, 28177776356)):
            with self.subTest(build=build):
                rows = collect_hotfix_push_rows(self.fetch, lambda p: p, 113268,
                                                region_id=3, locale='enUS', build_number=build)
                self.assertEqual([r['id'] for r in rows], [sid])
                history = collect_hotfix_record_history(self.fetch, lambda p: p, 1207523,
                            table_name='SpellEffect', region_id=3, locale='enUS', build_number=build)
                self.assertEqual(history, rows)
                discovered = collect_hotfix_build_rows(self.fetch, lambda p: p, build,
                                                       region_id=3, locale='enUS')
                self.assertEqual(discovered, rows)

    def test_same_build_conflict_is_still_rejected(self):
        self.props['hotfixes']['data'][1]['build'] = 70575
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'duplicate record'):
            collect_hotfix_push_rows(self.fetch, lambda p: p, 113268, region_id=3, locale='enUS')

    def test_missing_build_cannot_be_silently_excluded_from_explicit_scope(self):
        del self.props['hotfixes']['data'][1]['build']
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'build identity'):
            collect_hotfix_push_rows(self.fetch, lambda p: p, 113268,
                                    region_id=3, locale='enUS', build_number=70575)

    def test_history_uses_same_build_not_newer_other_build_or_max_source_id(self):
        original = self.props['hotfixes']['data'][2]
        own = {**original, 'push_id': 113266, 'data': [1207523, 'own-build']}
        other = {**original, 'id': 99999999999, 'build': 70529, 'push_id': 113267,
                 'data': [1207523, 'other-build']}
        self.assertEqual(previous_hotfix_row([own, other], original), own)
        self.assertIsNone(previous_hotfix_row([other], original))
        # A conflicting source identity inside the same build still has no predecessor.
        duplicate = {**own, 'id': own['id'] + 1, 'data': [1207523, 'conflict']}
        self.assertIsNone(previous_hotfix_row([own, duplicate, other], original))

    def test_monitor_interval_and_history_caches_keep_build_scope(self):
        from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
        monitor = WagoSkillDiffMonitor(None, None)
        with patch.object(monitor, '_http_get_text', side_effect=self.fetch), \
             patch.object(monitor, '_extract_inertia_props', side_effect=lambda p: p):
            rows = monitor._collect_hotfix_interval_rows(113267, 113268, region_id=3,
                                                        locale='enUS', build_number=70575)
        self.assertEqual([r['id'] for r in rows], [28177750651])
        both = [r for r in self.props['hotfixes']['data'] if r['region_id'] == 3]
        history = [{**r, 'push_id': 113267, 'data': [1207523, str(r['build'])]} for r in both]
        targets = [{**r, 'data': [1207523, 'new']} for r in both]
        def history_for(*args, **kwargs):
            return [r for r in history if r['build'] == kwargs['build_number']]
        with patch.object(monitor, '_full_hotfix_source_build', side_effect=lambda r, b: str(r['build'])), \
             patch.object(monitor, '_fetch_db2_row_by_id', return_value={'ID': '1207523', 'Value': 'new'}), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.collect_hotfix_record_history', side_effect=history_for) as fetch:
            facts = monitor._resolve_hotfix_facts(targets, db2_build='12.1.5.70575')
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual([f['before']['Value'] for f in facts], ['70529', '70575'])

    def test_full_report_preserves_two_same_push_record_facts_without_overwriting(self):
        from bs4 import BeautifulSoup
        from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor
        monitor = WagoSkillDiffMonitor(None, None)
        rows = [r for r in self.props['hotfixes']['data'] if r['region_id'] == 3]
        # Distinct diagnostic values expose a lookup accidentally borrowing the
        # other build's fact. Source identities themselves are the real fixture.
        facts = [{'source': r, 'after': {'ID': str(r['record_id']), 'SpellID': '1221162',
                                        'EffectIndex': '0', 'BonusCoefficientFromAP': str(r['build']) + '001'},
                  'before_verified': False, 'after_verified': True, 'changes': []} for r in rows]
        with tempfile.TemporaryDirectory() as root, override_settings(BASE_DIR=root), \
             patch('botend.controller.plugins.wow.WagoSkillDiffMonitor.database_spell_metadata', return_value={}), \
             patch.object(monitor, '_http_get_text', side_effect=AssertionError('unexpected network')):
            path, _ = monitor._write_hotfix_full_html(
                branch='wow', locale='enUS', region_id=3, to_push=113268, from_push=113267,
                summary_title='Build identity regression', wago_url='https://wago.tools/hotfixes',
                build_num='70529 / 70575', db2_build='12.1.5.70575',
                table_stats=[('SpellEffect', 2)], by_table={'SpellEffect': rows},
                sample_per_table=2, enrich_max=0, facts=facts)
            body = Path(path).read_text()
        cards = BeautifulSoup(body, 'html.parser').select('#table-spelleffect article.record')
        self.assertEqual(len(cards), 2)
        for card, source in zip(cards, rows):
            self.assertIn(f"来源 build {source['build']}", card.get_text())
            self.assertIn(str(source['build']) + '001', card.get_text())
            other = 70575 if source['build'] == 70529 else 70529
            self.assertNotIn(str(other) + '001', card.get_text())
