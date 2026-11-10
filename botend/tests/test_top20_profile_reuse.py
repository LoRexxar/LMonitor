"""Top20资料复用、榜外停止补采与本地回填查询量回归。"""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.models import SeasonMeta, PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking, SpecAggregationWorkCache
from botend.services.player_ranking_reuse import ranking_fallbacks, identity
from botend.services.raiderio_profile_cache import ranking_signal


def nodes():
    return [dict(talent_id=i + 1, name='天赋', icon='图标') for i in range(5)]


class Top20ProfileReuseTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='复用测试', season_name='测试',
            is_active=True, rio_season='test', mplus_zone_id=1, raid_zone_id=2)
        self.monitor = SpecDetailPlayerMonitor(None, None)
        self.monitor._activity_season = self.season

    def player(self, name='测试角色', **extra):
        return {'name': name, 'region': 'cn', 'realm': '测试服务器', '_class_name': 'Mage', '_spec_name': 'Fire',
                'gear': [], 'talents': [], **extra}

    def saved(self, **extra):
        fields = dict(season_id=self.season.pk, class_name='Mage', spec_name='Fire', region='cn',
            realm='测试服务器', character_name='测试角色', rank=1, gear_json=[{'id': 11}],
            talents_json=nodes(), talent_build_code='旧代码', last_updated=timezone.now()-timedelta(days=2))
        return PlayerSpecTopPlayer.objects.create(**{**fields, **extra})

    def ranking(self, name='测试角色', **extra):
        fields = dict(season_id=self.season.pk, class_name='Mage', spec_name='Fire', region='cn',
            realm='测试服务器', character_name=name, dungeon_id=1, dps=1,
            talents_json=nodes(), gear_json=[{'id': 22}], last_updated=timezone.now())
        return SpecDungeonRanking.objects.create(**{**fields, **extra})

    def test_saved_complete_profile_avoids_fallback_and_preserves_original_signal_and_clock(self):
        old = self.saved(region='eu')
        player = self.player(region='eu')
        signal, clock = ranking_signal(player), old.last_updated
        self.monitor._reuse_saved_profiles([player], 'Mage', 'Fire')
        self.assertEqual(player['_ranking_signal'], signal)
        self.assertEqual((player['gear'], player['talents']), (old.gear_json, old.talents_json))
        with self.assertNumQueries(0):
            self.monitor._enrich_from_local_rankings([player])
        old.refresh_from_db()
        self.assertEqual(old.last_updated, clock)

    def test_changed_source_talent_code_does_not_reuse_old_nodes_or_overwrite_source_gear(self):
        self.saved()
        player = self.player(talent_build_code='新代码', gear=[{'id': 33}])
        self.monitor._reuse_saved_profiles([player], 'Mage', 'Fire')
        self.assertEqual(player['gear'], [{'id': 33}])
        self.assertEqual(player['talents'], [])

    def test_new_verified_profile_nodes_replace_reused_nodes_even_when_code_is_unchanged(self):
        self.saved(region='eu')
        player = self.player(region='eu')
        self.monitor._reuse_saved_profiles([player], 'Mage', 'Fire')
        fresh = nodes()
        fresh[0]['talent_id'] = 789
        with patch.object(self.monitor, '_fetch_profile_with_activity', return_value={'talentLoadoutText': '旧代码'}), \
                patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.TalentBuildCodeDecoder.matches_spec', return_value=True), \
                patch.object(self.monitor, '_parse_rio_talents_from_profile', return_value=fresh), \
                patch.object(self.monitor, '_normalize_talent_nodes', side_effect=lambda value, *a: value):
            self.monitor._enrich_talents([player])
        self.assertEqual(player['talents'], fresh)

    def test_drop_out_preserves_data_and_reentry_reuses_it(self):
        old = self.saved(rank=21)
        with patch.object(self.monitor, '_fetch_profile_with_activity') as fetch:
            self.assertFalse(self.monitor._enrich_profile_model_from_raiderio(old))
        fetch.assert_not_called()
        player = self.player()
        self.monitor._reuse_saved_profiles([player], 'Mage', 'Fire')
        self.assertEqual(player['gear'], old.gear_json)
        old.refresh_from_db()
        self.assertEqual(old.rank, 21)

    def test_same_name_other_realm_does_not_supply_profile(self):
        self.saved(realm='其他服务器')
        player = self.player()
        self.monitor._reuse_saved_profiles([player], 'Mage', 'Fire')
        self.assertEqual(player['gear'], [])

    def test_missing_twenty_profiles_use_two_batch_lookups_then_zero_and_do_not_extend_clock(self):
        players = [self.player('缺失'+str(i)) for i in range(20)]
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now), CaptureQueriesContext(connection) as queries:
            _, first = ranking_fallbacks(self.season, players)
        lookups = [q['sql'] for q in queries if '"talents_json"' in q['sql'] and 'NOT' in q['sql']]
        self.assertEqual(len(lookups), 2)
        self.assertEqual(first['queried'], 20)
        state = SpecAggregationWorkCache.objects.get()
        original, written = deepcopy(state.payload), state.updated_at
        with patch('django.utils.timezone.now', return_value=now+timedelta(hours=2)), CaptureQueriesContext(connection) as queries:
            _, second = ranking_fallbacks(self.season, players)
        self.assertEqual(second, {'candidates': 20, 'cache_hits': 20, 'queried': 0})
        self.assertFalse(any('"talents_json"' in q['sql'] for q in queries))
        state.refresh_from_db()
        self.assertEqual((state.payload, state.updated_at), (original, written))

    def test_negative_cache_expires_after_24_hours(self):
        now = timezone.now()
        with patch('django.utils.timezone.now', return_value=now):
            ranking_fallbacks(self.season, [self.player()])
        with patch('django.utils.timezone.now', return_value=now+timedelta(hours=24)):
            _, counts = ranking_fallbacks(self.season, [self.player()])
        self.assertEqual(counts['queried'], 1)

    def test_new_ranking_invalidates_negative_cache_and_positive_reference_is_reused(self):
        player = self.player()
        ranking_fallbacks(self.season, [player])
        row = self.ranking()
        result, counts = ranking_fallbacks(self.season, [player])
        key = (identity('cn', '测试服务器', '测试角色'), 'Mage', 'Fire')
        self.assertEqual(result[key].pk, row.pk)
        self.assertEqual(counts['queried'], 1)
        _, counts = ranking_fallbacks(self.season, [player])
        self.assertEqual(counts['cache_hits'], 1)
        self.assertEqual(counts['queried'], 0)

    def test_changed_existing_ranking_invalidates_reference(self):
        row = self.ranking()
        player = self.player()
        ranking_fallbacks(self.season, [player])
        SpecDungeonRanking.objects.filter(pk=row.pk).update(last_updated=timezone.now()+timedelta(seconds=1), gear_json=[{'id': 88}])
        result, counts = ranking_fallbacks(self.season, [player])
        self.assertEqual(next(iter(result.values())).gear_json, [{'id': 88}])
        self.assertEqual(counts['queried'], 1)

    def test_deletion_and_wrong_identity_never_reuse_other_profile(self):
        row = self.ranking()
        player = self.player()
        ranking_fallbacks(self.season, [player])
        row.delete()
        self.ranking(realm='另一个服务器')
        result, _ = ranking_fallbacks(self.season, [player])
        self.assertEqual(result, {})

    def test_local_fallback_does_not_overwrite_already_complete_talents(self):
        self.ranking(talents_json=[{'talent_id': 999}])
        player = self.player(talents=nodes())
        self.monitor._enrich_from_local_rankings([player])
        self.assertEqual(player['talents'], nodes())
        self.assertEqual(player['gear'][0]['id'], 22)

    def test_only_first_twenty_enter_profile_and_local_fallback(self):
        players = [self.player(str(i)) for i in range(21)]
        for player in players:
            player['region'] = 'eu'
        with patch.object(self.monitor, '_fetch_profile_with_activity', return_value=None) as fetch, \
                patch.object(self.monitor, '_enrich_from_local_rankings') as fallback:
            self.monitor._enrich_talents(players)
        self.assertEqual(fetch.call_count, 20)
        self.assertEqual(len(fallback.call_args.args[0]), 20)
        self.assertNotIn('_profile_refresh_failed', players[-1])

    def test_canonical_profile_identity_requires_no_directory_query(self):
        from botend.services.player_raiderio_activity import player_identity
        player = SimpleNamespace(region='cn', realm='测试服务器', character_name='测试角色',
            profile_url='https://raider.io/characters/cn/test/测试角色')
        with self.assertNumQueries(0):
            self.assertEqual(player_identity(player), {'region': 'cn', 'realm_slug': 'test', 'character_name': '测试角色'})
        player.profile_url = 'https://example.com/characters/cn/test/测试角色'
        self.assertIsNone(player_identity(player))

    def test_cn_saved_data_rechecks_changed_local_source_without_losing_last_good(self):
        self.saved(talent_build_code='')
        player = self.player()
        self.monitor._reuse_saved_profiles([player], 'Mage', 'Fire')
        self.monitor._enrich_from_local_rankings([player])
        self.assertEqual(player['gear'], [{'id': 11}])
        fresh = nodes()
        fresh[0]['talent_id'] = 999
        self.ranking(talents_json=fresh)
        with patch.object(self.monitor, '_normalize_talent_nodes', side_effect=lambda value, *a: value):
            self.monitor._enrich_from_local_rankings([player])
        self.assertEqual(player['gear'][0]['id'], 22)
        self.assertEqual(player['talents'][0]['talent_id'], 999)

    def test_bound_profile_cache_reuse_keeps_saved_time(self):
        from django.contrib.auth import get_user_model
        from botend.models import UserCharacterBinding
        from botend.raiderio_identity import activity_identity_digest
        from botend.services.character_bindings import refresh_bound_characters
        row = self.saved(rank=None, region='eu', profile_url='https://raider.io/characters/eu/test/测试角色')
        clock = row.last_updated
        user = get_user_model().objects.create_user(username='绑定复用')
        digest = activity_identity_digest(region='eu', realm_slug='test', character_name=row.character_name)
        UserCharacterBinding.objects.create(user=user, player=row, identity_digest=digest,
            region='eu', realm_slug='test', character_name=row.character_name)
        with patch.object(self.monitor, '_fetch_profile_with_activity', return_value={'name': row.character_name}), \
                patch.object(self.monitor, '_crawl_battlenet_stats_for_profile'):
            refresh_bound_characters(self.monitor, self.season)
        row.refresh_from_db()
        self.assertEqual(row.last_updated, clock)

    def test_bound_and_top20_activity_candidates_are_deduplicated_and_disable_takes_effect(self):
        from django.contrib.auth import get_user_model
        from botend.models import UserCharacterBinding
        from botend.raiderio_identity import activity_identity_digest
        from botend.services.player_raiderio_activity import maintained_candidates
        row = self.saved(profile_url='https://raider.io/characters/cn/test/测试角色')
        user = get_user_model().objects.create_user(username='绑定范围')
        binding = UserCharacterBinding.objects.create(user=user, player=row,
            identity_digest=activity_identity_digest(region='cn', realm_slug='test', character_name=row.character_name),
            region='cn', realm_slug='test', character_name=row.character_name)
        self.assertEqual(len(maintained_candidates(self.season)), 1)
        row.rank = None
        row.save(update_fields=['rank'])
        self.assertEqual(len(maintained_candidates(self.season)), 1)
        binding.is_active = False
        binding.save(update_fields=['is_active'])
        self.assertEqual(maintained_candidates(self.season), [])
