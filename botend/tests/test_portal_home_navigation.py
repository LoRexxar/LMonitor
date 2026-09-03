import json
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import PortalToolLink


ROOT = Path(__file__).resolve().parents[2]


class PortalHomeNavigationTests(TestCase):
    def test_tools_api_groups_navigation_and_filters_directory(self):
        topbar = PortalToolLink.objects.create(
            name='测试顶部入口',
            url='/portal/specs/',
            url_hash='nav-test-topbar',
            desc='顶部入口说明',
            category='data',
            icon_key='chart',
            badge='推荐',
            badge_tone='new',
            is_topbar=True,
            topbar_order=5,
            show_in_guide=True,
            show_in_tools=False,
            open_in_new_tab=False,
            is_active=True,
        )
        directory = PortalToolLink.objects.create(
            name='测试工具链接',
            url='https://example.com/tool',
            url_hash='nav-test-directory',
            desc='工具入口说明',
            category='tools',
            icon_key='tools',
            is_topbar=False,
            show_in_tools=True,
            open_in_new_tab=True,
            is_active=True,
        )

        response = self.client.get('/portal/api/tools/')
        self.assertEqual(response.status_code, 200)
        payload = response.json()['data']
        topbar_item = next(item for item in payload['topbar'] if item['name'] == topbar.name)
        self.assertEqual(topbar_item['category'], 'data')
        self.assertEqual(topbar_item['icon_key'], 'chart')
        self.assertEqual(topbar_item['badge'], '推荐')
        self.assertEqual(topbar_item['badge_tone'], 'new')
        self.assertTrue(topbar_item['show_in_guide'])
        self.assertFalse(topbar_item['show_in_tools'])
        self.assertFalse(topbar_item['open_in_new_tab'])
        self.assertNotIn(topbar.name, {item['name'] for item in payload['items']})
        self.assertIn(topbar.name, {item['name'] for item in payload['guide']})
        self.assertIn(directory.name, {item['name'] for item in payload['items']})
        version_change = next(item for item in payload['guide'] if item['url'] == '/#section-wow-skill-diff')
        self.assertEqual(version_change['badge'], '新')
        self.assertEqual(version_change['badge_tone'], 'new')
        self.assertEqual(version_change['category'], 'community')
        self.assertFalse(version_change['is_topbar'])
        guide_by_url = {item['url']: item for item in payload['guide']}
        self.assertNotIn('/#section-news', guide_by_url)
        self.assertNotIn('/#section-events', guide_by_url)
        for url in (
            '/#section-mplus-cutoffs',
            '/#section-rank',
            '/#section-peak-spec',
            '/#section-mythicstats',
        ):
            self.assertEqual(guide_by_url[url]['category'], 'mythic')
        self.assertIn('data', {item['key'] for item in payload['categories']})
        mythic_category = next(item for item in payload['categories'] if item['key'] == 'mythic')
        self.assertEqual(mythic_category['name'], '大秘境数据')
        self.assertIn('tools', {item['key'] for item in payload['categories']})

    def test_home_uses_grouped_search_guide_and_directory(self):
        template = (ROOT / 'templates/portal/index.html').read_text(encoding='utf-8')
        header = (ROOT / 'templates/portal/_header.html').read_text(encoding='utf-8')
        script = (ROOT / 'static/portal/js/main.js').read_text(encoding='utf-8')
        theme_script = (ROOT / 'static/portal/js/portal-theme.js').read_text(encoding='utf-8')
        css = (ROOT / 'static/portal/css/portal.css').read_text(encoding='utf-8')

        for contract in (
            '今天的艾泽拉斯，从这里开始',
            'aria-label="首页内容分类"',
            'id="topbar-tools"',
            'id="portal-search-meta"',
            'class="portal-tools-directory"',
        ):
            self.assertIn(contract, template)
        self.assertIn('id="portal-primary-nav"', header)
        self.assertIn('window.getPortalToolsData', theme_script)
        self.assertIn("badge.classList.add('is-new')", theme_script)
        self.assertIn('portal-guide-group', script)
        self.assertIn('portal-tool-group', script)
        self.assertIn('sharedData?.guide', script)
        self.assertIn('portal-guide-link-badge${badgeTone}', script)
        self.assertIn('Array.isArray(rawItems)', script)
        self.assertIn('data-portal-section=', script)
        self.assertIn('event.target.closest("[data-portal-section]")', script)
        self.assertIn('bodyIsScroller', script)
        self.assertIn('--portal-canvas: #eef1f4', css)
        self.assertIn('.portal-guide-links {\n    display: grid;', css)
        self.assertIn('grid-template-columns: minmax(0, 1fr) auto auto', css)
        self.assertIn('grid-template-columns: repeat(2, minmax(0, 1fr))', css)
        self.assertIn('.portal-guide-link-badge.is-new', css)
        self.assertIn('.portal-nav-group[open] { grid-column: 1 / -1; }', css)
        self.assertNotIn('id="section-spec-detail"', template)
        self.assertNotIn('id="spec-detail-grid"', template)
        self.assertNotIn('renderSpecDetailGrid', script)
        self.assertNotIn('"section-spec-detail":', script)

    def test_tool_link_model_keeps_navigation_configuration_per_item(self):
        field_names = {field.name for field in PortalToolLink._meta.fields}
        self.assertTrue({
            'category',
            'icon_key',
            'badge',
            'badge_tone',
            'show_in_guide',
            'show_in_tools',
            'open_in_new_tab',
        }.issubset(field_names))

    def test_dashboard_generic_crud_exposes_navigation_fields(self):
        admin = User.objects.create_user(
            username='portal-navigation-admin',
            password='test-pass',
            is_staff=True,
            is_superuser=True,
        )
        self.client.force_login(admin)
        response = self.client.post(
            '/dashboard/',
            data=json.dumps({
                'action': 'get_table_data',
                'table_name': 'PortalToolLink',
            }),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200, response.content)
        payload = response.json()
        self.assertEqual(payload['status'], 'success')
        for field_name, label in {
            'category': '分类',
            'icon_key': '内置图标',
            'badge': '入口徽标',
            'badge_tone': '徽标样式',
            'show_in_guide': '显示在首页引导',
            'show_in_tools': '显示在工具区',
            'open_in_new_tab': '新窗口打开',
        }.items():
            self.assertIn(field_name, payload['fields'])
            self.assertEqual(payload['field_labels'][field_name], label)
            self.assertTrue(payload['field_types'][field_name]['editable'])
        self.assertTrue(payload['capabilities']['can_create'])
        self.assertTrue(payload['capabilities']['can_update'])
        self.assertTrue(payload['capabilities']['can_delete'])

        editable = PortalToolLink.objects.create(
            name='待编辑入口',
            url='/#section-tools',
            url_hash='dashboard-editable-navigation',
            category='tools',
            show_in_guide=False,
            show_in_tools=False,
        )
        response = self.client.post(
            '/dashboard/',
            data=json.dumps({
                'action': 'update_table_row',
                'table_name': 'PortalToolLink',
                'row_id': editable.pk,
                'update_data': {
                    'badge': '新',
                    'badge_tone': 'new',
                    'show_in_guide': True,
                    'show_in_tools': False,
                },
            }),
            content_type='application/json',
        )
        self.assertEqual(response.status_code, 200, response.content)
        editable.refresh_from_db()
        self.assertEqual(editable.badge, '新')
        self.assertEqual(editable.badge_tone, 'new')
        self.assertTrue(editable.show_in_guide)
        self.assertFalse(editable.show_in_tools)

        dashboard_script = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn("PortalToolLink: {", dashboard_script)
        self.assertIn("'show_in_guide', 'show_in_tools', 'open_in_new_tab'", dashboard_script)
        self.assertIn("{ value: 'mythic', label: '大秘境数据' }", dashboard_script)
        self.assertIn("'category',\n            'icon_key',\n            'badge'", dashboard_script)
        self.assertIn("'badge_tone',\n            'source'", dashboard_script)
        self.assertIn("'show_in_guide',\n            'show_in_tools',\n            'open_in_new_tab'", dashboard_script)
