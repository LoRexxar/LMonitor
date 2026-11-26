from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from botend.models import CombatLogReport, WclUserBinding
from botend.services import combat_wcl_index as index
from botend.services import combat_log_analysis as analysis, combat_replay_activity as activity


class WclSingleTests(TestCase):
    code = "A" * 16

    def setUp(self):
        root = self.enterContext(TemporaryDirectory())
        self.enterContext(self.settings(COMBAT_LOG_ROOT=Path(root)/"logs"))
        self.user = get_user_model().objects.create_user(username="单场测试")
        self.client.force_login(self.user)
        self.url = reverse("combat_wcl_single",args=[self.code,12])
        self.requests = []
        self.enterContext(patch.object(index,"read",side_effect=self.upstream))

    def upstream(self, binding, query, variables):
        self.requests.append((query,variables))
        if query == index.ACCESS:
            return {"code":self.code}
        if query == index.META:
            fight = dict(id=12,encounterID=12923,name="Voidscar Arena",difficulty=10,
                         keystoneLevel=21,keystoneAffixes=[],kill=False,fightPercentage=None,
                         startTime=1000,endTime=11000,dungeonPulls=[
                             dict(id=1,encounterID=0,name="第一组",startTime=1000,endTime=3000,kill=False),
                             dict(id=2,encounterID=77,name="测试首领",startTime=7000,endTime=10000,kill=True)])
            actors = [dict(id=1,type="Player",gameID=123,name="队员"),dict(id=2,type="NPC",gameID=77,name="敌人")]
            return dict(code=self.code,title="测试日志",zone={"id":55,"name":"Mythic+ Season 2"},startTime=1730000000000,
                        fights=[fight],masterData=dict(actors=actors,abilities=[dict(gameID=5,name="技能")]))
        if query != index.EVENTS:
            self.fail("意外的数据请求")
        if variables["start"] == 1000:
            return {"events":{"data":[self.hit(1200,50),self.hit(2500,150)],"nextPageTimestamp":7000}}
        return {"events":{"data":[self.hit(7200,100)],"nextPageTimestamp":None}}

    @staticmethod
    def hit(ms, amount):
        return dict(type="damage",fight=12,timestamp=ms,sourceID=1,targetID=2,abilityGameID=5,amount=amount)

    def test_wcl_combatant_build_and_source_icons(self):
        upstream = self.upstream

        def with_metadata(binding, query, variables):
            payload = upstream(binding, query, variables)
            if query == index.META:
                payload["masterData"]["abilities"][0]["icon"] = "spell_arcane_blast.jpg"
                payload["masterData"]["actors"][0]["subType"] = "Mage"
            if query == index.EVENTS and variables["start"] == 1000:
                payload["events"]["data"].insert(0, dict(type="combatantinfo", timestamp=1000,
                    fight=12, sourceID=1, specID=63, talentTree=[dict(id=801, rank=2, nodeID=701, spellID=5)]))
            return payload

        with patch.object(index, "read", side_effect=with_metadata):
            page = self.client.get(self.url, follow=True)
            player = page.context["players"][0]
            self.assertEqual(player["icon_label"], "法师 · 火焰")
            actor = self.client.get(player["actor_url"])
            self.assertContains(actor, "spell_arcane_blast.jpg")
            self.assertEqual(actor.context["builds"][0]["items"][0]["rank"], 2)
            skill = self.client.get(page.request["PATH_INFO"], dict(fight=12, group="ability"))
            self.assertContains(skill, "spell_arcane_blast.jpg")

    def test_native_wave_metrics_and_evidence(self):
        page = self.client.get(self.url,follow=True)
        self.assertTemplateUsed(page,"dashboard/combat_explorer.html")
        ctx = page.context
        self.assertEqual((ctx["total"],ctx["rate"],ctx["active_wave"]["id"]),(200,100,1))
        self.assertEqual(len(ctx["waves"]),2)
        native = reverse("combat_analysis",args=[ctx["report"].pk])
        whole = self.client.get(native,{"fight":12,"wave":"all"})
        self.assertEqual((whole.context["total"],whole.context["rate"]),(300,30))
        boss = self.client.get(native,{"fight":12,"wave":ctx["waves"][1]["id"],"panel":"events"})
        self.assertEqual(boss.context["total"],100)
        event = self.client.get(boss.context["events"][0]["url"],HTTP_X_COMBAT_VIEW="evidence")
        self.assertIn('"timestamp":7200',event.json()["raw"])
        self.assertEqual(sum(q == index.EVENTS for q,_ in self.requests),2)
        self.assertFalse(CombatLogReport.objects.exists())
        report = ctx["report"]
        with analysis.database(report) as db:
            for fight in db.execute("SELECT * FROM fight"):
                args = db, fight, fight["end_ms"]
                buckets = activity.checkpoints(*args, report.summary["replay_damage_revision"])
                self.assertEqual(buckets, activity.checkpoints(*args, None))
                self.assertTrue(buckets)
                self.assertEqual(sum(row[2] for row in buckets), 300 if not fight["parent"] else 100)
        self.client.force_login(get_user_model().objects.create_user(username="另一个账号"))
        self.assertEqual(self.client.get(native).status_code,404)

    def test_unbinding_blocks_cached_private_source(self):
        binding = WclUserBinding.objects.create(user=self.user,wcl_id=100)
        page = self.client.get(self.url,{"channel":"private"},follow=True)
        self.assertEqual(page.status_code,200)
        pk = page.context["report"].pk
        binding.delete()
        self.assertEqual(self.client.get(reverse("combat_analysis",args=[pk])).status_code,404)

    def test_browser_entry_reports_real_event_pages_without_repeating_index(self):
        headers = {"HTTP_SEC_FETCH_DEST": "document", "HTTP_ACCEPT": "text/html"}
        shell = self.client.get(self.url, **headers)
        self.assertTemplateUsed(shell, "dashboard/combat_loading.html")
        self.assertFalse(self.requests)
        result = self.client.get(shell.context["continue_url"], **headers)
        self.assertEqual(result.status_code, 302)
        self.assertNotIn("_clwait", result.url)
        status_url = shell.context["progress_url"]
        self.assertIn("2 页", self.client.get(status_url).json()["message"])
        self.assertEqual(sum(q == index.EVENTS for q, _ in self.requests), 2)
        self.client.force_login(get_user_model().objects.create_user(username="独立进度"))
        self.assertIsNone(self.client.get(status_url).json()["message"])

    def test_broken_pagination_does_not_publish_partial_index(self):
        def stalled(binding,query,variables):
            if query == index.EVENTS:
                return {"events":{"data":[self.hit(1200,50)],"nextPageTimestamp":variables["start"]}}
            return self.upstream(binding,query,variables)
        with patch.object(index,"read",side_effect=stalled):
            self.assertEqual(self.client.get(self.url).status_code,503)
        self.assertEqual(list(index.folder().glob("*.sqlite3")),[])
        self.assertEqual(self.client.get(self.url,follow=True).status_code,200)
