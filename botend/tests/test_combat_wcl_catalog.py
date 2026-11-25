from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from botend.models import CombatPublicSource, CombatPublicRolePolicy, UserCharacterBinding

from botend.services import combat_wcl_catalog as catalog
from botend.services import combat_wcl_account as wcl


def source(code, offset=0, difficulty=5, realm="死亡之翼"):
    fights = [dict(id=i, encounterID=3429, name="The Coiled Altar", difficulty=difficulty, kill=False,
                   startTime=i*100000, endTime=i*100000+65000, fightPercentage=75-i, bossPercentage=35-i,
                   lastPhase=1, friendlyPlayers=list(range(1,6)), keystoneAffixes=None, keystoneLevel=None)
              for i in (1,2)]
    return dict(code=code,title="The Venomous Abyss",zone={"id":48,"name":"The Venomous Abyss"},
                startTime=1710000000000+offset,owner={"name":"记录者"},fights=fights,
                masterData={"actors":[dict(id=i,name=f"角色{i}",server=realm) for i in range(1,6)]})


class WclCatalogTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_user(username="开荒选场"))
        self.a,self.b="A"*16,"B"*16

    def test_only_matching_raid_views_collapse(self):
        reports = [source(self.a),source(self.b,2000),source("C"*16,1000,4),source("D"*16,realm="其他服")]
        catalog.summarize(reports)
        mythic,heroic=catalog.attempts(reports)
        self.assertEqual((mythic["recorded"],len(mythic["pulls"]),len(heroic["pulls"])),(6,4,2))
        self.assertEqual(mythic["name"],"盘卷祭坛")

    @patch("botend.services.combat_public_roles.profile",return_value=(("Warrior","Fury"),"deathwing"))
    @patch.object(wcl,"read")
    def test_connected_character_returns_to_shared_library_and_survives_next_visit(self,read,profile):
        read.return_value={"characterData":{"character":{"recentReports":{"data":[source(self.a)],"has_more_pages":False}}}}
        identity=dict(name="角色",region="cn",realm="死亡之翼",kind="character")
        added=self.client.post(reverse("combat_wcl"),identity,follow=True)
        self.assertTemplateUsed(added,"dashboard/combat_library.html")
        self.assertContains(added,"选择日志")
        self.assertNotContains(added,"首领对比复盘")
        self.assertEqual(read.call_count, 1)
        self.client.get(reverse("combat_logs"), {"source":added.context["source"].pk})
        self.assertEqual(read.call_count, 2)
        self.client.post(reverse("combat_wcl"),identity)
        self.assertEqual(CombatPublicSource.objects.count(),1)
        self.assertContains(self.client.get(reverse("combat_wcl")), "狂怒")
        self.assertContains(self.client.get(reverse("combat_logs")),self.a)
        self.client.force_login(get_user_model().objects.create_user(username="其他用户"))
        self.assertNotContains(self.client.get(reverse("combat_logs")),self.a)
        self.assertEqual(self.client.get(reverse("combat_logs"),{"source":added.context["source"].pk}).status_code,404)

    @patch.object(wcl, "read")
    def test_bound_roles_and_manual_sources_share_quota(self, read):
        user = get_user_model().objects.get(username="开荒选场")
        path = reverse("combat_wcl")
        for i in range(5):
            b = UserCharacterBinding.objects.create(user=user, identity_digest=str(i), region="cn",
                                                   realm_slug="deathwing", character_name=f"角色{i}", is_active=i != 4)
            self.assertRedirects(self.client.post(path, {"bind_role": b.pk}), path, fetch_redirect_response=False)
        self.client.post(path, {"bind_role": b.pk})
        b.refresh_from_db()
        self.assertFalse(b.is_active)
        self.assertEqual(CombatPublicSource.objects.filter(user=user).count(), 5)
        read.assert_not_called()
        read.return_value={"characterData":{"character":{"recentReports":{"data":[],"has_more_pages":False}}}}
        with patch("botend.services.combat_public_roles.profile", return_value=(None, "")):
            denied = self.client.post(path, dict(kind="character", name="新角色", realm="deathwing", region="cn"))
        self.assertContains(denied, "公开角色名额已满", status_code=400)
        extra = UserCharacterBinding.objects.create(user=user, identity_digest="extra", region="cn",
                                                    realm_slug="deathwing", character_name="新角色")
        self.assertContains(self.client.post(path, {"bind_role":extra.pk}), "公开角色名额已满", status_code=400)
        read.assert_not_called()
        source = CombatPublicSource.objects.get(user=user, name="角色0")
        self.client.post(path, {"detach":source.pk})
        self.assertTrue(UserCharacterBinding.objects.filter(user=user,character_name="角色0",is_active=True).exists())
        self.client.force_login(get_user_model().objects.create_user("其他账号"))
        self.assertEqual(self.client.post(path, {"bind_role":b.pk}).status_code, 404)

    def test_role_admin_and_account_override(self):
        path = reverse("dashboard")+"?section=combat-public-roles"
        user = get_user_model().objects.get(username="开荒选场")
        self.assertEqual(self.client.get(path).status_code, 403)
        self.assertEqual(self.client.post(path, {"scope":"default","limit":0}).status_code, 403)
        self.client.force_login(get_user_model().objects.create_superuser("名额管理"))
        self.assertContains(self.client.get(path), user.username)
        self.client.post(path, {"scope":"default","limit":2})
        self.client.post(path, {"scope":str(user.pk),"limit":0})
        self.assertEqual(self.client.post(path, {"scope":str(user.pk),"limit":-1}).status_code, 400)
        from botend.services.combat_public_roles import limit_for
        self.assertEqual(limit_for(user), 0)
        self.client.post(path, {"scope":str(user.pk),"inherit":"1"})
        self.assertEqual(limit_for(user), 2)
        self.assertFalse(CombatPublicRolePolicy.objects.filter(scope=str(user.pk)).exists())

    @patch.object(wcl,"read")
    def test_list_opens_only_its_report_then_explicit_review_combines_matching_sources(self,read):
        reports=[source(self.a),source(self.b,2000)]
        read.return_value={"characterData":{"character":{"recentReports":{"data":reports,"has_more_pages":False}}}}
        response=self.client.get(reverse("combat_wcl"),dict(name="角色",region="cn",realm="死亡之翼"))
        self.assertContains(response,"盘卷祭坛 · 史诗开荒")
        url=response.context["reports"][0]["url"]
        self.assertNotIn(self.b,url)
        read.return_value={"reportData":{"r0":source(self.a)}}
        response=self.client.get(url)
        self.assertEqual(read.call_args.args[2], {"c0":self.a})
        self.assertContains(response,"选择单场分析")
        self.assertNotContains(response,"历史最低")
        self.assertNotIn(self.b,response.context["active"]["review_url"])
        read.return_value={"reportData":{"r0":source(self.a),"r1":source(self.b,2000)}}
        directory=self.client.get(reverse("combat_logs"),{"also":[self.a,self.b],"analyze":"battle"},follow=True)
        self.assertTemplateUsed(directory,"dashboard/combat_battles.html")
        response=self.client.get(directory.context["active"]["review_url"])
        self.assertEqual(len(response.context["group"]["pulls"]),2)
        self.assertEqual(response.context["group"]["sources"],2)
        self.assertNotIn("fight",response.context[0].flatten())
        self.assertContains(response,"进入单场分析")
        self.assertIn(reverse("combat_battles"),response.context["directory_url"])
        self.assertEqual(read.call_count,4)

    @patch.object(wcl,"read")
    def test_mixed_report_requires_group_and_explicit_fight(self,read):
        raw=source(self.a)
        raw["fights"][1]["difficulty"]=4
        read.return_value={"reportData":{"r0":raw}}
        url=reverse("combat_wcl_report",args=[self.a])
        directory=self.client.get(url)
        self.assertEqual(len(directory.context["groups"]),2)
        self.assertNotIn("group",directory.context[0].flatten())
        other=directory.context["groups"][1]
        focused=self.client.get(other["focus_url"])
        self.assertEqual(focused.context["active"]["key"],other["key"])
        self.assertNotContains(focused,"历史最低")
        group=self.client.get(directory.context["groups"][0]["url"])
        self.assertEqual(len(group.context["group"]["pulls"]),1)
        with patch("botend.dashboard.combat_wcl.fetch_detail",return_value={"metrics":[],"deaths":[]}) as fetch:
            detail=self.client.get(url,{"fight":"2"})
            self.assertRedirects(detail,reverse("combat_wcl_single",args=[self.a,2]),fetch_redirect_response=False)
            fetch.assert_not_called()
            invalid=self.client.get(url,{"fight":"2","group":group.context["group"]["key"]})
            self.assertContains(invalid,"不在当前首领和难度范围内", status_code=400)
            fetch.assert_not_called()

    @patch.object(wcl,"read")
    def test_pasted_fight_link_marks_row_and_manual_selection_keeps_scope(self,read):
        read.return_value={"reportData":{"r0":source(self.a)}}
        home=reverse("combat_wcl")
        redirect=self.client.get(home,{"link":f"https://www.warcraftlogs.com/reports/{self.a}?fight=2"})
        directory=self.client.get(redirect.url)
        self.assertTemplateUsed(directory,"dashboard/combat_battles.html")
        self.assertEqual(directory.context["picked"],self.a+".2")
        self.assertNotContains(directory,"首领开荒推进")
        selected=self.client.get(reverse("combat_wcl_workspace"),{"also":self.a,"origin":"name=test"})
        self.assertNotIn("discover=",selected.context["active"]["review_url"])

    @patch.object(wcl,"read")
    def test_missing_keystone_level_stays_in_dungeon_directory(self,read):
        raw=source(self.a)
        raw["title"]=raw["zone"]["name"]="Mythic+ Season 2"
        for fight in raw["fights"]:
            fight.update(name="Voidscar Arena",difficulty=10)
        read.return_value={"reportData":{"r0":raw}}
        res=self.client.get(reverse("combat_wcl_report",args=[self.a]),{"origin":"name=test"})
        self.assertContains(res,"大秘境 · 层数未知")
        self.assertNotContains(res,"副本复盘")
        self.assertNotContains(res,"首领开荒复盘")
        old=self.client.get(reverse("combat_wcl_report",args=[self.a]),{"group":res.context["active"]["key"]})
        self.assertIn("focus=",old.url)
        self.assertNotIn("discover",res.context["active"]["review_url"])

    @patch.object(wcl,"read")
    def test_repeat_upload_cannot_be_compared_with_itself(self,read):
        read.return_value={"reportData":{"r0":source(self.a),"r1":source(self.b,2000)}}
        with patch("botend.dashboard.combat_wcl.fetch_detail",return_value={"metrics":[],"deaths":[]}):
            res=self.client.get(reverse("combat_wcl_workspace"),{"also":[self.a,self.b],"pull":self.a+".1",
                                "against":self.b+".1","compare":"1"})
            self.assertContains(res,"另一次独立尝试", status_code=400)

    @patch.object(wcl, "read", side_effect=wcl.WclError("WCL 暂时不可用"))
    def test_directory_and_review_distinguish_dependency_failure(self, read):
        for path, params in ((reverse("combat_battles"), {"also":self.a}),
                             (reverse("combat_wcl_report", args=[self.a]), {})):
            self.assertContains(self.client.get(path, params), "WCL 暂时不可用", status_code=503)
        self.assertEqual(self.client.get(reverse("combat_battles"), {"private_report":self.a}).status_code, 403)
        self.assertEqual(read.call_count, 2)
