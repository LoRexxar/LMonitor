"""验证重复运行请求量、变更失效、失败退避及活动刷新独立性。"""
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOProfileCache, SeasonMeta, PlayerSpecTopPlayer
from botend.services.raiderio_profile_cache import ProfileCache, ranking_signal
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor


class ProfileCacheTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='缓存验证', season_name='测试',
            is_active=True, rio_season='season-test', mplus_zone_id=1, raid_zone_id=2)
        self.player = SimpleNamespace(season_id=self.season.pk, region='eu', realm='test-realm',
            character_name='Tester', profile_url='https://raider.io/characters/eu/test-realm/Tester')
        self.payload = {'name': 'Tester', 'region': 'eu', 'realm': 'test-realm',
            'profile_url': self.player.profile_url, 'gear': {'items': {'head': {'item_id': 1}}}}
        self.fetch = Mock(return_value=self.payload)

    def get(self, signal=''):
        return ProfileCache().get(self.player, self.season, self.fetch, signal=signal)

    def age(self, **kwargs):
        RaiderIOProfileCache.objects.update(fetched_at=timezone.now() - timedelta(**kwargs))

    def test_new_monitor_reuses_persisted_cache_across_specializations(self):
        self.assertEqual(self.get(), self.payload)
        self.player.spec_name = 'Frost'
        self.assertEqual(self.get(), self.payload)
        self.fetch.assert_called_once()

    def test_different_specialization_signals_do_not_invalidate_each_other(self):
        for scope, signal in [('Mage/Fire', '火法装备'), ('Mage/Frost', '冰法装备'),
                              ('Mage/Fire', '火法装备'), ('Mage/Frost', '冰法装备')]:
            ProfileCache().get(self.player, self.season, self.fetch, signal=signal, signal_scope=scope)
        self.fetch.assert_called_once()
        ProfileCache().get(self.player, self.season, self.fetch, signal='火法装备变化', signal_scope='Mage/Fire')
        self.assertEqual(self.fetch.call_count, 2)

    def test_unchanged_signal_does_not_extend_daily_freshness(self):
        self.get('same')
        self.age(hours=12)
        self.get('same')
        self.assertEqual(self.fetch.call_count, 1)
        self.age(days=1, seconds=1)
        self.get('same')
        self.assertEqual(self.fetch.call_count, 2)

    def test_unknown_signal_refreshes_daily_and_changed_signal_immediately(self):
        self.get('old')
        self.get('new')
        self.assertEqual(self.fetch.call_count, 2)
        self.age(days=1, seconds=1)
        self.get()
        self.assertEqual(self.fetch.call_count, 3)

    def test_unchanged_response_advances_success_clock(self):
        self.get()
        self.age(days=2)
        old = RaiderIOProfileCache.objects.get().fetched_at
        self.get()
        self.assertGreater(RaiderIOProfileCache.objects.get().fetched_at, old)

    def test_failure_keeps_old_payload_and_backs_off_even_if_signal_changes(self):
        self.get('old')
        self.age(days=8)
        old = RaiderIOProfileCache.objects.get().fetched_at
        self.fetch.return_value = None
        self.assertIsNone(self.get('new'))
        self.assertIsNone(self.get('another'))
        self.assertEqual(self.fetch.call_count, 2)
        row = RaiderIOProfileCache.objects.get()
        self.assertEqual(row.payload, self.payload)
        self.assertEqual(row.fetched_at, old)
        self.assertEqual(row.failure_count, 1)
        RaiderIOProfileCache.objects.update(next_attempt_at=timezone.now() - timedelta(seconds=1))
        self.fetch.return_value = self.payload
        self.get('another')
        self.assertEqual(RaiderIOProfileCache.objects.get().failure_count, 0)

    def test_identity_mismatch_is_not_cached_as_success(self):
        self.fetch.return_value = {**self.payload, 'name': 'Other'}
        self.assertIsNone(self.get())
        self.assertIsNone(RaiderIOProfileCache.objects.get().fetched_at)

    def test_unexpected_validation_error_releases_lease_without_certifying_payload(self):
        with patch('botend.services.raiderio_profile_cache.validate_profile_identity',
                   side_effect=RuntimeError('验证中断')):
            with self.assertRaises(RuntimeError):
                self.get()
        row = RaiderIOProfileCache.objects.get()
        self.assertIsNone(row.fetched_at)
        self.assertEqual(row.payload, {})
        self.assertEqual(row.lease_owner, '')
        self.assertGreater(row.next_attempt_at, timezone.now())

    def test_busy_lease_and_lost_ownership_do_not_publish(self):
        self.get()
        self.age(days=2)
        RaiderIOProfileCache.objects.update(lease_owner='other',
            lease_expires_at=timezone.now() + timedelta(minutes=1))
        self.assertIsNone(self.get())
        self.assertEqual(self.fetch.call_count, 1)
        RaiderIOProfileCache.objects.update(lease_expires_at=timezone.now() - timedelta(seconds=1))
        def superseded():
            RaiderIOProfileCache.objects.update(lease_owner='replacement')
            return self.payload
        self.fetch.side_effect = superseded
        self.assertIsNone(self.get())
        self.assertEqual(RaiderIOProfileCache.objects.get().lease_owner, 'replacement')

    def test_wrong_season_and_anonymous_never_fetch(self):
        self.player.season_id += 1
        self.assertIsNone(self.get())
        self.player.season_id = self.season.pk
        self.player.realm = 'anonymous'
        self.assertIsNone(self.get())
        self.fetch.assert_not_called()

    def test_profile_hit_still_refreshes_activity_independently(self):
        monitor = SpecDetailPlayerMonitor(None, None)
        collector = Mock()
        collector.refresh.return_value = {'status': 'ready', 'profile_payload': self.payload}
        monitor._activity_collector = collector
        self.assertEqual(monitor._fetch_profile_with_activity(self.player, self.season), self.payload)
        self.assertEqual(monitor._fetch_profile_with_activity(self.player, self.season), self.payload)
        self.assertTrue(collector.refresh.call_args_list[0].kwargs['include_profile'])
        self.assertEqual(collector.refresh.call_args_list[1].kwargs, {})

    def test_twenty_unchanged_players_same_day_make_zero_detail_requests(self):
        for index in range(20):
            self.player.character_name = f'Tester{index}'
            self.player.profile_url = f'https://raider.io/characters/eu/test-realm/Tester{index}'
            self.fetch.return_value = {**self.payload, 'name': self.player.character_name,
                'profile_url': self.player.profile_url}
            self.get('unchanged')
        self.assertEqual(self.fetch.call_count, 20)
        self.age(hours=12)
        self.fetch.reset_mock()
        for index in range(20):
            self.player.character_name = f'Tester{index}'
            self.player.profile_url = f'https://raider.io/characters/eu/test-realm/Tester{index}'
            self.get('unchanged')
        self.fetch.assert_not_called()

    def test_signal_ignores_position_but_tracks_equipment_talents_and_score(self):
        data = {'gear': [{'id': 1}], 'talent_build_code': 'code', 'score': 10, 'rank': 1}
        original = ranking_signal(data)
        self.assertEqual(original, ranking_signal({**data, 'rank': 2}))
        for key, value in [('gear', [{'id': 2}]), ('talent_build_code', 'new'), ('score', 11)]:
            self.assertNotEqual(original, ranking_signal({**data, key: value}))
        self.assertEqual(ranking_signal({**data, 'gear': []}), '')

    def test_daily_stats_collection_excludes_historical_sample_players(self):
        for rank, name in [(1, 'Current'), (None, 'Historical'), (21, 'Beyond')]:
            PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu', realm='test',
                character_name=name, class_name='Mage', spec_name='Fire', rank=rank)
        monitor = SpecDetailPlayerMonitor(None, None)
        with patch.object(monitor, '_get_battlenet_token', return_value='token'), patch.object(
                monitor, 'fetch_battlenet_stats', return_value={}) as fetch, patch(
                'botend.controller.plugins.portal.SpecDetailPlayerMonitor.time.sleep'):
            monitor._crawl_battlenet_stats(self.season.pk, ranked_only=True)
        fetch.assert_called_once_with('test', 'Current', 'eu')
