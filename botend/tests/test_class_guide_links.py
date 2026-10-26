"""外链限制与页内链接必须同时满足，无法确定的锚点不能伪造跳转。"""
from types import SimpleNamespace

from bs4 import BeautifulSoup
from django.test import SimpleTestCase
from django.template.loader import render_to_string

from botend.services.class_guide_links import GuideLinkPolicy


class GuideLinkTests(SimpleTestCase):
    def setUp(self):
        self.guide = SimpleNamespace(pk=7, source_url='https://www.icy-veins.com/wow/guide', source_payload={
            'chapters': [{'title': '天赋', 'url': 'https://www.icy-veins.com/wow/talents'},
                         {'title': '循环', 'url': 'https://www.icy-veins.com/wow/rotation'}]})
        def heading(key, title, level):
            return {'id': key, 'type': 'heading', 'title': title, 'data': {'level': level}}
        self.links = GuideLinkPolicy([heading('md-1', '天赋', 2), heading('md-2', '方案', 3),
                                      heading('md-3', '循环', 2), heading('md-4', '方案', 3)], self.guide)

    def test_external_links_only_allowed_in_explicit_source_note(self):
        for url in ('https://www.curseforge.com/wow/addons/a', '//example.com/a', 'mailto:a@example.com',
                    'https://www.icy-veins.com/faq', 'https://www.icy-veins.com/wow/guide?redirect=other'):
            self.assertIsNone(self.links.href(url, source_note=True))
        self.assertEqual(self.links.href(self.guide.source_url, source_note=True), self.guide.source_url)
        self.assertIsNone(self.links.href(self.guide.source_url))

    def test_source_chapters_become_actual_local_targets(self):
        self.assertEqual(self.links.href('https://www.icy-veins.com/wow/talents'), '#md-1')
        self.assertEqual(self.links.href('/wow/rotation'), '#md-3')
        self.assertEqual(self.links.href('https://www.icy-veins.com/wow/rotation#方案'), '#md-4')
        self.assertIsNone(self.links.href('https://www.icy-veins.com/wow/rotation#md-2'))
        self.assertIsNone(self.links.href('https://www.icy-veins.com/wow/rotation#missing'))

    def test_missing_ambiguous_and_encoded_anchors(self):
        self.assertEqual(self.links.href('#%E5%A4%A9%E8%B5%8B'), '#md-1')
        self.assertEqual(self.links.href('#md-4'), '#md-4')
        self.assertEqual(self.links.href('/portal/class-guides/7/#md-4'), '#md-4')
        self.assertIsNone(self.links.href('#方案'))
        self.assertIsNone(self.links.href('#missing'))
        self.assertIsNone(self.links.href('/portal/class-guides/7/#missing'))
        self.assertIsNone(self.links.href('/wow/unimported'))

    def test_cleanup_keeps_prose_and_site_tool_links(self):
        soup = BeautifulSoup('<p>查看<a href="https://external.example/plugin">插件</a>。<a href="#missing">说明</a><a href="#md-4" target="_blank">循环</a><a href="/portal/talents/?code=ABC">天赋工具</a></p>', 'html.parser')
        self.links.clean(soup)
        self.assertEqual(soup.get_text(), '查看插件。说明循环天赋工具')
        self.assertEqual([a['href'] for a in soup.select('a')], ['#md-4', '/portal/talents/?code=ABC'])
        self.assertNotIn('target', soup.a.attrs)

    def test_author_bio_can_hide_external_links(self):
        markup = render_to_string('shared/class_guide_author.html', {'hide_author_links': True,
            'author_profile': {'name': '作者甲', 'bio': '作者介绍', 'links': [{'url': 'https://example.com', 'label': '社交账号'}]}})
        self.assertIn('作者介绍', markup)
        self.assertNotIn('<a ', markup)
