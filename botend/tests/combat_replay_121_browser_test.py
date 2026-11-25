"""显式运行的 12.1 正式服实录验收。固定公开脱敏样本的提交和原文摘要。"""

import hashlib
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import override_settings
from django.urls import reverse

from botend.models import CombatLogReport, MythicDungeon, MythicDungeonDataVersion, WowSpellSnapshot
from botend.services.combat_log_facts import DAMAGE
from botend.services import combat_log_analysis as analysis
from botend.services import combat_log_archive as archive
from botend.services import combat_replay_activity as activity

SOURCE = (
    "https://raw.githubusercontent.com/Sharpened-Banana/postmortem/"
    "cdea42359446f1f6eed2f51f6cfa2c1b54bce7b3/tests/fixtures/real_logs/"
    "abandoned_key_instance_mismatch.txt"
)
SHA256 = "f8254d701fff64fb46311e6da30b7ca804ef46936879520ea716472932e517c1"


@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class RetailReplayTests(StaticLiveServerTestCase):
    @classmethod
    def _make_connections_override(cls):
        # Django 的测试内存库使用命名共享缓存。各 HTTP 线程独立建连，避免并发请求共享事务状态。
        return {}

    def test_voidscar_arena(self):
        from playwright.sync_api import expect, sync_playwright

        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(
            COMBAT_LOG_ROOT=root/"hot", COMBAT_LOG_COLD_ROOT=root/"cold",
        ))
        fixture_path = os.environ.get("LMONITOR_REPLAY_121_FIXTURE")
        if fixture_path:
            content = Path(fixture_path).read_bytes()
        else:
            with requests.Session() as http:
                http.mount("https://", requests.adapters.HTTPAdapter(max_retries=2))
                response = http.get(SOURCE, timeout=(10, 45))
                response.raise_for_status()
                content = response.content
        self.assertEqual(hashlib.sha256(content).hexdigest(), SHA256)
        lines = content.splitlines()
        self.assertEqual(len(lines), 31761)
        self.assertIn(b"COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.0,PROJECT_ID,1", lines[6])
        self.client.force_login(get_user_model().objects.create_superuser(username="回放验收"))
        created = self.client.post(reverse("combat_logs"), {"name":"虚空之痕竞技场-12.1.0.txt", "size":len(content)})
        url = created.json()["url"]
        report = CombatLogReport.objects.get()
        for part, start in enumerate(range(0, len(content), report.part_size)):
            response = self.client.put(f"{url}parts/{part}/",content[start:start+report.part_size],content_type="application/octet-stream")
            self.assertEqual(response.status_code, 200)
        with patch.object(archive, "launch", side_effect=archive.collect):
            self.client.post(url+"finish/")
        report.refresh_from_db()
        self.assertEqual(report.state, "ready", report.message)
        self.assertEqual(report.digest, SHA256)
        version = MythicDungeonDataVersion.objects.create(key="retail-121",label="正式服",is_active=True)
        MythicDungeon.objects.create(data_version=version,key="voidscar-arena",map_id=585,
                                    name="Voidscar Arena",name_zh="虚空之痕竞技场")
        WowSpellSnapshot.objects.create(branch="wow",locale="zhCN",spell_id=44425,name="奥术弹幕")
        with analysis.database(report) as db:
            fight = db.execute("SELECT * FROM fight WHERE start_line=1").fetchone()
            self.assertEqual((fight["name"], str(fight["level"]), fight["result"]), ("虚空之痕竞技场","10","终点缺失"))
            self.assertEqual(db.execute("SELECT name FROM main.fight WHERE id=?", (fight["id"],)).fetchone()[0],"Voidscar Arena")
            self.assertEqual(db.execute("SELECT count(*) FROM fact WHERE keystone=?", (fight["id"],)).fetchone()[0], 28519)
            after = db.execute("SELECT * FROM fight WHERE start_line=31751").fetchone()
            self.assertEqual(after["name"], "The Blinding Vale")
            self.assertLess(fight["end_line"], after["start_line"])
        replay = reverse("combat_replay", args=[report.pk, fight["id"]])
        data = self.client.get(replay).context["replay"]
        self.assertEqual(len(data["deaths"]), 9)
        self.assertEqual(len([a for a in data["actors"] if a.get("spec_icon")]), 5)
        self.assertEqual(next(m for m in data["maps"] if m["id"]==2574)["terrain"]["source"], "MDT")

        # 同一毫秒先中熔岩箭，再获数次治疗。页面应显示最后一次采样的生命。
        guid, tick = "Player-11-0E8AC2E0", 38284
        player = next(a for a in data["actors"] if a["guid"]==guid)
        state = self.client.get(replay, {"subject":player["id"], "chunk":1}).json()
        samples = [r for r in state["health"] if r[0]==tick]
        self.assertIn((4705,607068,704700), [tuple(r[1:]) for r in samples])
        self.assertEqual(samples[-1][1:], [4718,658152,704700])
        self.assertIn(b"Chi Warding",lines[4718-1])
        self.assertIn(b"658152,704700",lines[4718-1])

        # 独立读取逐条事实，对照页面在任意时刻的结果，包括跨段回拖和循环。
        with analysis.database(report) as db:
            barrage = db.execute("""SELECT ms-? t,line FROM fact WHERE keystone=? AND kind='SPELL_CAST_SUCCESS'
                AND spell='44425' AND ms<=? ORDER BY ms DESC,line DESC LIMIT 1""",
                (fight["start_ms"],fight["id"],fight["start_ms"]+tick)).fetchone()
            raw = list(db.execute("""SELECT f.ms-? t,f.line,f.src,f.credited,f.kind,f.amount
                FROM fact f WHERE f.keystone=? AND f.line BETWEEN ? AND ? AND f.ms>=?
                ORDER BY f.ms,f.line""", (fight["start_ms"],fight["id"],fight["start_line"],fight["end_line"],fight["start_ms"])))
            waves = list(db.execute("""SELECT start_ms-? start,end_ms-? stop,first_line,last_line
                FROM wave WHERE keystone=? ORDER BY first_line""",(fight["start_ms"],fight["start_ms"],fight["id"])))
        roster = {p["id"] for p in data["actors"] if p["family"]=="Player"}
        pair = sorted(roster, key=lambda pid: sum((r["amount"] or 0) for r in raw
                      if r["credited"]==pid and r["kind"] in DAMAGE and r["t"]<=tick), reverse=True)[:2]
        hits = [r for r in raw if r["kind"] in DAMAGE and r["credited"] in pair]
        self.assertEqual(sum((r["amount"] or 0) for r in hits if r["t"]<=30000),3645257)
        hit_times = sorted({r["t"] for r in hits if r["credited"]==pair[0]})
        quiet = next(t+5000 for t, following in zip(hit_times, hit_times[1:] + [data["duration"]])
                     if following-t > 5000)

        def assert_activity(page, t, attempts=False, wave=None):
            past = [r for r in hits if r["t"]<=t]
            start = 0
            if wave is not None:
                start = wave["start"]
                past = [r for r in past if start<=r["t"] and wave["first_line"]<=r["line"]<=wave["last_line"]]
            def display(rows, seconds=1):
                known = sum(r["amount"] or 0 for r in rows)
                missing = any(r["amount"] is None for r in rows)
                return ("≥ " if missing else "") + f"{int(known/seconds+0.5):,}"
            for mode, records, seconds in (("amount",past,1), ("average",past,(t-start)/1000)):
                page.locator("#meter-metric").select_option(mode)
                expect(page.locator("#meter-value")).to_have_text(display(records,seconds) if seconds else "—")
                amounts = {pid:sum(r["amount"] or 0 for r in records if r["credited"]==pid) for pid in pair}
                order = sorted(pair,key=lambda pid:(-amounts[pid],pid))
                ranks = page.locator("#activity-damage li")
                expect(ranks).to_have_count(len(order))
                for index,pid in enumerate(order):
                    row = ranks.nth(index)
                    expect(row).to_have_attribute("data-actor",str(pid))
                    own = [r for r in records if r["credited"]==pid]
                    expect(row.locator(".meter-number")).to_have_text(display(own,seconds) if seconds else "—")
                    scale = row.locator(".meter-fill").evaluate("el => new DOMMatrix(getComputedStyle(el).transform).a")
                    self.assertAlmostEqual(scale, amounts[pid]/max(amounts.values()) if seconds and max(amounts.values()) else 0, places=5)
            kinds = {"SPELL_CAST_SUCCESS","SPELL_CAST_START","SPELL_CAST_FAILED"} if attempts else {"SPELL_CAST_SUCCESS"}
            expect(page.locator("#cast-cursor span")).to_have_text(f"{t//60000}:{t//1000%60:02}.{t%1000:03}")
            expect(page.locator("#casts-empty")).not_to_contain_text("正在读取")
            bounds = page.locator("#cast-body").evaluate("""body => {
                const head = document.querySelector('#cast-head').getBoundingClientRect();
                const pane = document.querySelector('#cast-scroll').getBoundingClientRect();
                const y = body.getBoundingClientRect().top + 16;
                return [(head.bottom-y)/.048,(pane.bottom-y)/.048];
            }""")
            matches = {r["line"]:r for r in raw if r["src"] in pair and r["kind"] in kinds}
            rendered = page.locator(".cast-marker").evaluate_all("nodes => nodes.map(n=>[+n.dataset.line,+n.parentNode.dataset.actor])")
            self.assertTrue(all(line in matches and matches[line]["src"]==actor for line,actor in rendered))
            expected = {line for line,r in matches.items() if bounds[0]<=r["t"]<=bounds[1]}
            self.assertTrue(expected.issubset({line for line,_ in rendered}))
            self.assertEqual(len(rendered),len({line for line,_ in rendered}))

        # 奥术弹幕走真实补查与图片请求；其余缺图模拟资料缺失，核验文字回退。
        lookup = activity._tooltip_icon
        self.enterContext(patch.object(activity,"_tooltip_icon",side_effect=lambda branch,sid: lookup(branch,sid) if sid==44425 else ""))
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            context = browser.new_context(viewport={"width":1360,"height":860})
            # 页面访问统计会与 SQLite 内存测试库的并行读取争锁；回放请求仍全部走本地服务。
            context.route("**/api/site-analytics/collect/", lambda route: route.fulfill(status=204))
            context.add_cookies([{"name":settings.SESSION_COOKIE_NAME,"value":self.client.cookies[settings.SESSION_COOKIE_NAME].value,
                                  "url":self.live_server_url}])
            page = context.new_page()
            errors, bad_requests = [], []
            page.on("response", lambda response: bad_requests.append((response.status,response.url)) if response.status>=400 and replay in response.url else None)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(self.live_server_url+replay+f"?t={tick}&unit={guid}")
            expect(page.locator("#map-reading")).to_contain_text("MDT 底图")
            tanks = [p for p in data["actors"] if p.get("role") == "tank"]
            self.assertEqual([p["class_label"] for p in tanks], ["死亡骑士"])
            expect(page.locator("#activity-players input:checked")).to_have_count(1)
            expect(page.locator(".cast-lane")).to_have_attribute("data-actor",str(tanks[0]["id"]))
            expect(page.locator("#activity-damage li")).to_have_count(len(roster))
            page.locator("#activity-none").click()
            for pid in pair:
                page.locator(f"#activity-players input[value='{pid}']").check()
            expect(page.locator(".cast-lane")).to_have_count(2)
            page.locator(f"#activity-players input[value='{pair[0]}']").uncheck()
            expect(page.locator(".cast-lane")).to_have_count(1)
            expect(page.locator(".cast-lane")).to_have_attribute("data-actor",str(pair[1]))
            page.locator("#activity-all").click()
            page.locator(".activity-picker summary").click()
            page.locator("#clock").fill(str(barrage["t"]+1))
            sequence = page.locator("#activity-casts")
            ability = sequence.locator(f"button[data-line='{barrage['line']}']")
            expect(page.locator("#casts-icons")).to_have_attribute("aria-pressed","true")
            expect(page.locator("#cast-targets")).not_to_be_checked()
            expect(ability).not_to_have_attribute("data-target",re.compile(".+"))
            expect(page.locator(".cast-player")).to_have_count(len(roster))
            expect(ability.locator("strong")).to_have_text("奥术弹幕")
            ability.scroll_into_view_if_needed()
            expect(ability).to_have_class(re.compile("has-cast-icon"),timeout=20000)
            self.assertGreater(ability.locator("img").evaluate("img=>img.naturalWidth"),0)
            page.locator("#cast-targets").check()
            expect(ability).to_have_attribute("data-target",re.compile(" → .+"))
            page.locator("#cast-targets").uncheck()
            ability.click()
            expect(page.locator("#clock")).to_have_value(str(barrage["t"]))
            page.goto(page.locator("#moment").get_attribute("href"))
            expect(page.locator("#casts-icons")).to_have_attribute("aria-pressed","true")
            page.locator("#casts-text").click()
            expect(ability.locator("strong")).to_be_visible()
            # 独立滚动到另一分片，表头固定，点击后才同步地图。
            header_y = page.locator("#cast-head").bounding_box()["y"]
            page.locator("#cast-scroll").evaluate("el=>el.scrollTop=4800")
            expect(page.locator("#cast-follow")).to_have_attribute("aria-pressed","false")
            expect(page.locator("#clock")).to_have_value(str(barrage["t"]))
            self.assertAlmostEqual(header_y,page.locator("#cast-head").bounding_box()["y"],delta=1)
            ruler = page.locator("#cast-ruler").bounding_box()
            head = page.locator("#cast-head").bounding_box()
            x,y = head["x"]+20,head["y"]+head["height"]+110
            target = round((y-ruler["y"]-16)/.048)
            page.mouse.click(x,y)
            self.assertAlmostEqual(int(page.locator("#clock").input_value()),target,delta=24)
            expect(page.locator("#cast-follow")).to_have_attribute("aria-pressed","true")
            page.locator("#clock").fill(str(tick))
            expect(page.locator(".replay-layers input:checked")).to_have_count(0)
            page.wait_for_function("Array.from(document.querySelectorAll('#team-health img')).filter(i=>i.naturalWidth>0).length===5")
            meter_list = page.locator("#activity-damage")
            expect(meter_list.locator("li")).to_have_count(len(roster))
            self.assertGreater(len(roster),5)
            expect(page.locator("#meter-metric")).to_have_value("average")
            self.assertEqual(page.locator("#meter-metric option").all_text_contents(),["秒伤（DPS）","总伤"])
            def check_corner():
                arena = page.locator("#arena").bounding_box()
                meter = page.locator(".damage-meter").bounding_box()
                self.assertAlmostEqual(arena["x"]+arena["width"]-meter["x"]-meter["width"],8,delta=1)
                self.assertAlmostEqual(arena["y"]+arena["height"]-meter["y"]-meter["height"],8,delta=1)
                self.assertLess(meter["width"],arena["width"]*.62)
            check_corner()
            self.assertEqual(meter_list.evaluate("el => el.clientHeight"),5*meter_list.locator("li").first.bounding_box()["height"])
            meter_list.hover()
            page.mouse.wheel(0,240)
            page.wait_for_function("document.querySelector('#activity-damage').scrollTop>0")
            self.assertEqual(int(page.locator("#clock").input_value()),tick)
            page.locator("#meter-toggle").click()
            expect(meter_list).to_be_hidden()
            page.locator("#meter-toggle").click()
            expect(meter_list).to_be_visible()
            page.locator("#state-only").click()
            expect(page.locator("#arena")).to_be_hidden()
            expect(meter_list).to_be_visible()
            page.locator("#with-map").click()
            for option in ("names","status-bars","buffs","debuffs"):
                page.locator("#"+option).check()
            card = page.locator(".unit-state").first
            expect(card.locator(".hp-reading")).to_contain_text("658,152 / 704,700")
            expect(card.locator(".monitor-debuffs")).to_contain_text("Demoralizing Shout")
            expect(card.locator(".monitor-buffs")).to_contain_text("Brush It Off")

            page.locator(".activity-picker summary").click()
            search = page.get_by_role("searchbox", name="查找玩家、专精或首领")
            search.fill("Player8-TestRealm-US")
            expect(page.locator("#activity-players label:visible")).to_have_count(1)
            page.get_by_role("button", name="取消匹配项").click()
            expect(page.locator("#activity-players input:checked")).to_have_count(len(roster)-1)
            search.fill("无此玩家")
            expect(page.locator("#no-players")).to_be_visible()
            search.fill("")
            page.locator("#activity-none").click()
            expect(page.locator("#cast-scroll")).to_be_hidden()
            expect(page.locator("#casts-empty")).to_contain_text("请勾选")
            expect(page.locator("#meter-empty")).to_contain_text("勾选玩家")
            expect(page.locator("#meter-value")).to_have_text("—")
            for pid in pair:
                page.locator(f"#activity-players input[value='{pid}']").check()
            page.locator(".activity-picker summary").click()
            assert_activity(page,tick)
            latest = page.locator(".cast-marker").first
            cast_line = int(latest.get_attribute("data-line"))
            cast_time = next(r["t"] for r in raw if r["line"]==cast_line)
            self.assertIn(b"SPELL_CAST_SUCCESS",lines[cast_line-1])
            latest.click()
            expect(page.locator("#clock")).to_have_value(str(cast_time))
            assert_activity(page,cast_time)
            for cursor in (0,2500,5000,quiet,29999,30000,63000,30000):
                page.locator("#clock").fill(str(cursor))
                assert_activity(page,cursor)
            expect(page.locator("#meter-value")).to_have_text("121,509")
            page.locator("#meter-pull").click()
            self.assertGreaterEqual(len(waves),2)
            for wave in (waves[-1],max(waves,key=lambda w:w["stop"]-w["start"]),waves[0]):
                for cursor in (wave["start"],(wave["start"]+wave["stop"])//2,wave["stop"]):
                    page.locator("#clock").fill(str(cursor))
                    assert_activity(page,cursor,wave=wave)
            page.goto(page.locator("#moment").get_attribute("href"))
            expect(page.locator("#meter-pull")).to_have_attribute("aria-pressed","true")
            assert_activity(page,waves[0]["stop"],wave=waves[0])
            gap = next(a["stop"]+1 for a,b in zip(waves,waves[1:]) if b["start"]>a["stop"]+1)
            page.locator("#clock").fill(str(gap))
            expect(page.locator("#meter-empty")).to_have_text("当前时刻没有交战波次。")
            expect(page.locator("#meter-value")).to_have_text("—")
            page.locator("#meter-fight").click()
            assert_activity(page,gap)
            page.locator("#clock").fill("30000")
            page.locator("#activity-attempts").check()
            page.goto(page.locator("#moment").get_attribute("href"))
            expect(page.locator("#activity-players input:checked")).to_have_count(2)
            expect(page.locator("#activity-attempts")).to_be_checked()
            assert_activity(page,30000,attempts=True)
            page.locator("#activity-attempts").uncheck()
            assert_activity(page,30000)
            page.locator("#speed").select_option("4")
            page.locator("#loop-toggle").click()
            page.wait_for_function("Number(document.querySelector('#clock').value)>33000")
            page.wait_for_function("Number(document.querySelector('#clock').value)<28500")
            page.locator("#play").click()
            paused = int(page.locator("#clock").input_value())
            assert_activity(page,paused)
            page.locator("#loop-toggle").click()
            page.set_viewport_size({"width":390,"height":844})
            expect(page.get_by_role("link",name="查看施法序列")).to_be_visible()
            check_corner()
            self.assertLessEqual(page.locator("#replay-activity").bounding_box()["width"],390)
            page.set_viewport_size({"width":1360,"height":860})

            # 真正从轨道上拖动，跨过多个 30 秒分段再回到开场时段。
            clock = page.locator("#clock")
            clock.scroll_into_view_if_needed()
            box = clock.bounding_box()
            y = box["y"]+box["height"]/2
            page.mouse.move(box["x"]+box["width"]*.3,y)
            page.mouse.down()
            page.mouse.move(box["x"]+box["width"]*.85,y,steps=6)
            self.assertGreater(int(clock.input_value()), 90000)
            page.mouse.move(box["x"]+box["width"]*.12,y,steps=5)
            page.mouse.up()
            self.assertLess(int(clock.input_value()), 30000)
            expect(page.locator("#play")).to_have_text("播放")
            page.get_by_role("tab", name="观察单位").click()
            expect(page.locator("#actor")).to_be_visible()
            page.get_by_role("tab", name="观察单位").press("ArrowRight")
            expect(page.get_by_role("tab", name="战斗事件")).to_be_focused()
            page.locator("#death").select_option(index=1)
            expect(card.locator(".hp-reading")).to_contain_text("死亡")
            reload_time = int(clock.input_value())
            page.goto(page.locator("#moment").get_attribute("href"))
            self.assertEqual(int(clock.input_value()),reload_time)
            expect(page.locator(".replay-layers input:checked")).to_have_count(4)
            expect(page.locator(".unit-state .hp-reading")).to_contain_text("死亡")
            expect(page.get_by_role("tab", name="战斗事件")).to_have_attribute("aria-selected","true")
            self.assertFalse(errors,errors)
            browser.close()
