import hashlib
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.test import TestCase, override_settings
from django.urls import reverse

from botend.management.commands.combat_log_storage import transfer
from botend.models import CombatLogCollection, CombatLogReview, WowSpellSnapshot
from botend.models import JournalEncounter, JournalInstance, JournalRelease, JournalState, MythicDungeon, MythicDungeonDataVersion, MythicDungeonEnemy
from botend.models import SeasonMeta, SpecDungeonRanking, SpecRaidRanking, WowTalentNodeMetadata, WowTalentVersion
from botend.services import combat_log_analysis as analysis
from botend.services import combat_log_archive as archive
from botend.services import combat_log_localization as localization
from botend.services import combat_public_events


def fixture(day=1):
    player, enemy = 'Player-1,"甲",0,0', 'Creature-1,"首领",0,0'
    geom = "Player-1,0,700,1000,0,0,0,0,0,0,3,60,100,0,101.0,55.0,2201,1.25,80"
    mob_geom = geom.replace("Player-1", "Creature-1")
    events = [
        (
            0,
            "COMBAT_LOG_VERSION,22,ADVANCED_LOG_ENABLED,1,BUILD_VERSION,12.1.0,PROJECT_ID,1",
        ),
        (1, 'CHALLENGE_MODE_START,"地城",55,2,10,[9,10]'),
        (
            2,
            f'SPELL_DAMAGE,{player},{enemy},11,"小怪伤害",4,{mob_geom},20,30,-1,4,0,0,0,nil,nil,nil,ST',
        ),
        (4, 'ENCOUNTER_START,42,"团本首领",8,5,55'),
        (5, f'SPELL_CAST_START,{enemy},{player},22,"读条",4'),
        (6, f'SPELL_CAST_SUCCESS,{enemy},{player},22,"读条",4'),
        (
            7,
            f'SPELL_DAMAGE,{enemy},{player},33,"爆炸",4,{geom},100,120,20,4,0,0,0,nil,nil,nil,ST',
        ),
        (8, f'SPELL_HEAL,{player},{player},44,"治疗",4,{geom},90,90,15,0,nil'),
        (9, f'SPELL_AURA_APPLIED,{enemy},{player},55,"光环",4,DEBUFF'),
        (10, f'SPELL_DISPEL,{player},{player},66,"驱散",4,55,"光环",4,DEBUFF'),
        (11, f'SPELL_AURA_REMOVED,{enemy},{player},55,"光环",4,DEBUFF'),
        (12, f'SPELL_CAST_START,{enemy},{player},22,"读条",4'),
        (13, f'SPELL_INTERRUPT,{player},{enemy},77,"打断",4,22,"读条",4'),
        (14, f"UNIT_DIED,0,nil,0,0,{player}"),
        (15, 'ENCOUNTER_END,42,"团本首领",8,5,0'),
        (
            16,
            f'SPELL_DAMAGE,{player},{enemy},11,"小怪伤害",4,{mob_geom},50,55,-1,4,0,0,0,nil,nil,nil,ST',
        ),
        (30, "CHALLENGE_MODE_END,55,1,10,29000"),
    ]
    return b"".join(
        f"9/{day}/2026 18:00:{sec:02}.000+8  {row}\r\n".encode() for sec, row in events
    )


def boss_fixture(day, second=False):
    rows = fixture(day).decode().replace('Creature-1,"首领"', 'Creature-1,"团本首领"').splitlines()
    hp = rows[2].replace("18:00:02", "18:00:05")
    late = hp.replace("18:00:05", "18:00:11").replace(",700,1000", ",250,1000")
    cast = rows[4].replace("SPELL_CAST_START", "SPELL_CAST_SUCCESS").replace(
        'Creature-1,"团本首领",0,0,Player-1,"甲"', 'Player-1,"甲",0,0,Creature-1,"团本首领"')
    rows.insert(5, hp)
    rows.insert(6, cast)
    if second:
        rows.insert(7, cast)
        rows.insert(8, cast.replace("SPELL_CAST_SUCCESS", "SPELL_CAST_FAILED"))
        rows = [r.replace("18:00:06.000+8  SPELL_CAST_SUCCESS",
                          "18:00:06.000+8  SPELL_INTERRUPT").replace(
                              'Creature-1,"团本首领",0,0,Player-1,"甲",0,0,22,"读条",4',
                              'Player-1,"甲",0,0,Creature-1,"团本首领",0,0,77,"打断",4,22,"读条",4')
                if "18:00:06" in r else r for r in rows]
    rows.insert(next(i for i,r in enumerate(rows) if "18:00:12" in r), late.replace(",250,1000", ",350,1000") if second else late)
    text = "\n".join(rows)+"\n"
    if second:
        text = text.replace("18:00:05.000+8  SPELL_CAST_START", "18:00:05.500+8  SPELL_CAST_START")
    return text.encode()


def raid_fixture(day, second=False):
    return b"".join(line.replace(b',8,5,', b',16,20,') for line in boss_fixture(day, second).splitlines(keepends=True)
                    if b"CHALLENGE_MODE_" not in line)


class CombatWorkbenchTests(TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(
            self.settings(
                COMBAT_LOG_ROOT=root / "hot", COMBAT_LOG_COLD_ROOT=root / "cold"
            )
        )
        User = get_user_model()
        self.owner = User.objects.create_superuser(username="主视角")
        self.member = User.objects.create_superuser(username="队员")
        self.client.force_login(self.owner)
        self.report = self.ingest(self.owner, 1)
        self.samples = analysis.catalog([self.report])[0]

    def ingest(self, owner, day, data=None):
        data = fixture(day) if data is None else data
        r = archive.create(owner, f"第{day}晚.txt", len(data))
        archive.put_part(owner, r.pk, 0, data)
        with patch.object(archive, "launch", side_effect=archive.collect):
            with self.captureOnCommitCallbacks(execute=True):
                archive.enqueue(owner, r.pk)
        r.refresh_from_db()
        self.assertEqual(r.state, "ready", r.message)
        return r

    def test_cached_battle_directory_rechecks_filters_revision_and_reader(self):
        from botend.dashboard import combat_library
        from django.utils import timezone

        book = CombatLogCollection.objects.create(owner=self.owner, title="共享目录")
        book.reports.add(self.report)
        book.readers.add(self.member)
        self.client.force_login(self.member)
        url = reverse("combat_battles")
        query = {"log": str(self.report.pk)}
        browser = {"HTTP_SEC_FETCH_DEST":"document", "HTTP_ACCEPT":"text/html"}
        fetch = {"HTTP_SEC_FETCH_DEST":"empty", "HTTP_ACCEPT":"text/html"}
        with patch.object(combat_library, "local_groups", wraps=combat_library.local_groups) as groups:
            cold = self.client.get(url, query, **browser)
            self.assertTemplateUsed(cold, "dashboard/combat_loading.html")
            page = self.client.get(cold.context["continue_url"], **fetch, HTTP_X_COMBAT_NAVIGATION="continue")
            self.assertEqual(page.status_code, 200)
            warm = self.client.get(url, query, **browser)
            self.assertEqual(warm.content, page.content)
            self.assertIn("no-store", warm["Cache-Control"])
            self.assertEqual(groups.call_count, 1)
            reload = self.client.get(url, query, **browser, HTTP_CACHE_CONTROL="no-cache")
            self.assertIn("refresh=1", reload.context["continue_url"])
            changed = self.client.get(url, {**query, "q":"另一组筛选"}, **browser)
            self.assertTemplateUsed(changed, "dashboard/combat_loading.html")
            type(self.report).objects.filter(pk=self.report.pk).update(updated_at=timezone.now())
            self.assertTemplateUsed(self.client.get(url, query, **browser), "dashboard/combat_loading.html")
            self.assertEqual(self.client.get(url, query, **fetch).status_code, 200)
            self.assertEqual(groups.call_count, 2)
            book.readers.remove(self.member)
            self.assertEqual(self.client.get(url, query, **browser).status_code, 404)

    def test_replay_cache_separates_document_and_chunks(self):
        from botend.services import combat_log_replay

        url = reverse("combat_replay", args=[self.report.pk, self.samples[0]["id"]])
        fetch = {"HTTP_SEC_FETCH_DEST":"empty"}
        with patch.object(combat_log_replay, "read", wraps=combat_log_replay.read) as read:
            page = self.client.get(url, **fetch)
            chunk = self.client.get(url, {"chunk":0}, **fetch)
            self.assertEqual((page.status_code, chunk.status_code), (200, 200))
            self.assertEqual(self.client.get(url, **fetch).content, page.content)
            self.assertEqual(self.client.get(url, {"chunk":0}, **fetch).json(), chunk.json())
            self.assertEqual(read.call_count, 2)

    def test_observed_talent_spec_and_spell_icons_reach_workbench(self):
        version = WowTalentVersion.objects.create(key="visual-retail", branch="retail")
        WowTalentNodeMetadata.objects.create(talent_version=version, class_name="Mage", spec_name="Fire",
            talent_id=701, node_id=801, name_zh="实测天赋", icon="spell_fire_flamebolt")
        WowSpellSnapshot.objects.create(branch="wow", locale="zhCN", spell_id=11, icon="spell_fire_fireball")
        lines = fixture(2).splitlines(keepends=True)
        info = b"9/2/2026 18:00:01.500+8  COMBATANT_INFO,Player-1,0,63,[(701,801,2)],[(999,998,1)]\r\n"
        lines.insert(2, info)
        r = self.ingest(self.owner, 2, b"".join(lines))
        sample = next(s for s in analysis.catalog([r])[0] if s["kind"] == "钥匙")
        path = reverse("combat_analysis", args=[r.pk])
        query = {"fight": sample["id"], "wave": "all"}
        page = self.client.get(path, query)
        row = next(a for a in page.context["rows"] if a["ident"] == "Player-1")
        self.assertEqual(row["icon_label"], "法师 · 火焰")
        actor = self.client.get(row["actor_url"])
        self.assertContains(actor, "实测天赋")
        self.assertContains(actor, "spell_fire_flamebolt.jpg")
        self.assertEqual([(t["entry"], t["rank"]) for t in actor.context["builds"][0]["items"]], [(801, 2)])
        self.assertEqual(self.client.get(actor.context["analysis_url"]).context["selected"]["id"], sample["id"])
        self.assertContains(self.client.get(path, dict(query, group="ability")), "spell_fire_fireball.jpg")
        self.assertContains(self.client.get(path, dict(query, panel="events")), "spell_fire_fireball.jpg")
        choices = self.client.get(path, dict(query, panel="pick", group="ability"), HTTP_X_COMBAT_VIEW="choices")
        self.assertTrue(any("spell_fire_fireball.jpg" in item["icon"] for item in choices.json()["items"]))

    def test_selected_logs_drive_battles_replay_and_character_comparison(self):
        from bs4 import BeautifulSoup
        from urllib.parse import urlsplit
        from django.http import QueryDict

        second = self.ingest(self.owner, 2)
        private = self.ingest(self.member, 3)
        logs = [str(second.pk), str(self.report.pk)]
        workspace = reverse("combat_workspace")
        library = reverse("combat_logs")
        library_shell = BeautifulSoup(self.client.get(library).content, "html.parser").select_one(".combat-shell")
        self.assertFalse(library_shell.select('.crumb, a[href="/"]'))
        home = self.client.get(library, {"log": logs, "analyze": "battle"}, follow=True)
        self.assertEqual(home.request["PATH_INFO"], reverse("combat_battles"))
        self.assertEqual(len(home.context["groups"][0]["pulls"]), 2)
        self.assertFalse(CombatLogCollection.objects.exists())
        overview = self.client.get(home.context["groups"][0]["combined_url"])
        self.assertEqual(len(overview.context["comparisons"]), 2)
        self.assertContains(overview, 'class="attempt-table" open')
        for pull in overview.context["comparisons"]:
            detail = self.client.get(pull["url"])
            self.assertEqual((detail.context["report"].pk, detail.context["selected"]["id"]),
                             (pull["report"].pk, pull["id"]))
            self.assertURLEqual(detail.context["directory_url"], home.context["groups"][0]["focus_url"])
            self.assertEqual(QueryDict(urlsplit(detail.context["directory_url"]).query).getlist("log"), logs)
        group_compare = self.client.get(overview.context["players_url"])
        self.assertEqual(group_compare.context["target_group"], overview.context["cohort"])
        detail = self.client.get(overview.context["comparisons"][0]["url"])
        self.assertEqual(len(detail.context["scope_logs"]), 2)
        report, fight = detail.context["report"], detail.context["selected"]["id"]
        replay = self.client.get(reverse("combat_replay", args=[report.pk, fight]),
                                 {"back": detail.context["replay_back"]})
        shell = BeautifulSoup(replay.content, "html.parser").select_one(".combat-shell")
        self.assertFalse(shell.select('main .crumb, a[href="/"]'))
        trail = shell.select_one('#combat-navigation > nav[aria-label="当前位置"]')
        self.assertEqual([a["href"] for a in trail.select("a")],
                         [library, detail.context["directory_url"], replay.context["analysis_url"]])
        self.assertEqual(self.client.get(replay.context["analysis_url"]).context["selected"]["id"], fight)
        compare = self.client.get(replay.context["players_url"])
        self.assertEqual(compare.context["pairing"], "same")
        self.assertEqual(compare.context["run"], f"{report.pk}.{fight}")
        state = QueryDict(urlsplit(compare.context["battle_url"]).query)
        self.assertEqual(state.getlist("log"), logs)
        self.assertEqual(state["sample"], str(report.pk))
        selected = self.client.get(compare.context["change_url"])
        self.assertEqual({r["value"] for r in selected.context["rows"] if r["checked"]}, set(logs))
        q = {"log": logs, "mode": "players", "left": "Player-1", "right": "Player-1"}
        duel = self.client.get(workspace, q).context
        self.assertEqual([str(w["sample"]["report"].pk) for w in duel["winners"]], logs)
        events = self.client.get(duel["skills"][0]["sides"][0]["url"]).context
        self.assertEqual((events["panel"], len(events["scope_logs"])), ("events", 2))
        returned = self.client.get(events["players_url"]).context
        self.assertEqual([str(w["sample"]["report"].pk) for w in returned["winners"]], logs)
        self.assertEqual(self.client.get(workspace, dict(q, options="left", left_file=private.pk)).status_code, 404)
        self.assertEqual(self.client.get(workspace, {"log": logs + [private.pk]}).status_code, 404)
        automatic = self.client.get(library, {"analyze":"players"}, follow=True)
        self.assertEqual(automatic.request["PATH_INFO"], library)
        self.assertContains(automatic, "请先勾选")

    def test_same_boss_review_aligns_health_player_casts_and_outcomes(self):
        left = self.ingest(self.owner, 2, raid_fixture(2))
        right = self.ingest(self.owner, 3, raid_fixture(3, True))
        params = {"log": [left.pk, right.pk], "panel": "boss", "cohort": analysis.catalog([left])[0][0]["cohort"]}
        directory = reverse("combat_battles")
        single = self.client.get(directory, {"log": left.pk})
        self.assertContains(single, "首领开荒复盘")
        self.assertNotContains(single, "副本首领对比")
        self.assertContains(self.client.get(directory, {"log": [left.pk, right.pk]}), "首领开荒复盘")
        url = reverse("combat_workspace")
        response = self.client.get(url, params)
        self.assertContains(response, "首领血量走势")
        ctx = response.context
        self.assertEqual(len(ctx["shown"]), 2)
        self.assertEqual(ctx["shown"][0]["curves"][0]["thresholds"], [1, 7, 7])
        self.assertEqual(ctx["charts"][0]["points"], [[1, 70], [7, 25]])
        self.assertEqual(ctx["players"][0]["cells"][1]["delta"], "+1")
        anchor = next(a for a in ctx["anchors"] if a["different"])
        self.assertEqual([c["outcome"] for c in anchor["cells"]], ["完成", "打断"])
        self.assertEqual(anchor["cells"][1]["responder"], "甲")
        self.assertEqual(anchor["cells"][1]["hp"][0]["pct"], 70)
        self.assertTrue(any(c and c["outcome"] == "施法失败" for a in ctx["anchors"] for c in a["cells"]))
        for kind in ("evidence", "replay", "response"):
            self.assertEqual(self.client.get(anchor["cells"][1][kind]).status_code, 200)
        window = self.client.get(url + anchor["cells"][1]["window"].split("#")[0]).context
        self.assertEqual(window["at"], 2)
        self.assertTrue(window["shown"][1]["timeline"])
        aligned = self.client.get(url + anchor["align"].split("#")[0]).context
        self.assertEqual([s["sync_sec"] for s in aligned["shown"]], [1, 1.5])
        self.assertEqual(aligned["shown"][1]["timeline"][0]["relative"], "-0.5")
        swapped = self.client.get(url, dict(params, base=ctx["shown"][1]["key"], player="Player-1")).context
        self.assertEqual(swapped["players"][0]["cells"][1]["delta"], "-1")
        self.assertTrue(swapped["skills"])
        invalid = self.client.get(url, dict(params, base="other"))
        self.assertEqual(invalid.status_code, 400)
        self.assertIn("基准场次", invalid.json()["message"])

    def test_dungeon_boss_is_a_wave_of_a_specific_run(self):
        other = self.ingest(self.owner, 2)
        private = self.ingest(self.member, 3)
        logs = [self.report.pk, other.pk]
        directory = self.client.get(reverse("combat_battles"), {"log": logs})
        group, = directory.context["groups"]
        self.assertEqual(len(group["pulls"]), 2)
        self.assertContains(directory, "大秘境首领明细")
        self.assertNotContains(directory, "首领开荒复盘")
        for pull in group["pulls"]:
            boss, = pull["bosses"]
            page = self.client.get(boss["wave_url"])
            self.assertEqual(page.context["selected"]["kind"], "钥匙")
            self.assertEqual(page.context["active_wave"]["id"], -boss["id"])
            self.assertEqual(page.context["report"].pk, boss["report"].pk)
        url = reverse("combat_workspace")
        menu = self.client.get(url, {"log": logs, "mode": "boss"})
        self.assertFalse(menu.context["overview"])
        cohort = analysis.catalog([other])[0][1]["cohort"]
        self.assertEqual(self.client.get(url, {"log": logs, "panel": "boss", "cohort": cohort}).status_code, 400)
        self.assertEqual(self.client.get(url, {"log": private.pk, "mode": "boss"}).status_code, 404)

    def test_unassigned_keystone_boss_and_rebuild_of_old_directory(self):
        raw = fixture(2).splitlines(keepends=True)
        # 模拟只有钥匙开场、重复版本头、同地图首领的历史归档。
        source = b"".join([raw[0], raw[1], raw[0].replace(b":00.000", b":21.000"),
                           raw[3].replace(b":04.000", b":24.000"), raw[14].replace(b":15.000", b":45.000")])
        report = self.ingest(self.owner, 2, source)
        report.summary["boundary_revision"] = 2
        key, boss = report.summary["activity_index"]
        boss["parent"] = None
        report.save(update_fields=["summary"])
        url = reverse("combat_battles")
        old = self.client.get(url, {"log": report.pk})
        group, = old.context["groups"]
        self.assertEqual(group["name"], key["name"])
        self.assertContains(old, "未归入场次的地下城记录")
        self.assertContains(old, "从已有归档重新识别场次")
        self.assertNotContains(old, "团本首领 · 难度")
        self.assertNotContains(old, "首领开荒复盘")
        with patch.object(archive, "launch", side_effect=archive.collect), self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("combat_rebuild", args=[report.pk]))
        fresh = self.client.get(url, {"log": report.pk})
        self.assertNotContains(fresh, "未归入场次的地下城记录")
        self.assertNotContains(fresh, "从已有归档重新识别场次")
        self.assertContains(fresh, "大秘境首领明细")

    def test_dungeon_comparison_keeps_log_map_and_key_level(self):
        higher = fixture(2).replace(b"55,2,10,[", b"55,2,21,[").replace(b"END,55,1,10,", b"END,55,1,21,")
        second = self.ingest(self.owner, 2, higher)
        second.source_filename = "第二晚原始日志.txt"
        second.save(update_fields=["source_filename"])
        different = self.ingest(self.owner, 3, fixture(3).replace(b",55,", b",66,").replace(b",5,55", b",5,66"))
        directory = self.client.get(reverse("combat_battles"), {"log":[self.report.pk, second.pk, different.pk]})
        bosses = [p["bosses"][0] for g in directory.context["groups"] for p in g["pulls"]]
        entries = {s["report"].pk:s for s in bosses}
        self.assertNotIn("compare_url", entries[different.pk])
        self.assertCountEqual(
            [s["compare_url"] for group in directory.context["groups"] for s in group["sidebar_comparisons"]],
            [entries[self.report.pk]["compare_url"], entries[second.pk]["compare_url"]],
        )
        from urllib.parse import urlsplit
        from django.http import QueryDict
        url = urlsplit(entries[second.pk]["compare_url"])
        params = QueryDict(url.query, mutable=True)
        page = self.client.get(url.path + "?" + params.urlencode())
        self.assertContains(page, "地城 +21")
        self.assertContains(page, "第二晚原始日志.txt")
        self.assertNotContains(page, "难度 8")
        self.assertEqual([s["report"].pk for s in page.context["shown"]], [second.pk])
        params["bracket"] = "all"
        page = self.client.get(url.path + "?" + params.urlencode())
        self.assertEqual({s["level_label"] for s in page.context["shown"]}, {"+10","+21"})
        for s in page.context["shown"]:
            detail = self.client.get(s["url"])
            self.assertEqual((detail.context["report"].pk, detail.context["active_wave"]["id"]), (s["report"].pk, -s["id"]))

    def test_target_boundaries_participants_and_public_benchmarks(self):
        rows = fixture(2).splitlines(keepends=True)
        rows.insert(2, b"9/2/2026 18:00:01.500+8  COMBATANT_INFO,Player-1,0,63,[]\r\n")
        rows.insert(4, rows[3].replace(b"Player-1", b"Player-2").replace("甲".encode(), "乙".encode()))
        r = self.ingest(self.owner, 2, b"".join(rows))
        sample, boss = analysis.catalog([r])[0]
        url = reverse("combat_workspace")
        query = dict(log=str(r.pk), mode="players", target_group=sample["cohort"], run=f"{r.pk}.1",
                     left="Player-1", right="Player-2", pairing="same")
        same = self.client.get(url, query).context
        self.assertEqual([side["amount"] for side in same["results"]], [70, 20])
        self.assertEqual(same["skills"][0]["values"], [70, 20])
        self.assertEqual((same["total_contrast"]["side"], same["total_contrast"]["gap"],
                          same["total_contrast"]["percent"]), ("A", 50, "250%"))
        self.assertEqual(same["matrix"]["heal"]["values"], [75, 0])
        self.assertEqual(same["matrix"]["overheal"]["values"], [15, 0])
        self.assertEqual(same["matrix"]["interrupts"]["values"], [1, 0])
        self.assertEqual(same["matrix"]["deaths"]["values"], [1, 0])
        hits = self.client.get(url, dict(query, measure="events")).context["skills"][0]["delta"]
        self.assertEqual((hits["left"], hits["right"], hits["amount"]), ("2", "1", "1"))
        average = self.client.get(url, dict(query, measure="average")).context["skills"][0]["delta"]
        self.assertEqual((average["left"], average["right"], average["percent"]), ("35", "20", "75%"))
        no_average = self.client.get(url, dict(query, metric="heal", measure="average")).context["skills"][0]["delta"]
        self.assertEqual(no_average["status"], "数据不足")
        received = self.client.get(url, dict(query, metric="taken", breakdown="unit")).context
        self.assertEqual(received["skills"][0]["name"], "首领")
        self.assertEqual(received["skills"][0]["values"], [80, 0])
        self.assertEqual(received["total_contrast"]["percent"], "")
        tick = self.client.get(url, dict(query, breakdown="time", left="Player-2", right="Player-1")).context
        self.assertEqual((tick["skills"][0]["delta"]["side"], tick["skills"][0]["name"]), ("B", "0:00 – 0:30"))
        self.assertEqual(tick["matrix"]["casts"]["delta"]["status"], "持平")
        returned = self.client.get(self.client.get(same["battle_url"]).context["players_url"])
        self.assertEqual((returned.context["left"], returned.context["right"]), ("Player-1", "Player-2"))
        self.assertEqual(self.client.get(url, dict(query, run=f"{r.pk}.2")).status_code, 404)
        self.assertEqual(self.client.get(url, dict(log=r.pk, part=[1, 2])).status_code, 400)
        wrong_actor = dict(query, target_group=boss["cohort"], run=f"{r.pk}.2")
        self.assertEqual(self.client.get(url, wrong_actor).status_code, 400)

        season = SeasonMeta.objects.create(season_key="test", season_name="测试赛季", game_build="12.1.0.999",
            mplus_zone_id=1, raid_zone_id=2, mplus_encounters=[{"id": 916, "name": "地城"}],
            raid_encounters=[{"id": 917, "name": "团本首领"}])
        fields = dict(season_id=season.pk, dungeon_id=916, dungeon_name="地城", keystone_level=10,
                      class_name="Mage", spec_name="Fire", character_name="公开甲", fight_id=3,
                      dps=500, score=200, report_code="RankA")
        for changes in ({}, dict(score=199, dps=9000), dict(score=999, keystone_level=11),
                        dict(score=1000,spec_name="Frost"), dict(score=1001, dungeon_id=999)):
            SpecDungeonRanking.objects.create(**(fields | changes))
        public = self.client.get(url, dict(query, pairing="public")).context
        self.assertEqual((public["reference"]["score"],public["reference"]["dps"]), (200,500))
        self.assertIn("RankA#fight=3", public["reference"]["url"])
        self.assertAlmostEqual(public["gap"], 70/29 - 500)
        self.assertEqual(public["rate_contrast"]["side"], "B")
        self.assertIsNone(public["duration_contrast"]["gap"])
        meta = dict(fights=[dict(id=3, encounterID=916, keystoneLevel=10, friendlyPlayers=[2], startTime=0, endTime=30000)],
                    masterData=dict(actors=[dict(type="Player", id=2, name="公开甲", server="", subType="Mage"),
                                            dict(type="Pet", id=3, name="水元素", petOwner=2)],
                                    abilities=[dict(gameID=11, name="小怪伤害")]))
        def page(events, cursor):
            return {"events": {"data": [dict(fight=3, **e) for e in events], "nextPageTimestamp": cursor}}
        first = page([dict(timestamp=0, type="damage", abilityGameID=11, sourceID=3, targetID=5, amount=105),
                      dict(timestamp=1, type="cast", abilityGameID=1, sourceID=2),
                      dict(timestamp=1, type="cast", abilityGameID=11, sourceID=2),
                      dict(timestamp=2, type="interrupt", abilityGameID=77, extraAbilityGameID=22, sourceID=2)], 200)
        last = page([dict(timestamp=200, type="damage", abilityGameID=33, sourceID=6, targetID=2, amount=40),
                     dict(timestamp=201, type="death", targetID=3)], None)
        with patch.object(combat_public_events.SpecDetailBase, "_wcl_graphql") as upstream:
            wrap = lambda data: {"reportData": {"report": data}}
            upstream.side_effect = [wrap(meta), wrap(first), None]
            read = dict(query, pairing="public", read_public=1, detail="taken", scale="rate")
            failed = self.client.get(url, read)
            self.assertContains(failed, "重新读取明细")
            self.assertNotIn("public_matrix", failed.context)
            upstream.side_effect = list(map(wrap, [meta, first, last]))
            detailed = self.client.get(url, read).context
            self.assertEqual(detailed["public_matrix"]["taken"]["left"], "80")
            self.assertEqual(detailed["public_matrix"]["taken"]["right"], "40")
            self.assertEqual(detailed["public_matrix"]["damage"]["right"], "105")
            self.assertEqual(detailed["public_matrix"]["casts"]["right"], "1")
            self.assertEqual(detailed["public_matrix"]["deaths"]["right"], "0")
            self.assertEqual(detailed["detail_rate"]["right"], "1.33")
            detail_url = next(link for key, _, _, link in detailed["public_links"] if key == "damage")
            cached = self.client.get(url + detail_url.split("#")[0])
            self.assertEqual(cached.context["detail"], "damage")
            utility = self.client.get(url, dict(read, detail="interrupts", scale="total"))
            self.assertContains(utility, "打断了哪些读条")
            self.assertEqual(utility.context["mechanisms"][0]["delta"]["gap"], 0)
            self.assertEqual(utility.context["mechanisms"][0]["spell"], "22")
            self.assertEqual(upstream.call_count, 6)
        SpecDungeonRanking.objects.all().delete()
        self.assertContains(self.client.get(url, dict(query,pairing="public")), "暂无同专精")
        raid = [line.replace(b',8,5,', b',16,20,') for line in rows if b'CHALLENGE_MODE' not in line]
        rr = self.ingest(self.owner, 3, b"".join(raid))
        fields = dict(season_id=season.pk, boss_id=917, boss_name="团本首领", difficulty=5,
                      class_name="Mage",spec_name="Fire",character_name="公开乙", fight_id=4,report_code="RankB",dps=700)
        for changes in ({}, dict(difficulty=4,dps=900), dict(dps=680)):
            SpecRaidRanking.objects.create(**(fields | changes))
        page = self.client.get(url, dict(log=rr.pk, mode="players",run=f"{rr.pk}.1",pairing="public",left="Player-1"))
        self.assertEqual(page.context["reference"]["dps"], 700)

    def test_same_filter_drives_totals_evidence_windows_and_waves(self):
        key, boss = self.samples
        result = analysis.run([key], {}, single=True)
        self.assertEqual(result["total"], 70)
        self.assertEqual(len(result["waves"]), 3)
        self.assertEqual(
            analysis.run([key], {"wave": str(result["waves"][0]["id"])}, single=True)[
                "total"
            ],
            20,
        )
        for metric, total in [
            ("taken", 80),
            ("heal", 75),
            ("overheal", 15),
            ("interrupts", 1),
            ("deaths", 1),
        ]:
            result = analysis.run([boss], {"metric": metric}, single=True)
            self.assertEqual(result["total"], total)
            self.assertEqual(sum(e["value"] for e in result["events"]), total)
        result = analysis.run(
            [boss],
            {
                "metric": "taken",
                "anchor": "22",
                "occurrence": "1",
                "before": "0",
                "beyond": "2",
            },
            single=True,
        )
        self.assertEqual(result["total"], 0)  # 右端事件不计入。
        self.assertEqual(result["rules"][0]["outcomes"]["完成"]["n"], 1)
        missing = analysis.run([boss], {"anchor": "22", "occurrence": "3"})
        self.assertEqual(
            (len(missing["comparisons"]), len(missing["excluded"])), (0, 1)
        )
        url = reverse("combat_actor", args=[self.report.pk, boss["id"], "Player-1"])
        page = self.client.get(url)
        self.assertContains(page, "位置、血量与资源采样")
        self.assertEqual(page.context["maps"][2201][0]["hp"], 700)
        self.assertEqual(page.context["auras"][0]["seconds"], 2)
        self.assertContains(
            self.client.get(
                reverse("combat_analysis", args=[self.report.pk]), {"fight": 1}
            ),
            "第 1 波",
        )

    def test_pull_picker_scopes_stats_events_and_review(self):
        report = self.ingest(self.owner, 2, fixture().split(b'9/1/2026 18:00:30')[0])
        url = reverse("combat_analysis", args=[report.pk])
        first = self.client.get(url, {"fight":1}).context
        waves = first["waves"]
        self.assertEqual([w["label"] for w in waves], ["第 1 波","团本首领","第 2 波"])
        self.assertEqual((first["active_wave"]["id"],first["total"]), (waves[0]["id"],20))
        boss_query = {"fight":1,"wave":waves[1]["id"],"metric":"taken","panel":"events"}
        boss = self.client.get(url,boss_query).context
        self.assertEqual(boss["total"],80)
        self.assertEqual(boss["rate"],round(80/11,2))
        self.assertEqual([e["line"] for e in boss["events"]],[7])
        review = self.client.get(url,dict(boss_query,panel="review")).context
        self.assertEqual(review["rules"][0]["n"],2)
        third = self.client.get(url,dict(fight=1,wave=waves[2]["id"])).context
        self.assertEqual(third["total"],50)
        self.assertIsNone(third["rate"])
        self.assertEqual(self.client.get(url,dict(fight=1,wave="all")).context["total"],70)
        self.assertEqual(self.client.get(url,dict(fight=1,wave="-999")).status_code,400)

    def test_replay_scopes_positions_and_markers_to_fight(self):
        key = reverse("combat_replay", args=[self.report.pk, 1])
        boss = reverse("combat_replay", args=[self.report.pk, 2])
        manifest = self.client.get(key).context["replay"]
        self.assertEqual(manifest["duration"], 29000)
        self.assertEqual({p["family"] for p in manifest["actors"]}, {"Player", "Creature"})
        self.assertEqual(manifest["maps"][0]["id"], 2201)
        self.assertEqual(manifest["deaths"][0]["t"], 13000)
        data = self.client.get(boss, {"chunk": 0}).json()
        self.assertEqual([p[1] for p in data["points"]], [3000, 4000])
        self.assertEqual(data["points"][0][2:], [2201, 101.0, 55.0, 700, 1000, 1.25])
        death = next(e for e in data["events"] if e["kind"] == "UNIT_DIED")
        self.assertEqual(death["t"], 10000)
        self.assertContains(self.client.get(death["url"]), "UNIT_DIED")
        self.assertEqual(self.client.get(key, {"chunk": 1}).status_code, 404)
        self.assertEqual(self.client.get(key, {"chunk": 0, "revision": "过期"}).status_code, 409)
        self.client.force_login(self.member)
        for params in ({}, {"chunk": 0}, {"event_page": 0}):
            self.assertEqual(self.client.get(key, params).status_code, 404)

    def test_replay_boss_sources_follow_pull_boundaries(self):
        version = MythicDungeonDataVersion.objects.create(key="boss-catalog", is_active=True)
        dungeon = MythicDungeon.objects.create(data_version=version,key="test",name="地城")
        MythicDungeonEnemy.objects.create(dungeon=dungeon,key="first",npc_id=999111,name="第一位",is_boss=True)
        rows = fixture().decode().replace('Creature-1,','Creature-0-1-1-1-999111-A,').splitlines(keepends=True)
        # 第二位首领按交战名识别，同时施法的小怪不应获得首领列。
        second = rows[5].replace("999111-A","999222-B").replace('"首领"','"团本首领"')
        add = rows[5].replace("999111-A","999333-C").replace('"首领"','"小怪"')
        outside = second.replace("18:00:06","18:00:16").replace('22,"读条"','99,"战后施法"')
        report = self.ingest(self.owner, 7, "".join(rows[:6] + [second,add] + rows[6:-1] + [outside,rows[-1]]).encode())
        url = reverse("combat_replay", args=[report.pk,1])
        data = self.client.get(url).context["replay"]
        first,second = [next(p["id"] for p in data["actors"] if tag in p["guid"])
                        for tag in ("999111","999222")]
        windows = data["boss_windows"]
        self.assertEqual([(w["t"],w["end"],set(w["actors"])) for w in windows],
                         [(1000,1000,{first}),(3000,14000,{first,second}),(15000,15000,{first})])
        for fid in (1,2):
            scoped = reverse("combat_replay",args=[report.pk,fid])
            casts = self.client.get(scoped,{"track":"casts","chunk":0}).json()["casts"]
            self.assertEqual({c["actor"] for c in casts},{first,second})
            self.assertNotIn("99",{c["spell_id"] for c in casts})
        solo = self.client.get(scoped).context["replay"]
        self.assertEqual((solo["boss_windows"][0]["t"],solo["boss_windows"][0]["end"]), (0,11000))

    def test_replay_damage_checkpoints_casts_and_pet_credit(self):
        rows = fixture().splitlines(keepends=True)
        cast = 'SPELL_CAST_SUCCESS,Player-1,"甲",0,0,Creature-1,"首领",0,0,22,"读条",4'
        def at(second, row):
            return f"9/1/2026 18:00:{second}.000+8  {row}\r\n".encode()
        summon = 'SPELL_SUMMON,Player-1,"甲",0,0,Pet-9,"狼",0,0,88,"召唤",1'
        pet = rows[2].replace(b"Player-1", b"Pet-9", 1).replace(b"18:00:02", b"18:00:31")
        report = self.ingest(self.owner, 4, b"".join([
            *rows[:7], rows[2].replace(b"18:00:02",b"18:00:07"), *rows[7:-1],
            at(29,summon), at(29,summon.replace("Pet-9","Creature-9")), at(30,cast.replace("SUCCESS","START")), at(31,cast),
            at(31,cast.replace("Player-1","Player-2").replace("甲","乙")),
            at(31,cast.replace("SUCCESS","FAILED")), pet, pet.split(b",20,30,-1")[0]+b"\r\n",
            pet.replace(b"Pet-9",b"Creature-9"),
            rows[-1].replace(b"18:00:30",b"18:00:41"),
        ]))
        url = reverse("combat_replay", args=[report.pk, 1])
        WowSpellSnapshot.objects.create(branch="wow",locale="enUS",spell_id=22,icon="spell_frost_frostbolt02")
        manifest = self.client.get(url).context["replay"]
        self.assertTrue(manifest["cast_icons"][22].endswith("/small/spell_frost_frostbolt02.jpg"))
        actor = next(p["id"] for p in manifest["actors"] if p["guid"]=="Player-1")
        first,boss_pull,second,third = manifest["pulls"]
        self.assertEqual([(p["t"],p["end"]) for p in manifest["pulls"]],
                         [(1000,1000),(3000,14000),(15000,15000),(30000,30000)])
        self.assertEqual([p["label"] for p in manifest["pulls"]],["第 1 波","团本首领","第 2 波","第 3 波"])
        self.assertEqual(manifest["damage_buckets"], [(0,actor,20,0,boss_pull["id"]),(0,actor,20,0,first["id"]),
            (0,actor,50,0,second["id"]),(1,actor,20,0,0),(1,actor,20,1,third["id"])])
        chunk = self.client.get(url, {"chunk":1}).json()["activity"]
        self.assertEqual(chunk["damage"], [[30000,actor,20,0,0],[30000,actor,20,1,third["id"]]])
        self.assertEqual([r["t"] for r in chunk["casts"]], [29000,30000,30000,30000])
        cast_slice = self.client.get(url, {"track":"casts", "chunk":1}).json()
        self.assertEqual(cast_slice, {"casts":chunk["casts"][1:]})
        self.assertEqual(self.client.get(url, {"track":"casts", "chunk":1, "revision":"过期"}).status_code,409)
        self.assertEqual({r["spell_id"] for r in chunk["casts"]},{"22"})
        self.assertEqual([r["kind"] for r in chunk["casts"]], ["SPELL_CAST_START","SPELL_CAST_SUCCESS",
                                                               "SPELL_CAST_SUCCESS","SPELL_CAST_FAILED"])
        self.assertEqual(len({r["actor"] for r in chunk["casts"]}), 2)
        self.assertEqual([r["line"] for r in chunk["casts"]], sorted(r["line"] for r in chunk["casts"]))
        boss = reverse("combat_replay", args=[report.pk, 2])
        with patch("botend.services.combat_replay_activity._tooltip_icon",return_value="spell_frost_frostbolt02") as lookup:
            self.assertEqual(self.client.get(boss,{"cast_icon":22}).status_code,404)
            self.assertEqual(self.client.get(url,{"cast_icon":44}).status_code,404)
            self.assertEqual(self.client.get(url,{"cast_icon":22,"revision":"过期"}).status_code,409)
            lookup.assert_not_called()
            icon = self.client.get(url,{"cast_icon":22})
            self.assertEqual(icon.status_code,302)
            self.assertTrue(icon.url.endswith("/small/spell_frost_frostbolt02.jpg"))
            lookup.assert_called_once_with("wow",22)
        self.assertEqual(self.client.get(boss, {"chunk":0}).json()["activity"], {"casts":[],"damage":[[3000,actor,20,0,0]]})
        self.assertEqual(self.client.get(boss).context["replay"]["pulls"],[])
        report.summary["replay_damage_revision"] = 1
        report.save(update_fields=["summary"])
        self.assertEqual(self.client.get(url).context["replay"]["damage_buckets"], manifest["damage_buckets"])

    def test_spell_translation_respects_identity_and_published_names(self):
        ptr = WowTalentVersion.objects.create(key="names-ptr",branch="ptr")
        beta = WowTalentVersion.objects.create(key="names-beta",branch="beta")
        for version, sid, english, zh in ((ptr,55,"Aura","测试光环"),(ptr,56,"Earlier","旧名称"),
                                         (ptr,57,"Duplicate","译名甲"),(beta,57,"Duplicate","译名乙")):
            WowTalentNodeMetadata.all_objects.create(talent_version=version,name_kind="spell",localization_only=True,
                                                      reference_id=sid,name=english,name_zh=zh)
        WowSpellSnapshot.objects.create(branch="wow",locale="zhCN",spell_id=58,name="旧技能",
                                        display_data={"default":{"name_zh":"已发布中文名"}})
        self.assertEqual(localization.spell_names({55:"Aura",56:"New",57:"Duplicate",58:"Spell",59:"Aura"}),
                         {55:"测试光环",58:"已发布中文名"})

    def test_shared_chinese_names_join_game_ids_and_keep_raw_evidence(self):
        raw = fixture(3).decode().replace("Creature-1","Creature-0-1-2-3-9876-00000001").replace('"首领"','"Caster"')
        for previous, english in (("读条","Bolt"),("爆炸","Burst"),("光环","Mark"),("地城","Dungeon"),("团本首领","Encounter")):
            raw = raw.replace(previous,english)
        report = self.ingest(self.owner,3,raw.encode())
        version = MythicDungeonDataVersion.objects.create(key="names",label="名称",is_active=True)
        dungeon = MythicDungeon.objects.create(data_version=version,key="test",name="Dungeon",name_zh="测试地下城",map_id=2)
        MythicDungeonEnemy.objects.create(dungeon=dungeon,key="caster",npc_id=9876,name="Caster",name_zh="唤雷者")
        release = JournalRelease.objects.create(build="12.1.0",status="complete")
        JournalState.objects.create(active_release=release)
        instance = JournalInstance.objects.create(release=release,journal_id=21,name="测试手册",kind="dungeon")
        JournalEncounter.objects.create(instance=instance,journal_id=999,name="测试首领",payload={"dungeon_encounter_id":42})
        for sid, zh in ((22,"寒冰箭"),(33,"震荡爆发"),(55,"雷霆印记")):
            WowSpellSnapshot.objects.create(branch="wow",locale="zhCN",spell_id=sid,name=zh)
        catalog = analysis.catalog([report])[0]
        self.assertEqual([r["name"] for r in catalog],["测试地下城","测试首领"])
        self.assertEqual(report.summary["activity_index"][0]["name"],"Dungeon")
        replay = reverse("combat_replay",args=[report.pk,2])
        people = self.client.get(replay).context["replay"]["actors"]
        self.assertEqual({p["name"] for p in people},{"甲","唤雷者"})
        player = next(p["id"] for p in people if p["family"]=="Player")
        auras = self.client.get(replay,{"subject":player,"chunk":0}).json()["auras"]
        self.assertTrue(any(a["spell"]=="雷霆印记" and a["source"]=="唤雷者" for a in auras))
        result = self.client.get(replay,{"event_page":0,"kind":"SPELL_CAST_START","q":"寒冰箭"}).json()
        self.assertEqual([(e["source"],e["spell"]) for e in result["events"]],[("唤雷者","寒冰箭")]*2)
        for term in ("Bolt","Caster","22"):
            self.assertEqual(self.client.get(replay,{"event_page":0,"kind":"SPELL_CAST_START","q":term}).json(),result)
        archive_view = archive.read_report(report,2,14)
        self.assertEqual(archive_view["interrupts"][0]["spell"],"寒冰箭")
        self.assertEqual(archive_view["interrupts"][0]["target"],"唤雷者")
        damage = archive_view["last_damage"]
        self.assertEqual((damage["source"],damage["skill"]),("唤雷者","震荡爆发"))
        self.assertIn('"Burst"',damage["raw"])
        self.assertIn('"Caster"',damage["raw"])

    def test_replay_event_finder_uses_fight_clock_and_interrupted_spell(self):
        key = reverse("combat_replay", args=[self.report.pk, 1])
        boss = reverse("combat_replay", args=[self.report.pk, 2])
        params = {"kind": "SPELL_INTERRUPT", "event_page": 0}
        for query in ("读条", "22", "打断", "77", "甲", "首领"):
            result = self.client.get(key, dict(params, q=query)).json()
            self.assertFalse(result["more"])
            self.assertEqual([(e["t"], e["interrupted_spell"]) for e in result["events"]], [(12000, "读条")])
        self.assertEqual(self.client.get(boss, params).json()["events"][0]["t"], 9000)
        self.assertEqual(self.client.get(boss, dict(params, q="%")).json()["events"], [])
        for invalid in ({"event_page": -1}, dict(params, kind="未知"), dict(params, chunk=0)):
            self.assertEqual(self.client.get(boss, invalid).status_code, 400)
        self.assertEqual(self.client.get(boss, dict(params, revision="过期")).status_code, 409)

    def test_replay_event_pages_preserve_events_at_the_same_millisecond(self):
        rows = fixture().splitlines(keepends=True)
        report = self.ingest(self.owner, 3, b"".join(rows[:5] + [rows[4]]*26 + rows[5:]))
        url = reverse("combat_replay", args=[report.pk, 2])
        first, last = [self.client.get(url, {"event_page": page, "kind": "SPELL_CAST_START"}).json()
                       for page in (0, 1)]
        self.assertTrue(first["more"])
        self.assertFalse(last["more"])
        lines = [row["line"] for page in (first, last) for row in page["events"]]
        self.assertEqual(lines, sorted(set(lines)))
        self.assertEqual((len(first["events"]), len(last["events"])), (25, 3))

    def test_replay_chunks_overlap_and_preserve_coordinate_owner(self):
        lines = fixture().splitlines(keepends=True)
        movement = lines[6].replace(b"SPELL_DAMAGE", b"SWING_DAMAGE_LANDED").replace(
            ',33,"爆炸",4'.encode(), b""
        )
        key_end = lines[-1].replace(b"18:00:30", b"18:01:10")
        data = b"".join(lines[:-1] + [
            movement.replace(b"18:00:07", b"18:00:29"),
            movement.replace(b"18:00:07", b"18:00:32").replace(b",101.0,55.0", b",108.0,50.0"),
            movement.replace(b"18:00:07", b"18:01:05").replace(b",2201,", b",2202,"),
            key_end,
        ])
        report = self.ingest(self.owner, 3, data)
        url = reverse("combat_replay", args=[report.pk, 1])
        page = self.client.get(url)
        self.assertEqual([m["id"] for m in page.context["replay"]["maps"]], [2201, 2202])
        actor = next(a["id"] for a in page.context["replay"]["actors"] if a["family"] == "Player")
        first = self.client.get(url, {"chunk": 0}).json()["points"]
        second = self.client.get(url, {"chunk": 1}).json()["points"]
        for point in [p for p in first if p[1] >= 28000]:
            self.assertEqual(point[0], actor)
            self.assertIn(point, second)
        self.assertIn((31000, 108.0), [(p[1], p[3]) for p in second])
        taken = self.client.get(reverse("combat_analysis", args=[report.pk]), {"fight": 1, "metric": "taken"})
        self.assertEqual(taken.context["total"], 80)
        self.assertContains(taken, url)

    def test_replay_reads_short_advanced_layout_in_format_22(self):
        data = fixture().replace(b",1000,0,0,0,0,0,0,3,60,100", b",1000,0,0,0,0,3,60,100")
        data = data.replace(b"100,120,20,4", b"1,1,0,4")
        report = self.ingest(self.owner, 2, data)
        url = reverse("combat_replay", args=[report.pk, 2])
        points = self.client.get(url, {"chunk": 0}).json()["points"]
        self.assertEqual(points[0][2:5], [2201, 101.0, 55.0])
        self.assertEqual(points[0][-1], 1.25)

    def test_replay_floor_uses_client_bounds_and_mdt_landmarks(self):
        raw = fixture().replace(b",55,2,10", b",2813,587,10").replace(
            b"CHALLENGE_MODE_END,55", b"CHALLENGE_MODE_END,2813"
        ).replace(b"101.0,55.0,2201", b"8729.39089,-4882.56901,2433")
        report = self.ingest(self.owner, 3, raw)
        for fight in (1, 2):
            url = reverse("combat_replay", args=[report.pk, fight])
            terrain = self.client.get(url).context["replay"]["maps"][0]["terrain"]
            self.assertEqual(terrain["name"], "密谋小径")
            self.assertIn("murder-row/floor-1.webp", terrain["url"])
            x0, y0, x1, y1 = terrain["bounds"]
            u = (y1 + 4882.56901) / (y1 - y0)
            v = (x1 - 8729.39089) / (x1 - x0)
            a, b, c, d, e, f = terrain["matrix"]
            # 客户端世界位置对应 MDT 的 NPC 236085、第 4 个刷新点。
            self.assertAlmostEqual((a*u + c*v + e)*1920, 0.83240*1920, delta=3)
            self.assertAlmostEqual((b*u + d*v + f)*1280, 0.78206*1280, delta=3)

    def test_replay_floor_disambiguation_and_assets(self):
        from botend.services.combat_replay_maps import attach_floor_assets, catalog
        from django.contrib.staticfiles import finders
        from PIL import Image
        index = catalog()
        def terrain_at(map_id, instance=None):
            x0, y0, x1, y1 = index["floors"][str(map_id)]["bounds"]
            sample = dict(id=map_id, x0=x0, x1=x1, y0=y0, y1=y1)
            attach_floor_assets([sample], instance)
            return sample.get("terrain")

        self.assertIsNone(terrain_at(2433, 999))
        self.assertEqual(terrain_at(2095)["name"], "注能室")
        self.assertEqual(terrain_at(2094)["name"], "红玉瞭望台")
        self.assertNotEqual(terrain_at(2095)["matrix"], terrain_at(2094)["matrix"])
        self.assertEqual(terrain_at(2511)["source"], "客户端")
        for key, floor in index["floors"].items():
            if not floor.get("mdt") and not floor.get("bundled"):
                continue
            asset = terrain_at(int(key))["url"].split("/static/")[-1]
            self.assertTrue(finders.find(asset), asset)
            with Image.open(finders.find(asset)) as image:
                expected = (1920, 1280) if floor.get("mdt") else tuple(index["art"][str(floor["art"])][:2])
                self.assertEqual(image.size, expected)

    def test_native_floor_art_cache_and_missing_tiles(self):
        from PIL import Image
        from botend.services import combat_replay_maps as maps
        index = maps.catalog()
        floor = index["floors"]["186"]
        art = floor["art"]
        url = reverse("combat_replay_map", args=[index["build"], art])
        layout = index["art"][str(art)]
        with TemporaryDirectory() as root, self.settings(BASE_DIR=root):
            with patch.object(maps, "read_tile", side_effect=OSError("缺少切片")):
                self.assertEqual(self.client.get(url).status_code, 503)
            with patch.object(maps, "read_tile", return_value=Image.new("RGBA", tuple(layout[2:4]), "blue")) as reader:
                result = self.client.get(url)
                self.assertEqual(result["Content-Type"], "image/webp")
                self.assertEqual(reader.call_count, len(layout[4]))
                self.assertEqual(self.client.get(url).content, result.content)
                self.assertEqual(reader.call_count, len(layout[4]))
            self.assertEqual(self.client.get(reverse("combat_replay_map", args=["unknown", art])).status_code, 404)

    def test_native_floor_tile_order_and_partial_edges(self):
        from io import BytesIO
        from PIL import Image
        from botend.services.combat_replay_maps import compose
        layout = [33, 35, 32, 32, ["red", "blue", "yellow", "black"]]
        raw = compose(layout, lambda color: Image.new("RGBA", (32, 32), color))
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual(image.size, (33, 35))
            red, green, blue = image.getpixel((16, 16))[:3]
            self.assertGreater(red, max(green, blue) + 150)
            red, green, blue = image.getpixel((32, 16))[:3]
            self.assertGreater(blue, max(red, green) + 60)
            red, green, blue = image.getpixel((16, 34))[:3]
            self.assertGreater(min(red, green), blue + 100)

    def test_legacy_format20_retains_world_positions(self):
        data = fixture().replace(b"COMBAT_LOG_VERSION,22", b"COMBAT_LOG_VERSION,20")
        data = data.replace(b",1000,0,0,0,0,0,0,3,60,100", b",1000,0,0,0,0,3,60,100")
        report = self.ingest(self.owner, 4, data)
        url = reverse("combat_replay", args=[report.pk, 2])
        self.assertEqual(self.client.get(url, {"chunk": 0}).json()["points"][0][2:5], [2201, 101.0, 55.0])

    def test_replay_subject_auras_survive_seek_and_separate_casters(self):
        rows = fixture().splitlines(keepends=True)
        aura = rows[8]
        def change(second, action=b"APPLIED", suffix=b"DEBUFF", other=False):
            row = aura.replace(b"18:00:09", f"18:00:{second:02}".encode())
            row = row.replace(b"AURA_APPLIED", b"AURA_" + action).replace(b"DEBUFF", suffix)
            return row.replace(b"Creature-1", b"Creature-2") if other else row

        # 开战前施放、同技能双施法者、跨窗口、同毫秒移除与再次施加。
        data = b"".join([rows[0], change(0, suffix=b"BUFF"), *rows[1:8],
                         change(9), change(9, other=True), change(10, b"APPLIED_DOSE", b"DEBUFF,4"),
                         change(11, b"REFRESH"), change(32, b"REMOVED_DOSE", b"DEBUFF,2"),
                         change(33, b"REMOVED"), change(33), rows[13].replace(b":14.", b":34."),
                         rows[14].replace(b":15.", b":35."), rows[-1].replace(b":30.", b":45.")])
        report = self.ingest(self.owner, 2, data)
        url = reverse("combat_replay", args=[report.pk, 2])
        player = next(a["id"] for a in self.client.get(url).context["replay"]["actors"] if a["guid"]=="Player-1")
        def active(payload, t, category):
            return sorted((a["stacks"], a["action"]) for a in payload["auras"] if a["category"]==category
                          and a["t"]<=t and (a["until"] is None or a["until"]>t))
        first, second = [self.client.get(url, {"subject":player, "chunk":n}).json() for n in (0, 1)]
        self.assertEqual(active(first, 0, "BUFF"), [(1,"获得")])
        self.assertEqual(active(first, 7000, "DEBUFF"), [(1,"获得"), (4,"刷新")])
        self.assertEqual(active(first, 28000, "DEBUFF"), [(1,"获得"), (2,"减层")])
        self.assertEqual(active(first, 29000, "DEBUFF"), [(1,"获得"), (1,"获得")])
        self.assertEqual(active(second, 30000, "DEBUFF"), [])
        self.assertEqual(active(second, 30000, "BUFF"), [])
        self.assertEqual(second["health"][-1][2:], [0, None])
        self.assertNotIn("party_auras", self.client.get(url, {"chunk":0}).json())
        counted = self.client.get(url, {"chunk":0,"party_auras":1}).json()["party_auras"]
        self.assertEqual(counted, [[player,0,1,0], [player,5000,1,2], [player,30000,0,0]])
        self.assertFalse(self.client.get(url, {"chunk":1,"party_auras":1}).json()["party_auras"])
        records = self.client.get(url, {"kind":"DEBUFF","event_page":0}).json()["events"]
        self.assertIn(("SPELL_AURA_REMOVED_DOSE",2), [(r["kind"], r["aura_stacks"]) for r in records])

    def test_delayed_key_header_keeps_roster_offsets_and_session_boundaries(self):
        rows = fixture().splitlines(keepends=True)
        info = b"9/1/2026 18:00:01.000+8  COMBATANT_INFO,Player-1,0,270,[]\r\n"
        header = rows[0].replace(b":00.000", b":02.000")
        first = b"".join([rows[1], info, header, *rows[2:-1]])
        # 第二场在另一个版本会话中，之前的未完成钥匙应止于新开场的上一行。
        second = b"".join([rows[1], info, header, *rows[2:]]).replace(b"9/1/", b"9/2/")
        report = self.ingest(self.owner, 2, first+second)
        with analysis.database(report) as db:
            keys = db.execute("SELECT * FROM fight WHERE kind='钥匙' ORDER BY id").fetchall()
            self.assertEqual(len(keys), 2)
            self.assertEqual([k["session"] for k in keys], [1, 2])
            self.assertEqual(keys[0]["end_line"]+1, keys[1]["start_line"])
            self.assertEqual(keys[1]["end_line"], report.summary["lines"])
            self.assertEqual(keys[1]["result"], "已完成")
            for key in keys:
                hit = db.execute("SELECT * FROM fact WHERE keystone=? AND src_spec=270 ORDER BY line LIMIT 1", (key["id"],)).fetchone()
                self.assertTrue(hit)
                raw = (first+second)[hit["offset"]:hit["offset"]+hit["length"]]
                self.assertEqual(raw, (first+second).splitlines(keepends=True)[hit["line"]-1])
            self.assertEqual(db.execute("SELECT count(*) FROM fight WHERE parent IS NOT NULL").fetchone()[0], 2)

    def test_replay_uses_the_logged_specialization_icon(self):
        from botend.constants.wow import specialization_catalog

        rows = fixture().splitlines(keepends=True)
        info = b"9/1/2026 18:00:04.500+8  COMBATANT_INFO,Player-1,0,63,[]\r\n"
        report = self.ingest(self.owner, 2, b"".join(rows[:4] + [info] + rows[4:]))
        url = reverse("combat_replay", args=[report.pk, 2])
        actors = self.client.get(url).context["replay"]["actors"]
        fire = next(s for s in specialization_catalog() if s["spec_id"] == 63)
        player = next(a for a in actors if a["guid"] == "Player-1")
        self.assertEqual((player["spec"], player["spec_icon"]), (fire["label"], fire["icon"]))
        self.assertTrue(all("spec_icon" not in a for a in actors if a["family"] != "Player"))

    def test_replay_health_without_coordinates_and_threshold_crossings(self):
        rows = fixture().splitlines(keepends=True)
        def health(second, value):
            return (rows[6].replace(b":07.", f":{second:02}.".encode()).replace(b",700,1000,", f",{value},1000,".encode())
                    .replace(b",2201,", b",0,"))
        report = self.ingest(self.owner, 2, b"".join([rows[0], rows[1], rows[3], health(5,900),
            health(6,350),health(7,150),health(8,850),health(9,200),health(14,180),rows[14],rows[-1]]))
        url = reverse("combat_replay", args=[report.pk, 2])
        manifest = self.client.get(url).context["replay"]
        self.assertFalse(manifest["maps"])
        player = next(a["id"] for a in manifest["actors"] if a["guid"]=="Player-1")
        health_rows = self.client.get(url, {"subject":player,"chunk":0}).json()["health"]
        self.assertEqual([r[2] for r in health_rows], [900,350,150,850,200,180])
        low = self.client.get(url, {"subject":player,"threshold":35}).json()["samples"]
        self.assertEqual([(e["t"],e["reason"]) for e in low], [(2000,"跌至阈值"), (5000,"跌至阈值"), (10000,"采样间断后")])
        self.assertEqual(self.client.get(url, {"subject":player,"threshold":101}).status_code,400)
        self.assertEqual(self.client.get(url, {"subject":player,"threshold":35,"revision":"过期"}).status_code,409)
        self.assertEqual(self.client.get(url, {"subject":player,"chunk":0,"revision":"过期"}).status_code,409)
        self.client.force_login(self.member)
        for query in ({"subject":player,"threshold":35}, {"subject":player,"chunk":0}):
            self.assertEqual(self.client.get(url, query).status_code,404)

    def test_replay_legacy_subject_and_new_session(self):
        rows = fixture().splitlines(keepends=True)
        # 第一段在钥匙结束前再次获得光环，第二个日志头应当切断它。
        report = self.ingest(self.owner, 2, b"".join(rows[:-1] + [rows[8].replace(b":09.",b":27."), rows[-1]])+fixture(2))
        url = reverse("combat_replay", args=[report.pk, 3])
        player = next(a["id"] for a in self.client.get(url).context["replay"]["actors"] if a["family"]=="Player")
        payload = self.client.get(url, {"subject":player,"chunk":0}).json()
        self.assertTrue(all(a["t"]>=0 for a in payload["auras"]))
        report.summary.pop("state_revision")
        report.save(update_fields=["summary"])
        payload = self.client.get(url, {"subject":player,"chunk":0}).json()
        self.assertFalse(payload["detailed"])
        self.assertEqual(payload["auras"],[])
        self.assertTrue(payload["health"])
        self.assertContains(self.client.get(url),"从归档补建回放索引")

    def test_replay_empty_and_legacy_indexes(self):
        report = self.ingest(self.owner, 2, fixture(2).replace(b"ADVANCED_LOG_ENABLED,1", b"ADVANCED_LOG_ENABLED,0"))
        self.assertContains(self.client.get(reverse("combat_replay", args=[report.pk, 1])), "本场没有可播放的坐标")
        self.report.summary.pop("position_revision")
        self.report.save(update_fields=["summary"])
        url = reverse("combat_replay", args=[self.report.pk, 1])
        self.assertContains(self.client.get(url), "旧索引仅包含玩家位置")
        self.assertIsNone(self.client.get(url, {"chunk": 0}).json()["points"][0][-1])

    def test_drawer_choices_and_evidence_use_the_current_scope(self):
        url = reverse("combat_analysis", args=[self.report.pk])
        query = dict(fight=2, metric="taken", panel="pick", group="ability", term="爆炸")
        response = self.client.get(url, query, HTTP_X_COMBAT_VIEW="choices")
        data = response.json()
        self.assertEqual(data["count"], 1)
        self.assertEqual(str(data["items"][0]["id"]), "33")
        self.assertEqual(data["items"][0]["hits"], 1)
        self.assertIsNone(data["next"])
        query["start"] = 8
        self.assertEqual(self.client.get(url, query, HTTP_X_COMBAT_VIEW="choices").json()["count"], 0)
        evidence = reverse("combat_evidence", args=[self.report.pk, 7])
        raw = self.client.get(evidence, HTTP_X_COMBAT_VIEW="evidence").json()
        self.assertEqual(raw["line"], 7)
        self.assertEqual(raw["ability"], "爆炸")
        self.assertIn("SPELL_DAMAGE", raw["raw"])
        self.assertFalse(raw["clipped"])
        self.client.force_login(self.member)
        self.assertEqual(self.client.get(evidence, HTTP_X_COMBAT_VIEW="evidence").status_code, 404)
        self.assertEqual(self.client.get(url, query, HTTP_X_COMBAT_VIEW="choices").status_code, 404)

    def test_report_sequence_preserves_filters_and_prevents_nested_double_count(self):
        report = self.ingest(self.owner, 2, fixture(1) + fixture(2))
        url = reverse("combat_analysis", args=[report.pk])
        page = self.client.get(url, {"metric": "taken"})
        self.assertEqual(page.context["total"], 160)
        self.assertEqual(page.context["covered"], 2)
        self.assertEqual([s["id"] for s in page.context["comparisons"]], [1, 3])
        page = self.client.get(
            url,
            {"part": ["2", "4"], "metric": "taken", "ability": "33", "panel": "events"},
        )
        self.assertEqual(page.context["total"], 160)
        self.assertEqual({e["sample"] for e in page.context["events"]}, {2, 4})
        first = self.client.get(url + page.context["comparisons"][0]["url"])
        self.assertEqual(first.context["position"], 1)
        self.assertEqual(first.context["total"], 80)
        second = self.client.get(url + first.context["next_fight"]["url"])
        self.assertEqual(second.context["selected"]["id"], 4)
        self.assertEqual(second.context["panel"], "events")
        combined = self.client.get(url + second.context["combined_url"])
        self.assertEqual(combined.context["total"], 160)
        self.assertEqual(combined.wsgi_request.GET.getlist("part"), ["2", "4"])

    def test_players_best_manual_fallback_and_access(self):
        better = self.ingest(
            self.owner, 2, fixture(2).replace(b",20,30,-1", b",200,300,-1")
        )

        def player_b(day):
            return (
                fixture(day)
                .replace(b"Player-1", b"Player-2")
                .replace("甲".encode(), "乙".encode())
            )

        right = self.ingest(self.owner, 3, player_b(3))
        self.ingest(
            self.owner,
            4,
            player_b(4)
            .replace(b",55,2,10,[", b",55,2,12,[")
            .replace(b",50,55,-1", b",5000,5500,-1"),
        )
        # 总伤害更高、耗时更久的记录不能按总量夺走每秒最佳。
        slower = (
            fixture(5)
            .replace(b",20,30,-1", b",220,300,-1")
            .replace(b"18:00:30.", b"18:00:59.")
        )
        self.ingest(self.owner, 5, slower)
        url = reverse("combat_player_comparison")
        query = dict(left="Player-1", right="Player-2")
        page = self.client.get(url, query)
        a, b = page.context["winners"]
        self.assertEqual(
            (a["sample"]["report"].pk, b["sample"]["report"].pk), (better.pk, right.pk)
        )
        self.assertEqual((a["amount"], b["amount"]), (250, 70))
        self.assertEqual(a["seconds"], 29)
        self.assertEqual(
            sum(t["values"][0] or 0 for t in page.context["timeline"]), 250
        )
        cell = next(r for r in page.context["skills"] if r["spell"] == "11")["sides"][0]
        self.assertEqual(self.client.get(cell["url"]).context["total"], cell["amount"])
        # 旧索引没有预聚合时仍使用相同的事件口径。
        self.report.summary.pop("player_totals_version")
        self.report.summary.pop("player_catalog")
        self.report.save(update_fields=["summary"])
        page = self.client.get(
            url,
            dict(query, left_file=self.report.pk, right_file=right.pk, left_fight="1"),
        )
        self.assertEqual([r["amount"] for r in page.context["winners"]], [70, 70])
        mismatch = self.client.get(
            url,
            dict(
                query,
                left_file=self.report.pk,
                left_fight="2",
                right_file=right.pk,
                right_fight="1",
            ),
        )
        self.assertContains(mismatch, "没有同一副本或首领")
        self.assertFalse(mismatch.context.get("winners"))
        hps = self.client.get(url, dict(query, metric="heal", detail="interrupts"))
        self.assertEqual(hps.context["unit"], "分钟")
        self.assertEqual([w["sample"]["id"] for w in hps.context["winners"]], [2, 2])
        self.assertEqual([w["amount"] for w in hps.context["winners"]], [75, 75])
        self.assertEqual(hps.context["skills"][0]["sides"][0]["hits"], 1)
        self.client.force_login(self.member)
        self.assertEqual(
            self.client.get(url, dict(query, left_file=better.pk)).status_code, 404
        )
        self.assertNotContains(self.client.get(url), "Player-1")

    def test_team_sources_bookmark_rules_and_revocation(self):
        book = CombatLogCollection.objects.create(owner=self.owner, title="开荒")
        book.reports.add(self.report)
        url = reverse("combat_collection", args=[book.pk])
        self.client.post(url, {"action": "invite", "username": self.member.username})
        self.client.force_login(self.member)
        other = self.ingest(self.member, 2)
        self.client.post(url, {"action": "attach", "report": other.pk})
        cohort = self.samples[1]["cohort"]
        params = {"cohort": cohort, "metric": "taken", "group": "ability"}
        page = self.client.get(url, params)
        self.assertContains(page, "跨日志样本")
        self.assertEqual(page.context["total"], 160)
        self.assertEqual(len(page.context["comparisons"]), 2)
        self.assertEqual(
            self.client.get(
                reverse("combat_download", args=[self.report.pk])
            ).status_code,
            404,
        )
        event = reverse("combat_evidence", args=[self.report.pk, 7])
        self.assertContains(self.client.get(event), "爆炸")
        self.client.force_login(self.owner)
        from urllib.parse import urlencode

        query = url + "?" + urlencode(params)
        self.client.post(query, {"action": "bookmark", "name": "承伤"})
        self.assertEqual(book.bookmarks.get().filters, params)
        for kind, trigger, response, label in [
            ("impact", 22, 33, "记录受击"),
            ("dispel", 55, 55, "记录驱散"),
        ]:
            page = self.client.post(
                query,
                {
                    "action": "rule",
                    "name": label,
                    "kind": kind,
                    "trigger": trigger,
                    "response": response,
                    "window": 4,
                },
                follow=True,
            )
            self.assertEqual(page.context["matrix"]["totals"], {label: 2})
            self.assertTrue(
                all(op["responses"] for op in page.context["matrix"]["rounds"])
            )
        self.client.post(url, {"action": "revoke", "username": self.member.username})
        self.assertEqual(book.reports.count(), 1)
        self.client.force_login(self.member)
        for path in [event, url, reverse("combat_report", args=[self.report.pk])]:
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(
            self.client.get(reverse("combat_report", args=[other.pk])).status_code, 200
        )

    def test_explore_pivot_cell_and_event_scope(self):
        url = reverse("combat_analysis", args=[self.report.pk])
        query = dict(
            fight=2, metric="taken", group="target", cross="ability", panel="pivot"
        )
        result = self.client.get(url, query)
        self.assertContains(result, "交叉表")
        row = result.context["rows"][0]
        self.assertEqual(row["name"], "甲")
        self.assertEqual(row["cells"][0]["value"], 80)
        detail = self.client.get(url + row["cells"][0]["url"])
        self.assertEqual([e["line"] for e in detail.context["events"]], [7])
        self.assertContains(detail, "SPELL_DAMAGE")
        for metric, side, total in [
            ("damage", "npc", 80),
            ("dispels", "players", 1),
            ("auras", "players", 1),
            ("enemycasts", "players", 2),
            ("deaths", "players", 1),
        ]:
            page = self.client.get(
                url, dict(fight=2, metric=metric, side=side, panel="events")
            )
            self.assertEqual(page.context["total"], total)
        self.assertContains(page, "死亡复盘")

    def test_curve_uses_the_filtered_table_and_bucket_boundaries(self):
        url = reverse("combat_analysis", args=[self.report.pk])
        query = dict(
            fight=1,
            metric="damage",
            group="ability",
            seriesgroup="ability",
            series=["11"],
        )
        ctx = self.client.get(url, query).context
        self.assertEqual(ctx["curves"][0]["values"], [20, 50])
        self.assertEqual(sum(ctx["curves"][0]["values"]), ctx["total"])
        ctx = self.client.get(url, dict(query, start=10)).context
        self.assertEqual(ctx["curves"][0]["values"], [50])
        ctx = self.client.get(url, dict(query, group="target")).context
        self.assertFalse(ctx["curves"])

    def test_team_pivot_cell_includes_events_from_both_reports(self):
        book = CombatLogCollection.objects.create(
            owner=self.owner, title="同技能跨场比较"
        )
        other = self.ingest(self.owner, 2)
        book.reports.add(self.report, other)
        url = reverse("combat_collection", args=[book.pk])
        page = self.client.get(
            url,
            dict(
                cohort=self.samples[1]["cohort"],
                metric="taken",
                panel="pivot",
                group="target",
                cross="ability",
            ),
        )
        cell = page.context["rows"][0]["cells"][0]
        self.assertEqual(cell["value"], 160)
        detail = self.client.get(url + cell["url"])
        self.assertEqual(
            {event["filename"] for event in detail.context["events"]},
            {self.report.filename, other.filename},
        )
        self.assertEqual(sum(e["value"] for e in detail.context["events"]), 160)

    def test_select_duplicate_recording_explicitly(self):
        book = CombatLogCollection.objects.create(owner=self.owner, title="同场双视角")
        other = self.ingest(self.member, 1)
        book.reports.add(self.report, other)
        url = reverse("combat_collection", args=[book.pk])
        params = {"cohort": self.samples[1]["cohort"], "metric": "taken"}
        page = self.client.get(url, params)
        self.assertEqual(page.context["total"], 80)
        self.assertEqual(len(page.context["skipped"]), 1)
        page = self.client.get(url, dict(params, file=other.pk))
        self.assertEqual(page.context["comparisons"][0]["report"].pk, other.pk)
        self.assertEqual(page.context["total"], 80)

    def test_compare_selected_attempts_and_reopen_bookmark(self):
        from urllib.parse import urlencode

        book = CombatLogCollection.objects.create(
            owner=self.owner, title="指定开荒场次"
        )
        other = self.ingest(self.owner, 2)
        latest = self.ingest(self.owner, 3)
        book.reports.add(self.report, other, latest)
        url = reverse("combat_collection", args=[book.pk])
        boss = self.samples[1]
        keys = [f"{r.pk}.{boss['start_line']}" for r in (self.report, latest)]
        query = "?" + urlencode(
            {"cohort": boss["cohort"], "metric": "taken", "attempt": keys}, doseq=True
        )
        self.client.post(url + query, {"action": "bookmark", "name": "开荒首末场"})
        saved = self.client.get(url).context["bookmarks"][0]["url"]
        page = self.client.get(url + saved)
        self.assertEqual(page.context["total"], 160)
        self.assertEqual(
            {r["report"].pk for r in page.context["comparisons"]},
            {self.report.pk, latest.pk},
        )
        book.reports.remove(latest)
        page = self.client.get(url + saved)
        self.assertEqual(page.context["total"], 80)
        self.assertTrue(page.context["skipped"])

    def test_roster_required_for_overlapping_recordings(self):
        other = self.ingest(self.member, 2)
        first = dict(self.samples[1], roster="相同")
        second = dict(first, report=other)

        def count(variant):
            return len(analysis.unique_attempts([first, variant])[0])

        self.assertEqual(count(second), 1)
        self.assertEqual(count(dict(second, roster="另一队")), 2)
        self.assertEqual(count(dict(second, roster="")), 2)
        self.assertEqual(count(dict(second, start_ms=first["end_ms"])), 2)

    def test_ai_snapshot_is_validated_and_reused(self):
        url = reverse("combat_review", args=[self.report.pk, 2, "mechanic", "22"])
        fetch = {"HTTP_SEC_FETCH_DEST":"empty"}
        self.assertContains(self.client.get(url, **fetch), "响应")
        with patch("core.glm.GLMClient") as ctor:
            ai = ctor.return_value
            ai.available = True
            ai.send_message.return_value = '[{"hypothesis":"需查看第二次响应","check":"检查打断原行","lines":[12,13]}]'
            self.assertContains(self.client.post(url), "检查打断原行")
            self.assertContains(self.client.get(url, **fetch), "检查打断原行")
            self.client.post(url)
            self.assertEqual(ai.send_message.call_count, 1)
            self.assertNotIn("Player-1", ai.send_message.call_args.args[0])
            ai.send_message.return_value = (
                '[{"hypothesis":"示例","check":"复核","lines":[9000]}]'
            )
            death = reverse("combat_review", args=[self.report.pk, 2, "death", "14"])
            self.assertContains(self.client.post(death), "未返回可引用")
        self.assertEqual(CombatLogReview.objects.exclude(findings=[]).count(), 1)

    def test_offline_pack_live_evidence_restore_and_rebuild(self):
        source = fixture()
        transfer(self.report.pk, "cold")
        self.report.refresh_from_db()
        self.assertTrue(
            (archive.directory(self.report, "cold") / "archive.zip").is_file()
        )
        self.assertFalse(list(archive.directory(self.report).glob("*.gz")))
        self.assertEqual(b"".join(archive.archived_chunks(self.report)), source)
        self.assertContains(
            self.client.get(reverse("combat_evidence", args=[self.report.pk, 7])),
            "爆炸",
        )
        old = self.report.published
        with patch.object(archive, "launch") as worker:
            with self.captureOnCommitCallbacks(execute=True):
                archive.enqueue(self.owner, self.report.pk, rebuild=True)
        self.report.refresh_from_db()
        self.assertEqual(self.report.published, old)
        self.assertEqual(archive.index_path(self.report).stem, str(old))
        archive.collect(*worker.call_args.args)
        self.report.refresh_from_db()
        self.assertNotEqual(self.report.published, old)
        self.assertEqual(self.report.digest, hashlib.sha256(source).hexdigest())
        transfer(self.report.pk, "hot")
        self.report.refresh_from_db()
        self.assertEqual(b"".join(archive.archived_chunks(self.report)), source)
        self.assertEqual(
            analysis.run([analysis.catalog([self.report])[0][1]], {"metric": "heal"})[
                "total"
            ],
            75,
        )

    def test_collection_ai_only_sends_owned_samples_and_invalidates_changed_scope(self):
        from urllib.parse import urlencode

        book = CombatLogCollection.objects.create(owner=self.owner, title="机制对照")
        book.reports.add(self.report, self.ingest(self.owner, 2))
        cohort = self.samples[1]["cohort"]
        rule = book.mechanics.create(
            name="爆炸",
            kind="impact",
            cohort=cohort,
            trigger="22",
            response="33",
            window=4,
        )
        url = (
            reverse("combat_collection", args=[book.pk])
            + "?"
            + urlencode({"cohort": cohort, "rule": rule.pk})
        )
        with patch("core.glm.GLMClient") as ctor:
            model = ctor.return_value
            model.available = True
            model.send_message.return_value = '[{"hypothesis":"两轮均有受击记录","check":"查看触发与响应","lines":[1,2]}]'
            page = self.client.post(url, {"action": "generate-rule"})
            self.assertContains(page, "两轮均有受击记录")
            self.assertEqual(len(page.context["rule_notes"][0]["urls"]), 2)
            self.assertNotIn("Player-1", model.send_message.call_args.args[0])
            self.client.post(url, {"action": "generate-rule"})
            self.assertEqual(model.send_message.call_count, 1)
            book.reports.add(self.ingest(self.member, 3))
            self.assertNotContains(self.client.get(url), "两轮均有受击记录")
            self.assertEqual(
                self.client.post(url, {"action": "generate-rule"}).status_code, 404
            )
            self.assertEqual(model.send_message.call_count, 1)

    def test_idle_upload_releases_reservation_and_resume_rechecks_quota(self):
        with self.settings(COMBAT_LOG_ACCOUNT_BYTES=self.report.size + 1024):
            upload = archive.create(self.owner, "断点.txt", 1024)
            archive.pause_upload(self.owner, upload.pk)
            upload.refresh_from_db()
            self.assertEqual(upload.reserved_bytes, 0)
            another = archive.create(self.owner, "另一份.txt", 1024)
            with self.assertRaisesMessage(ValueError, "配额不足"):
                archive.put_part(self.owner, upload.pk, 0, b"x" * 1024)
            archive.pause_upload(self.owner, another.pk)
            archive.put_part(self.owner, upload.pk, 0, b"x" * 1024)
            upload.refresh_from_db()
            self.assertEqual((upload.state, upload.reserved_bytes), ("uploading", None))

    def test_queue_recovers_lost_worker_without_publishing_abandoned_attempt(self):
        from django.utils import timezone

        with patch.object(archive, "launch"):
            archive.enqueue(self.owner, self.report.pk, rebuild=True)
            self.report.refresh_from_db()
            abandoned = self.report.attempt
            type(self.report).objects.filter(pk=self.report.pk).update(
                state="running", updated_at=timezone.now() - archive.LEASE * 2
            )
            self.assertTrue(archive.drain())
            self.report.refresh_from_db()
            self.assertEqual((self.report.state, self.report.recoveries), ("ready", 1))
            self.assertNotEqual(self.report.published, abandoned)
            self.assertFalse(archive.collect(self.report.pk, abandoned))

@override_settings(SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies")
class BossReviewBrowserTests(StaticLiveServerTestCase):
    @classmethod
    def _make_connections_override(cls):
        return {}

    def test_wait_page_handles_gateway_failure_offline_and_deadline(self):
        from playwright.sync_api import expect, sync_playwright
        self.client.force_login(get_user_model().objects.create_user(username="等待异常"))
        url = self.live_server_url + reverse("combat_wcl_report", args=["A"*16])
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page(viewport={"width":390, "height":820}, color_scheme="dark")
                page.context.add_cookies([{"url":self.live_server_url, "name":"sessionid",
                                           "value":self.client.cookies["sessionid"].value}])
                page.route("**/*_clwait=*", lambda route:route.fulfill(status=504, body="Gateway timeout"))
                page.goto(url)
                expect(page.get_by_role("alert")).to_contain_text("服务器响应超时")
                expect(page.locator(".portal-header")).to_be_visible()
                expect(page.get_by_role("link",name="返回日志库")).to_be_visible()
                page.unroute("**/*_clwait=*")
                page.add_init_script("""{
                    const fetchNormally = window.fetch.bind(window);
                    window.fetch = (url, options) => String(url).includes('_clwait=')
                        ? new Promise((resolve, reject) => options.signal.addEventListener('abort',
                              () => reject(new DOMException('已中止', 'AbortError')), {once:true}))
                        : fetchNormally(url, options);
                }""")
                page.clock.install()
                page.get_by_role("link", name="重新读取").click()
                expect(page.locator("[data-stop]")).to_be_visible()
                page.clock.fast_forward(121000)
                expect(page.get_by_role("alert")).to_contain_text("已超过 2 分钟")
                page.get_by_role("link", name="重新读取").click()
                expect(page.locator("[data-stop]")).to_be_visible()
                page.context.set_offline(True)
                expect(page.get_by_role("alert")).to_contain_text("网络连接已断开")
            finally:
                browser.close()

    def test_open_slow_wcl_report_and_retry(self):
        from threading import Event
        from playwright.sync_api import expect, sync_playwright
        from botend.services.combat_wcl_account import WclError
        from botend.services import combat_wcl_catalog
        from botend.tests.test_combat_wcl_catalog import source

        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root/"hot", COMBAT_LOG_COLD_ROOT=root/"cold"))
        self.client.force_login(get_user_model().objects.create_user(username="读取进度"))
        release = Event()
        calls = []
        def delayed(codes, binding):
            calls.append(codes)
            if len(calls) == 1:
                release.wait(12)
            if len(calls) <= 2:
                raise WclError("WCL 报告读取失败")
            rows = [source(codes[0])]
            combat_wcl_catalog.summarize(rows)
            return rows
        path = reverse("combat_wcl_report", args=["A"*16])
        with patch.object(combat_wcl_catalog, "fetch_reports", side_effect=delayed), sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page()
                page.context.add_cookies([{"url":self.live_server_url, "name":"sessionid",
                                           "value":self.client.cookies["sessionid"].value}])
                page.goto(self.live_server_url + path, wait_until="commit")
                page.wait_for_function("""() => {
                    if (!document.querySelector('[data-stage]')?.innerText.includes('向 WCL 请求 1 份报告')
                        || document.querySelector('.combat-wait-time').innerText.includes('已等待 0 秒')) return false;
                    document.querySelector('[data-stop]').click();
                    return true;
                }""", timeout=8000)
                expect(page.locator("[data-stage]")).to_have_text("已停止等待。")
                release.set()
                page.get_by_role("link", name="重新尝试").click()
                expect(page.get_by_role("alert")).to_contain_text("WCL 报告读取失败")
                page.get_by_role("link", name="重新读取", exact=True).click()
                expect(page.get_by_role("heading", name="战斗分组")).to_be_visible()
                self.assertEqual(len(calls), 3)
                self.assertEqual(page.url, self.live_server_url + path)
                page.goto(self.live_server_url + reverse("combat_logs") + "?source=uploads")
                continuations = []
                page.on("request", lambda req: continuations.append(req.url) if "_clwait=" in req.url else None)
                page.goto(self.live_server_url + path)
                expect(page.get_by_role("heading", name="战斗分组")).to_be_visible()
                self.assertEqual(continuations, [])
                self.assertEqual(len(calls), 3)
            finally:
                release.set()
                browser.close()

    def test_raid_chart_window_and_anchor_navigation(self):
        from playwright.sync_api import expect, sync_playwright

        root = Path(self.enterContext(TemporaryDirectory()))
        self.enterContext(self.settings(COMBAT_LOG_ROOT=root/"hot", COMBAT_LOG_COLD_ROOT=root/"cold"))
        user = get_user_model().objects.create_user(username="首领复盘验收")
        for day in (2, 3):
            raw = raid_fixture(day, day == 3)
            report = archive.create(user, f"团本第{day}晚.txt", len(raw))
            archive.put_part(user, report.pk, 0, raw)
            with patch.object(archive, "launch", side_effect=archive.collect):
                archive.enqueue(user, report.pk)
        self.client.force_login(user)
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge")
            try:
                page = browser.new_page(viewport={"width":1280, "height":800})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.context.add_cookies([{"url":self.live_server_url, "name":"sessionid",
                                           "value":self.client.cookies["sessionid"].value}])
                page.goto(self.live_server_url+reverse("combat_logs"))
                page.locator("#log-selection").wait_for()
                for box in page.locator("#log-selection input[type=checkbox]").all():
                    box.check()
                page.get_by_role("button", name="分析所选日志").click()
                page.get_by_role("link", name="首领开荒复盘", exact=True).click()
                expect(page.locator("#boss-chart")).to_be_visible()
                page.locator("#boss-cursor").fill("7")
                readings = page.locator("[data-cursor-hp]")
                expect(readings.first).to_have_text("25%")
                expect(readings.nth(1)).to_contain_text("+10.0 个百分点")
                page.get_by_role("button", name="对比前后 5 秒").click()
                expect(page.get_by_role("heading", name="窗口内的行动顺序")).to_be_visible()
                page.set_viewport_size({"width":390, "height":844})
                row = page.locator("#anchors tbody tr").filter(has_text="打断结果不同")
                row.locator("summary").last.click()
                anchor = row.get_by_role("link", name="定位回放").last
                expect(anchor).to_be_visible()
                self.assertIn("?t=1500", anchor.get_attribute("href"))
                self.assertLessEqual(page.evaluate("document.documentElement.scrollWidth"), 391)
                self.assertFalse(errors)
            finally:
                browser.close()
