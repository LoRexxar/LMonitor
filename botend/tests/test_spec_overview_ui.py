import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TestCase, override_settings


@override_settings(ALLOWED_HOSTS=['testserver'])
class SpecOverviewIntegrationTests(TestCase):
    """The rendered overview must form a real, independently loadable HTTP graph."""

    def _rendered_endpoints(self):
        response = self.client.get('/portal/spec/Mage/Fire/')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        cards = soup.select('#spec-overview [data-spec-module]')
        self.assertEqual(len(cards), 3)
        return {card['data-spec-module']: card.get('data-endpoint') for card in cards}

    @patch('botend.services.spec_overview_service.SpecOverviewService._aggregate', return_value=({}, None))
    def test_all_three_rendered_endpoints_resolve_and_return_json(self, aggregate):
        endpoints = self._rendered_endpoints()
        self.assertEqual(set(endpoints), {'players', 'mythic-plus', 'raid'})
        for module, endpoint in endpoints.items():
            with self.subTest(module=module, endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertNotEqual(response.status_code, 404)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/json')
                self.assertIsInstance(response.json(), dict)

        self.assertEqual(aggregate.call_count, 3)

    @patch('botend.services.spec_overview_service.SpecOverviewService._aggregate')
    def test_stats_endpoints_are_independent_service_projections(
        self, aggregate,
    ):
        aggregate.side_effect = [
            ({'players': [{'rank': 1}], 'updated_at': 'players-time'}, None),
            ({'dungeons': [{'dungeon_id': 11}]}, None),
            ({'zone_groups': [{'zone_id': 22, 'bosses': []}]}, None),
        ]
        endpoints = self._rendered_endpoints()

        players = self.client.get(endpoints['players']).json()
        mythic = self.client.get(endpoints['mythic-plus']).json()
        raid = self.client.get(endpoints['raid']).json()

        self.assertEqual(players, {
            'source': 'Raider.IO', 'updated_at': 'players-time',
            'players': [{'rank': 1}], 'total': 1, 'page': 1, 'pages': 1,
        })
        self.assertEqual(mythic['source'], 'Raider.IO')
        self.assertEqual(mythic['dungeons'], [{'dungeon_id': 11}])
        self.assertIn('updated_at', mythic)
        self.assertEqual(raid['source'], 'Warcraft Logs')
        self.assertEqual(raid['zone_groups'], [{'zone_id': 22, 'bosses': []}])
        self.assertIn('updated_at', raid)

    @patch('botend.services.spec_overview_service.SpecOverviewService._aggregate')
    def test_stats_overviews_expose_only_summary_fields_needed_by_first_screen(self, aggregate):
        aggregate.side_effect = [
            ({
                'dungeons': [{
                    'dungeon_id': 11, 'dungeon_name': 'Test dungeon', 'sample_size': 24,
                    'dps': {'median': 123456, 'avg': 120000, 'p75': 130000},
                    'keystone': {'avg': 12.5, 'max': 18},
                    'clear_time': {'median_fmt': '24:00', 'avg_fmt': '25:00'},
                    'talent_popularity': [{'name': 'Heavy payload'}],
                    'talent_usage': {'nodes': ['heavy']},
                    'talent_popularity_tree': {'tree': ['heavy']},
                    'gear_popularity': [{'item_id': 123}],
                    'gem_popularity': [{'item_id': 456}],
                    'enchant_popularity': [{'spell_id': 789}],
                    'top5': [{'character_name': 'Not for overview'}],
                }],
            }, None),
            ({
                'zone_groups': [{
                    'zone_id': 22, 'zone_name': 'Test raid', 'zone_cn': '测试团本',
                    'bosses': [{
                        'boss_id': 33, 'boss_name': 'Test boss', 'sample_size': 48,
                        'dps': {'median': 234567, 'avg': 230000, 'p75': 240000},
                        'kill_time': {'median_fmt': '05:00', 'avg_fmt': '05:30'},
                        'talent_popularity': [{'name': 'Heavy payload'}],
                        'talent_usage': {'nodes': ['heavy']},
                        'gear_popularity': [{'item_id': 123}],
                        'top5': [{'character_name': 'Not for overview'}],
                    }],
                }],
            }, None),
        ]
        endpoints = self._rendered_endpoints()

        mythic = self.client.get(endpoints['mythic-plus']).json()
        raid = self.client.get(endpoints['raid']).json()

        self.assertEqual(mythic['dungeons'], [{
            'dungeon_id': 11, 'dungeon_name': 'Test dungeon', 'sample_size': 24,
            'dps': {'median': 123456, 'avg': 120000},
            'keystone': {'avg': 12.5}, 'clear_time': {'median_fmt': '24:00'},
        }])
        self.assertEqual(raid['zone_groups'], [{
            'zone_id': 22, 'zone_name': 'Test raid', 'zone_cn': '测试团本',
            'bosses': [{
                'boss_id': 33, 'boss_name': 'Test boss', 'sample_size': 48,
                'dps': {'median': 234567, 'avg': 230000},
                'kill_time': {'median_fmt': '05:00'},
            }],
        }])

    @patch('botend.services.spec_overview_service.SpecOverviewService._aggregate')
    def test_stats_overviews_ignore_malformed_collection_fields(self, aggregate):
        aggregate.side_effect = [
            ({'dungeons': 1}, None),
            ({'zone_groups': [{'zone_id': 22, 'bosses': 1}]}, None),
        ]
        endpoints = self._rendered_endpoints()

        self.assertEqual(self.client.get(endpoints['mythic-plus']).json()['dungeons'], [])
        self.assertEqual(self.client.get(endpoints['raid']).json()['zone_groups'], [
            {'zone_id': 22, 'bosses': []},
        ])

    def test_stats_endpoints_reject_stale_season_projection(self):
        with tempfile.TemporaryDirectory() as media_root:
            season_id = 99
            base = Path(media_root) / 'aggregated' / str(season_id) / 'Mage' / 'Fire'
            base.mkdir(parents=True)
            (base / 'dungeon.json').write_text(json.dumps({
                'dungeons': [{'dungeon_id': 1, 'dungeon_name': 'Season one dungeon'}],
            }), encoding='utf-8')
            (base / 'raid.json').write_text(json.dumps({
                'zone_groups': [{'zone_id': 1, 'bosses': [{'boss_id': 2, 'boss_name': 'Season one boss'}]}],
            }), encoding='utf-8')
            cache.clear()
            active_season = SimpleNamespace(
                id=season_id, season_name='Test season', season_key='test-season',
                mplus_encounters=[{'id': 3, 'name': 'Season two dungeon'}],
                raid_encounters=[{'id': 4, 'name': 'Season two boss'}],
            )
            with override_settings(MEDIA_ROOT=media_root), patch(
                'botend.services.spec_overview_service.SpecStatsService.get_active_season',
                return_value=active_season,
            ):
                endpoints = self._rendered_endpoints()
                self.assertEqual(self.client.get(endpoints['mythic-plus']).json()['dungeons'], [])
                self.assertEqual(self.client.get(endpoints['raid']).json()['zone_groups'], [])

    def test_stats_endpoints_prefer_aggregate_files_and_cache_each_module(self):
        with tempfile.TemporaryDirectory() as media_root:
            season_id = 99
            base = Path(media_root) / 'aggregated' / str(season_id) / 'Mage' / 'Fire'
            base.mkdir(parents=True)
            (base / 'leaderboard.json').write_text(json.dumps({
                'players': [{'id': 7, 'character_name': 'Safe'}], 'total': 1,
            }), encoding='utf-8')
            (base / 'dungeon.json').write_text(json.dumps({
                'dungeons': [{'dungeon_id': 3}], 'updated_at': 'aggregate-time',
            }), encoding='utf-8')
            (base / 'raid.json').write_text(json.dumps({
                'zone_groups': [{'zone_id': 4, 'bosses': []}],
            }), encoding='utf-8')
            cache.clear()
            with override_settings(MEDIA_ROOT=media_root), patch(
                'botend.services.spec_overview_service.SpecStatsService.get_active_season',
                return_value=SimpleNamespace(
                    id=season_id, season_name='Test season', season_key='test-season',
                ),
            ), patch(
                'botend.services.spec_overview_service.SpecStatsService.get_player_list'
            ) as live_players, patch(
                'botend.services.spec_overview_service.SpecStatsService.get_dungeon_overview'
            ) as live_dungeons, patch(
                'botend.services.spec_overview_service.SpecStatsService.get_raid_overview'
            ) as live_raid:
                endpoints = self._rendered_endpoints()
                players = self.client.get(endpoints['players']).json()
                mythic = self.client.get(endpoints['mythic-plus']).json()
                raid = self.client.get(endpoints['raid']).json()
                os.unlink(base / 'dungeon.json')
                self.assertEqual(self.client.get(endpoints['mythic-plus']).json(), mythic)
            live_players.assert_not_called()
            live_dungeons.assert_not_called()
            live_raid.assert_not_called()
            self.assertEqual(players['players'][0]['detail_url'], '/portal/spec/Mage/Fire/player/7/')
            self.assertEqual(mythic['updated_at'], 'aggregate-time')
            self.assertEqual(raid['zone_groups'][0]['zone_id'], 4)


class SpecOverviewDOMContractTests(TestCase):
    """Keep only structural/failure-isolation contracts that HTTP tests cannot cover."""

    def test_cards_keep_independent_state_and_content_nodes(self):
        response = self.client.get('/portal/spec/Mage/Fire/')
        soup = BeautifulSoup(response.content, 'html.parser')
        cards = soup.select('#spec-overview [data-spec-module]')
        self.assertEqual(len(cards), 3)
        for card in cards:
            self.assertIsNotNone(card.select_one('[data-module-state][role="status"]'))
            self.assertIsNotNone(card.select_one('[data-module-content]'))

    def test_loader_is_failure_isolated_for_live_data_modules(self):
        js = (Path(__file__).resolve().parents[2] / 'static/portal/js/spec-overview.js').read_text()
        self.assertIn('cards.forEach(loadModule)', js)
        self.assertIn('fetch(endpoint', js)
        self.assertIn('payload?.status === "not_ready"', js)
        self.assertIn('description.detail_url', js)
        self.assertIn('AbortController', js)
        self.assertIn('signal: controller.signal', js)

    def test_overview_assets_keep_cache_query_outside_static_path(self):
        response = self.client.get('/portal/spec/Mage/Fire/')
        content = response.content.decode()

        self.assertIn('<link rel="stylesheet" href="/static/portal/css/spec-overview.css?v=20260809">', content)
        self.assertIn('<script src="/static/portal/js/spec-overview.js?v=20260810" defer></script>', content)
        self.assertNotIn('spec-overview.js%3Fv', content)
        self.assertNotIn('spec-overview.css%3Fv', content)

    def test_overview_keeps_only_live_data_modules_and_standard_portal_header(self):
        response = self.client.get('/portal/spec/Mage/Fire/')
        soup = BeautifulSoup(response.content, 'html.parser')
        js = (Path(__file__).resolve().parents[2] / 'static/portal/js/spec-overview.js').read_text()

        self.assertEqual(
            {card['data-spec-module'] for card in soup.select('#spec-overview [data-spec-module]')},
            {'players', 'mythic-plus', 'raid'},
        )
        self.assertIsNone(soup.select_one('#module-simc'))
        self.assertNotIn('/portal/api/simc-benchmarks/', response.content.decode())
        self.assertIsNotNone(soup.select_one('.portal-header'))
        self.assertIn('中位 DPS', js)
        self.assertIn('M+ 评分', js)
        self.assertIn('样本有限', js)
        self.assertIn('dungeon_id', js)
        self.assertIn('boss_id', js)
        self.assertNotIn('renderSimc', js)
        self.assertNotIn('simc-benchmarks', js)
