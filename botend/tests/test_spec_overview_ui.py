import hashlib
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from bs4 import BeautifulSoup
from django.core.cache import cache
from django.test import TestCase, override_settings

from botend.models import (
    SimcApl,
    SimcBackendBinary,
    SimcBenchmarkPanel,
    SimcBenchmarkProfile,
    SimcBenchmarkScenario,
    SimcBenchmarkSpec,
    SimcContentTemplate,
    SimcProfile,
)


@override_settings(ALLOWED_HOSTS=['testserver'])
class SpecOverviewIntegrationTests(TestCase):
    """The rendered overview must form a real, independently loadable HTTP graph."""

    def setUp(self):
        backend = SimcBackendBinary.objects.create(
            identifier='overview-simc', name='Overview SimC',
            current_version='a' * 40, is_active=True,
        )
        apl_content = 'actions=/fireball'
        apl = SimcApl.objects.create(
            name='Fire standard', spec='mage_fire', content=apl_content,
            owner_user_id=1, is_active=True, is_selectable=True,
            validation_status=SimcApl.VALIDATION_VALID,
            validated_content_hash=hashlib.sha256(apl_content.encode()).hexdigest(),
            validation_revision='a' * 40, validation_game_build='12.0.1',
        )
        template = SimcContentTemplate.objects.create(
            name='Fire template', spec='mage_fire', content='iterations=1000',
            owner_user_id=1,
        )
        self.panel = SimcBenchmarkPanel.objects.create(
            name='Public overview', slug='public-overview', created_by_id=1,
            is_active=True, is_public=True,
        )
        SimcBenchmarkSpec.objects.create(
            panel=self.panel, class_name='mage', spec_key='mage_fire',
            label='Fire', apl=apl, template=template, backend=backend,
        )
        self.scenario = SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='single', name='Single target',
            simulation_params={'iterations': 1000}, is_enabled=True,
        )
        overview_profile = SimcProfile.objects.create(
            user_id=1, name='Fire overview profile', class_name='mage', spec='mage_fire',
        )
        SimcBenchmarkProfile.objects.create(
            panel_spec=self.panel.specs.get(spec_key='mage_fire'),
            profile=overview_profile, label='Fire standard', is_enabled=True,
        )
        # This panel must never be selected by overview discovery.
        private = SimcBenchmarkPanel.objects.create(
            name='Private overview', slug='private-overview', created_by_id=1,
            is_active=True, is_public=False,
        )
        SimcBenchmarkSpec.objects.create(
            panel=private, class_name='mage', spec_key='mage_fire', label='Secret Fire',
            apl=apl, template=template, backend=backend,
        )
        SimcBenchmarkScenario.objects.create(
            panel=private, key='secret', name='Secret', simulation_params={},
            is_enabled=True,
        )

    def _rendered_endpoints(self):
        response = self.client.get('/portal/spec/Mage/Fire/')
        self.assertEqual(response.status_code, 200)
        soup = BeautifulSoup(response.content, 'html.parser')
        cards = soup.select('#spec-overview [data-spec-module][data-endpoint]')
        self.assertEqual(len(cards), 5)
        return {card['data-spec-module']: card['data-endpoint'] for card in cards}

    @patch('botend.portal.simc_benchmark_api.serialize_incremental_panel_results', return_value={'coordinates': []})
    @patch('botend.services.spec_overview_service.SpecOverviewService._aggregate', return_value=({}, None))
    def test_all_five_rendered_endpoints_resolve_and_return_json(
        self, aggregate, projection,
    ):
        endpoints = self._rendered_endpoints()
        self.assertEqual(set(endpoints), {
            'players', 'mythic-plus', 'raid', 'simc-apl', 'simc-cross-spec',
        })
        for module, endpoint in endpoints.items():
            with self.subTest(module=module, endpoint=endpoint):
                response = self.client.get(endpoint)
                self.assertNotEqual(response.status_code, 404)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response['Content-Type'], 'application/json')
                self.assertIsInstance(response.json(), dict)

        self.assertEqual(aggregate.call_count, 3)

    def test_simc_endpoints_use_discovered_public_configured_dimensions(self):
        endpoints = self._rendered_endpoints()
        for module in ('simc-apl', 'simc-cross-spec'):
            parsed = urlsplit(endpoints[module])
            params = parse_qs(parsed.query)
            self.assertIn(parsed.path, {
                '/portal/api/simc-benchmarks/apl-rankings/',
                '/portal/api/simc-benchmarks/spec-rankings/',
            })
            self.assertEqual(params['panel'], [self.panel.slug])
            self.assertEqual(params['scenario'], [self.scenario.key])
            self.assertNotIn('private-overview', endpoints[module])
        self.assertEqual(parse_qs(urlsplit(endpoints['simc-apl']).query)['spec'], ['mage_fire'])

    def test_simc_dimension_controls_expose_all_enabled_profiles_and_scenarios(self):
        extra_profile = SimcProfile.objects.create(
            user_id=1, name='Fire alt', class_name='mage', spec='mage_fire',
        )
        SimcBenchmarkProfile.objects.create(
            panel_spec=self.panel.specs.get(spec_key='mage_fire'),
            profile=extra_profile, label='Fire alternate', display_order=2,
        )
        extra_scenario = SimcBenchmarkScenario.objects.create(
            panel=self.panel, key='cleave', name='Cleave',
            simulation_params={'desired_targets': 3}, is_enabled=True, display_order=2,
        )
        response = self.client.get('/portal/spec/Mage/Fire/')
        soup = BeautifulSoup(response.content, 'html.parser')
        root = soup.select_one('#spec-overview')
        self.assertEqual(root.get('data-simc-panel'), self.panel.slug)
        self.assertEqual(root.get('data-simc-spec'), 'mage_fire')
        self.assertEqual(root.get('data-simc-default-scenario'), self.scenario.key)
        self.assertIn(extra_scenario.key, root.get('data-simc-scenarios'))
        self.assertIn(str(extra_profile.pk), root.get('data-simc-profiles'))
        self.assertEqual(root.get('data-simc-default-profile'), str(self.panel.specs.get(spec_key='mage_fire').profiles.order_by('display_order', 'id').first().profile_id))
        catalog = json.loads(soup.select_one('#spec-overview-scenarios').string)
        self.assertEqual(catalog[0]['key'], self.scenario.key)

    def test_simc_dimension_controls_are_hidden_when_no_profile_is_enabled(self):
        panel_spec = self.panel.specs.get(spec_key='mage_fire')
        panel_spec.profiles.update(is_enabled=False)

        response = self.client.get('/portal/spec/Mage/Fire/')

        soup = BeautifulSoup(response.content, 'html.parser')
        root = soup.select_one('#spec-overview')
        self.assertNotIn('data-simc-panel', root.attrs)
        self.assertIsNone(soup.select_one('#spec-overview-scenario'))

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
        self.assertEqual(len(cards), 5)
        for card in cards:
            self.assertIsNotNone(card.select_one('[data-module-state][role="status"]'))
            self.assertIsNotNone(card.select_one('[data-module-content]'))

    def test_loader_is_failure_isolated_and_uses_backend_audit_field(self):
        js = (Path(__file__).resolve().parents[2] / 'static/portal/js/spec-overview.js').read_text()
        self.assertIn('cards.forEach(loadModule)', js)
        self.assertNotIn('Promise.all', js)
        self.assertIn('"apl_label"', js)
        self.assertIn('payload?.status === "not_ready"', js)
        self.assertIn('description.detail_url', js)
        self.assertIn('AbortController', js)
        self.assertIn('signal: controller.signal', js)

    def test_simc_loader_explains_not_ready_reason_and_renders_frozen_identity(self):
        js = (Path(__file__).resolve().parents[2] / 'static/portal/js/spec-overview.js').read_text()
        self.assertIn('incomplete_frozen_identity', js)
        self.assertIn('no_comparable_baseline_results', js)
        self.assertIn('dimension_not_configured', js)
        self.assertIn('resource_versions', js)
        self.assertIn('source_result_id', js)

    def test_overview_surfaces_decision_context_without_cross_source_score(self):
        response = self.client.get('/portal/spec/Mage/Fire/')
        soup = BeautifulSoup(response.content, 'html.parser')
        js = (Path(__file__).resolve().parents[2] / 'static/portal/js/spec-overview.js').read_text()

        self.assertIsNotNone(soup.select_one('#module-simc-apl [data-simc-context]'))
        self.assertIsNotNone(soup.select_one('#module-simc-cross-spec [data-simc-context]'))
        self.assertIn('中位 DPS', js)
        self.assertIn('M+ 评分', js)
        self.assertIn('样本有限', js)
        self.assertIn('dungeon_id', js)
        self.assertIn('boss_id', js)
        self.assertIn('跨专精使用各专精配置的标准 Profile', js)
