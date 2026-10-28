"""Synthetic HTTP contracts for the live-verified record/build/table search."""
from unittest import TestCase
from urllib.parse import parse_qs, urlsplit

from botend.services.wago_hotfix_source import (
    HotfixSourceIncomplete, collect_hotfix_record_history,
)


class HotfixHistoryFallbackTests(TestCase):
    def setUp(self):
        self.calls = []
        self.cache = {}
        self.rows = [dict(id=i, record_id=104655, build=70575,
                          table_name='SpellCategories', region_id=region,
                          locale=locale, push_id=100 + i)
                     for i, region, locale in [(1, 3, 'enUS'), (2, 1, 'enUS'),
                                               (3, 3, 'zhCN')]]
        self.mode = 'complete'

    def fetch(self, url, **kwargs):
        params = parse_qs(urlsplit(url).query)
        query, page = params['search'][0], int(params['page'][0])
        self.calls.append((query, page))
        if query == '104655':
            if self.mode == 'network':
                raise OSError('transport unavailable')
            # Production failure shape: the broad record search exceeds budget.
            return {'hotfixes': dict(total=1025, current_page=1, last_page=41,
                                    per_page=25, data=[])}
        rows = self.rows
        if self.mode == 'empty':
            rows = []
        if self.mode == 'repeated':
            rows = [self.rows[0]]
            total, pages, per_page = 2, 2, 1
        else:
            total, pages, per_page = len(rows), 1, 25
        return {'filters': {'search': 'ignored' if self.mode == 'echo' else query},
                'hotfixes': dict(total=total, current_page=page, last_page=pages,
                                per_page=per_page, data=rows)}

    def collect(self, **scope):
        options = dict(table_name='SpellCategories', region_id=3, locale='enUS',
                       build_number=70575, search_cache=self.cache)
        options.update(scope)
        return collect_hotfix_record_history(self.fetch, lambda p: p, 104655, **options)

    def test_incomplete_broad_query_falls_back_and_reuses_exact_search_across_locales(self):
        self.assertEqual(self.collect(), [self.rows[0]])
        self.assertEqual(self.collect(region_id=1), [self.rows[1]])
        self.assertEqual(self.collect(locale='zhCN'), [self.rows[2]])
        self.assertEqual(self.calls, [('104655', 1), ('104655 70575 spellcategories', 1)])

    def test_no_build_or_transport_failure_never_uses_narrow_fallback(self):
        with self.assertRaises(HotfixSourceIncomplete):
            self.collect(build_number=None)
        self.assertEqual(self.calls, [('104655', 1)])
        self.cache.clear()
        self.calls.clear()
        self.mode = 'network'
        for _ in range(2):
            with self.assertRaises(OSError):
                self.collect()
        self.assertEqual(self.calls, [('104655', 1)])

    def test_successful_empty_is_cached_but_failure_is_not_empty(self):
        self.mode = 'empty'
        self.assertEqual(self.collect(), [])
        self.assertEqual(self.collect(), [])
        self.assertEqual(len(self.calls), 2)
        self.cache.clear()
        self.mode = 'echo'
        for _ in range(2):
            with self.assertRaises(HotfixSourceIncomplete):
                self.collect()
        self.assertEqual(len(self.calls), 4)
        # A fresh resolver-local cache allows a later recovery.
        self.cache.clear()
        self.mode = 'complete'
        self.assertEqual(self.collect(), [self.rows[0]])

    def test_fallback_rejects_wrong_record_build_table_and_missing_identity(self):
        for changed in ({'record_id': 104656}, {'build': 70529},
                        {'table_name': 'SpellCategoriesExtra'}, {'build': None}):
            with self.subTest(changed=changed):
                self.cache.clear()
                original = self.rows[0]
                self.rows[0] = {**original, **changed}
                with self.assertRaisesRegex(HotfixSourceIncomplete, 'identity'):
                    self.collect()
                self.rows[0] = original

    def test_narrow_incomplete_pages_are_not_combined_or_cached_as_empty(self):
        self.mode = 'repeated'
        for _ in range(2):
            with self.assertRaises(HotfixSourceIncomplete):
                self.collect()
        self.assertEqual(self.calls, [('104655', 1)] +
                         [('104655 70575 spellcategories', p) for p in [1, 2] * 3])

    def test_narrow_search_completes_all_pages_and_keeps_build_cache_isolated(self):
        broad_fetch = self.fetch
        def fetch(url, **kwargs):
            params = parse_qs(urlsplit(url).query)
            query, page = params['search'][0], int(params['page'][0])
            if query == '104655':
                return broad_fetch(url, **kwargs)
            self.calls.append((query, page))
            build = int(query.split()[1])
            row = {**self.rows[0], 'id': build + page, 'build': build,
                   'push_id': 100 + page}
            return {'filters': {'search': query}, 'hotfixes': dict(
                total=2, current_page=page, last_page=2, per_page=1, data=[row])}
        self.fetch = fetch
        for build in (70575, 70529):
            result = self.collect(build_number=build)
            self.assertEqual([r['id'] for r in result], [build + 1, build + 2])
            self.assertEqual({r['build'] for r in result}, {build})
            self.assertEqual(self.collect(build_number=build), result)
        self.assertEqual(self.calls, [('104655', 1)] + [
            (f'104655 {build} spellcategories', page)
            for build in (70575, 70529) for page in (1, 2)])

    def test_table_prefix_search_collision_is_filtered_after_complete_pagination(self):
        # Live Wago search for "1327777 70575 spell" also returns SpellName.
        # Its search_text proves the substring match; it is not a Spell source.
        query = '104655 70575 spell'
        spell = {**self.rows[0], 'table_name': 'Spell'}
        name = {**self.rows[1], 'table_name': 'SpellName', 'region_id': 3,
                'search_text': f'enus 102 {query}name [104655,"name"]'}
        broad_fetch = self.fetch
        def fetch(url, **kwargs):
            params = parse_qs(urlsplit(url).query)
            if params['search'][0] == '104655':
                return broad_fetch(url, **kwargs)
            page = int(params['page'][0])
            self.calls.append((params['search'][0], page))
            return {'filters': {'search': query}, 'hotfixes': dict(
                total=2, current_page=page, last_page=2, per_page=1,
                data=[name if page == 1 else spell])}
        self.fetch = fetch
        self.assertEqual(self.collect(table_name='Spell'), [spell])
        self.assertEqual(self.calls, [('104655', 1), (query, 1), (query, 2)])
        # A table-prefix collision alone cannot attest an unrelated response.
        self.cache.clear()
        name['search_text'] = 'unrelated search'
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'identity'):
            self.collect(table_name='Spell')

    def test_scope_conflict_is_not_a_reason_to_fallback(self):
        rows = [self.rows[0], {**self.rows[0], 'id': 9}]
        def fetch(url, **kwargs):
            self.calls.append(url)
            return {'hotfixes': dict(total=2, current_page=1, last_page=1,
                                    per_page=25, data=rows)}
        self.fetch = fetch
        with self.assertRaisesRegex(HotfixSourceIncomplete, 'duplicate record'):
            self.collect()
        self.assertEqual(len(self.calls), 1)
