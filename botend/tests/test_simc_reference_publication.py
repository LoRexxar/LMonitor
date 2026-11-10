"""前台资料显式发布、独立权限、方案聚合和报告归属回归。"""
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from botend.models import DashboardUserGroup, SimcApl, SimcProfile, SimcTalentString, SimcReferencePublication, SimcReferenceSimulation, SimcReferenceSimulationConfig, SimcReferenceSimulationSettings, SimcTaskArtifact, SimcTask
from botend.services.simc_reference import reference_catalog
from botend.tests import test_simc_reference_simulations as fixtures
from botend.tests.simc_reference_test_fixtures import scenario_settings_payload


class ReferencePublicationTests(TestCase):
    def setUp(self):
        fixtures.ReferenceSimulationTests.setUp(self)
        self.publication_url = reverse('simc_reference_publication')
    enqueue = fixtures.ReferenceSimulationTests.enqueue
    dispatch = fixtures.ReferenceSimulationTests.dispatch
    complete = fixtures.ReferenceSimulationTests.complete
    results = fixtures.ReferenceSimulationTests.results

    def login(self):
        self.client.force_login(self.user)

    def make_spec_config(self, spec_key, use_ptr=False):
        settings, _ = SimcReferenceSimulationSettings.objects.get_or_create(use_ptr=use_ptr)
        return SimcReferenceSimulationConfig.objects.create(spec_key=spec_key, use_ptr=use_ptr, settings=settings)

    def test_unrun_public_matrix_is_visible_without_creating_tasks(self):
        extra = SimcProfile.objects.create(name='第二套公开装备', spec='warrior_fury', class_name='warrior', player_config_mode='manual_equipment')
        talent = SimcTalentString.objects.create(name='第二份公开天赋', spec='warrior_fury', talent='other', is_system=True, default_apl=self.apl)
        ptr = SimcProfile.objects.create(name='PTR装备', spec='warrior_fury', class_name='warrior', use_ptr=True)
        SimcProfile.objects.create(name='未公开装备', spec='warrior_fury', class_name='warrior')
        self.publication.profiles.add(extra, ptr)
        self.publication.talents.add(talent)
        self.login()
        response = self.client.get(self.url)
        rows = response.context['scheme_page']
        self.assertEqual(rows.paginator.count, 4)
        self.assertEqual({(r['profile_id'], r['talent_id'], r['apl_id']) for r in rows}, {
            (p, t, self.apl.pk) for p in (self.profile.pk, extra.pk) for t in (self.talent.pk, talent.pk)
        })
        self.assertTrue(all(not r['record_count'] and r['can_run'] for r in rows))
        self.assertContains(response, '开始模拟', count=4)
        self.assertContains(response, '未模拟', count=8)
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_start_public_combination_is_scoped_and_deduplicated(self):
        extra = SimcProfile.objects.create(name='另一套装备', spec='warrior_fury', class_name='warrior', player_config_mode='manual_equipment')
        self.publication.profiles.add(extra)
        self.login()
        payload = {'action': 'start_scheme', 'config': self.config.pk, 'profile': self.profile.pk,
                   'talent': self.talent.pk, 'apl': self.apl.pk}
        response = self.client.post(self.url, payload)
        self.assertEqual(response.context['result']['created'], 2)
        self.assertEqual(set(SimcReferenceSimulation.objects.values_list('profile_id', flat=True)), {self.profile.pk})
        self.assertEqual(self.client.post(self.url, payload).context['result']['created'], 0)
        rows = self.client.get(self.url).context['scheme_page']
        self.assertEqual({r['profile_id']: r['record_count'] for r in rows}, {self.profile.pk: 2, extra.pk: 0})
        self.publication.profiles.remove(self.profile)
        self.assertEqual(self.client.post(self.url, payload).status_code, 404)
        self.assertEqual(SimcReferenceSimulation.objects.count(), 2)
        other_url = reverse('simc_reference_spec_results', kwargs={'spec_key': 'mage_fire'})
        other = self.make_spec_config('mage_fire')
        self.assertEqual(self.client.post(other_url, dict(payload, config=other.pk)).status_code, 404)
        self.assertEqual(self.client.post(self.publication_url, payload).status_code, 400)

    def test_no_published_apl_requires_configuration_and_rejects_start(self):
        self.login()
        self.publication.apls.clear()
        response = self.client.get(self.url)
        row = response.context['scheme_page'][0]
        self.assertFalse(row['can_run'])
        self.assertContains(response, '请在公开配置管理中选择 APL')
        self.assertNotContains(response, '开始模拟')
        payload = {'action': 'start_scheme', 'config': self.config.pk, 'profile': self.profile.pk,
                   'talent': self.talent.pk, 'apl': self.apl.pk}
        self.assertEqual(self.client.post(self.url, payload).status_code, 404)
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)

    def test_new_published_apl_keeps_old_public_history_and_adds_unrun_combination(self):
        self.enqueue()
        alternate = SimcApl.objects.create(name='新的公开APL', spec='warrior_fury', is_system=True, is_selectable=True, content='actions=/bloodthirst', source='simc_upstream')
        self.publication.apls.add(alternate)
        self.talent.default_apl = alternate
        self.talent.save()
        self.login()
        rows = list(self.client.get(self.url).context['scheme_page'])
        self.assertEqual(len(rows), 2)
        rows = {row['apl_id']: row for row in rows}
        self.assertTrue(rows[alternate.pk]['can_run'])
        self.assertEqual(rows[alternate.pk]['record_count'], 0)
        self.assertTrue(rows[self.apl.pk]['is_public'])
        self.assertTrue(rows[self.apl.pk]['can_run'])
        self.assertEqual(rows[self.apl.pk]['record_count'], 2)

    def test_public_matrix_pagination_is_stable_after_starting_a_row(self):
        for index in range(22):
            profile = SimcProfile.objects.create(name=f'额外装备{index:02}', spec='warrior_fury', class_name='warrior', player_config_mode='manual_equipment')
            self.publication.profiles.add(profile)
        self.login()
        before = self.client.get(self.url, {'page': 2}).context['scheme_page']
        ids = [r['profile_id'] for r in before]
        self.assertEqual(before.paginator.count, 23)
        self.assertEqual(len(ids), 3)
        row = before[0]
        response = self.client.post(self.url + '?page=2', {'action': 'start_scheme', 'config': self.config.pk,
            'profile': row['profile_id'], 'talent': row['talent_id'], 'apl': row['apl_id']})
        self.assertEqual([r['profile_id'] for r in response.context['scheme_page']], ids)
        self.assertEqual(response.context['result']['created'], 2)

    def test_results_keep_spec_selector_above_combinations_and_preserve_legacy_links(self):
        self.login()
        other = self.make_spec_config('mage_fire')
        root = reverse('simc_reference_results')
        response = self.client.get(root, follow=True)
        self.assertEqual(response.context['selected'], other)
        self.assertContains(response, '选择专精')
        self.assertContains(response, self.url)
        self.assertContains(response, reverse('simc_reference_spec_results', kwargs={'spec_key': 'mage_fire'}))
        self.assertNotContains(response, '保存模拟条件')
        self.assertContains(response, reverse('simc_reference_settings'))
        self.assertNotContains(response, '返回专精列表')
        self.enqueue()
        response = self.client.get(self.url)
        self.assertContains(response, 'aria-label="选择专精"')
        self.assertContains(response, '组合与状态')
        self.assertEqual(response.context['scheme_page'].paginator.count, 1)
        self.assertContains(response, '等待派发')
        self.assertContains(response, f'<option value="{self.url}" selected>')
        html = response.content.decode()
        self.assertLess(html.index('class="result-spec-selector"'), html.index('class="results-workspace"'))
        self.assertRedirects(self.client.get(root, {'config': self.config.pk}), f'{self.url}?config={self.config.pk}')
        before = SimcReferenceSimulation.objects.count()
        self.assertEqual(self.client.post(root, {'action': 'generate'}).status_code, 400)
        self.assertEqual(SimcReferenceSimulation.objects.count(), before)

    def test_spec_detail_rejects_cross_spec_reads_and_writes(self):
        self.login()
        self.enqueue()
        row = SimcReferenceSimulation.objects.first()
        other = self.make_spec_config('mage_fire')
        url = reverse('simc_reference_spec_results', kwargs={'spec_key': other.spec_key})
        response = self.client.get(url)
        self.assertEqual(response.context['selected'], other)
        self.assertEqual(response.context['scheme_page'].paginator.count, 0)
        self.assertNotContains(response, '专精目录')
        self.assertEqual(self.client.get(url, {'config': self.config.pk}).status_code, 404)
        self.assertEqual(self.client.get(url, {'scheme': row.pk}).status_code, 404)
        for action in ('save', 'generate', 'rerun', 'rerun_scheme', 'cancel'):
            with self.subTest(action=action):
                self.assertEqual(self.client.post(url, {'action': action, 'config': self.config.pk,
                    'scheme': row.pk, 'simulation': row.pk}).status_code, 404)
        for action, key in (('rerun_scheme', 'scheme'), ('cancel', 'simulation')):
            self.assertEqual(self.client.post(url, {'action': action, key: row.pk}).status_code, 404)
        row.refresh_from_db()
        self.assertEqual(row.status, 'queued')

    def test_spec_actions_are_scoped_and_keep_current_branch(self):
        self.login()
        other = self.make_spec_config('mage_fire')
        with patch('botend.dashboard.simc_reference_results.enqueue_configs', return_value={'created': 0, 'skipped': 0, 'errors': []}) as enqueue:
            self.client.post(self.url, {'action': 'generate'})
            self.assertEqual(enqueue.call_args.args[0], [self.config.pk])
            self.assertNotIn(other.pk, enqueue.call_args.args[0])
        ptr = self.make_spec_config(self.config.spec_key, use_ptr=True)
        response = self.client.get(self.url, {'config': ptr.pk})
        self.assertEqual(response.context['selected'], ptr)
        self.assertContains(response, 'aria-label="游戏分支"')
        self.enqueue()
        row = SimcReferenceSimulation.objects.first()
        response = self.client.post(self.url, {'action': 'cancel', 'simulation': row.pk})
        self.assertRedirects(response, f'{self.url}?config={self.config.pk}&scheme={row.pk}')
        row.refresh_from_db()
        self.assertEqual(row.status, 'cancelled')

    def test_two_pages_have_separate_content_and_actions(self):
        self.login()
        publication = self.client.get(self.publication_url, {'tab': 'results'})
        self.assertContains(publication, '保存公开范围')
        self.assertNotContains(publication, '保存模拟条件')
        self.assertNotContains(publication, '补齐已公开方案结果')
        self.assertEqual(publication.context['dashboard_default_section'], 'frontend-publication')
        self.assertTemplateUsed(publication, 'dashboard/simc_reference_publication.html')
        results = self.client.get(self.url, {'tab': 'publication'})
        self.assertNotContains(results, '保存模拟条件')
        self.assertNotContains(results, 'name="action" value="publish"')
        self.assertNotContains(results, '同步可用资料目录')
        self.assertEqual(results.context['dashboard_default_section'], 'frontend-results')
        self.assertTemplateUsed(results, 'dashboard/simc_reference_results.html')
        for url, action in ((self.url, 'publish'), (self.publication_url, 'generate'),
                            (self.url, 'sync'), (self.publication_url, 'save')):
            with self.subTest(action=action):
                self.assertEqual(self.client.post(url, {'action': action, 'config': self.config.pk}).status_code, 400)
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)

    def test_publication_selects_spec_before_editing_without_a_directory(self):
        from bs4 import BeautifulSoup
        fire_profile = SimcProfile.objects.create(name='火焰装备', spec='mage_fire', class_name='mage')
        fire = self.make_spec_config('mage_fire')
        ptr = self.make_spec_config(self.config.spec_key, use_ptr=True)
        self.login()
        response = self.client.get(self.publication_url, {'config': fire.pk})
        soup = BeautifulSoup(response.content, 'html.parser')
        selector = soup.select_one('#publication-spec-select')
        self.assertIsNotNone(selector)
        self.assertEqual(len(selector.select('option')), 2)
        self.assertEqual(len(selector.select('optgroup')), 2)
        self.assertEqual(selector.select_one('option[selected]')['value'], f'{self.publication_url}?config={fire.pk}')
        self.assertIsNone(soup.select_one('aside[aria-label="专精目录"]'))
        form = soup.select_one('.publication-form')
        self.assertEqual(form.select_one('input[name=config]')['value'], str(fire.pk))
        self.assertEqual([node['value'] for node in form.select('input[name=profiles]')], [str(fire_profile.pk)])
        ptr_response = self.client.get(self.publication_url, {'config': ptr.pk})
        ptr_soup = BeautifulSoup(ptr_response.content, 'html.parser')
        self.assertEqual(len(ptr_soup.select('#publication-spec-select option')), 2)
        self.assertEqual(ptr_soup.select_one('#publication-spec-select option[selected]')['value'],
                         f'{self.publication_url}?config={ptr.pk}')
        saved = self.client.post(self.publication_url, {'action': 'publish', 'config': self.config.pk,
            'is_published': 'on', 'profiles': [self.profile.pk], 'talents': [self.talent.pk], 'apls': [self.apl.pk]}, follow=True)
        saved_soup = BeautifulSoup(saved.content, 'html.parser')
        self.assertEqual(saved_soup.select_one('#publication-spec-select option[selected]')['value'],
                         f'{self.publication_url}?config={self.config.pk}')
        self.assertEqual([node['value'] for node in saved_soup.select('.publication-form input[name=profiles]:checked')],
                         [str(self.profile.pk)])
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)
        self.assertEqual(SimcTask.objects.count(), 0)

    def test_admin_navigation_uses_sidebar_and_publication_controls_share_header(self):
        from bs4 import BeautifulSoup
        self.login()
        publication = self.client.get(self.publication_url)
        soup = BeautifulSoup(publication.content, 'html.parser')
        controls = soup.select_one('.publication-heading .publication-controls')
        self.assertIsNotNone(controls)
        self.assertIsNotNone(controls.select_one('#publication-spec-select'))
        self.assertIsNotNone(controls.select_one('button[name=action][value=sync]'))
        self.assertEqual(len(soup.select('button[name=action][value=sync]')), 1)
        ranking_url = reverse('simc_reference_rankings_entry')
        for url in (self.publication_url, self.url, ranking_url):
            response = self.client.get(url)
            document = BeautifulSoup(response.content, 'html.parser')
            self.assertIsNone(document.select_one('nav[aria-label="模拟资料页面导航"]'))
            menu = document.select_one('#frontend-simulation-sidebar-submenu')
            self.assertEqual({a['href'] for a in menu.select('a')},
                             {self.publication_url, reverse('simc_reference_settings'), reverse('simc_reference_results'), ranking_url})
        ranking = self.client.get(ranking_url)
        self.assertEqual(ranking.context['dashboard_default_section'], 'frontend-rankings')
        from botend.dashboard.permissions import SECTION_PERMISSION_CODES
        self.assertEqual(SECTION_PERMISSION_CODES['frontend-rankings'], 'frontend.simulations')
        portal = self.client.get(reverse('portal_simc_reference'), {'spec': self.config.spec_key})
        self.assertIsNotNone(BeautifulSoup(portal.content, 'html.parser').select_one('nav[aria-label="模拟资料页面导航"]'))
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)

    def test_global_settings_page_updates_all_specs_without_enqueuing(self):
        self.login()
        other = self.make_spec_config('mage_fire')
        url = reverse('simc_reference_settings')
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['dashboard_default_section'], 'frontend-settings')
        self.assertContains(response, '保存模拟配置')
        self.assertNotContains(response, 'id="result-spec-select"')
        self.assertNotContains(self.client.get(self.url), '保存模拟条件')
        payload = scenario_settings_payload(self.config.settings, self.backend, self.template,
                                            [(1, 240, 2000), (3, 240, 2000), (5, 240, 2000)])
        saved = self.client.post(url, payload, follow=True)
        self.assertEqual(saved.status_code, 200)
        self.config.refresh_from_db()
        other.refresh_from_db()
        self.assertEqual(self.config.settings_id, other.settings_id)
        self.assertEqual(list(self.config.settings.scenarios.values_list('simulation_params__max_time', flat=True)), [240] * 3)
        self.assertEqual([s.simulation_params['desired_targets'] for s in
                          other.settings.scenarios.order_by('display_order', 'pk')], [1, 3, 5])
        self.assertEqual(self.client.post(self.url, dict(payload, config=self.config.pk)).status_code, 400)
        invalid_payload = scenario_settings_payload(self.config.settings, self.backend, self.template,
                                                    [(1, 240, 2000), (0, 240, 2000), (5, 240, 2000)])
        invalid = self.client.post(url, invalid_payload)
        self.assertEqual(invalid.status_code, 400)
        self.assertTrue(invalid.context['scenario_formset'].forms[1].errors)
        self.assertContains(invalid, 'name="scenarios-1-desired_targets"', status_code=400)
        self.config.refresh_from_db()
        self.assertEqual([s.simulation_params['desired_targets'] for s in
                          self.config.settings.scenarios.order_by('display_order', 'pk')], [1, 3, 5])
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)
        self.assertEqual(SimcTask.objects.count(), 0)
        self.client.force_login(User.objects.create_user('无模拟配置权限'))
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_old_entry_redirects_to_the_separate_page(self):
        self.login()
        entry = reverse('simc_reference_entry')
        self.assertRedirects(self.client.get(entry), self.publication_url)
        self.assertRedirects(self.client.get(entry, {'tab': 'results', 'config': self.config.pk}),
                             f"{reverse('simc_reference_results')}?config={self.config.pk}", fetch_redirect_response=False)

    def test_shared_resources_are_not_implicitly_published(self):
        self.publication.is_published = False
        self.publication.save()
        self.assertFalse(reference_catalog()[1]['warrior_fury']['has_data'])
        self.assertTrue(reference_catalog(public_only=False)[1]['warrior_fury']['has_data'])
        self.login()
        response = self.client.get(reverse('portal_simc_reference'), {'profile': self.profile.pk})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.enqueue()['created'], 0)

    def test_new_resources_require_separate_opt_in(self):
        extra = SimcProfile.objects.create(name='尚未指定的装备', spec='warrior_fury', class_name='warrior')
        self.login()
        self.assertEqual(self.client.get(reverse('portal_simc_reference'), {'profile': extra.pk}).status_code, 404)
        self.assertEqual(self.enqueue()['created'], 2)

    def test_removing_talent_and_apl_cannot_leak_from_equipment_text(self):
        self.publication.talents.clear()
        self.publication.apls.clear()
        SimcProfile.objects.filter(pk=self.profile.pk).update(
            talent='未授权天赋原文', player_equipment='warrior="测试"\nspec=fury\ntalents=未授权天赋原文\nactions=/未授权优先级\nhead=,id=123',
        )
        self.login()
        response = self.client.get(reverse('portal_simc_reference'), {'spec': 'warrior_fury'})
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, '未授权天赋原文')
        self.assertNotContains(response, '未授权优先级')
        self.assertEqual(response.context['talent_code'], '')

    def test_publication_removal_hides_results_and_stops_queued_jobs(self):
        self.enqueue()
        self.publication.apls.clear()
        self.dispatch()
        self.assertEqual(SimcTask.objects.count(), 0)
        self.assertEqual(SimcReferenceSimulation.objects.filter(status='failed').count(), 2)
        self.assertEqual(self.results(), [])

    def test_publish_form_rejects_cross_spec_and_private_resources(self):
        private = SimcProfile.objects.create(name='私有', spec='warrior_fury', user_id=self.user.pk)
        wrong = SimcApl.objects.create(name='错误专精', spec='mage_fire', is_system=True, is_selectable=True)
        self.login()
        response = self.client.post(self.publication_url + f'?config={self.config.pk}', {
            'action': 'publish', 'config': self.config.pk, 'is_published': 'on',
            'profiles': [private.pk], 'talents': [self.talent.pk], 'apls': [wrong.pk],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(list(self.publication.profiles.all()), [self.profile])

    def test_publish_form_saves_exact_selection_and_can_clear_it(self):
        self.login()
        response = self.client.post(self.publication_url, {'action': 'publish', 'config': self.config.pk,
                                              'is_published': 'on', 'profiles': [self.profile.pk]})
        self.assertEqual(response.status_code, 302)
        self.assertFalse(self.publication.talents.exists())
        self.assertFalse(self.publication.apls.exists())
        self.assertEqual(len(reference_catalog()[1]['warrior_fury']['profiles']), 1)
        self.client.post(self.publication_url, {'action': 'publish', 'config': self.config.pk})
        self.assertFalse(reference_catalog()[1]['warrior_fury']['has_data'])

    def test_moved_resource_does_not_gain_publication_in_another_spec(self):
        SimcProfile.objects.filter(pk=self.profile.pk).update(spec='mage_fire', class_name='mage')
        self.assertEqual(reference_catalog()[1]['mage_fire']['profiles'], [])

    def test_results_group_scenarios_and_reruns_by_scheme(self):
        self.enqueue()
        self.complete()
        self.enqueue(force=True)
        self.complete(dps=234567)
        self.login()
        response = self.client.get(self.url, {'config': self.config.pk})
        self.assertEqual(response.status_code, 200)
        page = response.context['scheme_page']
        self.assertEqual(page.paginator.count, 1)
        self.assertEqual(page[0]['record_count'], 4)
        self.assertEqual([s['result'].dps for s in page[0]['scenes']], [234567, 234567])
        history = self.client.get(self.url, {'config': self.config.pk, 'scheme': page[0]['latest_id']})
        self.assertEqual(history.context['page'].paginator.count, 4)

    def test_rerun_one_scheme_does_not_rerun_other_equipment(self):
        extra = SimcProfile.objects.create(name='另一套装备', spec='warrior_fury', class_name='warrior', player_config_mode='manual_equipment')
        self.publication.profiles.add(extra)
        self.enqueue()
        self.complete()
        representative = SimcReferenceSimulation.objects.filter(profile=self.profile).first()
        self.login()
        self.client.post(self.url, {'action': 'rerun_scheme', 'scheme': representative.pk})
        self.assertEqual(SimcReferenceSimulation.objects.filter(profile=self.profile, status='queued').count(), 2)
        self.assertEqual(SimcReferenceSimulation.objects.filter(profile=extra, status='queued').count(), 0)

    def test_independent_permission_can_open_own_reports_but_not_general_simc_history(self):
        manager = User.objects.create_user('前台数据管理员')
        group = DashboardUserGroup.objects.create(name='前台数据管理', permission_codes=['frontend.simulations'])
        group.users.add(manager)
        self.enqueue()
        self.complete()
        row = SimcReferenceSimulation.objects.first()
        run = row.task.simulation_runs.get()
        artifact = SimcTaskArtifact.objects.create(task=row.task, run=run, artifact_type='html_report',
                                                  file_path=f'simc_agent_results/simc_task_{row.task_id}_run_{run.pk}.html')
        self.client.force_login(manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'dashboard/index.html')
        self.assertContains(response, 'id="sidebar"')
        self.assertContains(response, 'id="dashboard-theme-toggle"')
        self.assertContains(response, 'dashboard/js/dashboard_shell.js')
        self.assertNotContains(response, 'dashboard/js/main.js')
        self.assertEqual(response.context['dashboard_default_section'], 'frontend-results')
        self.assertEqual(response.context['dashboard_permissions'], ['frontend.simulations'])
        report_url = reverse('simc_reference_report', args=[row.pk])
        with patch('botend.services.simc_agent_oss.public_report_url', return_value='https://reports.example/report.html') as build:
            response = self.client.get(report_url)
        self.assertEqual(response.status_code, 302)
        build.assert_called_once_with(artifact.file_path)
        self.assertEqual(self.client.get(reverse('simc_task_detail_page', args=[row.task_id])).status_code, 403)
        self.client.force_login(User.objects.create_user('普通账号'))
        self.assertEqual(self.client.get(report_url).status_code, 403)

    def test_report_cannot_be_selected_using_an_unrelated_artifact_id(self):
        self.enqueue()
        self.complete()
        self.login()
        row = SimcReferenceSimulation.objects.first()
        self.assertEqual(self.client.get(reverse('simc_reference_report', args=[row.pk]), {'artifact': 999}).status_code, 404)
