"""History HTTP reuse must not weaken exact identity or pagination proof."""
import copy
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from botend.controller.plugins.wow.WagoSkillDiffMonitor import WagoSkillDiffMonitor


class HotfixHistoryReuseTests(SimpleTestCase):
    def setUp(self):
        self.monitor = WagoSkillDiffMonitor(None, SimpleNamespace())
        self.enterContext(patch.object(self.monitor, '_full_hotfix_source_build',
                                       side_effect=lambda source, current: str(source['build'])))
        self.enterContext(patch.object(self.monitor, '_fetch_db2_row_by_id',
                                       return_value={'ID': 7, 'Value': '0'}))
        self.enterContext(patch.object(self.monitor, '_extract_inertia_props',
                                       side_effect=lambda value: value))
        self.rows = [
            {'id': i, 'region_id': region, 'locale': locale, 'table_name': table,
             'record_id': 7, 'build': build, 'push_id': 100, 'status': 1,
             'data': [7, str(i)]}
            for i, (region, locale, table, build) in enumerate([
                (3, 'enUS', 'Spell', 70575), (3, 'enUS', 'Spell', 70529),
                (3, 'enUS', 'SpellName', 70575), (3, 'enUS', 'SpellName', 70529),
                (1, 'enUS', 'Spell', 70575), (3, 'zhCN', 'Spell', 70575),
            ], 1)
        ]
        self.targets = [{**row, 'id': row['id'] + 100, 'push_id': 101,
                         'data': [7, 'new']} for row in self.rows]
        self.calls = []
        self.mode = 'complete'
        self.enterContext(patch.object(self.monitor, '_http_get_text', side_effect=self.fetch))

    def fetch(self, url, **kwargs):
        self.calls.append(url)
        params = parse_qs(urlsplit(url).query)
        page = int(params['page'][0])
        # Two pages; a failed sweep repeats its first slice, never a full set.
        attempt = (len(self.calls) - 1) // 2
        duplicate = self.mode == 'incomplete' or (self.mode == 'recover' and attempt == 0)
        start = 0 if duplicate else (page - 1) * 3
        return {'filters': {'search': params['search'][0]}, 'hotfixes': {
            'data': copy.deepcopy(self.rows[start:start + 3]), 'total': 6,
            'current_page': page, 'last_page': 2, 'per_page': 3}}

    def resolve(self, targets=None):
        return self.monitor._resolve_hotfix_facts(
            self.targets if targets is None else targets, db2_build='12.1.5.70575')

    def test_complete_search_reused_across_exact_scopes(self):
        facts = self.resolve()
        self.assertEqual([f['before_source'] for f in facts], self.rows)
        self.assertEqual([f['source'] for f in facts], self.targets)
        self.assertEqual(len(self.calls), 2)

    def test_incomplete_search_budget_shared_but_next_resolution_can_retry(self):
        self.mode = 'incomplete'
        facts = self.resolve()
        self.assertTrue(all(not f['before_verified'] for f in facts))
        self.assertEqual(len(self.calls), 6)
        self.mode = 'complete'
        self.calls.clear()
        facts = self.resolve()
        self.assertEqual([f['before_source'] for f in facts], self.rows)
        self.assertEqual(len(self.calls), 2)

    def test_fresh_sweep_not_served_cached_incomplete_pages(self):
        self.mode = 'recover'
        facts = self.resolve()
        self.assertEqual([f['before_source'] for f in facts], self.rows)
        self.assertEqual(len(self.calls), 4)

    def test_scope_conflict_does_not_poison_valid_scope(self):
        self.rows[1] = {**self.rows[0], 'id': 2, 'data': [7, 'conflict']}
        facts = self.resolve([self.targets[0], self.targets[2]])
        self.assertFalse(facts[0]['before_verified'])
        self.assertEqual(facts[1]['before_source'], self.rows[2])
        self.assertEqual(len(self.calls), 2)
