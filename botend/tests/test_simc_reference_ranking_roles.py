"""Role-local ranking projection through real indexes and HTTP consumers."""
from contextlib import ExitStack
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from botend.constants.wow import specialization_catalog
from botend.models import SimcApl, SimcProfile, SimcTalentString, SimcReferenceSimulationConfig, SimcReferenceSimulation, SimcTask
from botend.services.simc_reference_rankings import current_rankings, leaderboard
from botend.tests import test_simc_reference_rankings as ranking_tests
from botend.tests.simc_reference_test_fixtures import publish_resources


class ReferenceRankingRoleTests(TestCase):
    setUp = ranking_tests.ReferenceRankingTests.setUp
    result = ranking_tests.ReferenceRankingTests.result
    other_spec = ranking_tests.ReferenceRankingTests.other_spec

    def tank(self):
        key = 'warrior_protection'
        apl = SimcApl.objects.create(name='坦克APL', spec=key, class_name='warrior', is_system=True,
                                    is_selectable=True, content='actions=/auto_attack', source='user')
        talent = SimcTalentString.objects.create(name='坦克天赋', spec=key, talent='demo', is_system=True, default_apl=apl)
        profile = SimcProfile.objects.create(name='坦克装备', spec=key, class_name='warrior', player_config_mode='manual_equipment')
        config = SimcReferenceSimulationConfig.objects.create(spec_key=key, settings_id=self.config.settings_id)
        publish_resources(key, profiles=[profile], talents=[talent], apls=[apl])
        return dict(config=config, profile=profile, talent=talent, apl=apl)

    def ranking_url(self, row, scope='overall'):
        return reverse('portal_simc_reference_rankings', args=[row.config_id, row.signature, scope])

    def test_role_local_summary_and_authoritative_icons_for_two_scenes(self):
        tank, mage = self.tank(), self.other_spec()
        anchors = [self.result(200), self.result(600, 5)]
        for targets in (1, 5):
            self.result(9000, targets, **tank)
            self.result(100, targets, **mage)
        expected = {item['spec_name']: item for item in specialization_catalog() if item['class_name'] == 'Warrior'}
        with ExitStack() as stack:
            for name in ('snapshot', 'catalog'):
                stack.enter_context(patch('botend.services.simc_reference_simulations.' + name, side_effect=AssertionError('read path rebuilt resources')))
            stack.enter_context(patch('botend.services.simc_reference.reference_catalog', side_effect=AssertionError('heavy catalog')))
            ranks = current_rankings(self.config)
            for anchor in anchors:
                self.assertEqual(ranks[(anchor.config_id, anchor.signature)],
                                 dict(overall_rank=1, overall_total=2, spec_rank=1, spec_total=1))
                board = leaderboard(self.config, anchor.signature, 'overall')
                self.assertEqual([entry['rank'] for entry in board['entries']], [1, 1, 2])
                self.assertEqual([entry['role'] for entry in board['entries']], ['tank', 'dps', 'dps'])
                self.assertEqual(board['entries'][0]['icon'], expected['Protection']['icon'])
                self.assertEqual(board['current']['icon'], expected['Fury']['icon'])
                self.assertEqual(board['current']['role_label'], '输出')
        from botend.services.simc_reference_rankings import apply_ranking_role
        with self.assertNumQueries(0):
            apply_ranking_role(board, 'tank', self.ranking_url(anchors[-1]))
        self.assertEqual([entry['role'] for entry in board['entries']], ['tank'])
        self.assertEqual(board['current']['role'], 'dps')
        self.assertFalse(board['current_in_ranking'])

    def test_portal_and_admin_switches_preserve_identity_and_are_read_only(self):
        tank = self.tank()
        anchors = [self.result(200), self.result(600, 5)]
        tank_rows = [self.result(9000, **tank), self.result(10000, 5, **tank)]
        self.client.force_login(self.user)
        counts = (SimcReferenceSimulation.objects.count(), SimcTask.objects.count())
        with CaptureQueriesContext(connection) as queries, patch(
            'botend.services.simc_reference_simulations.snapshot', side_effect=AssertionError('GET rebuilt snapshot')
        ):
            default = self.client.get(self.ranking_url(tank_rows[0]))
            self.assertEqual(default.context['selected_role'], 'tank')
            self.assertTrue(default.context['current_in_ranking'])
            response = self.client.get(self.ranking_url(anchors[0]), {'role': 'tank'})
            self.assertEqual([entry['role'] for entry in response.context['entries']], ['tank'])
            self.assertEqual(response.context['current']['role'], 'dps')
            self.assertFalse(response.context['current_in_ranking'])
            for option in response.context['role_options']:
                self.assertEqual(set(option), {'label', 'url', 'selected'})
                self.assertEqual(urlsplit(option['url']).path, self.ranking_url(anchors[0]))
            for option in response.context['scope_options'] + response.context['scene_options']:
                self.assertEqual(parse_qs(urlsplit(option['url']).query)['role'], ['tank'])
                switched = self.client.get(option['url'])
                self.assertEqual(switched.status_code, 200)
                self.assertEqual(switched.context['selected_role'], 'tank')
            aoe = self.client.get(response.context['scene_options'][1]['url'])
            self.assertEqual(aoe.context['entries'][0]['dps'], 10000)
            self.assertEqual(aoe.context['targets'], 5)
            admin_url = reverse('simc_reference_rankings_entry')
            admin = self.client.get(admin_url)
            self.assertEqual(admin.context['selected_role'], 'dps')
            self.assertEqual([entry['role'] for entry in admin.context['entries']], ['dps'])
            admin_tank = self.client.get(admin_url, {'role': 'tank', 'settings': self.config.settings_id})
            self.assertEqual([entry['role'] for entry in admin_tank.context['entries']], ['tank'])
            self.assertIsNone(admin_tank.context['current'])
            for option in admin_tank.context['settings_options'] + admin_tank.context['scene_options']:
                query = parse_qs(urlsplit(option['url']).query)
                self.assertEqual(query['role'], ['tank'])
                self.assertEqual(query['settings'], [str(self.config.settings_id)])
                self.assertEqual(self.client.get(option['url']).context['selected_role'], 'tank')
            legacy = reverse('simc_reference_rankings', args=[self.config.pk, anchors[1].signature, 'spec'])
            redirected = self.client.get(legacy, {'role': 'tank'}, follow=True)
            self.assertEqual(redirected.context['selected_role'], 'tank')
            self.assertEqual(redirected.context['scenario_id'], anchors[1].scenario_id)
            entry = self.client.get(reverse('portal_simc_reference_rankings_entry'), {'role': 'tank', 'config': self.config.pk}, follow=True)
            self.assertEqual(entry.context['selected_role'], 'tank')
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')) for q in queries))
        result_page = self.client.get(reverse('simc_reference_spec_results', args=[tank['config'].spec_key]))
        self.assertEqual(result_page.status_code, 200)
        for scheme in result_page.context['scheme_page']:
            for scene in scheme['scenes']:
                self.assertEqual(scene['ranking_role'], 'tank')
                self.assertEqual(set(scene['ranking']), {'overall_rank', 'overall_total', 'spec_rank', 'spec_total'})
        from bs4 import BeautifulSoup
        links = BeautifulSoup(result_page.content, 'html.parser').select('a.simc-ranking-button')
        self.assertTrue(links)
        for link in links:
            self.assertEqual(parse_qs(urlsplit(link['href']).query)['role'], ['tank'])
            self.assertEqual(self.client.get(link['href']).context['selected_role'], 'tank')
        self.assertEqual((SimcReferenceSimulation.objects.count(), SimcTask.objects.count()), counts)

    def test_empty_roles_scenes_and_invalid_role_keep_filter_contract(self):
        anchor = self.result(200)
        self.client.force_login(self.user)
        admin_url = reverse('simc_reference_rankings_entry')
        for url in (admin_url, self.ranking_url(anchor), reverse('portal_simc_reference_rankings_entry')):
            for role in ('invalid', '', 'DPS'):
                self.assertEqual(self.client.get(url, {'role': role}).status_code, 404)
        empty_role = self.client.get(self.ranking_url(anchor), {'role': 'tank'})
        self.assertEqual(empty_role.status_code, 200)
        self.assertTrue(empty_role.context['empty_ranking'])
        self.assertEqual(empty_role.context['current']['role'], 'dps')
        empty_scene = self.config.settings.scenarios.get(simulation_params__desired_targets=5)
        response = self.client.get(admin_url, {'scenario': empty_scene.pk, 'settings': self.config.settings_id, 'role': 'tank'})
        self.assertTrue(response.context['empty_ranking'])
        self.assertEqual(response.context['selected_role'], 'tank')
        self.assertTrue(response.context['role_options'])
        self.assertTrue(all(parse_qs(urlsplit(option['url']).query)['role'] == ['tank'] for option in response.context['scene_options']))
        from botend.models import SimcReferenceRankingEntry
        SimcReferenceRankingEntry.objects.all().delete()
        empty_portal = self.client.get(reverse('portal_simc_reference_rankings_entry'), {'role': 'tank'})
        self.assertEqual(empty_portal.status_code, 200)
        self.assertTrue(empty_portal.context['empty_ranking'])
        self.assertEqual(empty_portal.context['selected_role'], 'tank')
        self.assertFalse(empty_portal.context['current_in_ranking'])

    def test_unrecognized_metadata_is_separate_and_does_not_default_to_dps(self):
        anchor = self.result(200)
        other = self.other_spec()
        unknown = self.result(9000, **other)
        from botend.models import SimcReferencePublication, SimcReferenceRankingEntry
        # Simulate an indexed historical specialization unknown to this deployment.
        SimcReferenceSimulationConfig.objects.filter(pk=other['config'].pk).update(spec_key='future_spec')
        SimcReferencePublication.objects.filter(spec_key='mage_fire').update(spec_key='future_spec')
        SimcReferenceRankingEntry.objects.filter(config_id=other['config'].pk).update(spec_key='future_spec')
        ranks = current_rankings(self.config)
        self.assertEqual(ranks[(anchor.config_id, anchor.signature)]['overall_total'], 1)
        self.assertEqual(ranks[(unknown.config_id, unknown.signature)]['overall_total'], 1)
        board = leaderboard(self.config, anchor.signature, 'overall')
        self.assertEqual(board['entries'][0]['role'], 'unknown')
        self.assertEqual(board['entries'][0]['icon'], '')
        self.client.force_login(self.user)
        response = self.client.get(self.ranking_url(anchor), {'role': 'unknown'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row['role'] for row in response.context['entries']], ['unknown'])
        self.assertEqual(response.context['role_label'], '未识别')
