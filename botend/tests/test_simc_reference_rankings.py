"""公开组合的跨职业、专精内排名及条件隔离回归。"""

from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.test import TestCase
from django.urls import reverse

from botend.models import SimcApl, SimcProfile, SimcTalentString, SimcReferenceSimulation, SimcReferenceSimulationConfig
from botend.services import simc_reference_simulations as service
from botend.services.simc_reference_management import scheme_results
from botend.services.simc_reference_rankings import current_rankings, leaderboard
from botend.services.simc_reference_ranking_index import sync_result, refresh_configs
from botend.tests.simc_reference_test_fixtures import (
    publish_resources, update_scenario_params,
)
from botend.tests import test_simc_reference_simulations as simulation_tests


class ReferenceRankingTests(TestCase):
    setUp = simulation_tests.ReferenceSimulationTests.setUp

    def result(self, dps, targets=1, *, config=None, profile=None, talent=None, apl=None, status='completed'):
        config = config or self.config
        scene = config.settings.scenarios.get(simulation_params__desired_targets=targets)
        frozen = service.snapshot(config, profile or self.profile, talent or self.talent, apl or self.apl, scene)
        row = SimcReferenceSimulation.objects.create(
            requested_by_id=self.user.pk,
            config=config, scenario=scene, profile=profile or self.profile, talent=talent or self.talent, apl=apl or self.apl,
            signature=service.tasks._compute_content_hash(frozen), snapshot=frozen, dps=dps, status=status,
        )
        # TestCase does not commit writer callbacks; publish the complete fixture.
        refresh_configs([config.pk])
        return row

    def other_spec(self):
        apl = SimcApl.objects.create(name='火法优先级', spec='mage_fire', class_name='mage', is_system=True,
                                     is_selectable=True, content='actions=/fireball', source='user')
        talent = SimcTalentString.objects.create(name='火法天赋', spec='mage_fire', talent='demo', is_system=True, default_apl=apl)
        profile = SimcProfile.objects.create(name='火法装备', spec='mage_fire', class_name='mage', player_config_mode='manual_equipment')
        config = SimcReferenceSimulationConfig.objects.create(spec_key='mage_fire', settings_id=self.config.settings_id)
        publish_resources('mage_fire', profiles=[profile], talents=[talent], apls=[apl])
        return dict(config=config, profile=profile, talent=talent, apl=apl)

    def alternate_talent(self):
        talent = SimcTalentString.objects.create(name='另一份天赋', spec='warrior_fury', talent='other', is_system=True, default_apl=self.apl)
        self.publication.talents.add(talent)
        return talent

    def rank(self, row):
        return current_rankings(self.config).get((row.config_id, row.signature))

    def test_two_dimensions_and_targets_rank_independently(self):
        row = self.result(200)
        aoe = self.result(600, 5)
        talent = self.alternate_talent()
        self.result(100, talent=talent)
        self.result(800, 5, talent=talent)
        other = self.other_spec()
        self.result(300, **other)
        self.result(400, 5, **other)
        self.assertEqual(self.rank(row), dict(overall_rank=2, overall_total=3, spec_rank=1, spec_total=2))
        self.assertEqual(self.rank(aoe), dict(overall_rank=2, overall_total=3, spec_rank=2, spec_total=2))
        projected = service.public_results('warrior_fury', self.profile, self.talent, self.apl)
        self.assertEqual([r['ranking'] for r in projected], [self.rank(row), self.rank(aoe)])
        self.client.force_login(self.user)
        response = self.client.get(self.url)
        self.assertContains(response, '全职业 <strong>2</strong> / 3')
        self.assertContains(response, '本专精 <strong>1</strong> / 2')

    def test_ties_latest_success_and_pending_rerun(self):
        row = self.result(200)
        self.result(200, talent=self.alternate_talent())
        other = self.other_spec()
        low = self.result(100, **other)
        self.assertEqual(self.rank(low)['overall_rank'], 3)
        self.result(None, status='queued')
        self.assertEqual(self.rank(row)['overall_rank'], 1)
        self.result(150)
        self.assertEqual(self.rank(row), dict(overall_rank=2, overall_total=3, spec_rank=2, spec_total=2))

    def test_current_public_valid_results_only(self):
        row = self.result(200)
        talent = self.alternate_talent()
        rival = self.result(300, talent=talent)
        other = self.other_spec()
        self.result(400, **other)
        # 编辑输入、撤下公开资料后立即排除，不能继续展示旧排名。
        talent.talent = 'changed'
        talent.save()
        refresh_configs()
        other['profile'].is_active = False
        other['profile'].save()
        refresh_configs()
        self.assertEqual(self.rank(row)['overall_total'], 1)
        self.assertIsNone(self.rank(rival))
        self.publication.talents.remove(self.talent)
        refresh_configs()
        self.assertIsNone(self.rank(row))

    def test_distinct_simulation_conditions_are_not_compared(self):
        row = self.result(200)
        other = self.other_spec()
        self.result(400, **other)
        settings = self.config.settings
        for field, value in (('max_time', 240), ('iterations', 2000)):
            with self.subTest(field=field):
                update_scenario_params(settings, **{field: value})
                self.config.refresh_from_db()
                other['config'].refresh_from_db()
                current = self.result(500, **other)
                self.assertIsNone(self.rank(row))
                self.assertEqual(self.rank(current)['overall_total'], 1)

    def test_backend_route_template_and_branch_are_isolated(self):
        from botend.models import SimcBackendBinary, SimcContentTemplate
        row = self.result(200)
        other = self.other_spec()
        settings = self.config.settings
        settings.backend = SimcBackendBinary.objects.create(identifier='ranking-other', name='不同后端路由', current_version='b' * 40)
        settings.save()
        refresh_configs()
        self.config.refresh_from_db()
        other['config'].refresh_from_db()
        current = self.result(400, **other)
        self.assertIsNone(self.rank(row))
        self.assertEqual(self.rank(current)['overall_total'], 1)
        settings.backend = self.backend
        settings.template = SimcContentTemplate.objects.create(name='不同条件', spec='all', content='optimal_raid=1')
        settings.save()
        refresh_configs()
        self.config.refresh_from_db()
        other['config'].refresh_from_db()
        current = self.result(400, **other)
        self.assertIsNone(self.rank(row))
        self.assertEqual(self.rank(current)['overall_total'], 1)
        anchor = self.result(300)
        # PTR owns a different central settings row; never compare across environments.
        ptr = service.initialize_settings(use_ptr=True)
        ptr.backend, ptr.template = self.backend, self.template
        ptr.save()
        refresh_configs()
        other['config'].use_ptr = True
        other['config'].settings = ptr
        other['config'].save()
        refresh_configs()
        other['profile'].use_ptr = True
        other['profile'].save()
        refresh_configs()
        self.result(600, **other)
        self.assertEqual(self.rank(anchor)['overall_total'], 1)

    def test_invalid_and_unrun_combinations_have_no_rank(self):
        row = self.result(200)
        talent = self.alternate_talent()
        for dps, status in ((0, 'completed'), (float('inf'), 'completed'), (500, 'failed'), (None, 'queued')):
            self.result(dps, talent=talent, status=status)
        self.assertEqual(self.rank(row)['overall_total'], 1)
        self.assertTrue(all(not scene['ranking'] for group in scheme_results(self.config, 1)
                            if group['talent_id'] == talent.pk for scene in group['scenes']))

    def test_pagination_does_not_limit_ranking_and_queries_are_batched(self):
        row = self.result(100)
        for index in range(22):
            profile = SimcProfile.objects.create(name=f'装备 {index:02}', spec='warrior_fury', class_name='warrior',
                                                player_config_mode='manual_equipment')
            self.publication.profiles.add(profile)
            self.result(200 + index, profile=profile)
        config = SimcReferenceSimulationConfig.objects.select_related('settings__backend', 'settings__template').get(pk=self.config.pk)
        with CaptureQueriesContext(connection) as queries:
            ranks = current_rankings(config)
        self.assertLessEqual(len(queries), 4)
        self.assertEqual(ranks[(row.config_id, row.signature)]['overall_rank'], 23)
        page = scheme_results(config, 2)
        self.assertEqual(page.paginator.count, 23)
        for group in page:
            self.assertEqual(group['scenes'][0]['ranking']['spec_total'], 23)

    def test_full_leaderboard_matches_summary_and_marks_current_combination(self):
        anchor = self.result(100)
        self.result(200, talent=self.alternate_talent())
        other = self.other_spec()
        self.result(300, **other)
        self.result(900, 5, **other)
        for scope, total, rank in (('overall', 3, 3), ('spec', 2, 2)):
            board = leaderboard(self.config, anchor.signature, scope)
            self.assertEqual(len(board['entries']), total)
            current = [r for r in board['entries'] if r['is_current']]
            self.assertEqual(len(current), 1)
            self.assertEqual(current[0]['rank'], rank)
            self.assertEqual(current[0]['profile'], self.profile.name)
            self.assertEqual(current[0]['talent'], self.talent.name)
            self.assertEqual(current[0]['apl'], self.apl.name)
            self.assertEqual(current[0]['dps'], 100)
        self.assertIsNone(leaderboard(self.config, '0' * 64, 'overall'))
        self.assertIsNone(leaderboard(self.config, anchor.signature, 'invalid'))

    def test_leaderboard_requires_login_and_escapes_names(self):
        self.profile.name = '<script>alert(1)</script>'
        self.profile.save()
        refresh_configs()
        anchor = self.result(100)
        url = reverse('portal_simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall'])
        self.assertEqual(self.client.get(url).status_code, 302)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertContains(response, '<!DOCTYPE html>')
        self.assertContains(response, '<title>全职业模拟排行榜 · WowDaily</title>')
        self.assertNotContains(response, 'data-ranking-close')
        self.assertContains(response, '&lt;script&gt;alert(1)&lt;/script&gt;')
        self.assertContains(response, 'data-ranking-current', count=1)
        self.assertIn('no-store', response['Cache-Control'])
        self.assertEqual(response['X-Robots-Tag'], 'noindex, nofollow')
        self.assertEqual(self.client.post(url).status_code, 405)
        self.publication.profiles.clear()
        refresh_configs()
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_full_leaderboard_is_not_limited_to_management_page(self):
        anchor = self.result(50)
        for index in range(22):
            talent = SimcTalentString.objects.create(name=f'天赋 {index}', spec='warrior_fury', talent=f'code{index}',
                                                     is_system=True, default_apl=self.apl)
            self.publication.talents.add(talent)
            self.result(100 + index, talent=talent)
        board = leaderboard(self.config, anchor.signature, 'overall')
        self.assertEqual(len(board['entries']), 23)
        self.assertEqual(board['entries'][-1]['rank'], 23)
        self.assertTrue(board['entries'][-1]['is_current'])
        self.apl.content = 'changed'
        self.apl.save()
        refresh_configs()
        self.assertIsNone(leaderboard(self.config, anchor.signature, 'overall'))

    def test_standalone_page_switches_scope_and_scene_and_returns_to_configuration(self):
        anchor = self.result(100)
        aoe = self.result(500, 5)
        self.client.force_login(self.user)
        url = reverse('portal_simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall'])
        response = self.client.get(url)
        spec_url = reverse('portal_simc_reference_rankings', args=[self.config.pk, anchor.signature, 'spec'])
        aoe_url = reverse('portal_simc_reference_rankings', args=[self.config.pk, aoe.signature, 'overall'])
        self.assertContains(response, f'href="{spec_url}"')
        self.assertContains(response, f'href="{aoe_url}"')
        self.assertEqual(self.client.get(aoe_url).context['targets'], 5)
        self.assertEqual(self.client.get(spec_url).context['scope_label'], '本专精')
        from urllib.parse import parse_qs, urlsplit
        query = parse_qs(urlsplit(response.context['return_url']).query)
        self.assertEqual(query, {'spec': ['warrior_fury'], 'profile': [str(self.profile.pk)],
                                 'talent': [str(self.talent.pk)], 'apl': [str(self.apl.pk)]})
        source = self.client.get(reverse('portal_simc_reference'), {'spec': 'warrior_fury'})
        self.assertContains(source, f'href="{url}"')
        self.assertNotContains(source, 'data-ranking-panel')
        self.assertNotContains(source, 'data-ranking-url')

    def test_admin_ranking_page_keeps_shell_and_permissions(self):
        from django.contrib.auth.models import User
        anchor = self.result(100)
        url = reverse('simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall'])
        self.client.force_login(User.objects.create_user('榜单内测用户'))
        self.assertEqual(self.client.get(url).status_code, 403)
        portal_url = reverse('portal_simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall'])
        self.assertEqual(self.client.get(portal_url).status_code, 200)
        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertContains(response, 'dashboard-primary-nav')
        self.assertContains(response, '返回模拟结果管理')
        self.assertContains(response, '5 目标')
        self.assertContains(response, '300 秒')
        self.assertContains(response, '未模拟')
        self.assertContains(response, 'aria-disabled="true"')
        self.assertIn('no-store', response['Cache-Control'])
        self.assertContains(self.client.get(self.url), f'href="{url}"')
        self.assertNotContains(self.client.get(self.url), 'data-ranking-panel')

    def test_navigation_exists_without_selected_simulation_and_empty_entry_is_read_only(self):
        entry_url = reverse('portal_simc_reference_rankings_entry')
        self.assertEqual(self.client.get(entry_url).status_code, 302)
        self.client.force_login(self.user)
        source = self.client.get(reverse('portal_simc_reference'), {'spec': 'warrior_fury'})
        self.assertContains(source, '模拟资料页面导航', count=1)
        self.assertContains(source, f'href="{entry_url}"')
        empty = self.client.get(entry_url)
        self.assertContains(empty, '暂无可展示的模拟结果')
        self.assertNotContains(empty, 'class="simc-leaderboard"')
        admin_empty = self.client.get(reverse('simc_reference_rankings_entry'))
        self.assertContains(admin_empty, '前往模拟结果管理')
        self.assertContains(admin_empty, 'dashboard-primary-nav')
        results = self.client.get(self.url)
        self.assertNotContains(results, '模拟资料页面导航')
        self.assertNotContains(results, 'class="simc-ranking-panel"')
        self.assertNotContains(results, 'href=""')
        self.assertEqual(SimcReferenceSimulation.objects.count(), 0)

    def test_entry_only_redirects_to_current_public_results_and_prefers_selected_config(self):
        anchor = self.result(100)
        other = self.other_spec()
        other_row = self.result(200, **other)
        self.client.force_login(self.user)
        entry_url = reverse('portal_simc_reference_rankings_entry')
        response = self.client.get(entry_url, {'config': self.config.pk})
        self.assertRedirects(response, reverse('portal_simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall']))
        self.apl.content = 'changed'
        self.apl.save()
        refresh_configs()
        response = self.client.get(entry_url, {'config': self.config.pk})
        self.assertRedirects(response, reverse('portal_simc_reference_rankings', args=[other['config'].pk, other_row.signature, 'overall']))
        from botend.models import SimcReferencePublication
        SimcReferencePublication.objects.update(is_published=False)
        refresh_configs()
        self.assertContains(self.client.get(entry_url), '暂无可展示的模拟结果')

    def test_ranking_resource_links_preserve_whole_combination_and_open_correct_section(self):
        from urllib.parse import parse_qs, urlsplit
        from django.utils.html import escape
        anchor = self.result(100)
        other = self.other_spec()
        other_row = self.result(300, **other)
        board = leaderboard(self.config, anchor.signature, 'overall')
        row = board['entries'][0]
        self.client.force_login(self.user)
        portal = self.client.get(reverse('portal_simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall']))
        for kind, fragment in (('profile', 'reference-equipment'), ('talent', 'reference-talents'), ('apl', 'reference-apl')):
            parsed = urlsplit(row['resource_links'][kind])
            self.assertEqual(parsed.fragment, fragment)
            self.assertEqual(parse_qs(parsed.query), {'spec': ['mage_fire'], 'profile': [str(other['profile'].pk)],
                                                     'talent': [str(other['talent'].pk)], 'apl': [str(other['apl'].pk)],
                                                     'scenario': [str(other_row.scenario_id)]})
            self.assertContains(portal, f'href="{escape(row["resource_links"][kind])}"')
        admin = self.client.get(reverse('simc_reference_rankings', args=[self.config.pk, anchor.signature, 'overall']))
        self.assertContains(admin, f'data-wow-item-tooltip-url="{escape(row["preview_links"]["talent"])}"')
        self.assertContains(admin, 'talent_tree_thumbnail.js')
        self.assertContains(admin, '前台配置')
        self.assertNotContains(portal, 'data-wow-item-tooltip-url=')
