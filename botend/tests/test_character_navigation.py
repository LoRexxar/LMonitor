from urllib.parse import quote, urlencode

from django.test import TestCase
from django.urls import reverse

from botend.models import PlayerSpecTopPlayer, SeasonMeta, WowRealmDirectory


class CharacterNavigationTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='nav-current', season_name='Current',
            rio_season='season-nav', mplus_zone_id=1, raid_zone_id=2, is_active=True)
        WowRealmDirectory.objects.create(region='cn', slug='silver-hand', name='Silver Hand',
            localized_name='白银之手', locale='zh_CN')
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            region='cn', realm='白银之手', character_name='测试', class_name='DeathKnight',
            spec_name='Blood', rank=20, profile_url='https://raider.io/characters/cn/silver-hand/测试')

    def visit(self, **params):
        return self.client.get('/portal/character/?' + urlencode(params))

    def test_current_top20_identity_localized_realm_and_cross_spec(self):
        expected = reverse('spec_detail_player_detail', kwargs={
            'class_name': 'DeathKnight', 'spec_name': 'Blood', 'player_id': self.player.pk})
        for params in ({'region': 'cn', 'realm': '白银之手', 'name': '测试'},
                       {'url': self.player.profile_url}, {'player_id': self.player.pk}):
            with self.subTest(params=params):
                response = self.visit(**params)
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response['Location'], expected)
        self.player.rank = 21
        self.player.save(update_fields=['rank'])
        sibling = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            region='cn', realm='白银之手', character_name='测试', class_name='DeathKnight',
            spec_name='Frost', rank=1, profile_url=self.player.profile_url)
        self.assertEqual(self.visit(player_id=self.player.pk)['Location'],
            reverse('spec_detail_player_detail', kwargs={
                'class_name': 'DeathKnight', 'spec_name': 'Frost', 'player_id': sibling.pk}))

    def test_non_top20_and_old_season_go_to_exact_io_character(self):
        expected = 'https://raider.io/characters/cn/silver-hand/' + quote('测试', safe='')
        for rank in (21, 0, None):
            self.player.rank = rank
            self.player.save(update_fields=['rank'])
            self.assertEqual(self.visit(player_id=self.player.pk)['Location'], expected)
        self.player.rank = 1
        self.player.season_id = self.season.pk + 100
        self.player.save(update_fields=['rank', 'season_id'])
        self.assertEqual(self.visit(url=self.player.profile_url)['Location'], expected)
        self.assertEqual(self.visit(region='cn', realm='silver-hand', name='不存在')['Location'],
            'https://raider.io/characters/cn/silver-hand/' + quote('不存在', safe=''))

    def test_identity_is_accent_sensitive_nfc_and_not_name_only(self):
        self.player.character_name = 'Café'
        self.player.profile_url = 'https://raider.io/characters/cn/silver-hand/Café'
        self.player.save(update_fields=['character_name', 'profile_url'])
        expected = reverse('spec_detail_player_detail', kwargs={
            'class_name': 'DeathKnight', 'spec_name': 'Blood', 'player_id': self.player.pk})
        self.assertEqual(self.visit(region='cn', realm='silver-hand', name='CAFE\u0301')['Location'], expected)
        for params in ({'region': 'cn', 'realm': 'silver-hand', 'name': 'Cafe'},
                       {'region': 'cn', 'realm': 'other-realm', 'name': 'Café'},
                       {'region': 'eu', 'realm': 'silver-hand', 'name': 'Café'}):
            self.assertTrue(self.visit(**params)['Location'].startswith('https://raider.io/characters/'))

    def test_snapshot_link_building_is_query_free_and_click_is_bounded(self):
        from botend.services.character_navigation import character_link
        with self.assertNumQueries(0):
            url = character_link(url=self.player.profile_url)
            self.assertTrue(url.startswith('/portal/character/?'))
            self.assertEqual(character_link(url='javascript:alert(1)'), '')
        with self.assertNumQueries(3):
            response = self.client.get(url)
        self.assertEqual(response.status_code, 302)
        self.assertIn('/player/', response['Location'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.post(url).status_code, 405)

    def test_invalid_links_never_redirect_to_arbitrary_hosts_or_paths(self):
        for params in ({'url': 'https://evil.test/characters/cn/silver-hand/测试'},
                       {'url': 'https://raider.io/characters/cn/a%2Fb/测试'},
                       {'url': '//evil.test/characters/cn/silver-hand/测试'},
                       {'url': 'https://raider.io/characters/cn/../测试'},
                       {'region': 'world', 'realm': 'silver-hand', 'name': '测试'},
                       {'region': 'cn', 'realm': 'silver-hand', 'name': 'a/b'},
                       {'player_id': 'not-an-id'}, {}):
            with self.subTest(params=params):
                self.assertEqual(self.visit(**params).status_code, 404)
