import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import TestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.portal.PortalPeakSpecRankMonitor import PortalPeakSpecRankMonitor
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.constants.wow import canonical_class_spec
from botend.models import PlayerSpecTopPlayer, SeasonMeta


class PortalPeakSpecRankMonitorSeasonTests(TestCase):
    def test_all_peak_rank_source_slugs_resolve_to_canonical_spec_identities(self):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        unresolved = [
            (item['class_slug'], item['spec_slug'])
            for item in monitor._spec_list()
            if canonical_class_spec(item['class_slug'], item['spec_slug']) is None
        ]

        self.assertEqual(unresolved, [])

    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.SeasonMeta.objects.filter')
    def test_resolve_season_uses_active_season_metadata_rio_season(self, season_filter):
        season_filter.return_value.first.return_value = Mock(rio_season='season-mn-2')
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-2')
        season_filter.assert_called_once_with(is_active=True)

    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.SeasonMeta.objects.filter')
    def test_resolve_season_returns_empty_when_active_metadata_has_no_rio_season(self, season_filter):
        season_filter.return_value.first.return_value = Mock(rio_season='')
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), '')

    @patch.object(PortalPeakSpecRankMonitor, '_fetch_and_upsert')
    @patch.object(PortalPeakSpecRankMonitor, '_resolve_season', return_value='')
    def test_scan_fails_without_metadata_season_and_does_not_fetch_rankings(self, resolve_season, fetch):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertFalse(monitor.scan(''))
        fetch.assert_not_called()

    def test_empty_rankings_are_failure_and_do_not_replace_current_rows(self):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())
        response = Mock(status_code=200)
        response.json.return_value = {'rankings': {'rankedCharacters': []}}

        with patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get', return_value=response), \
                patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.PortalPeakSpecRankRow.objects') as objects:
            ok = monitor._fetch_and_upsert(
                season='season-mn-1', region='world', class_slug='death-knight', spec_slug='blood'
            )

        self.assertFalse(ok)
        objects.filter.assert_not_called()

    def test_duplicate_top_twenty_identity_is_failure_and_preserves_snapshot(self):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())
        duplicate = {
            'score': 3000,
            'character': {
                'name': 'Duplicate',
                'realm': {'name': 'Test', 'slug': 'test'},
                'region': {'slug': 'us'},
            },
        }
        response = Mock(status_code=200)
        response.json.return_value = {'rankings': {'rankedCharacters': [duplicate] * 20}}

        with patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get', return_value=response), \
                patch.object(monitor, '_persist_rank_snapshot') as persist:
            ok = monitor._fetch_and_upsert(
                season='season-mn-1', region='world', class_slug='mage', spec_slug='arcane'
            )

        self.assertFalse(ok)
        persist.assert_not_called()

    def test_fetch_and_upsert_persists_raiderio_top_twenty(self):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())
        rankings = [
            {
                'rank': index,
                'score': 3000 - index,
                'scoreColor': '#ffffff',
                'character': {
                    'name': f'Player{index}',
                    'path': f'/characters/us/test/Player{index}',
                    'class': {'name': 'Mage'},
                    'spec': {'name': 'Arcane', 'role': 'dps'},
                    'realm': {'slug': 'test', 'name': 'Test'},
                    'region': {'slug': 'us'},
                },
            }
            for index in range(1, 21)
        ]
        response = Mock(status_code=200)
        response.json.return_value = {'rankings': {'rankedCharacters': rankings}}

        with patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get', return_value=response), \
                patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.PortalPeakSpecRankRow.objects') as objects, \
                patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.SpecDetailPlayerMonitor.preload_peak_rankings'):
            ok = monitor._fetch_and_upsert(
                season='season-mn-2', region='world', class_slug='mage', spec_slug='arcane'
            )

        self.assertTrue(ok)
        self.assertEqual(objects.update_or_create.call_count, 20)
        self.assertEqual(
            [call.kwargs['rank'] for call in objects.update_or_create.call_args_list],
            list(range(1, 21)),
        )


class PortalPeakSpecRankPreloadTests(TestCase):
    def test_peak_refresh_immediately_rebuilds_the_public_leaderboard_projection(self):
        season = SeasonMeta.objects.create(
            season_key='mn-s2', season_name='MN S2', rio_season='season-mn-2',
            mplus_zone_id=1, raid_zone_id=1, is_active=True,
        )
        content_updated_at = timezone.now()
        player = PlayerSpecTopPlayer.objects.create(
            season_id=season.id, region='us', realm='Test', character_name='Noxiv',
            class_name='Warrior', spec_name='Fury', rank=9, score=2900,
            gear_json=[{'id': 123}], stats_crawl_status=1, last_updated=content_updated_at,
        )
        rankings = [{
            'score': 3147.92,
            'character': {
                'name': 'Noxiv',
                'class': {'name': 'Warrior'},
                'spec': {'name': 'Fury', 'role': 'dps'},
                'realm': {'slug': 'test', 'name': 'Test'},
                'region': {'slug': 'us'},
            },
        }]

        with tempfile.TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            SpecDetailPlayerMonitor(Mock(), Mock()).preload_peak_rankings(
                rio_season='season-mn-2', class_name='Warrior', spec_name='Fury', rankings=rankings,
            )
            projection = Path(media_root) / 'aggregated' / str(season.id) / 'Warrior' / 'Fury' / 'leaderboard.json'
            payload = json.loads(projection.read_text(encoding='utf-8'))

        player.refresh_from_db()
        self.assertEqual(payload['players'][0]['score'], 3147.92)
        self.assertIsNotNone(payload['updated_at'])
        self.assertEqual(player.last_updated, content_updated_at)

    def test_peak_refresh_initializes_only_new_players_and_lightly_updates_existing_players(self):
        season = SeasonMeta.objects.create(
            season_key='mn-s2', season_name='MN S2', rio_season='season-mn-2',
            mplus_zone_id=1, raid_zone_id=1,
        )
        existing = PlayerSpecTopPlayer.objects.create(
            season_id=season.id, region='us', realm='test', character_name='Existing',
            class_name='Mage', spec_name='Arcane', rank=9, score=2900,
            gear_json=[{'id': 123, 'name': '保留的旧装备'}],
            talent_build_code='OLD-TALENT', stats_json={'critical_strike': 42},
            stats_crawl_status=1, last_updated=timezone.now(),
        )
        existing_content_updated_at = existing.last_updated
        misclassified = PlayerSpecTopPlayer.objects.create(
            season_id=season.id, region='us', realm='Test', character_name='NewPlayer2',
            class_name='Warlock', spec_name='Arcane', rank=5, score=2500,
            gear_json=[{'id': 789, 'name': '错误职业记录的旧装备'}],
            talent_build_code='MISCLASSIFIED-TALENT', stats_crawl_status=1,
            last_updated=timezone.now(),
        )
        rankings = [
            {
                'score': 4000 - index,
                'scoreColor': '#ffffff',
                'character': {
                    'name': 'Existing' if index == 0 else f'NewPlayer{index}',
                    'path': f"/characters/us/test/{'Existing' if index == 0 else f'NewPlayer{index}'}",
                    'class': {'name': 'Mage'},
                    'spec': {'name': 'Arcane', 'role': 'dps'},
                    'realm': {'slug': 'test', 'name': 'Test'},
                    'region': {'slug': 'us'},
                },
            }
            for index in range(20)
        ]
        response = Mock(status_code=200)
        response.json.return_value = {'rankings': {'rankedCharacters': rankings}}
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        attempts = {}
        stats_attempts = {}

        def initialize_new_player(profile):
            attempts[profile.character_name] = attempts.get(profile.character_name, 0) + 1
            profile.gear_json = [{'id': 456, 'name': '新玩家初始化装备', 'slot': 'head'}]
            profile.talent_build_code = 'NEW-TALENT'
            return True

        def initialize_new_player_stats(profile):
            stats_attempts[profile.character_name] = stats_attempts.get(profile.character_name, 0) + 1
            if profile.character_name == 'NewPlayer1' and stats_attempts[profile.character_name] == 1:
                profile.stats_crawl_status = -1
                profile.save(update_fields=['stats_crawl_status'])
                return False
            profile.stats_crawl_status = 1
            profile.save(update_fields=['stats_crawl_status'])
            return True

        with patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get', return_value=response), \
                patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.SpecDetailPlayerMonitor._enrich_profile_model_from_raiderio', side_effect=initialize_new_player) as enrich, \
                patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.SpecDetailPlayerMonitor._crawl_battlenet_stats_for_profile', side_effect=initialize_new_player_stats) as crawl_stats:
            self.assertTrue(monitor._fetch_and_upsert(
                season='season-mn-2', region='world', class_slug='mage', spec_slug='arcane'
            ))
            # 初始化失败的占位人物应在下一次轻量榜单刷新中继续初始化。
            self.assertTrue(monitor._fetch_and_upsert(
                season='season-mn-2', region='world', class_slug='mage', spec_slug='arcane'
            ))

        existing.refresh_from_db()
        self.assertEqual((existing.rank, existing.score), (1, 4000))
        self.assertEqual(existing.gear_json, [{'id': 123, 'name': '保留的旧装备'}])
        self.assertEqual(existing.talent_build_code, 'OLD-TALENT')
        self.assertEqual(existing.stats_json, {'critical_strike': 42})
        self.assertEqual(existing.last_updated, existing_content_updated_at)
        misclassified.refresh_from_db()
        self.assertEqual((misclassified.class_name, misclassified.rank, misclassified.score), ('Mage', 3, 3998))
        self.assertEqual(misclassified.gear_json, [{'id': 789, 'name': '错误职业记录的旧装备'}])
        self.assertEqual(misclassified.talent_build_code, 'MISCLASSIFIED-TALENT')
        self.assertEqual(enrich.call_count, 19)
        self.assertEqual(attempts['NewPlayer1'], 2)
        self.assertNotIn('NewPlayer2', attempts)
        self.assertTrue(all(attempts[f'NewPlayer{index}'] == 1 for index in range(3, 20)))
        self.assertEqual(crawl_stats.call_count, 19)
        self.assertEqual(stats_attempts['NewPlayer1'], 2)
        initialized = PlayerSpecTopPlayer.objects.get(character_name='NewPlayer1')
        self.assertEqual((initialized.rank, initialized.score), (2, 3999))
        self.assertEqual(initialized.gear_json[0]['id'], 456)
        self.assertEqual(initialized.gear_json[0]['name'], '新玩家初始化装备')
        self.assertEqual(initialized.gear_json[0]['slot'], 'head')
        self.assertEqual(initialized.talent_build_code, 'NEW-TALENT')