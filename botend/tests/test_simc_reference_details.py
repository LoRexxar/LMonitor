"""组合资料提示及共享资料访问边界回归。"""
from urllib.parse import urlencode

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import DashboardUserGroup, SimcReferenceSimulation, SimcReferenceSimulationConfig, SimcApl
from botend.tests import test_simc_reference_simulations as fixtures


class ReferenceDetailsTests(TestCase):
    setUp = fixtures.ReferenceSimulationTests.setUp
    enqueue = fixtures.ReferenceSimulationTests.enqueue

    def detail_url(self, kind, **overrides):
        query = {'config': self.config.pk, 'page': 2, 'profile': self.profile.pk,
                 'talent': self.talent.pk, 'apl': self.apl.pk}
        query.update(overrides)
        return reverse('simc_reference_resource_preview', kwargs={
            'spec_key': self.config.spec_key, 'kind': kind, 'resource_id': getattr(self, kind).pk,
        }) + '?' + urlencode(query)

    def test_table_previews_are_lazy_and_do_not_open_detail_pages(self):
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        links = response.context['scheme_page'][0]['preview_links']
        self.assertEqual(set(links), {'profile', 'talent', 'apl'})
        self.assertContains(response, 'data-wow-item-tooltip-url=', count=3)
        self.assertNotContains(response, '/resources/')
        for kind in links:
            response = self.client.get(self.detail_url(kind))
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()), {'name', 'description', 'talent_render_model'} if kind == 'talent' else {'name', 'description'})
            self.assertIn('当前资源版本', response.json()['description'])
            self.assertEqual(response['Cache-Control'], 'private, no-store')
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)
        self.assertEqual(self.client.get(
            f'/dashboard/frontend-simulations/results/warrior_fury/resources/profile/{self.profile.pk}/'
        ).status_code, 404)

    def test_requires_independent_management_permission(self):
        url = self.detail_url('apl')
        self.assertEqual(self.client.get(url).status_code, 403)
        user = User.objects.create_user('资料管理员')
        self.client.force_login(user)
        self.assertEqual(self.client.get(url).status_code, 403)
        group = DashboardUserGroup.objects.create(name='资料权限', permission_codes=['frontend.simulations'])
        group.users.add(user)
        self.assertEqual(self.client.get(url).status_code, 200)

    def test_rejects_mismatched_ids_and_invalid_query(self):
        self.client.force_login(self.user)
        other = SimcApl.objects.create(name='未授权优先级', spec='warrior_fury', is_system=True, source='simc_upstream')
        for query in ({'apl': other.pk}, {'config': 'bad'}, {'talent': '1e2'}, {'profile': 0}):
            self.assertEqual(self.client.get(self.detail_url('apl', **query)).status_code, 404)
        url = self.detail_url('profile').replace('/warrior_fury/', '/mage_frost/')
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_unpublished_without_history_is_inaccessible(self):
        self.client.force_login(self.user)
        self.publication.profiles.clear()
        self.assertEqual(self.client.get(self.detail_url('profile')).status_code, 404)

    def test_historical_global_resources_remain_readable_but_private_or_changed_spec_do_not(self):
        self.enqueue()
        self.publication.profiles.clear()
        self.client.force_login(self.user)
        response = self.client.get(self.detail_url('profile'))
        self.assertIn('已不在当前公开范围', response.json()['description'])
        self.profile.user_id = self.user.pk
        self.profile.save(update_fields=['user_id'])
        self.assertEqual(self.client.get(self.detail_url('profile')).status_code, 404)
        self.apl.spec = 'mage_frost'
        self.apl.class_name = 'mage'
        self.apl.save(update_fields=['spec', 'class_name'])
        self.assertEqual(self.client.get(self.detail_url('apl')).status_code, 404)
        self.talent.owner_user_id = self.user.pk
        self.talent.save(update_fields=['owner_user_id'])
        self.assertEqual(self.client.get(self.detail_url('talent')).status_code, 404)

    def test_missing_default_apl_still_allows_gear_and_talent_details(self):
        self.publication.apls.clear()
        self.client.force_login(self.user)
        response = self.client.get(self.detail_url('talent', apl=0))
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('apl', self.client.get(self.url).context['scheme_page'][0]['preview_links'])

    def test_equipment_code_excludes_embedded_apl_and_talent(self):
        self.profile.player_equipment += '\ntalents=不展示内嵌天赋\nactions=/不展示内嵌优先级'
        self.profile.save(update_fields=['player_equipment'])
        self.client.force_login(self.user)
        response = self.client.get(self.detail_url('profile'))
        self.assertNotContains(response, '不展示内嵌天赋')
        self.assertNotContains(response, '不展示内嵌优先级')


class ReferenceDetailsPresentationTests(TestCase):
    def test_real_display_builders_render_equipment_trees_and_readable_apl(self):
        from botend.tests.test_simc_reference_presentation import presentation_fixture
        user, version, profile, talent, apl = presentation_fixture()
        user.is_superuser = True
        user.save(update_fields=['is_superuser'])
        config = SimcReferenceSimulationConfig.objects.create(spec_key='warrior_fury')
        self.client.force_login(user)
        for kind, resource, expected in [('profile', profile, '勇士的战盔'),
                                         ('talent', talent, '职业天赋'),
                                         ('apl', apl, '暴怒')]:
            url = reverse('simc_reference_resource_preview', kwargs={
                'spec_key': config.spec_key, 'kind': kind, 'resource_id': resource.pk,
            }) + '?' + urlencode({'config': config.pk, 'profile': profile.pk,
                                   'talent': talent.pk, 'apl': apl.pk})
            response = self.client.get(url)
            if kind == 'talent':
                model = response.json()['talent_render_model']
                self.assertEqual({t['tree_type'] for t in model['trees']}, {'class', 'spec', 'hero'})
                self.assertTrue(any(n['selected'] for t in model['trees'] for n in t['nodes']))
            else:
                self.assertIn(expected, response.json()['description'])
