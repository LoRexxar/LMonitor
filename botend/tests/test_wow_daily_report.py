import json
import os
import tempfile
from datetime import date, datetime, time, timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
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


class WowDailyReportDashboardListTest(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='daily-report-dashboard', password='test-password')
        self.client.force_login(self.user)

    def test_list_exposes_the_same_portal_report_url(self):
        WowDailyReport.objects.create(
            report_date=date(2026, 7, 1),
            md_path='portal/reports/wow_daily_report_2026-07-01.html',
        )

        response = self.client.get('/api/wow-daily-report/list/')

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'])
        self.assertEqual(payload['data'][0]['portal_url'], '/portal/reports/wow_daily_report_2026-07-01.html')

    def test_latest_public_report_url_changes_only_when_the_edition_changes(self):
        from datetime import timedelta
        from urllib.parse import urlsplit, parse_qs
        row = WowDailyReport.objects.create(report_date=date(2026, 7, 2),
            md_path='portal/reports/wow_daily_report_2026-07-02.html')
        first = self.client.get('/portal/api/daily-report/latest/').json()['data']['url']
        parts = urlsplit(first)
        self.assertEqual(parts.path, '/portal/reports/wow_daily_report_2026-07-02.html')
        self.assertTrue(parse_qs(parts.query).get('v'))
        self.assertEqual(self.client.get('/portal/api/daily-report/latest/').json()['data']['url'], first)
        WowDailyReport.objects.filter(pk=row.pk).update(updated_at=row.updated_at+timedelta(seconds=1))
        second = self.client.get('/portal/api/daily-report/latest/').json()['data']['url']
        self.assertNotEqual(second, first)
        self.assertEqual(urlsplit(second).path, parts.path)
        self.assertEqual(WowDailyReport.objects.get(pk=row.pk).md_path, row.md_path)

    def test_latest_report_exposes_new_source_sections(self):
        WowDailyReport.objects.create(report_date=date(2026, 7, 1),
            md_path='portal/reports/wow_daily_report_2026-07-01.html',
            ext_json=json.dumps({'sections': {'events': {'count': 2},
                'today': {'count': 13}, 'cutoffs': {'count': 3}}}))
        data = self.client.get('/portal/api/daily-report/latest/').json()['data']
        self.assertEqual([s['key'] for s in data['sections']], ['events', 'today', 'cutoffs'])
        self.assertEqual([s['count'] for s in data['sections']], [2, 13, 3])
        self.assertEqual(data['sections'][1]['label'], '北美轮换')


class WowDailyReportCutoffSnapshotTest(TestCase):
    def test_frozen_cutoffs_survive_central_season_and_region_changes(self):
        from bs4 import BeautifulSoup

        day = date(2026, 11, 3)
        season = 'season-mn-2'
        frozen = {region: {'season': season, 'cutoff_0_1': 100.01 + n,
                           'cutoff_1': None if region == 'us' else 90.02 + n,
                           'source_updated_at': '2026-11-03 20:58',
                           'source_timezone': 'Asia/Shanghai'}
                  for n, region in enumerate(('cn', 'eu', 'us'))}
        baseline = {region: {**snapshot, 'cutoff_0_1': snapshot['cutoff_0_1'] - 1}
                    for region, snapshot in frozen.items()}
        cases = ('new_season', 'missing_region', 'no_central_rows', 'empty_central_values',
                 'fill_same_season', 'reject_future_fill', 'reject_new_season_fill')
        with tempfile.TemporaryDirectory() as base_dir, override_settings(BASE_DIR=base_dir):
            for case in cases:
                with self.subTest(case=case):
                    WowDailyReport.objects.all().delete()
                    PortalMplusSeasonCutoff.objects.all().delete()
                    saved = {r: v for r, v in frozen.items()
                             if r != 'us' or case not in ('fill_same_season', 'reject_future_fill',
                                                         'reject_new_season_fill')}
                    prior = WowDailyReport.objects.create(
                        report_date=day - timedelta(days=1), md_path='prior.html',
                        ext_json=json.dumps({'cutoffs': {'by_region': baseline}}))
                    WowDailyReport.objects.create(report_date=day, md_path='current.html',
                        ext_json=json.dumps({'cutoffs': {'by_region': saved}}))
                    for region in ('cn', 'eu', 'us'):
                        if case == 'no_central_rows' or (case == 'missing_region' and region == 'eu'):
                            continue
                        PortalMplusSeasonCutoff.objects.create(
                            season='season-mn-3' if case in ('new_season', 'reject_new_season_fill') else season,
                            region=region, cutoff_0_1=None if case == 'empty_central_values' else 9999,
                            cutoff_1=None if case == 'empty_central_values' else 8888,
                            source_updated_at='2026-11-04T01:00:00Z' if case == 'reject_future_fill'
                                              else '2026-11-03T12:58:00Z')
                    central_before = list(PortalMplusSeasonCutoff.objects.order_by('pk').values())
                    expected = dict(saved)
                    if case == 'fill_same_season':
                        expected['us'] = {**frozen['us'], 'cutoff_0_1': 9999, 'cutoff_1': 8888}
                    meta = generate_wow_daily_report(report_date=day, use_llm=False)
                    self.assertEqual(meta['ext']['cutoffs']['by_region'], expected)
                    report = WowDailyReport.objects.get(report_date=day)
                    self.assertEqual(json.loads(report.ext_json), meta['ext'])
                    with open(meta['full_path'], encoding='utf-8') as report_file:
                        section = BeautifulSoup(report_file.read(), 'html.parser').select_one(
                            'section[data-section="cutoffs"]')
                    self.assertEqual(len(section.select('tbody tr')), len(expected))
                    self.assertIn('+1.00', section.get_text())
                    self.assertNotIn('season-mn-3', str(section))
                    prior.refresh_from_db()
                    self.assertEqual(json.loads(prior.ext_json)['cutoffs']['by_region'], baseline)
                    self.assertEqual(list(PortalMplusSeasonCutoff.objects.order_by('pk').values()),
                                     central_before)

    def test_daily_cutoffs_compare_exact_yesterday_in_live_and_frozen_editions(self):
        from bs4 import BeautifulSoup

        day = date(2026, 11, 3)
        season = 'season-mn-2'
        labels = {'cn': '国服', 'eu': '欧服', 'us': '美服'}
        production = {'cn': (4133.81, 3909.58), 'eu': (4089.91, 3861.94),
                      'us': (4037.60, 3778.44)}
        yesterday = {'cn': {'season': season, 'cutoff_0_1': 4132.79, 'cutoff_1': 3906.71},
                     'eu': {'season': season, 'cutoff_0_1': 4086.99, 'cutoff_1': 3853.74},
                     'us': {'season': season, 'cutoff_0_1': 4033.46, 'cutoff_1': 3775.96}}
        scenarios = [
            ('production', production, yesterday, 1, {
                'cn': ['4133.81', '4132.79', '+1.02', '3909.58', '3906.71', '+2.87'],
                'eu': ['4089.91', '4086.99', '+2.92', '3861.94', '3853.74', '+8.20'],
                'us': ['4037.60', '4033.46', '+4.14', '3778.44', '3775.96', '+2.48'],
            }),
            ('missing_yesterday', {'cn': production['cn']}, yesterday, 2, {
                'cn': ['4133.81', '-', '-', '3909.58', '-', '-'],
            }),
            ('season_and_field_identity', production, {
                'cn': {**yesterday['cn'], 'season': 'old-season'},
                'eu': {'season': season, 'cutoff_0_1': 4086.99},
                # No US snapshot: another region cannot stand in for it.
            }, 1, {
                'cn': ['4133.81', '-', '-', '3909.58', '-', '-'],
                'eu': ['4089.91', '4086.99', '+2.92', '3861.94', '-', '-'],
                'us': ['4037.60', '-', '-', '3778.44', '-', '-'],
            }),
            ('zero_negative_and_raw_decimal', {'cn': (0, 1.01), 'eu': (10.014, 20.01),
                                               'us': (3, 4)}, {
                'cn': {'season': season, 'cutoff_0_1': 0, 'cutoff_1': 1.03},
                'eu': {'season': season, 'cutoff_0_1': 10.006, 'cutoff_1': 20},
                'us': {'season': season, 'cutoff_0_1': 3.02, 'cutoff_1': 4},
            }, 1, {
                'cn': ['0.00', '0.00', '0.00', '1.01', '1.03', '-0.02'],
                'eu': ['10.01', '10.01', '+0.01', '20.01', '20.00', '+0.01'],
                'us': ['3.00', '3.02', '-0.02', '4.00', '4.00', '0.00'],
            }),
        ]
        with tempfile.TemporaryDirectory() as base_dir, override_settings(BASE_DIR=base_dir):
            for name, current, baseline, days_back, expected in scenarios:
                with self.subTest(scenario=name):
                    WowDailyReport.objects.all().delete()
                    PortalMplusSeasonCutoff.objects.all().delete()
                    prior = WowDailyReport.objects.create(
                        report_date=day - timedelta(days=days_back),
                        md_path='portal/reports/prior.html',
                        ext_json=json.dumps({'cutoffs': {'by_region': baseline}}),
                    )
                    prior_ext = prior.ext_json
                    # Legacy source clocks without a saved zone must stay unknown.
                    legacy_clock = name == 'zero_negative_and_raw_decimal'
                    source = '2026-11-03 20:58' if legacy_clock else '2026-11-03T12:58:00Z'
                    snapshots = {}
                    for region, (score, score_1) in current.items():
                        PortalMplusSeasonCutoff.objects.create(
                            season=season, region=region, cutoff_0_1=score, cutoff_1=score_1,
                            cutoff_0_1_prev=score, cutoff_1_prev=score_1,
                            source_updated_at=source,
                        )
                        snapshots[region] = {'season': season, 'cutoff_0_1': score,
                            'cutoff_1': score_1, 'source_updated_at': '2026-11-03 20:58',
                            'source_timezone': '' if legacy_clock else 'Asia/Shanghai'}
                    for branch in ('live', 'frozen_future', 'frozen_same_day'):
                        with self.subTest(branch=branch):
                            if branch != 'live':
                                # Both future and still-eligible newer central facts must
                                # leave this edition's already published values intact.
                                PortalMplusSeasonCutoff.objects.update(
                                    cutoff_0_1=9999, cutoff_1=8888,
                                    source_updated_at=('2026-11-04T07:03:43Z'
                                        if branch == 'frozen_future' else '2026-11-03T13:00:00Z'),
                                )
                            central_before = list(PortalMplusSeasonCutoff.objects.order_by('pk').values())
                            meta = generate_wow_daily_report(report_date=day, use_llm=False)
                            published = WowDailyReport.objects.get(report_date=day)
                            ext = json.loads(published.ext_json)
                            self.assertEqual(ext, meta['ext'])
                            self.assertEqual(ext['cutoffs']['by_region'], snapshots)
                            with open(meta['full_path'], encoding='utf-8') as report_file:
                                document = report_file.read()
                            section = BeautifulSoup(document, 'html.parser').select_one(
                                'section[data-section="cutoffs"]')
                            self.assertIsNotNone(section)
                            rows = [row.find_all('td') for row in section.select('tbody tr')]
                            actual = {cells[0].get_text(): [c.get_text() for c in cells[1:7]]
                                      for cells in rows}
                            self.assertEqual(actual, {labels[r]: cells for r, cells in expected.items()})
                            self.assertIn('较昨日已发布快照', section.get_text())
                            self.assertIn('2026-11-02', section.get_text())
                            self.assertIn('采样时点可能不同', section.get_text())
                            self.assertIn('昨日已发布', section.select_one('thead').get_text())
                            self.assertNotIn('snapshot_note', str(section))
                            self.assertNotIn('原记录未保存来源时区', str(section))
                            self.assertEqual(list(PortalMplusSeasonCutoff.objects.order_by('pk').values()),
                                             central_before)
                            prior.refresh_from_db()
                            self.assertEqual(prior.ext_json, prior_ext)
            with self.subTest(scenario='future_without_frozen'):
                WowDailyReport.objects.filter(report_date=day).delete()
                PortalMplusSeasonCutoff.objects.update(source_updated_at='2026-11-04T07:03:43Z')
                meta = generate_wow_daily_report(report_date=day, use_llm=False)
                self.assertEqual(meta['ext']['cutoffs']['by_region'], {})
                self.assertEqual(meta['ext']['sections']['cutoffs']['count'], 0)


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

            # Daily deltas compare published editions, not the collector's latch.
            WowDailyReport.objects.create(
                report_date=self.report_date - timedelta(days=1),
                md_path='portal/reports/yesterday.html',
                ext_json=json.dumps({'cutoffs': {'by_region': {
                    region: {'season': 'season-mn-1', 'cutoff_0_1': prev, 'cutoff_1': prev - 300}
                    for region, prev in [('cn', 3820), ('eu', 3818), ('us', 3790)]
                }}}),
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
            self.assertFalse(ext["sections"]["cutoffs"]["summary_llm_ok"])
            self.assertEqual(ext["sections"]["cutoffs"]["summary_error"], "summary_disabled")

            html = open(meta["full_path"], encoding="utf-8").read()
            self.assertIn("魔兽世界当天新闻", html)
            self.assertIn("NGA 热议", html)
            self.assertIn("当前更新的 WoW 视频列表", html)
            self.assertIn("大秘境分数线汇总", html)
            cutoff_section = html.split('大秘境分数线汇总', 1)[1]
            self.assertNotIn('section-summary', cutoff_section)
            self.assertIn("Wowhead 新闻", html)
            self.assertIn("暴雪蓝贴", html)
            self.assertIn("来源：wowhead", html)
            self.assertIn("原文链接", html)
            self.assertIn("内容摘要：", html)
            self.assertIn("新闻原文", html)
            self.assertIn("这是一段新闻原文内容，应该出现在日报模块中。", html)
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

    def test_news_section_includes_wowhead_and_excludes_exwind(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(title="Wowhead 应展示", source="wowhead", category="news")
            self._article(title="Exwind 不展示", source="exwind", category="news")
            self._article(title="暴雪追踪也展示", source="blizzard_tracker", category="bluepost")

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()
            news_section = html.split('魔兽世界当天新闻', 1)[1].split('NGA 热议', 1)[0]

            self.assertIn("Wowhead 应展示", news_section)
            self.assertIn("暴雪追踪也展示", news_section)
            self.assertNotIn("Exwind 不展示", news_section)

    def test_video_section_hidden_when_no_today_videos(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(title="Wowhead 新闻", source="wowhead", category="news")
            PortalVideo.objects.create(
                title="昨天视频",
                url="https://www.bilibili.com/video/BV1old",
                bvid="BV1old",
                published_at=self.yesterday,
                author_name="劳瑞",
                tag="wow",
                is_active=True,
            )

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()
            ext = meta["ext"]

            self.assertNotIn("videos", ext["sections"])
            self.assertNotIn("当前更新的 WoW 视频列表", html)
            self.assertNotIn("今日暂无新视频", html)
            self.assertNotIn("昨天视频", html)

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
                    {"type": "html", "html": "<p>你好，<strong>冒险者</strong>。</p><ul><li>保留列表格式</li></ul>"},
                ], ensure_ascii=False),
            )

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()

            self.assertIn("你好，", html)
            self.assertIn("<strong>冒险者</strong>", html)
            self.assertIn("<ul><li>保留列表格式</li></ul>", html)
            self.assertNotIn("&quot;original&quot;", html)
            self.assertNotIn("&quot;translated&quot;", html)

    def test_daily_report_converts_nga_img_bbcode_to_images(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(
                title="NGA 带图主楼",
                source="nga",
                category="nga",
                reply_count=999,
                description="",
                content="开头[img]https://img.example.com/a.jpg[/img]结尾",
            )

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()

            self.assertIn('src="https://img.example.com/a.jpg"', html)
            self.assertIn('alt="NGA 图片"', html)
            self.assertNotIn("[img]", html)
            self.assertNotIn("[/img]", html)

    def test_news_body_falls_back_to_full_description_without_truncation(self):
        long_description = "正文开始 " + ("完整正文内容 " * 160) + " 正文结束"
        with override_settings(BASE_DIR=self.base_dir):
            self._article(
                title="只有描述的 Wowhead 新闻",
                source="wowhead",
                category="news",
                content="",
                content_cn="",
                content_blocks="",
                content_blocks_cn="",
                description=long_description,
            )

            meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
            html = open(meta["full_path"], encoding="utf-8").read()

            self.assertIn("只有描述的 Wowhead 新闻", html)
            self.assertIn("正文开始", html)
            self.assertIn("正文结束", html)
            self.assertNotIn("暂无正文片段", html)

    def test_editorial_renderer_keeps_structured_html_without_visual_ai(self):
        with override_settings(BASE_DIR=self.base_dir):
            self._article(title="格式新闻", source="blizzard_tracker", category="news",
                description="", content_blocks_cn=json.dumps([{"type": "html",
                    "html": "<p>你好，<strong>冒险者</strong>。</p><table><tr><td>数值</td></tr></table>"}], ensure_ascii=False))
            with patch("botend.wow_daily_report.html_design.GLMClient") as visual_client:
                meta = generate_wow_daily_report(report_date=self.report_date, use_llm=False)
                visual_client.assert_not_called()
            html = open(meta["full_path"], encoding="utf-8").read()
            self.assertIn("<strong>冒险者</strong>", html)
            self.assertIn("<table><tr><td>数值</td></tr></table>", html)
            self.assertIn("站内阅读", html)
            self.assertIn("内容摘要：", html)
            self.assertNotIn("AI 总结：", html)
            self.assertEqual(meta["ext"]["html_renderer"], "daily_editorial_v2")

    def test_legacy_visual_utility_still_receives_structured_html(self):
        from botend.wow_daily_report.html_design import render_daily_html_with_skill
        document = "<!doctype html><html><body><main>完整原文</main></body></html>"
        with patch("botend.wow_daily_report.html_design.GLMClient") as client:
            client.return_value.send_message.return_value = document
            result, meta = render_daily_html_with_skill(self.report_date,
                [{"key": "news", "items": [{"body_html": "<strong>冒险者</strong>"}]}])
            self.assertIn('"body_html"', client.return_value.send_message.call_args.args[0])
            self.assertIn("<strong>冒险者</strong>", client.return_value.send_message.call_args.args[0])
            self.assertEqual(result, document)
            self.assertEqual(meta["renderer"], "ai_html_skill")
