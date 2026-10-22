"""Rendering contract tests; fixed view models are not production source data."""
from copy import deepcopy
from datetime import datetime, timezone
from html import unescape
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import TestCase, override_settings
from django.utils.html import strip_tags

from botend.journal_models import JournalEncounter, JournalInstance, JournalRelease, JournalState
from botend.models import SeasonMeta
from botend.services.player_raid_progression import REFERENCE, physical_scope, project_progress


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.links.append(dict(attrs))


class RosterMembers(HTMLParser):
    """Observe rendered members and whether a disclosure hides them."""

    def __init__(self, html):
        super().__init__()
        self.members = []
        self.details_depth = 0
        self.current = None
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.details_depth += 1
        if tag == "li" and "rio-roster__member" in attrs.get("class", "").split():
            self.current = {"text": "", "hidden": bool(self.details_depth), "links": []}
            self.members.append(self.current)
        if tag == "a" and self.current is not None:
            self.current["links"].append(attrs)

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1
        if tag == "li":
            self.current = None

    def handle_data(self, data):
        if self.current is not None:
            self.current["text"] += data


@override_settings(TIME_ZONE="UTC")
class PlayerRaiderIOActivityTemplateTests(TestCase):
    def setUp(self):
        # Render the actual physical-raid projection: missing character progress
        # retains the known raid and denominator, unlike a missing M+ module.
        encounters = [{'id': bid} for bid in range(1, 10)]
        self.season = SeasonMeta.objects.create(
            season_key='template-test', season_name='测试赛季', rio_season='test-season',
            mplus_zone_id=1, raid_zone_id=2, raid_encounters=encounters,
            raid_zones=[{'id': 2, 'encounters': encounters}])
        release = JournalRelease.objects.create(build='template-test', status='ready')
        JournalState.objects.create(active_release=release)
        instance = JournalInstance.objects.create(
            release=release, journal_id=10, name='测试团本', kind='raid')
        for encounter in encounters:
            JournalEncounter.objects.create(instance=instance, journal_id=encounter['id'],
                name=str(encounter['id']), payload={'dungeon_encounter_id': encounter['id']})
        self.season.raid_zones[0][REFERENCE] = {
            'scope_fingerprint': physical_scope(self.season)['fingerprint'],
            'bindings': [{'journal_id': 10, 'slug': 'test-raid'}]}
        self.season.save(update_fields=['raid_zones'])

    def raid_progression(self, **counts):
        return project_progress(self.season, {'test-raid': {
            'total_bosses': 9, 'normal_bosses_killed': 9,
            'heroic_bosses_killed': 8, 'mythic_bosses_killed': 3, **counts}})

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
            "raid_progression": self.raid_progression(),
        }

    def render(self, activity):
        return render_to_string("portal/spec_detail/_player_raiderio_activity.html", {"activity": activity})

    def text(self, html):
        return " ".join(unescape(strip_tags(html)).split())

    def roster(self):
        return [
            {"name": name, "class_label": class_label, "spec_label": spec_label,
             "role_label": role_label, "class_slug": class_slug, "spec_slug": spec_slug,
             "profile_url": f"https://raider.io/characters/eu/test/{index}" if index else "",
             "character_url": f"/portal/character/?player_id={index}" if index else ""}
            for index, (name, class_label, spec_label, role_label, class_slug, spec_slug) in enumerate([
                ("坦克玩家", "战士", "防护", "坦克", "Warrior", "Protection"),
                ("治疗玩家", "牧师", "神圣", "治疗", "Priest", "Holy"),
                ("火法玩家", "法师", "火焰", "输出", "Mage", "Fire"),
                ("鸟德玩家", "德鲁伊", "平衡", "输出", "Druid", "Balance"),
                ('<script>alert("玩家")</script>', "潜行者", "狂徒", "输出", "Rogue", "Outlaw"),
            ])
        ]

    def test_best_run_roster_is_visible_localized_and_keeps_original_result(self):
        for timed, title in ((True, "限时阵容"), (False, "通关阵容"), (None, "队伍阵容")):
            with self.subTest(timed=timed):
                activity = self.activity()
                run = activity["best_runs"]["rows"][0]
                run.update(timed=timed, roster_status="ready", roster=self.roster())
                # The same record in recent runs must not duplicate the roster.
                activity["recent_runs"]["rows"] = [deepcopy(run)]
                html = self.render(activity)
                text = self.text(html)
                self.assertEqual(text.count(title), 1)
                self.assertIn("各本最高得分记录，非最高层记录", text)
                self.assertIn("得分 321.5", text)
                self.assertIn("29:08", text)
                self.assertEqual(sum(link["href"] == run["url"] for link in Links(html).links), 2)
                members = RosterMembers(html).members
                self.assertEqual(len(members), 5)
                for rendered, source in zip(members, run["roster"]):
                    self.assertFalse(rendered["hidden"])
                    for field in ("name", "class_label", "spec_label", "role_label"):
                        self.assertIn(source[field], rendered["text"])
                    if source["character_url"]:
                        self.assertEqual(len(rendered["links"]), 1)
                        link = rendered["links"][0]
                        self.assertEqual(link["href"], source["character_url"])
                        self.assertNotIn("target", link)
                    else:
                        self.assertEqual(rendered["links"], [])
                self.assertNotIn("<script>", html)

    def test_best_run_single_line_summary_drops_redundant_facts_only_for_best(self):
        run = self.activity()['best_runs']['rows'][0]
        run.update(roster_status='ready', roster=self.roster())
        template = 'portal/spec_detail/_player_raiderio_run.html'
        compact = render_to_string(template, {'run': run, 'show_roster': True})
        recent = render_to_string(template, {'run': run, 'show_roster': False})
        for label in ('升级', '当场专精', '职责 ', '完成时间'):
            self.assertNotIn(label, self.text(compact))
            self.assertIn(label, self.text(recent))
        for value in ('测试副本', '+12', '321.5', '已限时', '29:08'):
            self.assertIn(value, self.text(compact))
        self.assertIn('class="rio-run rio-run--compact"', compact)
        self.assertEqual(len(RosterMembers(compact).members), 5)
        self.assertEqual(len(RosterMembers(recent).members), 0)

    def test_roster_missing_states_are_explicit_and_recent_has_no_placeholder(self):
        for status, roster, message in (
            ("pending", [], "阵容待采集"),
            ("unavailable", [], "阵容暂不可用（来源未公开或未提供）"),
            ("ready", [], "来源未提供阵容"),
            ("stale", [], "阵容已过期，暂无可用成员，等待刷新"),
            ("stale", self.roster(), "阵容已过期，保留上次可用记录，等待刷新"),
        ):
            with self.subTest(status=status, has_roster=bool(roster)):
                activity = self.activity()
                run = activity["best_runs"]["rows"][0]
                run.update(roster_status=status, roster=roster)
                activity["recent_runs"]["rows"][0].update(roster_status="pending", roster=[])
                html = self.render(activity)
                text = self.text(html)
                self.assertEqual(text.count(message), 1)
                self.assertEqual(len(RosterMembers(html).members), len(roster))
                self.assertEqual(text.count("阵容待采集"), 1 if status == "pending" else 0)

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
        messages = {
            'best_runs': ('来源未提供最佳记录', '暂无最佳记录'),
            'recent_runs': ('来源未提供近期记录', '暂无收录记录'),
            'dungeon_counts': ('来源未提供赛季副本次数', '暂无副本次数记录'),
        }
        for key in messages:
            activity[key]["rows"] = []
            activity[key]["status"] = "missing"
        activity['dungeon_counts'].update(total=None, timed=None, timed_percent=None)
        activity['raid_progression'] = project_progress(self.season, {})
        self.assertEqual(activity['raid_progression']['status'], 'missing')
        self.assertEqual(activity['raid_progression']['rows'], [
            {'name': '测试团本', 'total_bosses': 9, 'normal': None, 'heroic': None, 'mythic': None}])
        activity["source_updated_at"] = activity["fetched_at"] = None
        activity["status"] = "partial"
        missing = self.text(self.render(activity))
        self.assertIn("部分数据缺失", missing)
        for key, (missing_message, empty_message) in messages.items():
            with self.subTest(module=key, state='missing'):
                self.assertIn(missing_message, missing)
                self.assertNotIn(empty_message, missing)
        self.assertIn('测试团本', missing)
        for difficulty in ('普通', '英雄', '史诗'):
            self.assertIn(f'{difficulty} —/9', missing)
        self.assertNotIn('0/9', missing)
        self.assertNotIn('暂无当前团本进度', missing)
        self.assertIn("源更新时间 —", missing)
        self.assertIn("本站采集时间 —", missing)
        for key in messages:
            activity[key]["status"] = "ready"
        activity['dungeon_counts'].update(total=0, timed=0, timed_percent=None)
        empty = self.text(self.render(activity))
        self.assertNotIn("来源未提供", empty)
        for key, (_, empty_message) in messages.items():
            with self.subTest(module=key, state='empty'):
                self.assertIn(empty_message, empty)
        # A configured raid with no character data is unknown, not an empty
        # ready module. An explicit successful zero response is distinct.
        self.assertIn('普通 —/9', empty)
        activity['raid_progression'] = self.raid_progression(
            normal_bosses_killed=0, heroic_bosses_killed=0, mythic_bosses_killed=0)
        self.assertEqual(activity['raid_progression']['status'], 'ready')
        zero = self.text(self.render(activity))
        for difficulty in ('普通', '英雄', '史诗'):
            self.assertIn(f'{difficulty} 0/9', zero)
        # An unresolved physical scope really produces no rows. It must show
        # both the mapping warning and empty placeholder, not invent progress.
        self.season.raid_encounters.append({'id': 999})
        activity['raid_progression'] = project_progress(self.season, {})
        self.assertEqual(activity['raid_progression'], {'status': 'unresolved', 'rows': []})
        unresolved = self.text(self.render(activity))
        self.assertIn('当前团本来源映射待校验', unresolved)
        self.assertIn('暂无当前团本进度', unresolved)
        self.assertNotIn('0/9', unresolved)
        self.season.refresh_from_db()
        for status, label in (("pending", "等待采集"), ("unavailable", "暂不可用"), ("stale", "数据已过期")):
            with self.subTest(status=status):
                activity = self.activity()
                activity["status"] = status
                text = self.text(self.render(activity))
                self.assertIn(label, text)
                self.assertIn("测试副本", text)
                self.assertIn('测试团本', text)
                self.assertIn('史诗 3/9', text)

    def test_zero_unknown_tristate_and_untrusted_text_are_rendered_faithfully(self):
        activity = self.activity()
        zero = activity["best_runs"]["rows"][0]
        zero.update(score=0, upgrades=0, timed=False, spec_label="", role_label="")
        # Full metadata remains a recent-run contract, not the compact best row.
        activity["recent_runs"]["rows"].append(deepcopy(zero))
        unknown = activity["recent_runs"]["rows"][0]
        unknown.update(score=None, upgrades=None, timed=None, completed_at=None,
                       dungeon_name='<script>alert("name")</script>', spec_label='<img src=x onerror="bad">')
        activity["dungeon_counts"].update(total=0, timed=0, timed_percent=None)
        activity["dungeon_counts"]["rows"] = [
            {"dungeon_name": "零次数副本", "total": 0, "timed": 0, "timed_percent": 0.0},
            {"dungeon_name": "未知次数副本", "total": None, "timed": None, "timed_percent": None},
        ]
        activity['raid_progression'] = self.raid_progression(
            normal_bosses_killed=0, heroic_bosses_killed=None, mythic_bosses_killed=0)
        self.assertEqual(activity['raid_progression']['status'], 'partial')
        html = self.render(activity)
        text = self.text(html)
        for label in ("得分 0.0", "得分 —", "升级 0", "升级 —", "未限时", "限时未知",
                      "当场专精 —", "职责 —", "完成时间 —", "已完成 0", "限时 0", "限时比例 0.0%",
                      "已完成 —", "限时 —", "限时比例 —", "普通 0/9", "英雄 —/9", "史诗 0/9"):
            self.assertIn(label, text)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img", html)
