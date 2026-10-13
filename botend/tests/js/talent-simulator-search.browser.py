"""Focused browser contract; requires Django + Playwright with Chromium.

Run: python3 botend/tests/js/talent-simulator-search.browser.py
Kept beside the standalone JS tests, outside Django's test discovery so the
optional browser dependency does not expand the ordinary backend test suite.

The API is intercepted with small render-model fixtures; the real template, CSS,
and unmodified simulator script execute in Chromium. No database/server needed.
"""
import copy
import json
from pathlib import Path
import unittest
from urllib.parse import parse_qs, urlparse

from django.template import Context, Engine
from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parents[3]


def simulator_payload(spec="Blood", hero="", empty=False):
    # Synthetic content exercises the actual display/raw fields from
    # botend.portal.talent_simulator._decorate_render_model, not a locale map.
    nodes = [
        dict(node_key="class:1", tree_type="class", display_name="符文护盾",
             name="Rune Shield", display_desc="吸收伤害 [10%]", points=0),
        dict(node_key="class:2", tree_type="class", display_name="防御姿态",
             display_desc="获得护盾效果", points=0, parents=[1]),
        dict(node_key="class:3", tree_type="class", display_name="当前选项",
             display_desc="无关效果", points=1, selected=True, is_choice_node=True,
             choice_selection=0, choice_options=[
                 dict(display_name="当前选项", display_desc="无关效果"),
                 dict(display_name="备用护盾", display_desc="触发 Arcane Burst"),
             ]),
        dict(node_key="class:4", tree_type="class", display_name="普通攻击",
             display_desc=None, points=0),
    ]
    if spec == "Frost":
        nodes = [dict(node_key="spec:5", tree_type="spec", display_name="冰霜护盾",
                      display_desc="冰霜效果", points=0)]
    if hero:
        nodes.append(dict(node_key="hero:6", tree_type="hero", display_name="英雄护盾",
                          display_desc="英雄效果", points=0))
    for i, node in enumerate(nodes):
        node.update(node_id=i + 1, talent_id=i + 100, x=70 + i * 90, y=100,
                    width=36, height=36, max_points=1, row=1000)
    return dict(
        success=True, class_name="DeathKnight", spec_name=spec,
        class_cn="测试职业", spec_cn=spec, build_code="fixture-code",
        parse_status="success", active_hero_subtree=hero,
        hero_subtrees=[dict(id="1", title="测试英雄一"), dict(id="2", title="测试英雄二")],
        render_model=dict(layout=dict(width=900, height=680), trees=[] if empty else [
            dict(tree_type="class", title="测试树", nodes=nodes, paths=[],
                 panel=dict(x=0, y=0, width=850, height=650)),
        ]),
    )


class TalentSimulatorSearchBrowserTest(unittest.TestCase):
    def test_search_highlights_without_editing_build_and_survives_redraws(self):
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(STATIC_URL="/static/")
            django.setup()
        engine = Engine(
            dirs=[str(ROOT / "templates")],
            libraries={"static": "django.templatetags.static"},
            loaders=[("django.template.loaders.locmem.Loader", {"portal/_header.html": ""}),
                     "django.template.loaders.filesystem.Loader"],
        )
        html = engine.get_template("portal/talent_simulator.html").render(Context(dict(
            mini_mode=False, class_name="DeathKnight", spec_name="Blood",
            default_talent_version={"key": "retail"},
            talent_versions=[dict(key="retail"), dict(key="ptr")],
            specs_payload=[dict(class_name="DeathKnight", class_cn="测试职业", specs=[
                dict(spec_name="Blood", spec_cn="鲜血"),
                dict(spec_name="Frost", spec_cn="冰霜"),
            ])],
        )))
        requests = []
        fail_next = False
        with sync_playwright() as pw, pw.chromium.launch() as browser:
            page = browser.new_page(viewport={"width": 1200, "height": 900})
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))

            def route_request(route):
                nonlocal fail_next
                url = urlparse(route.request.url)
                if url.path == "/portal/api/talents/simulator/":
                    params = parse_qs(url.query)
                    requests.append((route.request.method, params))
                    if fail_next:
                        fail_next = False
                        route.fulfill(status=500, json={"success": False, "error": "测试加载失败"})
                    else:
                        route.fulfill(json=simulator_payload(
                            spec=params.get("spec", ["Blood"])[0],
                            hero=params.get("hero", [""])[0],
                            empty=params.get("version") == ["ptr"],
                        ))
                elif url.path == "/portal/api/talents/simulator/encode/":
                    requests.append((route.request.method, json.loads(route.request.post_data)))
                    route.fulfill(json={"success": True, "build_code": "fixture-edited"})
                elif url.path == "/portal/talents/":
                    route.fulfill(content_type="text/html", body=html)
                elif url.path in ("/static/portal/js/talent_simulator.js",
                                  "/static/portal/css/talent_simulator.css"):
                    route.fulfill(path=str(ROOT / url.path.lstrip("/")))
                else:
                    route.fulfill(body="")

            page.route("**/*", route_request)
            page.goto("https://talent.test/portal/talents/")
            nodes = page.locator(".talent-node-card--tree")
            matches = page.locator(".talent-node-card--tree.is-search-match")
            expect(nodes).to_have_count(4)
            search = page.get_by_role("searchbox", name="搜索天赋")
            expect(search).to_have_count(1)
            layout = search.evaluate("""el => {
                const box = el.getBoundingClientRect();
                const container = el.closest('.talent-tree-search');
                return {width: box.width, height: box.height,
                    aboveTree: container?.nextElementSibling?.id === 'talent-stage-container',
                    inWorkspace: !!container?.closest('.talent-sim-workspace')};
            }""")
            self.assertTrue(layout['aboveTree'] and layout['inWorkspace'])
            self.assertLessEqual(layout['width'], 280)
            self.assertLessEqual(layout['height'], 34)
            status = page.locator("#talent-search-status")
            clear = page.get_by_role("button", name="清空搜索")
            before = page.evaluate("""() => ({url: location.href,
                code: document.querySelector('#talent-build-code-output').textContent,
                nodes: [...document.querySelectorAll('[data-node-key]')].map(n =>
                    [n.dataset.nodeKey, n.getAttribute('aria-pressed'), n.getAttribute('aria-disabled')])})""")
            requests_before = copy.deepcopy(requests)

            search.fill("  护盾  ")
            expect(matches).to_have_count(3)
            expect(status).to_have_text("匹配 3 个节点")
            expect(nodes).to_have_count(4)
            locked = page.locator('[data-node-key="class:2"]')
            expect(locked).to_have_attribute("aria-disabled", "true")
            expect(locked).to_have_css("opacity", "1")
            expect(locked).to_have_css("outline-style", "solid")
            search.fill("  rUnE SHIELD  ")
            expect(matches).to_have_count(1)
            search.fill("arcane burst")
            expect(matches).to_have_count(1)
            expect(matches).to_have_attribute("data-node-key", "class:3")
            search.fill("[10%]")
            expect(matches).to_have_count(1)
            search.fill(".*")
            expect(matches).to_have_count(0)
            expect(status).to_have_text("未找到匹配的天赋")
            search.fill(" ")
            expect(matches).to_have_count(0)
            after = page.evaluate("""() => ({url: location.href,
                code: document.querySelector('#talent-build-code-output').textContent,
                nodes: [...document.querySelectorAll('[data-node-key]')].map(n =>
                    [n.dataset.nodeKey, n.getAttribute('aria-pressed'), n.getAttribute('aria-disabled')])})""")
            self.assertEqual(before, after)
            self.assertEqual(requests_before, requests)

            search.fill("护盾")
            page.locator('[data-node-key="class:1"]').click()
            expect(matches).to_have_count(3)
            expect(page.locator('[data-node-key="class:1"]')).to_have_attribute("aria-pressed", "true")
            expect(page.locator("#talent-build-code-output")).to_have_text("fixture-edited")
            self.assertTrue(any(method == "POST" for method, _ in requests))
            page.locator("#talent-reset-btn").click()
            expect(matches).to_have_count(3)
            clear.click()
            expect(search).to_have_value("")
            expect(search).to_be_focused()
            expect(matches).to_have_count(0)
            expect(locked).to_have_css("opacity", "0.32")

            search.fill("护盾")
            page.locator("#talent-import-input").fill("fixture-import")
            page.locator("#talent-import-btn").click()
            expect(matches).to_have_count(3)
            expect(search).to_have_value("护盾")
            page.locator('[data-hero-subtree="1"]').first.click()
            expect(matches).to_have_count(4)
            page.locator("#talent-spec-select").select_option("Frost")
            expect(matches).to_have_count(1)
            expect(status).to_have_text("匹配 1 个节点")
            page.locator("#talent-version-select").select_option("ptr")
            expect(nodes).to_have_count(0)
            expect(status).to_have_text("当前没有可搜索的天赋节点")
            fail_next = True
            page.locator("#talent-version-select").select_option("retail")
            expect(page.locator("#talent-stage-container")).to_contain_text("测试加载失败")
            expect(status).to_have_text("当前没有可搜索的天赋节点")
            page.locator("#talent-import-btn").click()
            expect(matches).to_have_count(1)
            page.set_viewport_size({"width": 375, "height": 812})
            expect(search).to_be_visible()
            expect(clear).to_be_visible()
            for control in (search, clear, status):
                box = control.bounding_box()
                self.assertGreaterEqual(box["x"], 0)
                self.assertLessEqual(box["x"] + box["width"], 375)
            self.assertEqual(errors, [])



if __name__ == "__main__":
    unittest.main(verbosity=2)
