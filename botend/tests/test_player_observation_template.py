"""Read-only observation UI contract; fixtures are synthetic, not player findings."""
from datetime import datetime, timezone
from html.parser import HTMLParser

from django.template.loader import render_to_string
from django.test import SimpleTestCase, override_settings
from django.utils.html import escape, strip_tags


class ObservationMarkup(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.elements = []
        self.text = []
        self.details_depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if tag == "details":
            self.details_depth += 1

    def handle_endtag(self, tag):
        if tag == "details":
            self.details_depth -= 1

    def handle_data(self, data):
        self.text.append((data, self.details_depth))


@override_settings(TIME_ZONE="UTC")
class PlayerObservationTemplateTests(SimpleTestCase):
    def observation(self):
        return {
            "status": "ready",
            "generated_at": datetime(2026, 10, 13, 9, 30, tzinfo=timezone.utc),
            "model_label": "测试模型",
            "coverage": {
                "best_runs": 8, "recent_runs": 10, "unique_runs": 14,
                "timed_runs": 12, "roster_runs": 11, "comparison_runs": 9,
                "score_characters": 17, "total_characters": 22,
            },
            "season_counts": {"total": 120, "timed": 100},
            "observations": [{
                "label": "测试协作线索", "confidence": "低", "reason": "测试简短解释",
                "limitations": ["测试替代解释"], "evidence": [{
                    "id": "run:123", "text": "测试已验证证据",
                    "run_url": "https://raider.io/mythic-plus-runs/test-season/123-test",
                }],
            }],
            "conclusion": "测试观察结论", "limitations": ["测试覆盖限制"],
        }

    def render(self, observation):
        return render_to_string("portal/spec_detail/_player_observation.html", {"observation": observation})

    def text(self, html):
        return " ".join(strip_tags(html).split())

    def test_ready_exposes_summary_and_backend_counts_with_collapsed_evidence(self):
        observation = self.observation()
        html = self.render(observation)
        text = self.text(html)
        for expected in (
            "玩家观察 · AI 辅助", "测试模型", "生成时间", "2026-10-13 09:30",
            "最佳记录 8", "最近记录 10", "去重记录 14", "限时记录 12",
            "阵容记录 11", "可比较记录 9", "当前成绩角色 17 / 22",
            "赛季已完成 120", "赛季限时 100", "非完整赛季", "当前成绩非历史加分",
            "无法判定付费或工作室", "低置信度", "测试覆盖限制",
        ):
            self.assertIn(expected, text)
        markup = ObservationMarkup(html)
        visible = "".join(text for text, depth in markup.text if not depth)
        collapsed = "".join(text for text, depth in markup.text if depth)
        self.assertIn(observation["conclusion"], visible)
        self.assertIn(observation["observations"][0]["label"], visible)
        for value in ("测试简短解释", "测试替代解释", "run:123", "测试已验证证据"):
            self.assertIn(value, collapsed)
            self.assertNotIn(value, visible)
        details = [attrs for tag, attrs in markup.elements if tag == "details"]
        self.assertTrue(details)
        self.assertTrue(all("open" not in attrs for attrs in details))
        links = [attrs for tag, attrs in markup.elements if tag == "a"]
        self.assertEqual(len(links), 1)
        self.assertEqual(links[0]["href"], observation["observations"][0]["evidence"][0]["run_url"])
        self.assertEqual(links[0]["target"], "_blank")
        self.assertEqual(set(links[0]["rel"].split()), {"noopener", "noreferrer"})
        self.assertFalse({"script", "button", "form", "iframe"} & {tag for tag, _ in markup.elements})

    def test_only_ready_or_stale_can_display_published_findings(self):
        for status, message in (("stale", "已过期"), ("pending", "尚未生成"),
                                ("error", "生成失败"), ("unavailable", "暂不可用"),
                                ("unexpected", "暂不可用")):
            with self.subTest(status=status):
                observation = self.observation()
                observation["status"] = status
                html = self.render(observation)
                self.assertIn(message, html)
                for value in ("测试协作线索", "测试观察结论", "测试已验证证据"):
                    if status == "stale":
                        self.assertIn(value, html)
                    else:
                        self.assertNotIn(value, html)
                if status != "stale":
                    self.assertNotIn("<a ", html)
                    self.assertNotIn("<button", html)

    def test_missing_observation_is_not_a_fabricated_result(self):
        self.assertEqual(self.render(None).strip(), "")
        html = self.render({"status": "pending"})
        self.assertIn("尚未生成", html)
        self.assertNotIn("置信度", html)
        observation = self.observation()
        observation.update(observations=[], conclusion="")
        html = self.render(observation)
        self.assertIn("暂无可展示的观察结论", html)
        self.assertNotIn("测试协作线索", html)

    def test_zero_unknown_and_absent_source_links_remain_distinct(self):
        observation = self.observation()
        observation["coverage"] = dict.fromkeys(observation["coverage"], 0)
        observation["season_counts"] = {"total": 0, "timed": None}
        observation["generated_at"] = None
        observation["observations"][0]["evidence"][0]["run_url"] = ""
        html = self.render(observation)
        text = self.text(html)
        for expected in ("最佳记录 0", "去重记录 0", "当前成绩角色 0 / 0",
                         "赛季已完成 0", "赛季限时 —", "生成时间 —", "测试已验证证据"):
            self.assertIn(expected, text)
        self.assertNotIn("<a ", html)

    def test_every_model_text_field_and_link_attribute_is_autoescaped(self):
        observation = self.observation()
        payload = '<img src=x onerror="alert(1)">'
        observation.update(model_label=payload, conclusion=payload, limitations=[payload])
        observation["observations"] = [{
            "label": payload, "confidence": payload, "reason": payload,
            "limitations": [payload], "evidence": [{
                "id": payload, "text": payload,
                "run_url": 'https://raider.io/test?q=" onmouseover="bad',
            }],
        }]
        html = self.render(observation)
        self.assertNotIn("<img", html)
        self.assertEqual(html.count(str(escape(payload))), 9)
        self.assertNotIn(' onmouseover="bad', html)
        self.assertNotIn("onmouseover", next(attrs for tag, attrs in ObservationMarkup(html).elements if tag == "a"))

    def test_player_page_places_observation_after_activity_before_talents(self):
        html = render_to_string("portal/spec_detail/player_detail.html", {
            "class_name": "Mage", "spec_name": "Fire", "nav": {},
            "player_detail": {
                "observation": self.observation(),
                "raiderio_activity": {"status": "pending"},
                "talent_groups": [{"title": "测试天赋", "nodes": []}],
            },
        })
        self.assertLess(html.index('id="player-rio-activity"'), html.index('id="player-observation"'))
        self.assertLess(html.index('id="player-observation"'), html.index("天赋配置"))
        styles = [attrs.get("href", "") for tag, attrs in ObservationMarkup(html).elements if tag == "link"]
        self.assertTrue(any("portal/css/player-observation.css?v=" in href for href in styles))
