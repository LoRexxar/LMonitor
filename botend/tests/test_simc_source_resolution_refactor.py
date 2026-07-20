import json
import unittest
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import Client, TestCase


ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
MAIN = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
WORKFLOW = HTML[HTML.index('id="simc-workbench-import-panel"'):HTML.index('<!-- End L1 Panel: 模拟工作流 -->')]


class SimcSourceResolutionApiTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='source_resolution', password='pwd')
        self.client = Client()
        self.client.force_login(self.user)

    def test_battlenet_preflight_returns_valid_canonical_spec_without_requested_spec(self):
        fetched = {
            'identity': {'name': 'Tester', 'realm': 'Kazzak', 'region': 'eu', 'class_name': 'warrior'},
            'spec': {'key': 'fury', 'name': 'Fury'},
            'equipment': {'count': 15}, 'stats': {}, 'simc_ready': True, 'warnings': [],
        }
        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight', return_value=fetched) as fetch:
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'eu', 'realm': 'Kazzak', 'character': 'Tester',
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['canonical_spec'], 'warrior_fury')
        fetch.assert_called_once_with(region='eu', realm='Kazzak', character='Tester', requested_spec='')

    def test_battlenet_preflight_uses_class_to_disambiguate_shared_spec_names(self):
        fetched = {
            'identity': {'name': 'FrostTester', 'realm': 'Kazzak', 'region': 'eu', 'class_name': 'deathknight'},
            'spec': {'key': 'frost', 'name': 'Frost'},
            'equipment': {'count': 15}, 'stats': {}, 'simc_ready': True, 'warnings': [],
        }
        with patch('botend.services.battlenet_preflight.fetch_battlenet_character_preflight', return_value=fetched):
            response = self.client.post('/api/simc-battlenet-preflight/', data=json.dumps({
                'region': 'eu', 'realm': 'Kazzak', 'character': 'FrostTester',
            }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['canonical_spec'], 'deathknight_frost')

    def test_addon_preflight_infers_canonical_spec_and_returns_full_detail(self):
        addon = '\n'.join([
            'warrior="AddonTester"', 'level=90', 'race=orc', 'region=eu', 'server=Kazzak',
            'spec=fury', 'talents=BUILD', 'head=,id=111,ilevel=639',
            'main_hand=,id=222,ilevel=646', 'crit_rating=1000',
        ])
        response = self.client.post('/api/simc-player-config-detail/', data=json.dumps({
            'player_config_mode': 'simc_addon', 'simc_code': addon,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload['success'], payload)
        self.assertEqual(payload['canonical_spec'], 'warrior_fury')
        self.assertEqual(payload['data']['identity']['name'], 'AddonTester')
        self.assertEqual(payload['data']['identity']['class_name'], 'warrior')
        self.assertEqual(payload['data']['identity']['spec'], 'fury')
        self.assertEqual(payload['data']['equipment'][0]['id'], 111)
        self.assertEqual(payload['data']['talents']['build_code'], 'BUILD')
        self.assertIn('comparison_candidates', payload['data'])

    def test_addon_preflight_uses_actor_to_disambiguate_shared_spec_names(self):
        addon = '\n'.join([
            'shaman="RestoTester"', 'level=90', 'spec=restoration',
            'head=,id=111,ilevel=639', 'main_hand=,id=222,ilevel=646',
        ])
        response = self.client.post('/api/simc-player-config-detail/', data=json.dumps({
            'player_config_mode': 'simc_addon', 'simc_code': addon,
        }), content_type='application/json')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['canonical_spec'], 'shaman_restoration')

    def test_addon_preflight_rejects_unrecognized_or_illegal_spec(self):
        cases = [
            'warrior="NoSpec"\nlevel=90\nhead=,id=1\nmain_hand=,id=2',
            'warrior="WrongSpec"\nlevel=90\nspec=restoration\nhead=,id=1\nmain_hand=,id=2',
        ]
        for addon in cases:
            with self.subTest(addon=addon):
                response = self.client.post('/api/simc-player-config-detail/', data=json.dumps({
                    'player_config_mode': 'simc_addon', 'simc_code': addon,
                }), content_type='application/json')
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.json()['success'])


class SimcSourceResolutionFrontendContractTests(unittest.TestCase):
    def test_exactly_three_peer_sources_and_spec_is_scoped_to_specified_panel(self):
        source_panel = WORKFLOW[WORKFLOW.index('id="simc-sim-player-sources"'):WORKFLOW.index('id="simc-sim-apl-list"')]
        self.assertEqual(source_panel.count('data-simc-player-source='), 3)
        for source in ('battlenet', 'simc_addon', 'specified_spec'):
            self.assertEqual(source_panel.count(f'data-simc-player-source="{source}"'), 1)
        prefix = WORKFLOW[:WORKFLOW.index('id="simc-sim-player-sources"')]
        self.assertNotIn('id="simc-sim-spec"', prefix)
        specified = source_panel[source_panel.index('id="simc-sim-source-specified-spec"'):]
        self.assertEqual(specified.count('id="simc-sim-spec"'), 1)
        self.assertIn('id="simc-sim-profile-select"', specified)

    def test_source_resolution_drives_resources_cancellation_and_all_submit_specs(self):
        for token in (
            'simcResolvedCanonicalSpec', 'resolveSimcPlayerSource', 'canonical_spec',
            "'/api/simc-battlenet-preflight/'", "'/api/simc-player-config-detail/'",
            'simcSourceResolutionAbortController', 'new AbortController()', '.abort()',
            'clearSimcResolvedResources', 'loadSimcAplCandidates(canonicalSpec',
        ):
            self.assertIn(token, MAIN)
        resolution = MAIN.split('async function resolveSimcPlayerSource', 1)[1].split('\nasync function', 1)[0]
        self.assertIn("type === 'specified_spec'", resolution)
        self.assertIn('loadSimcSimProfileSelect', resolution)
        self.assertIn("type !== 'specified_spec'", resolution)
        self.assertIn("renderSimcSavedProfileDetail", resolution)
        detail_renderer = MAIN.split('function renderSimcSavedProfileDetail', 1)[1].split('\nasync function refreshSavedSimcPlayerDetail', 1)[0]
        self.assertIn('Array.isArray(detail.equipment)', detail_renderer)
        self.assertIn('equipmentSummary.count', detail_renderer)
        self.assertIn('equipmentSummary.item_level', detail_renderer)
        for function_name in ('createSimcSimulationTask', 'simcAttributeSearchRequestBody', 'startSelectedSimcCandidateComparisons'):
            body = MAIN.split(f'function {function_name}', 1)[-1] if function_name == 'simcAttributeSearchRequestBody' else MAIN.split(f'async function {function_name}', 1)[-1]
            body = body.split('\n}', 1)[0]
            self.assertIn('spec:', body)
            self.assertIn('simcResolvedCanonicalSpec', body)

    def test_battlenet_preflight_shows_loading_success_and_failure_states(self):
        source_panel = WORKFLOW[WORKFLOW.index('id="simc-sim-source-battlenet"'):WORKFLOW.index('id="simc-sim-source-addon"')]
        self.assertIn('id="simc-sim-bnet-load-status"', source_panel)
        self.assertIn('aria-live="polite"', source_panel)
        self.assertIn('function renderSimcBattlenetLoadState', MAIN)
        resolution = MAIN.split('async function resolveSimcPlayerSource', 1)[1].split('\nasync function', 1)[0]
        self.assertIn("renderSimcBattlenetLoadState('loading'", resolution)
        self.assertIn("renderSimcBattlenetLoadState('success'", resolution)
        self.assertIn("renderSimcBattlenetLoadState('error'", resolution)
        self.assertIn('正在从 Battle.net 加载角色信息', MAIN)
        self.assertIn('Battle.net 角色信息加载失败', MAIN)

    def test_dashboard_initialization_does_not_validate_unopened_battlenet_source(self):
        switch = MAIN.split('function switchSimcPlayerImportMode', 1)[1].split('\n}', 1)[0]
        binding = MAIN.split('function bindSimcWorkbenchSimulationControls', 1)[1].split('\n}', 1)[0]
        navigation = MAIN.split('function initNavigation', 1)[1].split('\n    // 处理子菜单项点击', 1)[0]
        self.assertIn('resolve = true', switch)
        self.assertIn('if (!resolve) return;', switch)
        self.assertIn('switchSimcPlayerImportMode({ resolve: false });', binding)
        self.assertNotIn('\n    onSimcTargetSpecChange().catch', binding)
        self.assertIn("if (sectionId === 'simc-workbench')", navigation)
        self.assertIn('switchSimcPlayerImportMode();', navigation)
        self.assertIn("dashboard/js/main.js' %}?v=20260720i", HTML)
