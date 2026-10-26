"""预生成目录和相关文章的内容保真、权限与请求零数据库读取回归。"""
from datetime import datetime, timezone
from pathlib import Path
import uuid

from bs4 import BeautifulSoup
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings

from botend.guide_models import ClassGuide, ClassGuideTag
from botend.portal.class_guides import PortalClassGuideArticleView, PortalClassGuideCatalogView
from botend.services import class_guide_snapshot as files


class GuideCatalogReadPathTests(TestCase):
    def setUp(self):
        override = override_settings(CLASS_GUIDE_SNAPSHOT_ROOT=Path('tmp/guide-catalog-readpaths') / uuid.uuid4().hex)
        override.enable()
        self.addCleanup(override.disable)
        self.addCleanup(files._cached_content.cache_clear)
        self.addCleanup(files._cached_html.cache_clear)
        files.refresh_guide_snapshot()

    @classmethod
    def setUpTestData(cls):
        cls.reader = get_user_model().objects.create_user(username='catalog-reader')
        cls.editor = get_user_model().objects.create_user(username='catalog-editor', is_superuser=True)
        cls.cover = 'https://example.org/封面.jpg?q="甲"&other=<乙>&escaped=%22'
        cls.saved_at = datetime(2026, 9, 10, 12, tzinfo=timezone.utc)
        # Cover extraction must preserve JSON escaping, missing/null semantics,
        # and the existing safe_url policy (including relative URLs).
        cases = [
            ('C special & <title>', {'featuredImage': cls.cover}, {'name': '自定义 & <作者>'},
             {'name': '源作者'}, '原作者', '2026-08-09T12:00:00Z', '自定义 & <作者>', '2026.08.09'),
            ('A missing', {}, None, {'name': '源作者'}, '原作者', '', '源作者', None),
            ('B null', {'featuredImage': None}, {}, {'name': '不应回退'}, '原作者',
             'invalid', '原作者', None),
            ('D unsafe', {'featuredImage': 'javascript:alert(1)'}, None, {}, '',
             '', '站内编辑', '2026.09.10'),
            ('E relative', {'featuredImage': '/封面.jpg?a=1&b=2'}, None, {}, '仅作者',
             '2026-02-30', '仅作者', None),
            ('F fragment', {'featuredImage': '#封面'}, None, {}, '作者',
             '2026-07-08', '作者', '2026.07.08'),
        ]
        cls.expected = []
        tags = [ClassGuideTag.objects.create(name=name) for name in ['Z 标签', 'A & <标签>']]
        for index, (title, payload, custom, source, author, modified, expected_author, date) in enumerate(cases):
            guide = ClassGuide.objects.create(
                title=title, slug=f'catalog-{index}', spec_id=62, game_version='12.1',
                author=author, author_profile=custom, source_author_profile=source,
                source_url='' if index == 3 else 'https://example.org/guide', source_modified=modified,
                source_payload={**payload, 'large_source': 'source-only-' * 5000},
                content_markdown='正文-only-' * 5000, source_markdown='original-only-' * 5000,
                check_data={'large_audit': 'audit-only-' * 5000},
            )
            guide.tags.set(tags)
            cls.expected.append((guide, expected_author, date, payload.get('featuredImage')))
        cls.main = cls.expected[0][0]
        ClassGuide.objects.filter(pk=cls.main.pk).update(content_markdown='## 阅读正文\n\n保留正文内容。')
        other = ClassGuide.objects.create(title='Other specialization', slug='other', spec_id=71, game_version='12.1')
        cls.expected.append((other, '站内编辑', '2026.09.10', None))
        ClassGuide.objects.create(title='Archived excluded', slug='archived', spec_id=62, game_version='12.1', archived=True)
        ClassGuide.objects.update(updated_at=cls.saved_at)
        cls.expected.sort(key=lambda row: (row[0].class_name, row[0].spec_name, row[0].title))

    def render_view(self, view, user, **kwargs):
        request = RequestFactory().get('/portal/class-guides/')
        request.user = user
        reads = []

        def capture(execute, sql, params, many, context):
            result = execute(sql, params, many, context)
            if f'FROM {connection.ops.quote_name(ClassGuide._meta.db_table)} ' in sql:
                reads.append((sql, [column[0] for column in context['cursor'].description], params))
            return result

        # Capture through completed template rendering: deferred attribute access
        # or per-card tag queries must not escape the read budget.
        from django.test.utils import CaptureQueriesContext
        with connection.execute_wrapper(capture), CaptureQueriesContext(connection) as queries:
            response = view.as_view()(request, **kwargs)
        self.assertEqual(response.status_code, 200)
        tag_reads = [q for q in queries if 'FROM "botend_classguidetag"' in q['sql']]
        return response, BeautifulSoup(response.content, 'html.parser'), reads, tag_reads

    def test_catalog_renders_all_metadata_without_body_reads_or_n_plus_one(self):
        for user, can_manage in [(self.reader, False), (self.editor, True)]:
            with self.subTest(can_manage=can_manage):
                response, soup, reads, tag_reads = self.render_view(PortalClassGuideCatalogView, user)
                self.assertEqual(reads, [], '目录请求不能查询攻略数据库')
                self.assertEqual(tag_reads, [], '标签随 HTML 预生成')
                cards = soup.select('.cg-guide-card')
                self.assertEqual([card.select_one('h2').get_text() for card in cards],
                                 [row[0].title for row in self.expected])
                self.assertEqual(soup.select_one('.cg-total').get_text(), f'{len(self.expected)} 篇攻略')
                for card, (guide, author, date, cover) in zip(cards, self.expected):
                    self.assertEqual(card['data-class'], guide.class_name)
                    self.assertEqual(card['data-spec'], str(guide.spec_id))
                    self.assertEqual(card['data-role'], 'dps')
                    self.assertEqual(card.select_one('h2 a')['href'], f'/portal/class-guides/{guide.pk}/')
                    self.assertEqual(card.select_one('.cg-card-footer span').get_text(), author)
                    self.assertEqual([tag.get_text() for tag in card.select('.cg-tag')],
                                     ['A & <标签>', 'Z 标签'] if guide.spec_id == 62 else [])
                    time = card.select_one('time')
                    self.assertEqual(time.get_text() if time else None, f'{date} 更新' if date else None)
                    image = card.select_one('.cg-card-cover > img')
                    expected_cover = '' if not cover or cover.startswith('javascript:') else cover
                    self.assertEqual(image['src'] if image else '', expected_cover)
                self.assertEqual(bool(soup.select_one('.cg-footer a[href^="/dashboard/"]')), can_manage)
                self.assertEqual(response['Cache-Control'], 'private, no-store')

    def test_article_related_uses_same_thin_catalog_and_keeps_full_article(self):
        response, soup, reads, tag_reads = self.render_view(
            PortalClassGuideArticleView, self.reader, guide_id=self.main.pk)
        self.assertEqual(reads, [], '文章及相关文章均从已发布页面读取')
        self.assertEqual(tag_reads, [])
        links = soup.select('.cg-related-tabs a')
        expected = sorted((row[0] for row in self.expected if row[0].spec_id == 62), key=lambda guide: guide.title)
        self.assertEqual([link.get_text() for link in links], [guide.title for guide in expected])
        self.assertEqual([link['href'] for link in links], [f'/portal/class-guides/{guide.pk}/' for guide in expected])
        self.assertEqual(soup.select_one('.cg-related-tabs [aria-current="page"]')['href'],
                         f'/portal/class-guides/{self.main.pk}/')
        self.assertIn('保留正文内容。', soup.select_one('.cg-article-body').get_text())
        self.assertEqual(files.read_guide_snapshot(self.main.pk)['cover'], self.cover)
