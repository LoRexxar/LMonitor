"""Source discovery stays scoped; partial source data never means no change."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase, override_settings

from botend.controller.plugins.wow.WagoSkillDiffMonitor import (
    WagoDiffUnavailable, WagoSkillDiffMonitor,
)


class WagoDiscoveryBoundsTests(SimpleTestCase):
    def setUp(self):
        self.monitor = WagoSkillDiffMonitor(None, SimpleNamespace())

    def manifest(self, page=1):
        return {
            'fromVersion': 'old', 'toVersion': 'new',
            'filters': {'from': 'old', 'to': 'new', 'filter': {'Type': 'db2'}},
            'items': {'total': 2, 'current_page': page, 'last_page': 2, 'per_page': 1,
                      'data': [{'Type': 'db2', 'Filename': 'dbfilesclient/' + ('spell' if page == 1 else 'spelleffect') + '.db2',
                                'FDID': page, 'Action': 'modified'}],
                      'next_page_url': '/builds-diff?page=2' if page == 1 else None},
        }

    def test_filtered_manifest_keeps_scope_on_every_page(self):
        urls = []
        def fetch(url):
            urls.append(url)
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query['filter[Type]'], ['db2'])
            self.assertEqual(query['from'], ['old'])
            self.assertEqual(query['to'], ['new'])
            return self.manifest(int(query.get('page', ['1'])[0]))
        with patch.object(self.monitor, '_http_get_text', side_effect=fetch), patch.object(
                self.monitor, '_extract_inertia_props', side_effect=lambda value: value):
            self.assertEqual(self.monitor._fetch_changed_db2_tables('old', 'new'), {'spell', 'spelleffect'})
        self.assertEqual(len(urls), 2)

    def test_manifest_rejects_ignored_filter_identity_drift_and_incomplete_pages(self):
        original = self.manifest()
        variants = []
        for key, value in [('fromVersion', 'wrong'), ('filters', {})]:
            bad = deepcopy(original)
            bad[key] = value
            variants.append(bad)
        for key, value in [('total', 100000), ('last_page', 4000), ('data', []), ('next_page_url', None)]:
            bad = deepcopy(original)
            bad['items'][key] = value
            variants.append(bad)
        bad = deepcopy(original)
        bad['items']['data'][0]['Type'] = 'blp'
        variants.append(bad)
        for payload in variants:
            with self.subTest(payload=payload), patch.object(self.monitor, '_http_get_text', return_value=payload) as fetch, patch.object(
                    self.monitor, '_extract_inertia_props', side_effect=lambda value: value):
                with self.assertRaises(WagoDiffUnavailable):
                    self.monitor._fetch_changed_db2_tables('old', 'new')
                self.assertEqual(fetch.call_count, 1)

    def test_manifest_duplicate_file_is_not_complete(self):
        def fetch(url):
            page = int(parse_qs(urlsplit(url).query).get('page', ['1'])[0])
            payload = self.manifest(page)
            payload['items']['data'] = self.manifest()['items']['data']
            return payload
        with patch.object(self.monitor, '_http_get_text', side_effect=fetch), patch.object(
                self.monitor, '_extract_inertia_props', side_effect=lambda value: value):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'duplicate'):
                self.monitor._fetch_changed_db2_tables('old', 'new')

    def test_hotfix_candidate_stops_before_unnecessary_unstable_history(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query['search'], ['enUS % 70575'])
            self.assertEqual(query['page'], ['1'])
            return {'filters': {'search': 'enUS % 70575'}, 'hotfixes': {
                'total': 7381, 'current_page': 1, 'last_page': 296, 'per_page': 25,
                'data': [
                    {'id': 1, 'region_id': 1, 'locale': 'enUS', 'build': 70575, 'push_id': 999999},
                    {'id': 2, 'region_id': 3, 'locale': 'enUS', 'build': 70529, 'push_id': 999999},
                    {'id': 3, 'region_id': 3, 'locale': 'enUS', 'build': 70575, 'push_id': 113268},
                    {'id': 4, 'region_id': 3, 'locale': 'enUS', 'build': 70575, 'push_id': 113265},
                ]}}
        with patch.object(self.monitor, '_http_get_text', side_effect=fetch) as transport, patch.object(
                self.monitor, '_extract_inertia_props', side_effect=lambda value: value):
            self.assertEqual(self.monitor._fetch_latest_hotfix_push_id(
                locale='enUS', region_id=3, current_build='12.1.5.70575'), 113268)
        self.assertEqual(transport.call_count, 1)

    def test_hotfix_already_seen_candidate_does_not_hide_later_page(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            page = int(query['page'][0])
            return {'hotfixes': {'total': 50, 'current_page': page, 'last_page': 2,
                'per_page': 25, 'data': [{'id': page, 'region_id': 3, 'locale': 'enUS',
                                        'build': 70575, 'push_id': 113267 + page}]}}
        with patch.object(self.monitor, '_http_get_text', side_effect=fetch) as transport, patch.object(
                self.monitor, '_extract_inertia_props', side_effect=lambda value: value):
            self.assertEqual(self.monitor._fetch_latest_hotfix_push_id(
                locale='enUS', region_id=3, current_build='12.1.5.70575', after_push=113268), 113269)
        self.assertEqual(transport.call_count, 2)

    @override_settings(WAGO_HOTFIX_DISCOVERY_MAX_PAGES=2)
    def test_hotfix_no_exact_candidate_fails_without_claiming_complete(self):
        def fetch(url, **kwargs):
            query = parse_qs(urlsplit(url).query)
            page = int(query['page'][0])
            return {'filters': {'search': query['search'][0]}, 'hotfixes': {
                'total': 100, 'current_page': page, 'last_page': 4, 'per_page': 25,
                'data': [{'id': page, 'region_id': 1, 'locale': 'enUS', 'build': 70575, 'push_id': 113268}]}}
        with patch.object(self.monitor, '_http_get_text', side_effect=fetch) as transport, patch.object(
                self.monitor, '_extract_inertia_props', side_effect=lambda value: value):
            with self.assertRaisesRegex(WagoDiffUnavailable, 'not observed'):
                self.monitor._fetch_latest_hotfix_push_id(locale='enUS', region_id=3, current_build='12.1.5.70575')
        self.assertEqual(transport.call_count, 2)
