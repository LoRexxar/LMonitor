from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor, _hash_url


class PortalPostMonitorUpsertTests(SimpleTestCase):
    def test_upsert_uses_indexed_url_hash_for_lookup_and_locking(self):
        monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        url = "https://wow.blizzard.cn/news/20260716/40565_1308032.html"
        url_hash = _hash_url(url)
        existing = MagicMock(publish_time=None)
        filtered = MagicMock()
        filtered.only.return_value.first.return_value = existing
        saved = MagicMock()

        with patch(
            "botend.controller.plugins.portal.PortalPostMonitor.WowArticle.objects"
        ) as objects:
            objects.filter.return_value = filtered
            objects.update_or_create.return_value = (saved, False)

            result = monitor._upsert_article(
                title="测试新闻",
                url=url,
                source="blizzard_cn",
                category="news",
            )

        self.assertIs(result, saved)
        objects.filter.assert_called_once_with(url_hash=url_hash)
        objects.update_or_create.assert_called_once()
        lookup_kwargs = objects.update_or_create.call_args.kwargs
        self.assertEqual(lookup_kwargs["url_hash"], url_hash)
        self.assertEqual(lookup_kwargs["defaults"]["url"], url)


class PortalPostMonitorExwindTests(SimpleTestCase):
    def test_parse_current_exwind_card_structure(self):
        html_text = """
        <a href="/post/blue/29958493" class="panel no-underline">
          <div class="flex items-center">
            <span class="text-xs">蓝帖</span>
            <span class="text-xs">2026-08-29 06:27</span>
          </div>
          <div class="font-bold text-lg sm:text-xl">职业调整即将到来 – 9月1日</div>
          <div class="text-sm line-clamp-2">文章摘要，不应被当成标题。</div>
          <span class="text-sm font-semibold">阅读全文 →</span>
        </a>
        """

        items = PortalPostMonitor._parse_exwind_latest(html_text)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["title"], "职业调整即将到来 – 9月1日")
        self.assertEqual(items[0]["url"], "https://exwind.net/post/blue/29958493")
        self.assertEqual(items[0]["publish_time"].strftime("%Y-%m-%d %H:%M:%S"), "2026-08-29 06:27:00")

    def test_parse_legacy_exwind_link_structure(self):
        html_text = '<a href="/post/news/42">旧版文章标题</a>'

        items = PortalPostMonitor._parse_exwind_latest(html_text)

        self.assertEqual(items, [{
            "title": "旧版文章标题",
            "url": "https://exwind.net/post/news/42",
            "publish_time": None,
        }])

    def test_update_reuses_listing_time_and_preserves_existing_description(self):
        response = MagicMock(status_code=200)
        response.text = """
        <a href="/post/blue/29958493">
          <span class="text-xs">2026-08-29 06:27</span>
          <div class="font-bold text-lg">职业调整即将到来 – 9月1日</div>
          <span>阅读全文 →</span>
        </a>
        """
        monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        monitor.req = MagicMock()
        monitor.req.get.return_value = response
        monitor._get_exwind_publish_time = MagicMock()
        monitor._fetch_full_text = MagicMock()
        monitor._upsert_article = MagicMock()
        existing_description = "已有正文摘要" * 200
        existing = MagicMock(description=existing_description)

        with patch(
            "botend.controller.plugins.portal.PortalPostMonitor.WowArticle.objects"
        ) as objects:
            objects.filter.return_value.only.return_value.first.return_value = existing
            monitor.update_exwind_latest()

        monitor._get_exwind_publish_time.assert_not_called()
        monitor._fetch_full_text.assert_not_called()
        monitor._upsert_article.assert_called_once()
        saved = monitor._upsert_article.call_args.kwargs
        self.assertEqual(saved["title"], "职业调整即将到来 – 9月1日")
        self.assertEqual(saved["description"], existing_description)
        self.assertEqual(saved["publish_time"].strftime("%Y-%m-%d %H:%M:%S"), "2026-08-29 06:27:00")
