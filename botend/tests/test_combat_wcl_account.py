from datetime import timedelta
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from botend.models import UserCharacterBinding, WclUserBinding
from botend.services import combat_wcl_account as wcl


@override_settings(WCL_V2_CONFIG={"client_id":"example", "client_secret":"example-secret"})
class WclAccountTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username="raid-member")
        self.client.force_login(self.user)
        self.home = reverse("combat_wcl")
        self.connect = reverse("combat_wcl_connect")
        self.callback = reverse("combat_wcl_callback")

    def begin(self):
        response = self.client.post(self.connect)
        params = parse_qs(urlsplit(response.url).query)
        self.assertEqual(params["redirect_uri"], ["http://testserver" + self.callback])
        self.assertIn("view-private-reports", params["scope"][0])
        return params["state"][0]

    def test_exchange_once_identity_and_unlink(self):
        state = self.begin()
        tokens = {"access":"private-access-value", "refresh":"private-refresh-value"}
        expires = timezone.now()+timedelta(hours=3)
        with patch.object(wcl,"exchange",return_value=(tokens, expires)) as exchange, patch.object(
                wcl,"query_user",return_value={"userData":{"currentUser":{"id":12,"name":"玩家"}}}):
            self.client.get(self.callback, {"code":"one-time","state":"wrong"})
            exchange.assert_not_called()
            result = self.client.get(self.callback, {"code":"one-time","state":state})
            self.assertNotIn("code=",result.url)
            self.client.get(self.callback, {"code":"one-time","state":state})
            self.assertEqual(exchange.call_count,1)
        binding = WclUserBinding.objects.get(user=self.user)
        self.assertEqual(binding.wcl_id,12)
        self.assertNotIn(tokens["access"],binding.credentials)
        self.assertEqual(wcl.unseal(binding),tokens)
        self.client.post(reverse("combat_wcl_disconnect"))
        self.assertFalse(WclUserBinding.objects.filter(user=self.user).exists())

    def test_old_callback_and_denial_preserve_existing_identity(self):
        old = self.begin()
        active = self.begin()
        WclUserBinding.objects.filter(user=self.user).update(wcl_id=81,name="原账号")
        with patch.object(wcl,"exchange") as ex:
            self.client.get(self.callback, {"state":old,"code":"old"})
            self.client.get(self.callback, {"state":active,"error":"access_denied"})
            ex.assert_not_called()
        self.assertEqual(WclUserBinding.objects.get(user=self.user).wcl_id,81)

    def test_public_queries_do_not_use_private_binding(self):
        WclUserBinding.objects.create(user=self.user,wcl_id=81,name="私有身份")
        UserCharacterBinding.objects.create(user=self.user,identity_digest="x",character_name="角色",
                                           region="eu",realm_slug="kazzak")
        listing = {"data":[], "has_more_pages":False}
        with patch.object(wcl,"read",return_value={"characterData":{"character":{"recentReports":listing}}}) as read:
            self.assertContains(self.client.get(self.home,{"name":"角色"}),"当前查询没有")
            self.assertEqual(read.call_args.args,(None,wcl.CHARACTER_REPORTS,
                             {"name":"角色","realm":"kazzak","region":"EU","page":1}))
        with patch.object(wcl,"read",return_value={"reportData":{"reports":listing}}) as read:
            self.client.get(self.home,{"channel":"private"})
            binding,q,params=read.call_args.args
            self.assertEqual((binding.user_id,q,params),(self.user.id,wcl.REPORTS,{"user":81,"page":1}))

    def test_other_user_and_ambiguous_name(self):
        other=get_user_model().objects.create_user(username="other")
        for i,realm in enumerate(("realm-one","realm-two")):
            UserCharacterBinding.objects.create(user=self.user,identity_digest=str(i),character_name="同名",
                                               region="cn",realm_slug=realm)
        WclUserBinding.objects.create(user=other,wcl_id=777,name="其他账号")
        with patch.object(wcl,"read") as read:
            self.assertContains(self.client.get(self.home,{"name":"同名"}),"请选择同名角色")
            self.assertContains(self.client.get(reverse("combat_wcl_report",args=["A"*16]),{"channel":"private"}),
                                "请先进入私有报告渠道", status_code=403)
            read.assert_not_called()

    def test_refresh_keeps_previous_refresh_when_not_rotated(self):
        row=WclUserBinding.objects.create(user=self.user,wcl_id=81,
                credentials=wcl.seal(self.user.pk,{"access":"old","refresh":"keep"}),
                expires_at=timezone.now()-timedelta(seconds=1))
        with patch.object(wcl,"exchange",return_value=({"access":"new","refresh":""},timezone.now()+timedelta(hours=1))) as ex:
            self.assertEqual(wcl.access_token(row),"new")
            self.assertEqual(wcl.access_token(row),"new")
            ex.assert_called_once_with({"grant_type":"refresh_token","refresh_token":"keep"})
        row.refresh_from_db()
        self.assertEqual(wcl.unseal(row)["refresh"],"keep")
        self.assertIsNone(row.refreshing_until)
