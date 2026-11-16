"""章节按需计算、局部响应及资源权限回归。"""
from unittest.mock import patch
from django.test import TestCase
from botend.tests import test_portal_simc_reference as fixtures


class ReferencePartialTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        fixtures.PortalSimcReferenceTests.setUpTestData.__func__(cls)

    setUp = fixtures.PortalSimcReferenceTests.setUp

    def test_only_selected_section_builds_its_expensive_projection(self):
        targets = {'reference-talents': 'reference_trees', 'reference-equipment': 'build_player_config_detail',
                   'reference-apl': 'readable_apl'}
        for section, expected in targets.items():
            with self.subTest(section=section), patch('botend.services.simc_reference.reference_trees', return_value={'trees': []}) as tree, \
                    patch('botend.services.simc_reference.build_player_config_detail', return_value={}) as equipment, \
                    patch('botend.services.simc_reference.readable_apl', return_value={'groups': []}) as apl:
                response = self.client.get(self.url, {'spec': 'warrior_fury', 'view': section})
                self.assertEqual(response.status_code, 200)
                for name, mock in [('reference_trees', tree), ('build_player_config_detail', equipment), ('readable_apl', apl)]:
                    self.assertEqual(mock.call_count, int(name == expected))
                self.assertContains(response, 'data-loaded="true"', count=1)
                self.assertContains(response, 'data-loaded="false"', count=2)

    def test_fragment_is_private_and_uses_canonical_resource_selection(self):
        response = self.client.get(self.url, {'spec': 'warrior_fury', 'view': 'reference-equipment'},
                                   HTTP_X_SIMC_REFERENCE='workspace')
        data = response.json()
        self.assertEqual(data['section'], 'reference-equipment')
        self.assertIn('测试头盔', data['html'])
        self.assertNotIn('<!DOCTYPE', data['html'])
        self.assertNotIn('<script', data['html'])
        self.assertIn(f'talent={self.talent.pk}', data['url'])
        self.assertIn(f'apl={self.apl.pk}', data['url'])
        self.assertIn('no-store', response['Cache-Control'])
        self.assertIn('Cookie', response['Vary'])
        self.assertIn('X-Simc-Reference', response['Vary'])
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.client.logout()
        self.assertEqual(self.client.get(self.url, HTTP_X_SIMC_REFERENCE='workspace').status_code, 302)

    def test_fragment_cannot_read_unpublished_or_cross_spec_inputs(self):
        for params in ({'spec': 'mage_fire', 'talent': self.talent.pk}, {'view': 'invalid'}):
            self.assertEqual(self.client.get(self.url, params, HTTP_X_SIMC_REFERENCE='workspace').status_code, 404)
        self.publication.talents.remove(self.talent)
        self.assertEqual(self.client.get(self.url, {'spec': 'warrior_fury', 'talent': self.talent.pk},
                                        HTTP_X_SIMC_REFERENCE='workspace').status_code, 404)

    def test_hero_selection_and_fallback_links_preserve_current_section(self):
        response = self.client.get(self.url, {'spec': 'warrior_fury', 'hero': '61', 'view': 'reference-apl'},
                                   HTTP_X_SIMC_REFERENCE='workspace')
        data = response.json()
        self.assertIn('hero=61', data['url'])
        self.assertIn('view=reference-apl', data['url'])
        self.assertIn('name="view" value="reference-apl"', data['html'])
        self.assertIn('view=reference-talents', data['html'])
