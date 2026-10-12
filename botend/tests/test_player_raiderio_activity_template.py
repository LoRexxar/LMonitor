"""Rendering contract tests; fixed view models are not production source data."""
from copy import deepcopy
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings
from django.utils.html import strip_tags


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


@override_settings(TIME_ZONE="UTC")
class PlayerRaiderIOActivityTemplateTests(SimpleTestCase):
    def activity(self):
        run = {
            "dungeon_name": "测试副本", "level": 12, "score": 321.5,
            "clear_time": "29:08", "completed_at": datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc),
            "timed": True, "upgrades": 2, "spec_label": "当场测试专精",
            "role_label": "输出", "url": "https://raider.io/mythic-plus-runs/test-season/123-test",
        }
        return {
            "status": "ready", "source_label": "Raider.IO", "season_name": "测试赛季",
            "rio_season": "test-season", "fetched_at": datetime(2026, 10, 12, 9, 45, tzinfo=timezone.utc),
            "source_updated_at": datetime(2026, 10, 11, 9, 15, tzinfo=timezone.utc),
            "best_runs": {"status": "ready", "rows": [deepcopy(run)]},
            "recent_runs": {"status": "ready", "rows": [deepcopy(run)]},
            "dungeon_counts": {"status": "ready", "rows": [
                {"dungeon_name": "测试副本", "total": 20, "timed": 15, "timed_percent": 75.0},
            ], "total": 20, "timed": 15, "timed_percent": 75.0},
            "raid_progression": {"status": "ready", "rows": [
                {"name": "测试团本", "total_bosses": 9, "normal": 9, "heroic": 8, "mythic": 3},
            ]},
        }

    def render(self, activity):
        return render_to_string("portal/spec_detail/_player_raiderio_activity.html", {"activity": activity})

    def text(self, html):
        return " ".join(unescape(strip_tags(html)).split())

    def test_player_page_places_four_modules_between_stats_and_talents(self):
        activity = self.activity()
        html = render_to_string("portal/spec_detail/player_detail.html", {
            "class_name": "Mage", "spec_name": "Fire", "nav": {},
            "player_detail": {"raiderio_activity": activity, "talent_groups": [{"title": "测试天赋", "nodes": []}]},
        })
        self.assertLess(html.index("属性面板"), html.index('id="player-rio-activity"'))
        self.assertLess(html.index('id="player-rio-activity"'), html.index("天赋配置"))
        self.assertIn("portal/css/player-raiderio-activity.css", html)
        fragment = self.render(activity)
        text = self.text(fragment)
        for label in ("各副本最佳成绩", "最近大秘境记录", "赛季副本经历", "当前团本进度",
                      "角色赛季记录", "可跨专精", "最高得分", "并非完整历史", "不含未完成尝试",
                      "Raider.IO", "测试赛季", "测试副本", "当场测试专精", "输出", "29:08", "321.5",
                      "源更新时间", "2026-10-11 09:15", "本站采集时间", "2026-10-12 09:45",
                      "2026-10-10 08:30", "普通", "9/9", "英雄", "8/9", "史诗", "3/9", "75.0%"):
            self.assertIn(label, text)
        self.assertIn('<details', fragment)
        links = Links(fragment).links
        self.assertEqual(len(links), 2)
        for link in links:
            self.assertEqual(link["href"], activity["best_runs"]["rows"][0]["url"])
            self.assertEqual(link["target"], "_blank")
            self.assertEqual(set(link["rel"].split()), {"noopener", "noreferrer"})

    def test_missing_empty_and_stale_remain_distinct_without_losing_last_good(self):
        activity = self.activity()
        for key in ("best_runs", "recent_runs", "dungeon_counts", "raid_progression"):
            activity[key]["rows"] = []
            activity[key]["status"] = "missing"
        activity["source_updated_at"] = activity["fetched_at"] = None
        activity["status"] = "partial"
        missing = self.text(self.render(activity))
        self.assertIn("部分数据缺失", missing)
        self.assertEqual(missing.count("来源未提供"), 4)
        self.assertIn("源更新时间 —", missing)
        self.assertIn("本站采集时间 —", missing)
        for key in ("best_runs", "recent_runs", "dungeon_counts", "raid_progression"):
            activity[key]["status"] = "ready"
        empty = self.text(self.render(activity))
        self.assertNotIn("来源未提供", empty)
        for label in ("暂无最佳记录", "暂无收录记录", "暂无副本次数记录", "暂无当前团本进度"):
            self.assertIn(label, empty)
        for status, label in (("pending", "等待采集"), ("unavailable", "暂不可用"), ("stale", "数据已过期")):
            with self.subTest(status=status):
                activity = self.activity()
                activity["status"] = status
                text = self.text(self.render(activity))
                self.assertIn(label, text)
                self.assertIn("测试副本", text)

    def test_zero_unknown_tristate_and_untrusted_text_are_rendered_faithfully(self):
        activity = self.activity()
        zero = activity["best_runs"]["rows"][0]
        zero.update(score=0, upgrades=0, timed=False, spec_label="", role_label="")
        unknown = activity["recent_runs"]["rows"][0]
        unknown.update(score=None, upgrades=None, timed=None, completed_at=None,
                       dungeon_name='<script>alert("name")</script>', spec_label='<img src=x onerror="bad">')
        activity["dungeon_counts"].update(total=0, timed=0, timed_percent=None)
        activity["dungeon_counts"]["rows"] = [
            {"dungeon_name": "零次数副本", "total": 0, "timed": 0, "timed_percent": 0.0},
            {"dungeon_name": "未知次数副本", "total": None, "timed": None, "timed_percent": None},
        ]
        activity["raid_progression"]["rows"][0].update(normal=0, heroic=None, mythic=0, total_bosses=None)
        html = self.render(activity)
        text = self.text(html)
        for label in ("得分 0.0", "得分 —", "升级 0", "升级 —", "未限时", "限时未知",
                      "当场专精 —", "职责 —", "完成时间 —", "已完成 0", "限时 0", "限时比例 0.0%",
                      "已完成 —", "限时 —", "限时比例 —", "普通 0/—", "英雄 —/—", "史诗 0/—"):
            self.assertIn(label, text)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
