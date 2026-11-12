"""验证 Icy Veins 新闻采集范围、失败保留、去重与翻译补偿。"""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor
from botend.controller.plugins.portal.PortalArticleTranslateMonitor import PortalArticleTranslateMonitor
from botend.models import WowArticle
from botend.services import icy_veins_news as icy
from botend.services.article_content_service import blocks_to_plain_text, loads_blocks
from botend.services.article_translation_service import ArticleTranslationService
from botend.services.wow_news_glossary_service import WowNewsGlossary


URL = 'https://www.icy-veins.com/wow/news/raid-tuning/'
LISTING = '<a href="/wow/news/raid-tuning/"><h3>团队副本调整</h3><span>1d ago by 作者</span></a>'
DETAIL = '''<html><head><meta property="article:published_time" content="2026-11-11T14:20:00Z">
<meta name="author" content="作者"></head><body><nav>攻略导航</nav><h1>团队副本调整</h1>
<div class="entry-content"><h2>职业改动</h2><p>副本与职业调整的完整正文，保留标题、列表与图片。</p>
<ul><li>第一项改动</li></ul><img src="/images/raid.jpg"><script>恶意脚本</script></div>
<aside>其他游戏推荐</aside></body></html>'''


def rss_item(url=URL, body='<p>来自订阅的完整正文，包含职业调整和副本信息。</p>', date='Tue, 10 Nov 2026 18:03:43 +0000'):
    return f'''<item><title>团队副本 &amp; 职业调整</title><link>{url}</link><pubDate>{date}</pubDate>
    <dc:creator>Starym</dc:creator><category>WoW</category><description>只有摘要</description>
    <content:encoded><![CDATA[{body}]]></content:encoded></item>'''


def rss(items=None):
    return ('<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>'
            + (rss_item() if items is None else items) + '</channel></rss>')


class IcyVeinsParserTests(SimpleTestCase):
    def test_version_labels_follow_categories_and_never_title_keywords(self):
        self.assertEqual(icy.label_title('WoW Forever changes', ['WoW']), '[正式服]WoW Forever changes')
        self.assertEqual(icy.label_title('每周新闻', ['WoW, WoW Classic, WoW Forever']),
                         '[正式服/怀旧服/无限]每周新闻')
        self.assertEqual(icy.label_title('更新', ['Unrecognized']), '[魔兽世界]更新')
        detail = icy.parse_detail(DETAIL.replace('</head>', '<meta property="article:section" content="WoW Classic"></head>'), URL)
        self.assertEqual(detail['title'], '[怀旧服]团队副本调整')

    def test_rss_full_body_metadata_and_cross_game_filter(self):
        items = icy.parse_rss(rss(rss_item() + rss_item() + rss_item('https://www.icy-veins.com/blizzard/news/diablo/')
                                 + rss_item('https://evil.test/wow/news/bad/')))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['title'], '[正式服]团队副本 & 职业调整')
        self.assertEqual(items[0]['author'], 'Starym')
        self.assertEqual(items[0]['publish_time'].isoformat(), '2026-11-10T18:03:43+00:00')
        self.assertIn('完整正文', items[0]['content_html'])
        self.assertNotEqual(items[0]['content_html'], items[0]['description'])

    def test_rss_browser_xml_viewer_preserves_namespace_and_cdata(self):
        raw = rss()
        viewer = '<html><body><div id="webkit-xml-viewer-source-xml">' + raw + '</div><div>XML 查看器</div></body></html>'
        self.assertEqual(icy.parse_rss(viewer), icy.parse_rss(raw))

    def test_rss_rejects_bad_dates_entities_and_challenge_pages(self):
        for text in [rss(rss_item(date='昨天')), rss(rss_item(date='Tue, 10 Nov 2026 18:03:43')),
                     '<html>Just a moment...</html>', '<rss><channel>', '<!DOCTYPE rss>' + rss()]:
            with self.subTest(text=text[:50]):
                self.assertEqual(icy.parse_rss(text), [])

    def test_browser_waits_for_rss_without_reloading_or_clicking(self):
        driver = SimpleNamespace(html='<html>验证及加载中</html>')
        req = Mock(is_chrome=True)
        req.get.side_effect = [SimpleNamespace(status_code=403), driver]
        with patch.object(icy.time, 'sleep', side_effect=lambda _: setattr(driver, 'html', rss())) as sleep:
            self.assertEqual(len(icy.fetch_rss(req)), 1)
        sleep.assert_called_once_with(1)
        self.assertEqual(req.get.call_count, 2)

    def test_browser_wait_is_bounded_when_challenge_does_not_clear(self):
        req = Mock(is_chrome=True)
        req.get.side_effect = [SimpleNamespace(status_code=403), SimpleNamespace(html='<html>验证页</html>')]
        with patch.object(icy.time, 'monotonic', side_effect=[0, 0, 45]), patch.object(icy.time, 'sleep') as sleep:
            with self.assertRaises(ValueError):
                icy.fetch_rss(req)
        sleep.assert_called_once_with(1)

    def test_actual_article_structure_uses_byline_and_removes_injected_ads(self):
        page = '''<meta property="article:published_time" content="2026-11-10T18:03:43+00:00">
        <h1>新闻标题</h1><div class="news-author"><span>Nov 10th, 2026 by <a>Starym</a></span></div>
        <script type="application/ld+json">{"@type":"NewsArticle","author":[{"name":"Icy Veins"}]}</script>
        <article class="news article-content"><p>真实结构中的新闻正文。</p>
        <div class="ofhkcG_in_content_ad"><div>Advertisement</div><div data-freestar-dataset="广告"></div></div>
        <h2>后续正文</h2></article>'''
        detail = icy.parse_detail(page, URL)
        self.assertEqual(detail['author'], 'Starym')
        self.assertIn('后续正文', blocks_to_plain_text(detail['blocks']))
        self.assertNotIn('Advertisement', blocks_to_plain_text(detail['blocks']))

    def test_listing_filters_other_games_guides_hosts_and_deduplicates(self):
        html = LISTING + '''<a href="/wow/news/raid-tuning?utm_source=home">重复标题</a>
        <a href="https://evil.test/wow/news/bad/">外站</a><a href="/d4/news/test/">其他游戏</a>
        <a href="/wow/news/">所有新闻</a><a href="/wow/warrior-guide">攻略</a>'''
        self.assertEqual(icy.parse_listing(html), [{'url': URL, 'title': '[魔兽世界]团队副本调整'}])

    def test_detail_keeps_body_and_absolute_date(self):
        detail = icy.parse_detail(DETAIL, URL)
        self.assertEqual(detail['author'], '作者')
        self.assertEqual(detail['publish_time'].isoformat(), '2026-11-11T14:20:00+00:00')
        body = blocks_to_plain_text(detail['blocks'])
        self.assertIn('第一项改动', body)
        self.assertNotIn('攻略导航', body)
        self.assertNotIn('其他游戏推荐', body)
        self.assertNotIn('恶意脚本', body)
        self.assertIn('https://www.icy-veins.com/images/raid.jpg', detail['blocks'][0]['html'])

    def test_jsonld_graph_metadata_and_invalid_date(self):
        html = '''<script type="application/ld+json">{"@graph":[{"@type":"NewsArticle",
        "datePublished":"2026-11-11T10:30:00-05:00","author":[{"name":"原作者"}]}]}</script>
        <div class="entry-content"><p>正文内容</p></div>'''
        detail = icy.parse_detail(html, URL)
        self.assertEqual(detail['author'], '原作者')
        self.assertEqual(detail['publish_time'].hour, 10)
        self.assertIsNone(icy.parse_detail(html.replace('2026-11-11T10:30:00-05:00', '1d ago'), URL)['publish_time'])

    def test_challenge_and_navigation_are_not_article_body(self):
        detail = icy.parse_detail('<main><h1>Just a moment...</h1><p>Enable JavaScript</p></main>', URL)
        self.assertEqual(detail['blocks'], [])
        self.assertIsNone(detail['publish_time'])

    def test_http_challenge_falls_back_only_when_browser_enabled(self):
        req = Mock(is_chrome=True)
        req.get.side_effect = [SimpleNamespace(status_code=403, text='验证页'), SimpleNamespace(html=LISTING)]
        self.assertEqual(icy.fetch_html(req, icy.HOME_URL, icy.parse_listing), LISTING)
        self.assertEqual(req.get.call_args.args[1], 'RespByChrome')
        req = Mock(is_chrome=False)
        req.get.return_value = SimpleNamespace(status_code=200, text='<main>验证页</main>')
        with self.assertRaises(ValueError):
            icy.fetch_html(req, icy.HOME_URL, icy.parse_listing)
        self.assertEqual(req.get.call_count, 1)

    def test_portal_runs_other_sources_before_reporting_icy_failure(self):
        monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        for method in ('update_blueposts', 'update_exwind_latest', 'update_blizzard_cn_news', 'update_nga_hot'):
            setattr(monitor, method, Mock())
        with patch.object(icy, 'update_news', side_effect=ValueError('站点验证失败')):
            self.assertFalse(monitor.scan(''))
        monitor.update_nga_hot.assert_called_once()
        self.assertEqual(monitor.last_error_detail, '站点验证失败')

    def test_translation_fetch_does_not_fall_back_to_whole_page(self):
        monitor = PortalArticleTranslateMonitor.__new__(PortalArticleTranslateMonitor)
        monitor._fetch_html = Mock(return_value='<main><h1>团队副本调整</h1><p>其他游戏推荐</p></main>')
        self.assertEqual(monitor._fetch_content_blocks(URL, 'icy_veins'), [])


class IcyVeinsIngestionTests(TestCase):
    def setUp(self):
        self.monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        self.monitor.req = Mock(is_chrome=False)
        self.monitor.translation_service = Mock()
        self.monitor.translation_service.available.return_value = True
        self.monitor.translation_service.needs_translation.return_value = True
        # 原网页路径用例明确模拟 RSS 无可用条目；RSS 主路径另行验证。
        self.rss_patch = patch.object(icy, 'fetch_rss', return_value=[])
        self.rss_patch.start()
        self.addCleanup(self.rss_patch.stop)

    def response(self, text):
        return SimpleNamespace(status_code=200, text=text)

    def translation_service(self):
        engine = Mock()
        engine.available.return_value = True
        engine.send_message.side_effect = ['猎人获胜', '["猎人在团队副本中获得胜利。"]', '["猎人在团队副本中获得胜利。"]']
        service = ArticleTranslationService(engine=engine, glossary=WowNewsGlossary.empty(), sleep_func=lambda _: None)
        service._glossary_for_article = Mock(return_value=WowNewsGlossary.empty())
        return service, engine

    def test_rss_runs_shared_translation_service_and_persists_all_chinese_fields(self):
        self.rss_patch.stop()
        self.monitor.translation_service, engine = self.translation_service()
        payload = rss(rss_item(body='<p>The hunter wins in the raid.</p>')).replace('团队副本 &amp; 职业调整', 'A hunter wins')
        self.monitor.req.get.return_value = self.response(payload)
        icy.update_news(self.monitor)
        article = WowArticle.objects.get()
        self.assertEqual((article.source, article.category), ('icy_veins', 'news'))
        self.assertEqual(article.title, '[正式服]A hunter wins')
        self.assertEqual(article.title_cn, '[正式服]猎人获胜')
        self.assertEqual(json.loads(article.content_cn)[0]['translated'], '猎人在团队副本中获得胜利。')
        self.assertIn('猎人在团队副本中获得胜利。', blocks_to_plain_text(loads_blocks(article.content_blocks_cn)))
        self.assertEqual(engine.send_message.call_count, 3)
        self.assertNotIn('[正式服]', engine.send_message.call_args_list[0].args[0])
        icy.update_news(self.monitor)
        self.assertEqual(engine.send_message.call_count, 3)

    def test_compensation_uses_shared_translation_service_after_initial_unavailability(self):
        self.rss_patch.stop()
        self.monitor.translation_service.available.return_value = False
        self.monitor.req.get.return_value = self.response(rss(rss_item(body='<p>The hunter wins in the raid.</p>')))
        icy.update_news(self.monitor)
        article = WowArticle.objects.get()
        self.assertFalse(article.title_cn)
        monitor = PortalArticleTranslateMonitor.__new__(PortalArticleTranslateMonitor)
        monitor.translation_service, engine = self.translation_service()
        with patch('botend.controller.plugins.portal.PortalArticleTranslateMonitor.time.sleep'):
            monitor.scan('')
        article.refresh_from_db()
        self.assertEqual(article.title_cn, '[正式服]猎人获胜')
        self.assertTrue(article.content_cn)
        self.assertTrue(article.content_blocks_cn)
        self.assertEqual(engine.send_message.call_count, 3)

    def test_existing_article_label_repair_preserves_translation_and_body(self):
        self.rss_patch.stop()
        article = WowArticle.objects.create(url=URL, source=icy.SOURCE, category='news',
                                           title='已有原题', title_cn='已有中文', content='已有正文')
        self.monitor.translation_service.needs_translation.return_value = False
        self.monitor.req.get.return_value = self.response(rss().replace('<category>WoW</category>', '<category>WoW, WoW Classic</category>'))
        icy.update_news(self.monitor)
        article.refresh_from_db()
        self.assertEqual(article.title, '[正式服/怀旧服]已有原题')
        self.assertEqual(article.title_cn, '[正式服/怀旧服]已有中文')
        self.assertEqual(article.content, '已有正文')
        self.monitor.translation_service.translate_article_fields.assert_not_called()

    def test_rss_import_needs_one_request_and_no_article_fetches(self):
        self.rss_patch.stop()
        self.monitor.req.get.return_value = self.response(rss())
        icy.update_news(self.monitor)
        article = WowArticle.objects.get()
        self.assertEqual(article.author, 'Starym')
        self.assertEqual(article.publish_time.isoformat(), '2026-11-10T18:03:43+00:00')
        self.assertIn('完整正文', article.content)
        self.assertNotIn('只有摘要', article.content)
        self.assertEqual(self.monitor.req.get.call_count, 1)
        self.assertEqual(self.monitor.req.get.call_args.args[0], icy.RSS_URL)
        icy.update_news(self.monitor)
        self.assertEqual(WowArticle.objects.count(), 1)
        self.assertEqual(self.monitor.req.get.call_count, 2)

    def test_rss_summary_only_fetches_detail_instead_of_saving_summary_as_body(self):
        self.rss_patch.stop()
        self.monitor.req.get.side_effect = [self.response(rss(rss_item(body=''))), self.response(DETAIL)]
        icy.update_news(self.monitor)
        self.assertEqual(self.monitor.req.get.call_count, 2)
        self.assertIn('完整正文', WowArticle.objects.get().content)
        self.assertNotIn('只有摘要', WowArticle.objects.get().content)

    def test_blocked_rss_falls_back_to_home_listing(self):
        self.rss_patch.stop()
        self.monitor.req.get.side_effect = [SimpleNamespace(status_code=403), self.response(LISTING), self.response(DETAIL)]
        icy.update_news(self.monitor)
        self.assertEqual(WowArticle.objects.count(), 1)
        self.assertEqual([call.args[0] for call in self.monitor.req.get.call_args_list], [icy.RSS_URL, icy.HOME_URL, URL])

    def test_ingestion_is_idempotent_and_preserves_translation_and_date(self):
        self.monitor.req.get.side_effect = [self.response(LISTING), self.response(DETAIL)]
        icy.update_news(self.monitor)
        article = WowArticle.objects.get(source=icy.SOURCE)
        self.assertEqual(article.url, URL)
        self.assertEqual(article.title, '[魔兽世界]团队副本调整')
        self.assertEqual(article.publish_time.day, 11)
        article.title_cn = '已有中文标题'
        article.save(update_fields=['title_cn'])
        self.monitor.req.get.reset_mock(side_effect=True)
        self.monitor.req.get.return_value = self.response(LISTING)
        icy.update_news(self.monitor)
        self.assertEqual(WowArticle.objects.count(), 1)
        self.assertEqual(self.monitor.req.get.call_count, 1)
        article.refresh_from_db()
        self.assertEqual(article.title_cn, '已有中文标题')
        self.assertEqual(article.publish_time.day, 11)
        self.assertEqual(self.monitor.translation_service.translate_article_fields.call_count, 2)

    def test_one_bad_article_does_not_block_next_and_retries_next_scan(self):
        listing = LISTING + '<a href="/wow/news/second/">第二篇</a>'
        self.monitor.req.get.side_effect = [self.response(listing), self.response('<main>验证页</main>'), self.response(DETAIL)]
        with self.assertRaises(ValueError):
            icy.update_news(self.monitor)
        self.assertEqual(WowArticle.objects.count(), 1)
        self.assertTrue(WowArticle.objects.get().url.endswith('/second/'))
        self.monitor.req.get.side_effect = [self.response(listing), self.response(DETAIL)]
        icy.update_news(self.monitor)
        self.assertEqual(WowArticle.objects.count(), 2)

    def test_failed_listing_keeps_existing_data(self):
        article = WowArticle.objects.create(url=URL, source=icy.SOURCE, content='旧正文', title_cn='旧译文')
        self.monitor.req.get.return_value = SimpleNamespace(status_code=403, text='验证页')
        with self.assertRaises(ValueError):
            icy.update_news(self.monitor)
        article.refresh_from_db()
        self.assertEqual(article.content, '旧正文')
        self.assertEqual(article.title_cn, '旧译文')

    def test_missing_time_is_not_replaced_by_crawl_time(self):
        self.monitor.req.get.side_effect = [self.response(LISTING), self.response(DETAIL.replace('2026-11-11T14:20:00Z', '昨天'))]
        with self.assertRaises(ValueError):
            icy.update_news(self.monitor)
        self.assertFalse(WowArticle.objects.exists())

    def test_translation_compensation_includes_icy_news(self):
        article = WowArticle.objects.create(url=URL, source=icy.SOURCE, category='news',
                                           title='待翻译', content='已有完整正文', publish_time=timezone.now())
        monitor = PortalArticleTranslateMonitor.__new__(PortalArticleTranslateMonitor)
        monitor.translation_service = Mock()
        with patch('botend.controller.plugins.portal.PortalArticleTranslateMonitor.time.sleep'):
            monitor.scan('')
        self.assertEqual(monitor.translation_service.translate_article_fields.call_args.args[0].pk, article.pk)

    def test_translation_failures_are_bounded_and_keep_originals(self):
        listing = ''.join(f'<a href="/wow/news/article-{i}/">新闻 {i}</a>' for i in range(5))
        for i in range(5):
            WowArticle.objects.create(url=f'https://www.icy-veins.com/wow/news/article-{i}/',
                                      source=icy.SOURCE, category='news', content='完整原文', title='待翻译新闻')
        self.monitor.req.get.return_value = self.response(listing)
        self.monitor.translation_service.translate_article_fields.side_effect = RuntimeError('翻译服务暂时不可用')
        with self.assertRaises(ValueError):
            icy.update_news(self.monitor)
        self.assertEqual(self.monitor.translation_service.translate_article_fields.call_count, 3)
        self.assertEqual(WowArticle.objects.filter(content='完整原文').count(), 5)
