import json
import os
import tempfile
from datetime import date, datetime, time, timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from botend.models import (
    PortalMplusSeasonCutoff,
    PortalVideo,
    VideoMonitorTarget,
    WowArticle,
    WowDailyReport,
)
from botend.wow_daily_report.generator import generate_wow_daily_report


class WowDailyReportHtmlGeneratorTest(TestCase):
    def setUp(self):
        self.tmpdir_ctx = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir_ctx.cleanup)
        self.base_dir = self.tmpdir_ctx.name
        self.report_date = date(2026, 7, 1)
        tz = timezone.get_current_timezone()
        self.today_noon = timezone.make_aware(datetime.combine(self.report_date, time(12, 0)), tz)
        self.yesterday = self.today_noon - timedelta(days=1)

    def _article(self, **kwargs):
        defaults = {
            "title": "测试新闻",
            "url": f"https://example.com/{WowArticle.objects.count() + 1}",
            "author": "author",
            "description": "这是一段正文摘要",
            "content": "这是一段新闻原文内容，应该出现在日报模块中。",
            "publish_time": self.today_noon,
            "source": "wowhead",
            "category": "news",
            "reply_count": 0,
            "is_active": True,
        }
        defaults.update(kwargs)
        return WowArticle.objects.create(**defaults)

    @override_settings(BASE_DIR="/tmp")
    def test_generate_html_daily_report_creates_four_sections_and_indexes_file(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(title="Wowhead 新闻", source="wowhead", category="news")
            self._article(title="暴雪蓝贴", source="blizzard_tracker", category="news")
            self._article(title="NGA 高楼", source="nga", category="nga", reply_count=888)
            self._article(title="NGA 次高楼", source="nga", category="nga", reply_count=666)
            self._article(title="NGA 第三楼", source="nga", category="nga", reply_count=1)
            self._article(title="昨天旧闻", source="wowhead", category="news", publish_time=self.yesterday)

            target = VideoMonitorTarget.objects.create(
                name="劳瑞",
                tag="wow",
                platform="bilibili",
                target_url="https://space.bilibili.com/20325887/video",
            )
            PortalVideo.objects.create(
                title="今日视频",
                url="https://www.bilibili.com/video/BV1today",
                bvid="BV1today",
                cover_url="https://i0.hdslb.com/cover.jpg",
                published_at=self.today_noon,
                author_name="劳瑞",
                author_url=target.target_url,
                tag="wow",
                target=target,
                is_active=True,
            )
            PortalVideo.objects.create(
                title="昨天视频",
                url="https://www.bilibili.com/video/BV1old",
                bvid="BV1old",
                published_at=self.yesterday,
                author_name="劳瑞",
                tag="wow",
                is_active=True,
            )
            for region, now, prev in [("cn", 3840, 3820), ("eu", 3810, 3818), ("us", 3790, 3790)]:
                PortalMplusSeasonCutoff.objects.create(
                    season="season-mn-1",
                    region=region,
                    cutoff_0_1=now,
                    cutoff_0_1_prev=prev,
                    cutoff_1=now - 300,
                    cutoff_1_prev=prev - 300,
                    source_updated_at="2026-07-01T00:00:00Z",
                )

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)

            self.assertTrue(meta["md_path"].endswith(".html"))
            self.assertTrue(os.path.exists(meta["full_path"]))
            row = WowDailyReport.objects.get(report_date=self.report_date)
            self.assertEqual(row.md_path, meta["md_path"])
            ext = json.loads(row.ext_json)
            self.assertEqual(ext["format"], "html")
            self.assertEqual(ext["sections"]["news"]["count"], 2)
            self.assertEqual(ext["sections"]["nga"]["count"], 2)
            self.assertEqual(ext["sections"]["videos"]["count"], 1)
            self.assertEqual(ext["sections"]["cutoffs"]["count"], 3)

            html = open(meta["full_path"], encoding="utf-8").read()
            self.assertIn("魔兽世界当天新闻", html)
            self.assertIn("NGA 热议", html)
            self.assertIn("当前更新的 WoW 视频列表", html)
            self.assertIn("大秘境分数线汇总", html)
            self.assertIn("Wowhead 新闻", html)
            self.assertIn("暴雪蓝贴", html)
            self.assertNotIn("昨天旧闻", html)
            self.assertIn("NGA 高楼", html)
            self.assertIn("NGA 次高楼", html)
            self.assertNotIn("NGA 第三楼", html)
            self.assertIn("今日视频", html)
            self.assertNotIn("昨天视频", html)
            self.assertIn("+20", html)
            self.assertIn("-8", html)

    def test_news_section_excludes_nga_and_video_articles(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(title="正式新闻", source="wowhead", category="news")
            self._article(title="NGA 不应进入新闻", source="nga", category="nga", reply_count=100)
            self._article(title="旧视频文章不应进入新闻", source="bilibili", category="video")

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()

            news_section = html.split('魔兽世界当天新闻', 1)[1].split('NGA 热议', 1)[0]
            self.assertIn("正式新闻", news_section)
            self.assertNotIn("NGA 不应进入新闻", news_section)
            self.assertNotIn("旧视频文章不应进入新闻", news_section)

    def test_article_body_uses_html_blocks_without_dumping_json_payloads(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(
                title="蓝贴正文",
                source="blizzard_tracker",
                category="news",
                description="",
                content=json.dumps([{"original": "Classes", "translated": "职业"}], ensure_ascii=False),
                content_cn=json.dumps([{"original": "Druid", "translated": "德鲁伊"}], ensure_ascii=False),
                content_blocks=json.dumps([
                    {"type": "html", "html": "<p>Hello <strong>adventurers</strong>.</p>"},
                ], ensure_ascii=False),
                content_blocks_cn=json.dumps([
                    {"type": "html", "html": "<p>你好，冒险者。</p>"},
                ], ensure_ascii=False),
            )

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()

            self.assertIn("你好，冒险者。", html)
            self.assertNotIn("&quot;original&quot;", html)
            self.assertNotIn("&quot;translated&quot;", html)
