import json
from unittest import TestCase
from unittest.mock import patch

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
