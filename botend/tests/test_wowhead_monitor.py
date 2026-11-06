import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from unittest.mock import patch

import requests

from botend.services.article_translation_service import ArticleTranslationService, FallbackTranslationEngine
from botend.services.wow_news_glossary_service import WowNewsGlossary

from django.test import TestCase

from botend.controller.plugins.wow.wowheadMonitor import wowheadMonitor
from botend.models import WowArticle


class WowheadMonitorParserTest(TestCase):
    def test_parse_posts_from_home_news_json(self):
        payload = {
            "newsPosts": [
                {
                    "postUrl": "/news/raid-testing-starts-this-week-the-venomous-abyss-testing-on-12-1-ptr-381963",
                    "title": "Raid Testing Starts This Week - The Venomous Abyss Testing on 12.1 PTR",
                    "preview": "The Venomous Abyss raid testing begins this week.",
                    "postedFull": "2026/06/22 at 5:33 PM",
                    "typeName": "PTR",
                },
                {
                    "postUrl": "/blue-tracker/topic/example",
                    "title": "Blue Tracker should be skipped",
                },
            ]
        }
        html = '<script type="application/json" id="data.home.newsData">{}</script>'.format(
            json.dumps(payload)
        )

        monitor = wowheadMonitor(None, None)
        posts = monitor._parse_posts_from_page_html(html, limit=10)

        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["type"], "PTR")
        self.assertEqual(
            posts[0]["link"],
            "https://www.wowhead.com/news/raid-testing-starts-this-week-the-venomous-abyss-testing-on-12-1-ptr-381963",
        )
        self.assertEqual(posts[0]["date"], "2026/06/22 at 5:33 PM")
        self.assertIn("Venomous Abyss", posts[0]["title"])
        self.assertIn("raid testing", posts[0]["preview"])

    @patch.object(wowheadMonitor, '_fetch_article_blocks', return_value=[])
    def test_skip_new_article_when_body_is_empty(self, mock_fetch):
        monitor = wowheadMonitor(None, None)
        monitor._parse_posts_from_page_html = lambda page_html, limit=10: [
            {
                'type': 'PTR',
                'title': 'Test Article',
                'link': 'https://www.wowhead.com/news/test-article-123',
                'preview': 'Preview text',
                'date': '2026/06/24 at 05:33 PM',
            }
        ]

        class _Task:
            flag = ''
            def save(self):
                pass

        setattr(monitor, 'task', _Task())
        before = WowArticle.objects.filter(url='https://www.wowhead.com/news/test-article-123').count()
        result = monitor.resolve_data(type('D', (), {'html': '<html></html>'})(), 'wowhead', limit=1)
        after = WowArticle.objects.filter(url='https://www.wowhead.com/news/test-article-123').count()

        self.assertEqual(before, after)
        self.assertEqual(result[0], 1)
        self.assertEqual(result[1], 0)

    def test_fetch_article_html_prefers_complete_requests_html_over_partial_chrome_body(self):
        class _Driver:
            html = '<html><body><div class="news-post-content text"><p>Partial intro.</p></div></body></html>'

        class _Resp:
            status_code = 200
            text = '<html><body><div class="news-post-content text"><p>{}</p></div></body></html>'.format('Complete article body. ' * 100)

        class _Req:
            is_chrome = True
            def __init__(self):
                self.calls = []
                self.s = type('S', (), {'proxies': {}, 'trust_env': True})()
            def get(self, url, mode, *args, **kwargs):
                self.calls.append(mode)
                if mode == 'Response':
                    return _Resp()
                if mode == 'RespByChrome':
                    return _Driver()
                raise AssertionError(mode)

        req = _Req()
        monitor = wowheadMonitor(req, None)

        html = monitor._fetch_article_html('https://www.wowhead.com/news/test-123')

        self.assertIn('Complete article body', html)
        self.assertEqual(req.calls, ['Response'])

    def test_existing_html_article_does_not_refetch_only_because_it_has_no_separate_image_block(self):
        body = 'Complete existing article body. ' * 40
        article = WowArticle.objects.create(
            title='Existing article',
            url='https://www.wowhead.com/news/existing-123',
            description=body,
            content=body,
            content_blocks=json.dumps([{'type': 'html', 'html': '<p>{}</p>'.format(body)}]),
            source='wowhead',
            category='news',
        )
        monitor = wowheadMonitor(None, None)
        monitor._parse_posts_from_page_html = lambda page_html, limit=10: [{
            'type': 'News',
            'title': 'Existing article',
            'link': article.url,
            'preview': 'Preview',
            'date': '2026/07/22 at 01:00 PM',
        }]
        fetch_calls = []
        monitor._fetch_article_blocks = lambda *args, **kwargs: fetch_calls.append((args, kwargs)) or []
        monitor._ensure_translated = lambda article: False
        monitor._translate_budget = 0

        result = monitor.resolve_data(type('D', (), {'html': '<html></html>'})(), 'wowhead', limit=1)

        self.assertEqual(result, (1, 0))
        self.assertEqual(fetch_calls, [])
        article.refresh_from_db()
        self.assertEqual(article.content, body)

    def test_fetch_article_html_falls_back_when_chrome_html_has_no_article_body(self):
        class _Driver:
            html = '<html><body><h1>Verification</h1><p>captcha placeholder</p></body></html>'

        class _Resp:
            status_code = 200
            text = '<html><body><div id="news-post"><div class="text"><p>Real article body with enough useful text to pass the complete article validation threshold and avoid accepting a truncated page.</p></div></div></body></html>'

        class _Req:
            is_chrome = True
            def __init__(self):
                self.calls = []
                self.s = type('S', (), {'proxies': {}, 'trust_env': True})()
            def get(self, url, mode, *args, **kwargs):
                self.calls.append(mode)
                if mode == 'RespByChrome':
                    return _Driver()
                if mode == 'Response':
                    return _Resp()
                raise AssertionError(mode)

        req = _Req()
        monitor = wowheadMonitor(req, None)

        html = monitor._fetch_article_html('https://www.wowhead.com/news/test-123')

        self.assertIn('Real article body', html)
        self.assertEqual(req.calls, ['Response'])

    def test_fetch_article_html_rejects_requests_html_without_article_body(self):
        class _Resp:
            status_code = 200
            text = '<html><body><h1>Verification</h1><p>captcha placeholder</p></body></html>'

        class _Req:
            is_chrome = False
            def __init__(self):
                self.s = type('S', (), {'proxies': {}, 'trust_env': True})()
            def get(self, url, mode, *args, **kwargs):
                return _Resp()

        monitor = wowheadMonitor(_Req(), None)

        self.assertEqual(monitor._fetch_article_html('https://www.wowhead.com/news/test-123'), '')


class WowheadForeverFeedTests(TestCase):
    def setUp(self):
        self.routes = {}
        self.received = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                owner.received.append(self.path)
                status, body = owner.routes.get(self.path, (404, 'missing fixture'))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(body.encode())

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        local_origin = 'http://127.0.0.1:{}'.format(self.server.server_port)

        class Req:
            is_chrome = False
            s = requests.Session()
            s.trust_env = False

            def get(self, url, mode, *args, **kwargs):
                if mode != 'Response':
                    raise RuntimeError('browser unavailable in loopback test')
                return self.s.get(local_origin + urlsplit(url).path, timeout=3)

        class Task:
            flag = ''
            def save(self):
                pass

        service = ArticleTranslationService(engine=FallbackTranslationEngine(engines=[]), glossary=WowNewsGlossary.empty())
        with patch('botend.controller.plugins.wow.wowheadMonitor.build_translation_service', return_value=service):
            self.monitor = wowheadMonitor(Req(), Task())
        self.monitor.trigger_webhook = lambda: None
        sleep = patch('botend.controller.plugins.wow.wowheadMonitor.time.sleep', return_value=None)
        sleep.start()
        self.addCleanup(sleep.stop)

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def post(self, article_id, kind='Forever', prefix='/forever', **extra):
        item = dict(postUrl='{}/news/article-{}'.format(prefix, article_id), title='Article title {}'.format(article_id), typeName=kind, type=24 if kind == 'Forever' else 20)
        item.update(extra)
        body = 'Article title {}. Complete source article body. '.format(article_id) * 30
        self.routes[item['postUrl']] = (200, '<title>{}</title><div class="news-post-content"><p>{}</p></div>'.format(item['title'], body))
        return item

    def feed(self, path, posts):
        self.routes[path] = (200, '<script id="data.home.newsData" type="application/json">{}</script>'.format(json.dumps({'newsPosts': posts})))

    def test_scan_both_feeds_preserves_types_and_deduplicates_exact_ids(self):
        self.feed('/wow/retail', [self.post(123, 'WoW', ''), self.post(1234, 'Live', '')])
        self.feed('/forever/news', [self.post(123, 'WoW'), self.post(124), self.post(125, '', type=24)])
        self.assertTrue(self.monitor.scan(''))
        self.assertIn('/wow/retail', self.received)
        self.assertIn('/forever/news', self.received)
        self.assertEqual(WowArticle.objects.count(), 4)
        self.assertEqual(WowArticle.objects.get(url__endswith='article-123').title, '[WoW]Article title 123')
        self.assertTrue(WowArticle.objects.get(url__endswith='article-124').title.startswith('[无限]'))
        self.assertTrue(WowArticle.objects.get(url__endswith='article-125').title.startswith('[无限]'))
        original = WowArticle.objects.get(url__endswith='article-123')
        self.assertEqual(original.url, 'https://www.wowhead.com/news/article-123')
        self.assertNotIn('/forever/news/article-123', self.received)
        self.assertEqual(self.monitor.target_url, 'https://www.wowhead.com/wow/retail')

    def test_partial_feed_failure_keeps_other_feed_and_specific_error(self):
        for failed_path in ('/wow/retail', '/forever/news'):
            with self.subTest(failed_path=failed_path):
                WowArticle.objects.all().delete()
                self.feed('/wow/retail', [self.post(201, 'Live', '')])
                self.feed('/forever/news', [self.post(202)])
                self.routes[failed_path] = (503, 'upstream unavailable')
                self.assertFalse(self.monitor.scan(''))
                self.assertEqual(WowArticle.objects.count(), 1)
                self.assertIn(failed_path, self.monitor.last_error_detail)
                self.assertIn('503', self.monitor.last_error_detail)

    def test_replays_current_forever_news_json_contract(self):
        # Source: captured Wowhead /forever/news data.news.newsData.
        exclusive = {
            'postUrl': '/forever/news/wow-forever-hotfixes-november-5-2026-world-of-warcraft-blizzard-news-383778',
            'title': 'Various NPC Fixes and Deathstalker Erland Spawn Rate - WoW: Forever Hotfixes',
            'typeId': 24, 'typeName': 'Forever', 'postedFull': '2026/11/05 at 8:36 PM',
            'preview': 'The first set of hotfixes are out for WoW: Forever, which fixes issues with various NPCs and spawns Deathstalker Erland faster for his quest!',
        }
        common = {
            'postUrl': '/forever/news/the-weekly-reset-with-taliesin-and-evitel-forever-forever-talebound-and-more-383691',
            'title': 'The Weekly Reset with Taliesin and Evitel: Forever Forever? Talebound and More',
            'typeId': 20, 'typeName': 'WoW', 'postedFull': '2026/11/05 at 11:00 AM',
        }
        original = WowArticle.objects.create(
            source='wowhead', category='news', title='[WoW]' + common['title'],
            url='https://www.wowhead.com' + common['postUrl'].replace('/forever/news/', '/news/'),
            content='Preserved original body', description='Preserved original description', title_cn='原有中文标题',
        )
        self.feed('/wow/retail', [self.post(710, 'Live', '')])
        self.routes['/forever/news'] = (200, '<script id="data.news.newsData">{}</script>'.format(json.dumps({'newsPosts': [exclusive, common], 'pinnedPosts': []})))
        self.routes[exclusive['postUrl']] = (200, '<title>{}</title><div class="news-post-content">{}</div>'.format(exclusive['title'], exclusive['preview'] * 15))
        self.assertTrue(self.monitor.scan(''))
        article = WowArticle.objects.get(url='https://www.wowhead.com' + exclusive['postUrl'])
        self.assertEqual(article.title, '[无限]' + exclusive['title'])
        original.refresh_from_db()
        self.assertEqual(original.content, 'Preserved original body')
        self.assertEqual(original.description, 'Preserved original description')
        self.assertEqual(original.title_cn, '原有中文标题')
        self.assertEqual(WowArticle.objects.count(), 3)

    def test_translated_retail_articles_leave_budget_for_forever(self):
        retail = [self.post(i, 'Live', '') for i in range(800, 810)]
        body = 'Complete source article body. ' * 50
        chinese = '这是一篇完整翻译的中文新闻正文。' * 100
        for post in retail:
            WowArticle.objects.create(
                source='wowhead', category='news', title=post['title'], title_cn='已有中文标题',
                url='https://www.wowhead.com' + post['postUrl'], description=body, content=body,
                content_cn=json.dumps([{'original': body, 'translated': chinese}], ensure_ascii=False),
                content_blocks=json.dumps([{'type': 'paragraph', 'text': body}]),
                content_blocks_cn=json.dumps([{'type': 'paragraph', 'text': chinese}], ensure_ascii=False),
            )
        self.feed('/wow/retail', retail)
        self.feed('/forever/news', [self.post(811)])

        class Engine:
            last_error = ''
            def available(self):
                return True
            def send_message(self, prompt, *, max_tokens):
                if max_tokens == 200:
                    return '新增中文标题'
                batch = json.loads(prompt.split('输入JSON：\n', 1)[1])
                return json.dumps([chinese for _ in batch], ensure_ascii=False)

        service = ArticleTranslationService(engine=Engine(), sleep_func=lambda _: None, glossary=WowNewsGlossary.empty())
        self.monitor.translation_service = service
        with patch.object(service, 'translate_article_fields', wraps=service.translate_article_fields) as translate:
            self.assertTrue(self.monitor.scan(''))
        article = WowArticle.objects.get(url__endswith='article-811')
        self.assertEqual(article.title_cn, '[无限]新增中文标题')
        self.assertIn(chinese, article.content_cn)
        self.assertIn(chinese, article.content_blocks_cn)
        self.assertEqual(translate.call_count, 1)
        self.assertEqual(self.monitor._remaining_translate_budget, 9)

    def test_translation_budget_is_shared_even_when_translation_fails(self):
        self.feed('/wow/retail', [self.post(i, 'Live', '') for i in range(300, 307)])
        self.feed('/forever/news', [self.post(300, 'Live')] + [self.post(i) for i in range(400, 407)])

        class Engine:
            last_error = 'controlled failure'
            def available(self):
                return True
            def send_message(self, prompt, *, max_tokens):
                raise RuntimeError('controlled failure')

        service = ArticleTranslationService(engine=Engine(), sleep_func=lambda _: None, glossary=WowNewsGlossary.empty())
        self.monitor.translation_service = service
        with patch.object(service, 'translate_article_fields', wraps=service.translate_article_fields) as translate:
            self.assertTrue(self.monitor.scan(''))
        attempts = [call.args[0].pk for call in translate.call_args_list]
        self.assertEqual(WowArticle.objects.count(), 14)
        self.assertEqual(len(attempts), 10)
        self.assertEqual(len(set(attempts)), 10)
        self.assertEqual(WowArticle.objects.filter(pk__in=attempts, url__contains='/forever/').count(), 3)
        self.assertEqual(self.monitor._remaining_translate_budget, 0)

    def test_html_fallback_uses_category_badges_not_forever_title(self):
        markup = '''<section>
          <div class="news-card-simple"><a class="news-card-simple-type type24" href="/forever/news?type=24">Forever</a><h3 class="news-card-simple-text-title"><a href="/forever/news/article-501">First actual source article</a></h3></div>
          <div class="news-card-simple"><a class="news-card-simple-type type20" href="/forever/news?type=20">WoW</a><h3 class="news-card-simple-text-title"><a href="/forever/news/article-502">Forever is in this general title</a></h3></div>
        </section>'''
        posts = self.monitor._parse_posts_from_page_html(markup)
        self.assertEqual([(p['type'], p['link'].rsplit('-', 1)[-1]) for p in posts], [('无限', '501'), ('WoW', '502')])

    def test_post_failure_does_not_hide_later_success_or_report_green(self):
        self.feed('/wow/retail', [self.post(601, 'Live', ''), self.post(602, 'Live', '')])
        self.feed('/forever/news', [self.post(603)])
        original = self.monitor._fetch_article_blocks
        def fetch(url, **kwargs):
            if url.endswith('-601'):
                raise RuntimeError('broken first article 601')
            return original(url, **kwargs)
        self.monitor._fetch_article_blocks = fetch
        self.assertFalse(self.monitor.scan(''))
        self.assertEqual(WowArticle.objects.count(), 2)
        self.assertIn('broken first article 601', self.monitor.last_error_detail)
