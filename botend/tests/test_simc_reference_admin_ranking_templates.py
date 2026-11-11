"""Synthetic projections exercise real templates only, not ranking data or HTTP services."""
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from django.urls import reverse


class AdminRankingTemplateTests(SimpleTestCase):
    def board(self, admin):
        return {
            'admin_ranking': admin, 'scenario_name': '单体长战斗', 'targets': 1,
            'scope_label': '当前专精', 'duration': 300, 'iterations': 1000,
            'return_url': '/dashboard/frontend-simulations/', 'return_label': '模拟结果管理',
            'scope_options': [{'label': '当前专精', 'url': '/legacy-spec/', 'selected': True}],
            'settings_options': [
                {'label': '正式服', 'url': '?settings=1&scenario=2', 'selected': True},
                {'label': 'PTR', 'url': '?settings=3&scenario=4', 'selected': False},
            ],
            'scene_options': [{'label': '单体长战斗', 'url': '?settings=1&scenario=2', 'selected': True}],
            'current': {'spec': '火焰', 'profile': '旧当前装备'},
            'entries': [{'rank': 1, 'spec': '火焰', 'profile': '公开装备',
                         'talent': '公开天赋', 'apl': '公开APL', 'dps': 100,
                         'detail_url': '/portal/config/1/', 'is_current': True}],
        }

    def test_admin_filters_and_rows_ignore_legacy_current_projection(self):
        html = render_to_string('shared/simc_reference_ranking_panel.html', self.board(True))
        for forbidden in ('当前专精', '当前配置', '旧当前装备', '定位', '排名范围',
                          'ranking-current', 'is-current', 'data-ranking-current', 'aria-current="true"',
                          '/legacy-spec/', '1 目标', '当前专精榜单'):
            self.assertNotIn(forbidden, html)
        for expected in ('aria-label="模拟配置"', '正式服', 'PTR', 'aria-label="模拟场景"',
                         '?settings=3&amp;scenario=4', '公开装备', '单体长战斗',
                         '1 个有效组合', 'aria-label="完整排名表"'):
            self.assertIn(expected, html)

    def test_admin_single_setting_is_label_not_empty_control(self):
        board = self.board(True)
        board['settings_options'] = board['settings_options'][:1]
        html = render_to_string('shared/simc_reference_ranking_panel.html', board)
        self.assertIn('aria-label="模拟配置"', html)
        self.assertIn('<span>正式服</span>', html)

    def test_portal_keeps_scope_current_card_and_row(self):
        html = render_to_string('shared/simc_reference_ranking_panel.html', self.board(False))
        for expected in ('排名范围', '/legacy-spec/', '当前配置', '旧当前装备',
                         'data-ranking-locate', 'data-ranking-current', 'aria-current="true"',
                         '1 目标 · 当前专精榜单'):
            self.assertIn(expected, html)
        self.assertNotIn('aria-label="模拟配置"', html)

    def test_admin_button_uses_group_entry_portal_keeps_two_scopes(self):
        context = {'ranking': {'overall_rank': 2, 'overall_total': 9, 'spec_rank': 1, 'spec_total': 3},
                   'config_id': 7, 'signature': 'frozen-signature', 'targets': 1,
                   'settings_id': 4, 'scenario_id': 8, 'ranking_route': 'simc_reference_rankings'}
        html = render_to_string('shared/simc_reference_rank_buttons.html', context)
        self.assertEqual(html.count('<a '), 1)
        self.assertIn(reverse('simc_reference_rankings_entry') + '?settings=4&amp;scenario=8', html)
        self.assertIn('排行榜', html)
        self.assertNotIn('本专精', html)
        self.assertNotIn('全职业', html)
        context['ranking_route'] = 'portal_simc_reference_rankings'
        html = render_to_string('shared/simc_reference_rank_buttons.html', context)
        self.assertEqual(html.count('<a '), 2)
        self.assertIn('/overall/', html)
        self.assertIn('/spec/', html)
        self.assertIn('本专精', html)

    def test_scheme_table_explains_same_settings_and_scenario(self):
        html = render_to_string('dashboard/_frontend_simulation_schemes.html', {
            'selected': {'pk': 7, 'settings_id': 4},
            'scheme_page': [{'is_public': True, 'scenes': [
                {'scenario_id': 8, 'ranking': {'overall_rank': 2, 'overall_total': 9}},
            ]}],
        })
        self.assertIn(reverse('simc_reference_rankings_entry') + '?settings=4&amp;scenario=8', html)
        self.assertIn('同一模拟配置与场景', html)
        for forbidden in ('本专精排名', '全职业 /', '同版本'):
            self.assertNotIn(forbidden, html)
