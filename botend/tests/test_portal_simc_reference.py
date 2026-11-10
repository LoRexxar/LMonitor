"""专精资料内测页的权限、现行数据与专精隔离回归。"""

from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import SimcApl, SimcProfile, SimcTalentString, WowTalentVersion
from botend.services.simc_reference import reference_catalog
from botend.tests.simc_reference_test_fixtures import publish_resources


class PortalSimcReferenceTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(username='资料内测账号')
        cls.apl = SimcApl.objects.create(
            name='狂怒默认 APL', spec='warrior_fury', class_name='warrior',
            content='actions=/bloodthirst\nactions+=/rampage', source='simc_upstream',
            is_system=True, is_selectable=True, sync_version='当前提交',
        )
        cls.talent = SimcTalentString.objects.create(
            name='狂怒默认天赋', spec='warrior_fury', talent='测试天赋代码',
            system_key='simc_upstream:warrior_fury', is_system=True,
            hero_talent_names=['山丘领主'], default_apl=cls.apl,
        )
        cls.profile = SimcProfile.objects.create(
            name='狂怒默认装备', spec='warrior_fury', class_name='warrior',
            source='simc_upstream', version='12.1', profile_set='MID2',
            player_config_mode='manual_equipment', gear_crit=0,
            player_equipment='warrior="测试角色"\nspec=fury\n# 测试头盔 (285)\nhead=,id=12345,ilevel=285,enchant_id=777,gem_id=888\n',
        )
        cls.publication = publish_resources('warrior_fury', profiles=[cls.profile], talents=[cls.talent], apls=[cls.apl])
        cls.url = reverse('portal_simc_reference')

    def setUp(self):
        self.client.force_login(self.user)

    def test_anonymous_redirects_before_loading_any_resources(self):
        self.client.logout()
        with patch('botend.portal.simc_reference.reference_context') as build:
            response = self.client.get(self.url, {'spec': 'warrior_fury'})
        self.assertEqual(response.status_code, 302)
        self.assertIn('/auth/login/?next=', response.url)
        build.assert_not_called()

    def test_regular_account_can_read_current_shared_resources(self):
        with patch('requests.sessions.Session.request', side_effect=AssertionError('禁止联网')):
            response = self.client.get(self.url, {'spec': 'warrior_fury'})
        self.assertEqual(response.status_code, 200)
        for text in ('测试天赋代码', '山丘领主', '测试头盔', '285', 'bloodthirst', '12.1', 'MID2', '暴击 0'):
            self.assertContains(response, text)
        self.assertEqual(response.context['profile'].id, self.profile.id)
        self.assertEqual(response.context['talent'].id, self.talent.id)
        self.assertEqual(response.context['apl'].id, self.apl.id)
        self.assertNotContains(response, '玩家块未提供 talents=')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('Cookie', response['Vary'])
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')

    def test_personal_and_inactive_resources_are_never_exposed(self):
        other = User.objects.create_user(username='另一个账号')
        for owner in (self.user.id, other.id):
            profile = SimcProfile.objects.create(name='私有装备', user_id=owner, spec='warrior_fury')
            talent = SimcTalentString.objects.create(name='私有天赋', owner_user_id=owner, spec='warrior_fury', talent='私密代码')
            apl = SimcApl.objects.create(name='私有 APL', owner_user_id=owner, spec='warrior_fury', content='私密逻辑')
            for key, resource in (('profile', profile), ('talent', talent), ('apl', apl)):
                with self.subTest(owner=owner, kind=key):
                    self.assertEqual(self.client.get(self.url, {'spec': 'warrior_fury', key: resource.id}).status_code, 404)
        self.profile.is_active = False
        self.profile.save()
        self.assertEqual(self.client.get(self.url, {'profile': self.profile.id}).status_code, 404)
        response = self.client.get(self.url, {'spec': 'warrior_fury'})
        self.assertNotContains(response, '私有')
        self.assertNotContains(response, '私密')

    def test_cross_spec_selection_and_malformed_ids_are_rejected(self):
        for params in (
            {'spec': 'mage_fire', 'profile': self.profile.id},
            {'spec': 'mage_fire', 'talent': self.talent.id},
            {'spec': 'mage_fire', 'apl': self.apl.id},
            {'spec': '不存在'}, {'spec': 'warrior_fury', 'profile': '-1'},
            {'spec': 'warrior_fury', 'talent': 'abc'},
        ):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params).status_code, 404)

    def test_missing_spec_data_is_an_honest_empty_state(self):
        response = self.client.get(self.url, {'spec': 'monk_mistweaver'})
        self.assertContains(response, '武僧 · 织雾')
        self.assertContains(response, '该专精暂无启用的共享装备配置')
        self.assertContains(response, '该专精暂无可展示的天赋代码')
        self.assertContains(response, '该专精暂无可展示的共享 APL')
        self.assertNotContains(response, 'bloodthirst')

    def test_fallback_version_is_kept_and_shared_short_spec_is_disambiguated(self):
        mage = SimcProfile.objects.create(
            name='法师回退配置', class_name='mage', spec='frost', version='12.0', profile_set='MID1',
        )
        dk = SimcProfile.objects.create(name='死亡骑士配置', class_name='deathknight', spec='frost', version='12.1')
        publish_resources('mage_frost', profiles=[mage])
        publish_resources('deathknight_frost', profiles=[dk])
        groups, catalog = reference_catalog()
        self.assertEqual(len(catalog), sum(len(group['specs']) for group in groups))
        self.assertEqual([row['id'] for row in catalog['mage_frost']['profiles']], [mage.id])
        self.assertEqual([row['id'] for row in catalog['deathknight_frost']['profiles']], [dk.id])
        response = self.client.get(self.url, {'spec': 'mage_frost'})
        self.assertContains(response, '12.0')
        self.assertContains(response, 'MID1')
        self.assertNotContains(response, '死亡骑士配置')

    def test_talent_link_selects_its_apl_and_manual_selection_is_labeled(self):
        alternate = SimcApl.objects.create(
            name='另一份 APL', spec='warrior_fury', content='actions=/execute',
            is_system=True, is_selectable=True,
        )
        self.publication.apls.add(alternate)
        response = self.client.get(self.url, {'spec': 'warrior_fury', 'apl': alternate.id})
        self.assertContains(response, '手动选择的参考 APL')
        self.assertContains(response, 'actions=/execute')
        self.assertNotContains(response, 'actions=/bloodthirst')

    def test_unavailable_default_apl_does_not_silently_fall_back(self):
        self.apl.is_selectable = False
        self.apl.save()
        SimcApl.objects.create(name='可选但非默认', spec='warrior_fury', content='不应自动选中', is_system=True, is_selectable=True)
        response = self.client.get(self.url, {'spec': 'warrior_fury'})
        self.assertContains(response, '默认 APL 当前不可展示')
        self.assertIsNone(response.context['apl'])
        self.assertNotContains(response, '不应自动选中')

    def test_multiple_apls_without_default_require_explicit_selection(self):
        self.talent.default_apl = None
        self.talent.save()
        self.apl.source = 'user'
        self.apl.save()
        alternate = SimcApl.objects.create(name='另一个系统方案', spec='warrior_fury', source='simc_builtin', is_system=True, is_selectable=True)
        self.publication.apls.add(alternate)
        response = self.client.get(self.url, {'spec': 'warrior_fury'})
        self.assertIsNone(response.context['apl'])
        self.assertContains(response, '无法确定唯一默认项')

    def test_updates_are_read_without_a_stale_page_cache(self):
        self.client.get(self.url)
        SimcTalentString.objects.filter(pk=self.talent.pk).update(talent='修改后的天赋')
        self.assertContains(self.client.get(self.url), '修改后的天赋')

    def test_ptr_profile_uses_ptr_talent_metadata(self):
        WowTalentVersion.objects.create(key='内测正式服', branch='retail', is_active=True, is_default_simulator=True)
        WowTalentVersion.objects.create(key='内测PTR', branch='ptr', is_active=True, is_default_simulator=True)
        self.profile.use_ptr = True
        self.profile.save()
        response = self.client.get(self.url, {'spec': 'warrior_fury'})
        self.assertEqual(response.context['talent_version'], '内测PTR')

    def test_database_text_is_escaped_and_page_is_read_only(self):
        SimcApl.objects.filter(pk=self.apl.pk).update(content='<script>alert(1)</script>')
        response = self.client.get(self.url)
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertNotContains(response, '<script>alert(1)</script>')
        self.assertEqual(self.client.post(self.url, {'name': '禁止写入'}).status_code, 405)

    def test_shared_header_and_home_have_no_entry_to_beta_page(self):
        from pathlib import Path
        from django.conf import settings
        for name in ('_header.html', 'index.html'):
            content = (Path(settings.BASE_DIR) / 'templates' / 'portal' / name).read_text(encoding='utf-8')
            self.assertNotIn(self.url, content)
