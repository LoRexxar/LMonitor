"""Admin rankings belong to shared settings/scenarios, not an anchor spec."""
from unittest.mock import patch
from django.test import TestCase
from django.urls import reverse
from botend.models import SimcReferenceSimulationSettings, SimcReferenceSimulationScenario, SimcTask
from botend.tests import test_simc_reference_rankings as fixtures


class AdminGroupRankingTests(TestCase):
    setUp = fixtures.ReferenceRankingTests.setUp
    result = fixtures.ReferenceRankingTests.result
    other_spec = fixtures.ReferenceRankingTests.other_spec

    def populate(self):
        self.result(200)
        self.result(600, 5)
        other = self.other_spec()
        self.result(300, **other)
        self.result(800, 5, **other)
        self.client.force_login(self.user)

    def test_sidebar_entry_renders_complete_group_without_anchor_lookup(self):
        self.populate()
        with patch('botend.services.simc_reference_rankings.ranking_entry', side_effect=AssertionError('admin must not select a current spec')), patch('botend.services.simc_reference_simulations.snapshot', side_effect=AssertionError('read must remain index-only')):
            response = self.client.get(reverse('simc_reference_rankings_entry'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e['dps'] for e in response.context['entries']], [300, 200])
        self.assertFalse(any(e['is_current'] for e in response.context['entries']))
        self.assertEqual(response.context['scope_options'], [])
        self.assertIsNone(response.context['current'])
        for text in ('当前专精', '当前配置', '定位榜单中的当前配置', 'data-ranking-current'):
            self.assertNotContains(response, text)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_scene_links_include_all_specs_and_only_group_identifiers(self):
        self.populate()
        scene = self.config.settings.scenarios.get(simulation_params__desired_targets=5)
        response = self.client.get(reverse('simc_reference_rankings_entry'), {'settings': self.config.settings_id, 'scenario': scene.pk})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([e['dps'] for e in response.context['entries']], [800, 600])
        self.assertEqual(response.context['scenario_id'], scene.pk)
        for option in response.context['scene_options']:
            self.assertIn('settings=', option['url'])
            self.assertIn('scenario=', option['url'])
            self.assertNotIn(self.config.spec_key, option['url'])
            self.assertNotIn('/spec/', option['url'])

    def test_old_spec_link_redirects_to_group_and_preserves_scene(self):
        self.populate()
        row = self.result(650, 5)
        old = reverse('simc_reference_rankings', args=[self.config.pk, row.signature, 'spec'])
        response = self.client.get(old, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.redirect_chain)
        self.assertIn('settings=', response.redirect_chain[-1][0])
        self.assertIn('scenario=', response.redirect_chain[-1][0])
        self.assertEqual([e['dps'] for e in response.context['entries']], [800, 650])
        self.assertNotContains(response, '当前专精')

    def test_foreign_or_disabled_scenario_is_not_a_group_member(self):
        self.populate()
        other, _ = SimcReferenceSimulationSettings.objects.get_or_create(use_ptr=True)
        scene = SimcReferenceSimulationScenario.objects.create(settings=other, name='foreign', simulation_params={'desired_targets':1,'max_time':300,'iterations':10000})
        url = reverse('simc_reference_rankings_entry')
        self.assertEqual(self.client.get(url, {'settings':self.config.settings_id,'scenario':scene.pk}).status_code, 404)
        scene = self.config.settings.scenarios.first()
        scene.is_enabled = False
        scene.save(update_fields=['is_enabled'])
        self.assertEqual(self.client.get(url, {'settings':self.config.settings_id,'scenario':scene.pk}).status_code, 404)
        self.assertEqual(self.client.get(url, {'settings':'bad'}).status_code, 404)

    def test_empty_scene_remains_selectable_without_any_anchor_result(self):
        from django.utils.html import escape
        self.result(600, 5)
        alternate, _ = SimcReferenceSimulationSettings.objects.get_or_create(use_ptr=True)
        alternate.backend, alternate.template, alternate.is_enabled = self.backend, self.template, True
        alternate.save()
        self.client.force_login(self.user)
        response = self.client.get(reverse('simc_reference_rankings_entry'))
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context['empty_ranking'])
        self.assertEqual(len(response.context['scene_options']), 2)
        self.assertContains(response, 'aria-label="模拟场景"')
        self.assertContains(response, 'aria-label="模拟配置"')
        for option in response.context['settings_options']:
            self.assertContains(response, 'href="'+escape(option['url'])+'"')
        aoe = self.config.settings.scenarios.get(simulation_params__desired_targets=5)
        option = next(item for item in response.context['scene_options'] if item['scenario_id'] == aoe.pk)
        self.assertContains(response, 'href="'+escape(option['url'])+'"')
        response = self.client.get(option['url'])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context['entries']), 1)

    def test_portal_keeps_its_existing_spec_context(self):
        row = self.result(200)
        self.client.force_login(self.user)
        response = self.client.get(reverse('portal_simc_reference_rankings', args=[self.config.pk,row.signature,'spec']))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '当前配置')
        self.assertContains(response, '当前专精')
