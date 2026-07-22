import json
from unittest.mock import patch

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
