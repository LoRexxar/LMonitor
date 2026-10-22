from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser
from django.core.exceptions import ValidationError
from django.test import Client, RequestFactory, TestCase
from django.template import Context, Template
from django.urls import reverse

from botend.models import CharacterBindingQuota, UserCharacterBinding, PlayerSpecTopPlayer, SeasonMeta, WowRealmDirectory
from botend.services import character_bindings as service
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor


class CharacterBindingTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user('binding-user')
        cls.other = get_user_model().objects.create_user('other-user')
        cls.admin = get_user_model().objects.create_superuser('binding-admin', password='test-password')
        cls.season = SeasonMeta.objects.create(season_key='binding-season', season_name='Test',
            rio_season='season-test', mplus_zone_id=1, raid_zone_id=2, is_active=True)
        WowRealmDirectory.objects.create(region='us', slug='area-52', name='Area 52', localized_name='52区', locale='en_US')

    def player(self, name='Example', **kwargs):
        values = dict(season_id=self.season.pk, region='us', realm='Area 52', character_name=name,
            class_name='Warrior', spec_name='Fury', rank=None,
            profile_url=f'https://raider.io/characters/us/area-52/{name}')
        values.update(kwargs)
        return PlayerSpecTopPlayer.objects.create(**values)

    def bind(self, name='Example', user=None):
        return service.bind_character(user or self.user, name, '52区')

    def test_default_limit_duplicate_and_disabled_slots(self):
        for i in range(6):
            self.player(f'Player{i}')
        first = self.bind('Player0')
        with self.assertRaisesMessage(ValidationError, '已经绑定'):
            self.bind('player0')
        for i in range(1, 5):
            self.bind(f'Player{i}')
        service.update_binding(self.user, first.pk, 'deactivate')
        with self.assertRaisesMessage(ValidationError, '上限'):
            self.bind('Player5')
        self.assertEqual(service.quota(self.user), 5)
        service.update_binding(self.user, first.pk, 'unbind')
        self.bind('Player5')
        self.assertEqual(self.user.character_bindings.count(), 5)

    def test_account_menu_is_private_and_includes_closed_character_details(self):
        from botend.templatetags.character_binding_tags import character_shortcuts
        player = self.player('MyCharacter')
        binding = self.bind('MyCharacter')
        self.player('OtherCharacter')
        self.bind('OtherCharacter', user=self.other)
        service.update_binding(self.user, binding.pk, 'deactivate')
        request = RequestFactory().get('/')
        request.user = self.user
        with self.assertNumQueries(1):
            payload = character_shortcuts({'request': request})
        self.assertEqual([item['name'] for item in payload['shortcuts']], ['MyCharacter'])
        self.assertEqual(payload['shortcuts'][0]['url'], service.detail_url(binding))
        self.assertFalse(payload['shortcuts'][0]['active'])
        self.assertTrue(payload['shortcuts'][0]['icon'])
        with self.assertNumQueries(0):
            html = Template('{% load character_binding_tags %}{% character_shortcuts %}').render(Context({'request': request}))
        self.assertIn('MyCharacter', html)
        self.assertIn('已关闭', html)
        self.assertNotIn('OtherCharacter', html)

    def test_anonymous_menu_has_no_query_and_missing_profile_has_no_broken_link(self):
        from botend.templatetags.character_binding_tags import character_shortcuts
        request = RequestFactory().get('/')
        request.user = AnonymousUser()
        with self.assertNumQueries(0):
            self.assertFalse(character_shortcuts({'request': request})['authenticated'])
        player = self.player()
        self.bind()
        player.delete()
        request.user = self.user
        with self.assertNumQueries(1):
            payload = character_shortcuts({'request': request})
        self.assertEqual(payload['shortcuts'][0]['url'], '')

    def test_shortcut_menu_is_bounded_and_reloads_after_binding_changes(self):
        from botend.templatetags.character_binding_tags import character_shortcuts, MENU_LIMIT
        player = self.player()
        UserCharacterBinding.objects.bulk_create([
            UserCharacterBinding(user=self.user, player=player, character_name=f'Role{i}',
                identity_digest=str(i), region='us', realm_slug='area-52')
            for i in range(MENU_LIMIT + 1)
        ])
        request = RequestFactory().get('/')
        request.user = self.user
        with self.assertNumQueries(1):
            payload = character_shortcuts({'request': request})
        self.assertEqual(len(payload['shortcuts']), MENU_LIMIT)
        self.assertTrue(payload['has_more'])
        self.user.character_bindings.all().delete()
        next_request = RequestFactory().get('/')
        next_request.user = self.user
        self.assertEqual(character_shortcuts({'request': next_request})['shortcuts'], [])

    def test_quota_is_per_account_and_lowering_is_safe(self):
        self.player()
        service.set_quota(self.user, 0)
        with self.assertRaises(ValidationError):
            self.bind()
        self.bind(user=self.other)
        service.set_quota(self.user, 8)
        self.bind()
        with self.assertRaises(ValidationError):
            service.set_quota(self.user, 0)
        for value in (-1, True, 1.5, 32768):
            with self.assertRaises(ValidationError):
                service.set_quota(self.user, value)
        self.assertEqual(service.quota(self.user), 8)

    def test_activation_ownership_and_idempotence(self):
        self.player()
        binding = self.bind()
        with self.assertRaises(UserCharacterBinding.DoesNotExist):
            service.update_binding(self.other, binding.pk, 'unbind')
        service.update_binding(self.user, binding.pk, 'activate')
        service.update_binding(self.user, binding.pk, 'deactivate')
        service.set_quota(self.user, 1)
        service.update_binding(self.user, binding.pk, 'activate')
        binding.refresh_from_db()
        self.assertTrue(binding.is_active)
        # Defensive activation guard also handles a pre-existing inconsistent limit.
        service.update_binding(self.user, binding.pk, 'deactivate')
        CharacterBindingQuota.objects.filter(user=self.user).update(limit=0)
        with self.assertRaisesMessage(ValidationError, '激活角色数量'):
            service.update_binding(self.user, binding.pk, 'activate')

    def test_aliases_multi_spec_and_region_ambiguity(self):
        self.player()
        self.player(spec_name='Arms')
        self.bind()
        with self.assertRaisesMessage(ValidationError, '已经绑定'):
            service.bind_character(self.user, 'example', 'area-52', 'us')
        self.player(region='eu', profile_url='https://raider.io/characters/eu/area-52/Example')
        with self.assertRaisesMessage(ValidationError, '多个区服'):
            service.resolve_character('Example', 'Area 52')
        self.assertEqual(service.resolve_character('Example', 'Area 52', 'us').region, 'us')

    @patch('botend.services.character_bindings.requests.get')
    def test_unknown_and_mismatched_remote_character_never_bind(self, get):
        get.return_value.status_code = 404
        with self.assertRaisesMessage(ValidationError, '未找到'):
            self.bind('Missing')
        get.return_value.status_code = 200
        get.return_value.json.return_value = {'name': 'Wrong', 'region': 'us', 'realm': 'Area 52',
            'profile_url': 'https://raider.io/characters/us/area-52/Wrong', 'class': 'Warrior', 'active_spec_name': 'Fury'}
        with self.assertRaises(ValidationError):
            self.bind('Missing')
        self.assertFalse(UserCharacterBinding.objects.exists())
        self.assertFalse(PlayerSpecTopPlayer.objects.exists())

    @patch('botend.services.character_bindings.requests.get')
    def test_remote_character_can_bind_without_ranking(self, get):
        get.return_value.status_code = 200
        get.return_value.json.return_value = {'name': 'Remote', 'region': 'us', 'realm': 'Area 52',
            'profile_url': 'https://raider.io/characters/us/area-52/Remote', 'class': 'Warrior',
            'active_spec_name': 'Fury', 'gear': {'item_level_equipped': 300}, 'race': 'Orc'}
        binding = self.bind('Remote')
        self.assertIsNone(binding.player.rank)
        self.assertEqual(binding.player.race, 'Orc')
        self.assertTrue(binding.is_active)

    @patch('botend.services.character_bindings.resolve_character')
    def test_capacity_is_rechecked_after_verification(self, resolve):
        player = self.player()
        def verification(*args):
            service.set_quota(self.user, 0)
            return player
        resolve.side_effect = verification
        with self.assertRaisesMessage(ValidationError, '上限'):
            self.bind()
        self.assertFalse(UserCharacterBinding.objects.exists())

    def test_user_pages_details_csrf_and_admin_boundary(self):
        player = self.player()
        binding = self.bind()
        url = reverse('dashboard') + '?section=my-characters'
        admin_url = reverse('character_binding_admin')
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.other)
        self.assertNotContains(self.client.get(url), 'Example')
        self.assertEqual(self.client.post(url, {'action': 'unbind', 'binding_id': binding.pk}).status_code, 404)
        self.assertEqual(self.client.get(admin_url).status_code, 403)
        self.assertEqual(self.client.post(admin_url, {'action': 'quota', 'user_id': self.other.pk, 'limit': 100}).status_code, 403)
        self.client.force_login(self.user)
        self.assertContains(self.client.get(url), service.detail_url(binding))
        self.assertEqual(service.detail_url(binding), reverse('spec_detail_player_detail',
            kwargs={'class_name': 'Warrior', 'spec_name': 'Fury', 'player_id': player.pk}))
        with patch('botend.portal.spec_detail_views._base_context', return_value={'class_name': 'Warrior', 'spec_name': 'Fury'}), patch(
                'botend.portal.spec_detail_views.SpecStatsService.get_player_detail', return_value={'id': player.pk}):
            response = self.client.get(service.detail_url(binding))
            self.assertEqual(response.status_code, 200)
            self.assertTemplateUsed(response, 'portal/spec_detail/player_detail.html')
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.user)
        self.assertEqual(csrf.post(url, {'action': 'deactivate', 'binding_id': binding.pk}).status_code, 403)

    def test_personal_page_uses_dashboard_for_all_actions_and_legacy_entry(self):
        self.player(item_level=701, score=3200)
        self.client.force_login(self.user)
        url = reverse('dashboard') + '?section=my-characters'
        self.assertRedirects(self.client.get(reverse('my_character_bindings')), url)
        response = self.client.get(url)
        self.assertTemplateUsed(response, 'dashboard/index.html')
        self.assertTemplateUsed(response, 'dashboard/character_bindings.html')
        self.assertTemplateNotUsed(response, 'dashboard/character_bindings_base.html')
        self.assertContains(response, 'id="sidebar"')
        self.assertContains(response, 'data-section="my-characters"')
        self.assertNotContains(response, 'data-section="character-bindings"')
        self.assertContains(response, 'action="' + url + '"')
        self.assertEqual(response.context['dashboard_permissions'], [])
        self.assertEqual(response.context['dashboard_default_section'], 'my-characters')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(self.client.get(reverse('dashboard')).context['dashboard_default_section'], 'dashboard-home')
        self.assertRedirects(self.client.post(url, {
            'action': 'bind', 'character_name': 'Example', 'realm': '52区'}), url)
        binding = self.user.character_bindings.get()
        roster = self.client.get(url)
        self.assertContains(roster, '701.0')
        self.assertContains(roster, '3200.0')
        duplicate = self.client.post(url, {'action': 'bind', 'character_name': 'Example', 'realm': '52区'})
        self.assertContains(duplicate, '已经绑定', status_code=400)
        self.assertContains(duplicate, 'id="sidebar"', status_code=400)
        self.assertTemplateUsed(duplicate, 'dashboard/index.html')
        for action in ('deactivate', 'activate', 'unbind'):
            self.assertRedirects(self.client.post(url, {'action': action, 'binding_id': binding.pk}), url)
        self.assertFalse(self.user.character_bindings.exists())
        for section in ('character-bindings', 'user-management', 'user-groups', 'database-tables'):
            self.assertEqual(self.client.get(reverse('dashboard') + '?section=' + section).status_code, 403)

    def test_admin_search_quota_and_status_actions(self):
        self.player()
        binding = self.bind()
        self.client.force_login(self.admin)
        url = reverse('dashboard') + '?section=character-bindings'
        self.assertContains(self.client.get(url + '&q=Example'), 'Example')
        self.assertNotContains(self.client.get(url + '&q=Unmatched'), 'Example')
        self.assertEqual(self.client.post(url, {'action': 'quota', 'user_id': self.user.pk, 'limit': '9'}).status_code, 302)
        self.assertEqual(service.quota(self.user), 9)
        self.client.post(url, {'action': 'deactivate', 'user_id': self.user.pk, 'binding_id': binding.pk})
        self.assertNotContains(self.client.get(url + '&status=active'), 'Example')
        self.assertContains(self.client.get(url + '&status=inactive'), 'Example')

    def test_admin_uses_dashboard_shell_for_search_pagination_and_validation(self):
        self.client.force_login(self.admin)
        url = reverse('dashboard') + '?section=character-bindings'
        response = self.client.get(url + '&q=binding&users_page=2&status=active')
        self.assertTemplateUsed(response, 'dashboard/index.html')
        self.assertTemplateUsed(response, 'dashboard/character_bindings_admin.html')
        self.assertTemplateNotUsed(response, 'dashboard/character_bindings_base.html')
        self.assertContains(response, 'id="sidebar"')
        self.assertContains(response, 'data-section="character-bindings"')
        self.assertContains(response, 'name="section" value="character-bindings"')
        self.assertEqual(response.context['dashboard_default_section'], 'character-bindings')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('section=character-bindings', response.context['search_params'])
        error = self.client.post(url, {'action': 'quota', 'user_id': self.user.pk, 'limit': '-1'})
        self.assertEqual(error.status_code, 400)
        self.assertTemplateUsed(error, 'dashboard/index.html')
        self.assertContains(error, 'id="sidebar"', status_code=400)
        saved = self.client.post(url + '&q=binding&status=active&users_page=2', {
            'action': 'quota', 'user_id': self.user.pk, 'limit': '8'})
        self.assertEqual(saved.status_code, 302)
        from urllib.parse import parse_qs, urlsplit
        location = urlsplit(saved.url)
        self.assertEqual(location.path, reverse('dashboard'))
        self.assertEqual(parse_qs(location.query), {
            'section': ['character-bindings'], 'q': ['binding'], 'status': ['active'],
            'users_page': ['2'], 'saved': ['1']})

    def test_admin_legacy_url_redirects_and_dashboard_requires_superuser(self):
        self.client.force_login(self.admin)
        legacy = self.client.get(reverse('character_binding_admin'), {'q': 'Example'})
        self.assertEqual(legacy.status_code, 302)
        self.assertTrue(legacy.url.startswith(reverse('dashboard') + '?'))
        self.assertIn('section=character-bindings', legacy.url)
        self.assertIn('q=Example', legacy.url)
        self.client.force_login(self.user)
        url = reverse('dashboard') + '?section=character-bindings'
        self.assertEqual(self.client.get(url).status_code, 403)
        self.assertEqual(self.client.post(url, {
            'action': 'quota', 'user_id': self.user.pk, 'limit': '100'}).status_code, 403)
        csrf = Client(enforce_csrf_checks=True)
        csrf.force_login(self.admin)
        self.assertEqual(csrf.post(url, {
            'action': 'quota', 'user_id': self.user.pk, 'limit': '100'}).status_code, 403)

    def test_daily_dedup_closed_accounts_and_rank_preserved(self):
        player = self.player(rank=37, gear_json=[{'id': 1}])
        self.bind()
        self.bind(user=self.other)
        self.player('Closed')
        closed = self.bind('Closed')
        service.update_binding(self.user, closed.pk, 'deactivate')
        disabled = get_user_model().objects.create_user('disabled', is_active=False)
        self.player('Disabled')
        self.bind('Disabled', user=disabled)
        monitor = Mock()
        monitor._fetch_profile_with_activity.return_value = {'race': 'Orc'}
        count = service.refresh_bound_characters(monitor, self.season)
        self.assertEqual(count, 1)
        monitor._fetch_profile_with_activity.assert_called_once()
        monitor._crawl_battlenet_stats_for_profile.assert_called_once()
        player.refresh_from_db()
        self.assertEqual(player.rank, 37)
        self.assertEqual(player.gear_json, [{'id': 1}])

    def test_daily_failure_keeps_last_good_and_continues(self):
        self.player('First', gear_json=[{'id': 10}])
        self.player('Second')
        self.bind('First')
        self.bind('Second')
        monitor = Mock()
        monitor._fetch_profile_with_activity.side_effect = [RuntimeError('offline'), None]
        self.assertEqual(service.refresh_bound_characters(monitor, self.season), 0)
        self.assertEqual(monitor._fetch_profile_with_activity.call_count, 2)
        self.assertEqual(PlayerSpecTopPlayer.objects.get(character_name='First').gear_json, [{'id': 10}])

    def test_season_rollover_and_top20_pointer_reuse(self):
        self.player()
        binding = self.bind()
        self.season.is_active = False
        self.season.save()
        new = SeasonMeta.objects.create(season_key='new-binding-season', season_name='New',
            rio_season='season-new', mplus_zone_id=1, raid_zone_id=2, is_active=True)
        monitor = Mock()
        monitor._fetch_profile_with_activity.return_value = {'race': 'Orc'}
        service.refresh_bound_characters(monitor, new)
        binding.refresh_from_db()
        self.assertEqual(binding.player.season_id, new.pk)
        self.assertIsNone(binding.player.rank)
        self.assertEqual(UserCharacterBinding.objects.count(), 1)
        monitor.reset_mock()
        service.refresh_bound_characters(monitor, new, {binding.identity_digest})
        monitor._fetch_profile_with_activity.assert_not_called()

    def test_bound_character_follows_active_spec_without_rewriting_rankings(self):
        original = self.player(rank=3, score=3000)
        binding = self.bind()
        monitor = SpecDetailPlayerMonitor(None, None)
        data = {'class': 'Warrior', 'active_spec_name': 'Arms', 'race': 'Orc',
            'mythic_plus_scores_by_season': [{'season': self.season.rio_season, 'scores': {'all': 2500}}]}
        with patch.object(monitor, '_fetch_profile_with_activity', return_value=data), patch.object(
            monitor, '_crawl_battlenet_stats_for_profile'):
            service.refresh_bound_characters(monitor, self.season)
        binding.refresh_from_db()
        original.refresh_from_db()
        self.assertEqual(binding.player.spec_name, 'Arms')
        self.assertEqual(binding.player.score, 2500)
        self.assertIsNone(binding.player.rank)
        self.assertEqual(original.rank, 3)
        self.assertEqual(original.score, 3000)

    def test_daily_scan_includes_bindings_when_rankings_are_empty(self):
        self.player()
        self.bind()
        monitor = SpecDetailPlayerMonitor(None, SimpleNamespace(save=Mock()))
        with patch.object(monitor, '_fetch_top_players', return_value=[]), patch.object(
            monitor, '_crawl_battlenet_stats'), patch.object(monitor, '_fetch_profile_with_activity', return_value=None) as fetch, patch.object(
            monitor, '_crawl_battlenet_stats_for_profile'):
            self.assertTrue(monitor.scan(''))
        fetch.assert_called_once()
