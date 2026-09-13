import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalPostMonitor import PortalPostMonitor, _hash_url
from botend.services.article_content_service import extract_structured_article


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


class PortalPostMonitorBlizzardChinaTests(SimpleTestCase):
    def test_update_stores_detail_as_structured_body_and_skips_existing_body(self):
        first_url = "https://wow.blizzard.cn/news/24302576"
        second_url = "https://wow.blizzard.cn/news/24302577"
        listing = MagicMock(status_code=200)
        listing.content = f"""
        <a href="{first_url}">
          <div class="list-title">第一篇国服新闻</div>
          <div class="list-desc">第一篇短摘要</div>
          <div class="list-time" data-time="2026-09-12"></div>
        </a>
        <a href="{second_url}">
          <div class="list-title">已有正文的新闻</div>
          <div class="list-desc">第二篇短摘要</div>
          <div class="list-time" data-time="2026-09-11"></div>
        </a>
        """.encode("utf-8")
        detail = MagicMock(status_code=200)
        detail.text = """
        <html><body>
          <div id="blog"><div class="Blog"><div class="detail">
            <h2>版本亮点</h2>
            <p>国服官网正文第一段。</p>
            <ul><li>保留列表项目</li></ul>
            <img src="/static/news/feature.jpg" alt="专题图片">
          </div></div></div>
          <div class="footer">不应进入正文</div>
        </body></html>
        """

        monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        monitor.req = MagicMock()
        monitor.req.get.side_effect = [listing, detail]
        monitor._upsert_article = MagicMock()

        missing_body = MagicMock(content="", content_blocks="")
        complete_body = MagicMock(
            content="已有完整正文",
            content_blocks=json.dumps([{"type": "html", "html": "<p>已有完整正文</p>"}]),
        )
        saved_missing = MagicMock(content="", content_blocks="")
        saved_complete = MagicMock(content=complete_body.content, content_blocks=complete_body.content_blocks)
        monitor._upsert_article.side_effect = [saved_missing, saved_complete]

        with patch(
            "botend.controller.plugins.portal.PortalPostMonitor.WowArticle.objects"
        ) as objects, patch(
            "botend.controller.plugins.portal.PortalPostMonitor.upload_article_images_in_blocks",
            side_effect=lambda blocks, **kwargs: blocks,
        ):
            objects.filter.return_value.only.return_value.first.side_effect = [missing_body, complete_body]
            monitor.update_blizzard_cn_news()

        self.assertEqual(monitor.req.get.call_count, 2)
        saved_missing.save.assert_called_once()
        self.assertEqual(set(saved_missing.save.call_args.kwargs["update_fields"]), {"content", "content_blocks"})
        self.assertIn("国服官网正文第一段", saved_missing.content)
        self.assertIn("保留列表项目", saved_missing.content)
        blocks = json.loads(saved_missing.content_blocks)
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn("<h2>版本亮点</h2>", blocks[0]["html"])
        self.assertIn("<ul><li>保留列表项目</li></ul>", blocks[0]["html"])
        self.assertIn('src="https://wow.blizzard.cn/static/news/feature.jpg"', blocks[0]["html"])
        self.assertNotIn("不应进入正文", blocks[0]["html"])
        saved_complete.save.assert_not_called()

        calls = monitor._upsert_article.call_args_list
        self.assertEqual(calls[0].kwargs["description"], "第一篇短摘要")
        self.assertEqual(calls[1].kwargs["description"], "第二篇短摘要")

    def test_update_preserves_image_only_detail_and_uses_source_summary_as_plain_content(self):
        url = "https://wow.blizzard.cn/news/2684192216/index.html"
        listing = MagicMock(status_code=200)
        listing.content = f"""
        <a href="{url}">
          <div class="list-title">国服21周年庆开启</div>
          <div class="list-desc">坐骑免费送，周年庆活动即将开启。</div>
          <div class="list-time" data-time="2026-08-03"></div>
        </a>
        """.encode("utf-8")
        detail = MagicMock(status_code=200)
        detail.text = """
        <html><body><div id="blog"><div class="detail">
          <p></p>
          <p><img src="https://nie.res.netease.com/event.png"></p>
        </div></div></body></html>
        """

        monitor = PortalPostMonitor.__new__(PortalPostMonitor)
        monitor.req = MagicMock()
        monitor.req.get.side_effect = [listing, detail]
        saved = MagicMock(content="", content_blocks="")
        monitor._upsert_article = MagicMock(return_value=saved)

        with patch(
            "botend.controller.plugins.portal.PortalPostMonitor.WowArticle.objects"
        ) as objects, patch(
            "botend.controller.plugins.portal.PortalPostMonitor.upload_article_images_in_blocks",
            side_effect=lambda blocks, **kwargs: blocks,
        ), patch(
            "botend.controller.plugins.portal.PortalPostMonitor.upsert_system_alert"
        ):
            objects.filter.return_value.only.return_value.first.return_value = MagicMock(
                content="", content_blocks=""
            )
            monitor.update_blizzard_cn_news()

        saved.save.assert_called_once()
        self.assertEqual(saved.content, "坐骑免费送，周年庆活动即将开启。")
        blocks = json.loads(saved.content_blocks)
        self.assertEqual(blocks[0]["type"], "html")
        self.assertIn('src="https://nie.res.netease.com/event.png"', blocks[0]["html"])

    def test_extractor_does_not_fall_back_when_blizzard_detail_root_is_missing(self):
        blocks = extract_structured_article(
            "<html><body><main><p>错误页通用内容，不是国服新闻正文。</p></main></body></html>",
            base_url="https://wow.blizzard.cn/news/missing",
            source="blizzard_cn",
        )

        self.assertEqual(blocks, [])
