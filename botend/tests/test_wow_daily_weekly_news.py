"""Only Wednesday news is a digest; ordinary daily/news source facts stay intact."""
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from django.test import SimpleTestCase, TestCase

from botend.models import WowArticle, WowDailyReport
from botend.services import rio_statistics_views
from botend.wow_daily_report.generator import generate_wow_daily_report, render_daily_html


class WeeklyNewsDigestRenderTests(SimpleTestCase):
    def item(self):
        return {'title': '测试新闻', 'url': 'https://example.test/story', 'source': 'wowhead',
                'portal_url': '/portal/article/123/', 'publish_time': '2026-11-10T12:00:00+08:00',
                'summary': '第一句总结。\n\n第二句总结。', 'summary_llm_ok': True,
                'body_html': '<p>全文唯一标记</p><img src="https://example.test/full.jpg">',
                'body': '全文唯一标记'}

    def document(self, day, items):
        return BeautifulSoup(render_daily_html(day, [{'key': 'news', 'items': items}]), 'html.parser')

    def test_weekly_keeps_one_summary_paragraph_title_source_and_links_but_no_fulltext(self):
        items = [self.item(), dict(self.item(), title='第二条新闻', url='https://example.test/second')]
        before = deepcopy(items)
        dom = self.document(date(2026, 11, 10), items)
        self.assertEqual(len(dom.select('.news-card')), 2)
        for card in dom.select('.news-card'):
            self.assertEqual(len(card.select('p.item-summary')), 1)
            self.assertIn('第一句总结。 第二句总结。', card.select_one('.item-summary').get_text())
            self.assertIn('AI 总结', card.get_text())
            self.assertIn('wowhead', card.get_text())
            self.assertEqual(len(card.select('a.original-link')), 2)
            self.assertFalse(card.select('.body-html,.body-text,.original-label,img,video,table'))
            self.assertNotIn('全文唯一标记', str(card))
        self.assertEqual(items, before)

    def test_daily_keeps_the_same_fulltext_and_existing_summary(self):
        dom = self.document(date(2026, 11, 11), [self.item()])
        card = dom.select_one('.news-card')
        self.assertIn('全文唯一标记', card.select_one('.body-html').get_text())
        self.assertEqual(len(card.select('.body-html img')), 1)
        self.assertIn('新闻原文', card.get_text())
        self.assertEqual(len(card.select('.item-summary')), 1)

    def test_summary_is_escaped_and_fallback_is_not_mislabeled_ai(self):
        item = self.item(); item.update(summary='<script>alert(1)</script>\n正文摘要', summary_llm_ok=False)
        card = self.document(date(2026, 11, 10), [item]).select_one('.news-card')
        self.assertFalse(card.select('script,br,.body-html,.body-text'))
        self.assertIn('<script>alert(1)</script>', card.get_text())
        self.assertIn('内容摘要', card.get_text())
        self.assertNotIn('AI 总结', card.get_text())
        item.update(summary='', body='', body_html='')
        card = self.document(date(2026, 11, 10), [item]).select_one('.news-card')
        self.assertIn('暂无可用正文', card.get_text())
        self.assertEqual(len(card.select('p.item-summary')), 1)


class WeeklyNewsDigestGenerationTests(TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='weekly-news-')
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        config = self.settings(BASE_DIR=self.base/'web', RIO_STATISTICS_ROOT=self.base/'central')
        config.enable(); self.addCleanup(config.disable)
        rio_statistics_views.clear_cache(); self.addCleanup(rio_statistics_views.clear_cache)
        no_network = patch('requests.sessions.Session.request', side_effect=AssertionError('no collection or AI during regression'))
        no_network.start(); self.addCleanup(no_network.stop)

    def test_real_generator_persists_weekly_edition_without_body_and_preserves_central_article(self):
        day = date(2026, 11, 10)
        body = '<p>' + '正文中已有的可核实内容。'*40 + '</p><p>仅全文末尾唯一标记</p>'
        article = WowArticle.objects.create(title='周报短新闻', url='https://example.test/weekly-real',
            source='wowhead', category='news', content=body, is_active=True,
            publish_time=datetime(2026, 11, 10, 12, tzinfo=ZoneInfo('Asia/Shanghai')))
        before = WowArticle.objects.values().get(pk=article.pk)
        result = generate_wow_daily_report(report_date=day, use_llm=False)
        row = WowDailyReport.objects.get(report_date=day)
        self.assertEqual(row.md_path, result['md_path'])
        self.assertEqual(result['ext']['edition']['kind'], 'wednesday_weekly')
        self.assertEqual(result['ext']['sections']['news']['count'], 1)
        dom = BeautifulSoup(Path(result['full_path']).read_text(), 'html.parser')
        card = dom.select_one('.news-card')
        self.assertEqual(len(card.select('p.item-summary')), 1)
        self.assertIn('正文中已有的可核实内容', card.get_text())
        self.assertNotIn('仅全文末尾唯一标记', str(card))
        self.assertFalse(card.select('.body-html,.body-text,.original-label'))
        self.assertIn('内容摘要', card.get_text())
        self.assertNotIn('AI 总结', card.get_text())
        self.assertEqual(WowArticle.objects.values().get(pk=article.pk), before)
