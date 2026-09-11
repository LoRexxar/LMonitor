"""完整目录分页、练级排除与缺失版本标签的导入回归。"""
import json
from urllib.parse import urlparse, parse_qs
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from botend.guide_models import ClassGuide
from botend.models import WowTalentVersion
from botend.services.class_guide_maxroll import MaxrollClient, discover, identity
from botend.services.class_guide_service import import_post, source_version, has_manual_content
from botend.tests.test_class_guides import source_post


def hit(index, slug, metas=None):
    return {'id': str(index), 'permalink': 'https://maxroll.gg/wow/class-guides/' + slug,
            'taxonomies': {'metas': metas or []}, 'tags': []}


def catalog(hits, total):
    data = {'state': {'loaderData': {
        'root': {'appContext': {'search': {'url': 'https://meilisearch-proxy.maxroll.gg',
                                         'index': 'wp_posts_15', 'api_key': '公开目录测试凭据'}}},
        '-class-guides-wow': {'searchData': {'initialSearchResponse': {
            'hits': hits, 'estimatedTotalHits': total, 'offset': 0, 'limit': len(hits)}}}}}}
    return '<script>window.__remixContext = ' + json.dumps(data) + ';</script>'


class CatalogPaginationTests(SimpleTestCase):
    def test_all_pages_are_included_and_only_leveling_is_excluded(self):
        initial = [hit(1, 'arcane-mage-raid-guide'), hit(2, 'arcane-mage-leveling-guide')]
        later = [hit(3, 'fury-warrior-mythic-plus-guide'), hit(4, 'arcane-mage-pvp-guide')]
        final = [hit(5, 'arcane-mage-beginner-guide', ['leveling']), hit(6, 'arcane-mage-advanced-build')]
        client = MaxrollClient()
        with patch.object(client, 'fetch', return_value=catalog(initial, 6)), patch.object(client, '_catalog_search',
                side_effect=[{'hits': later, 'estimatedTotalHits': 6}, {'hits': final, 'estimatedTotalHits': 6}]) as search:
            urls = client.discover()
        self.assertEqual(len(urls), 4)
        self.assertIn(later[0]['permalink'], urls)
        self.assertIn(final[1]['permalink'], urls)
        self.assertNotIn(final[0]['permalink'], urls)
        self.assertEqual([call.args[1] for call in search.call_args_list], [2, 4])
        self.assertEqual(identity('arcane-mage-advanced-build'), ('Mage', 'Arcane', 'general'))

    def test_repeated_or_truncated_page_cannot_report_complete(self):
        first = hit(1, 'arcane-mage-raid-guide')
        for page in [[], [first]]:
            client = MaxrollClient()
            with patch.object(client, 'fetch', return_value=catalog([first], 2)), patch.object(client, '_catalog_search', return_value={'hits': page}):
                with self.assertRaises(ValueError):
                    client.discover()

    def test_missing_catalog_total_cannot_silently_use_first_page(self):
        markup = catalog([hit(1, 'arcane-mage-raid-guide')], 1).replace('"estimatedTotalHits": 1,', '')
        client = MaxrollClient()
        with patch.object(client, 'fetch', return_value=markup):
            with self.assertRaises(ValueError):
                client.discover()

    def test_backend_request_client_handles_pagination_without_version_filter(self):
        req = Mock()
        url = 'https://meilisearch-proxy.maxroll.gg/indexes/wp_posts_15/search'
        response = Mock(status_code=200, url=url, content=b'{}')
        response.json.return_value = {'hits': [], 'estimatedTotalHits': 0}
        req.get.return_value = response
        client = MaxrollClient(request_client=req)
        client._catalog_search({'url': 'https://meilisearch-proxy.maxroll.gg', 'index': 'wp_posts_15', 'api_key': '测试'}, 100)
        args, kwargs = req.get.call_args
        self.assertEqual(args[1:3], ('Response', 0))
        query = parse_qs(urlparse(args[0]).query)
        self.assertEqual(query['offset'], ['100'])
        self.assertEqual(query['filter'], ['taxonomies.category = "class-guides"'])

    def test_catalog_scope_does_not_collect_foreign_links_or_class_navigation(self):
        rows = discover('<a href="/wow/class-guides/arcane-mage-pvp-guide">攻略</a>'
                        '<a href="/wow/class-guides/death-knight">职业</a>'
                        '<a href="https://example.org/wow/class-guides/arcane-mage-raid-guide">外链</a>')
        self.assertEqual(rows, ['https://maxroll.gg/wow/class-guides/arcane-mage-pvp-guide'])


class GuideMissingVersionTests(TestCase):
    def test_missing_tag_uses_catalog_version_and_later_tag_does_not_duplicate(self):
        post = source_post(); post['tags'] = []
        url = 'https://maxroll.gg/wow/class-guides/' + post['slug']
        guide, status = import_post(post, url, catalog_version='12.1')
        self.assertEqual(status, 'imported')
        self.assertEqual(guide.game_version, '12.1')
        self.assertFalse(has_manual_content(guide))
        post['tags'] = [{'name': '12.1 - Midnight'}]
        updated, _ = import_post(post, url)
        self.assertEqual(updated.pk, guide.pk)
        self.assertEqual(ClassGuide.objects.count(), 1)

    def test_missing_tag_uses_shared_active_version_on_empty_database(self):
        WowTalentVersion.objects.update_or_create(
            key='retail',
            defaults={'branch': 'retail', 'major_version': '12.1.0', 'is_active': True},
        )
        post = source_post(); post['tags'] = []
        self.assertEqual(source_version(post), '12.1')

    def test_direct_import_rejects_leveling_with_or_without_version_tag(self):
        for tags in [[], [{'name': '12.1'}]]:
            post = source_post(); post.update(slug='arcane-mage-leveling-guide', tags=tags)
            with self.assertRaises(ValueError):
                import_post(post, 'https://maxroll.gg/wow/class-guides/' + post['slug'])
        self.assertFalse(ClassGuide.objects.exists())
