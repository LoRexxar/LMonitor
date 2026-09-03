import json
from pathlib import Path

from django.contrib.auth.models import User
from django.test import TestCase

from botend.models import PortalNavigationGroup, PortalNavigationItem, PortalToolLink


ROOT = Path(__file__).resolve().parents[2]


class PortalHomeNavigationTests(TestCase):
    def test_navigation_and_external_tools_use_separate_models_and_apis(self):
        group = PortalNavigationGroup.objects.create(
            key='test-navigation', name='测试站内导航', description='仅站内入口', icon_key='chart', sort_order=1,
        )
        navigation = PortalNavigationItem.objects.create(
            group=group, name='站内分数线', url='/#section-mplus-cutoffs', desc='首页板块入口',
            badge='新', badge_tone='new', show_in_header=True, show_in_home_guide=True,
        )
        external = PortalToolLink.objects.create(
            name='外部工具', url='https://example.com/tool', url_hash='external-tool-test',
            category='tools', show_in_tools=True, is_active=True,
        )
        leaked_internal = PortalToolLink.objects.create(
            name='不应混入的旧站内数据', url='/portal/talents/', url_hash='legacy-internal-tool-test',
            category='tools', show_in_tools=True, is_active=True,
        )
        leaked_relative = PortalToolLink.objects.create(
            name='不应混入的相对地址', url='portal/talents/', url_hash='legacy-relative-tool-test',
            category='tools', show_in_tools=True, is_active=True,
        )

        navigation_response = self.client.get('/portal/api/navigation/')
        self.assertEqual(navigation_response.status_code, 200)
        navigation_data = navigation_response.json()['data']
        self.assertIn(navigation.name, {item['name'] for item in navigation_data['header']})
        guide_item = next(item for item in navigation_data['guide'] if item['name'] == navigation.name)
        self.assertEqual(guide_item['category'], group.key)
        self.assertEqual(guide_item['badge_tone'], 'new')
        self.assertFalse(guide_item['open_in_new_tab'])
        self.assertIn(group.key, {item['key'] for item in navigation_data['categories']})

        tools_response = self.client.get('/portal/api/tools/')
        self.assertEqual(tools_response.status_code, 200)
        tools_data = tools_response.json()['data']
        self.assertNotIn('header', tools_data)
        self.assertNotIn('guide', tools_data)
        self.assertIn(external.name, {item['name'] for item in tools_data['items']})
        self.assertEqual(next(item for item in tools_data['categories'] if item['key'] == 'tools')['name'], '实用工具')
        self.assertNotIn(leaked_internal.name, {item['name'] for item in tools_data['items']})
        self.assertNotIn(leaked_relative.name, {item['name'] for item in tools_data['items']})
        self.assertNotIn(navigation.name, {item['name'] for item in tools_data['items']})

    def test_home_uses_dedicated_navigation_and_external_link_sources(self):
        template = (ROOT / 'templates/portal/index.html').read_text(encoding='utf-8')
        header = (ROOT / 'templates/portal/_header.html').read_text(encoding='utf-8')
        script = (ROOT / 'static/portal/js/main.js').read_text(encoding='utf-8')
        theme_script = (ROOT / 'static/portal/js/portal-theme.js').read_text(encoding='utf-8')
        css = (ROOT / 'static/portal/css/portal.css').read_text(encoding='utf-8')

        for contract in ('今天的艾泽拉斯，从这里开始', 'aria-label="首页内容分类"', 'id="topbar-tools"', 'id="portal-search-meta"', 'class="portal-tools-directory"'):
            self.assertIn(contract, template)
        self.assertIn('id="portal-primary-nav"', header)
        self.assertIn('id="portal-external-links"', header)
        self.assertIn('id="portal-external-links-panel"', header)
        self.assertIn('window.getPortalNavigationData', theme_script)
        self.assertIn('window.getPortalToolsData', theme_script)
        self.assertIn("fetch('/portal/api/navigation/'", theme_script)
        self.assertIn("fetch('/portal/api/tools/'", theme_script)
        self.assertIn('renderExternalLinks', theme_script)
        self.assertIn("link.target = '_blank'", theme_script)
        self.assertIn('portal-guide-group', script)
        self.assertIn('portal-tool-group', script)
        self.assertIn('portal-guide-link-copy', script)
        self.assertIn('portal-guide-link-arrow', script)
        self.assertIn('navigationData?.guide', script)
        self.assertIn('toolsData?.items', script)
        self.assertIn('/portal/api/navigation/', script)
        self.assertIn('/portal/api/tools/', script)
        self.assertIn('data-portal-section=', script)
        self.assertIn('event.target.closest("[data-portal-section]")', script)
        self.assertIn('--portal-canvas: #eef1f4', css)
        self.assertIn('.portal-guide-links {\n    display: grid;', css)
        self.assertIn('.portal-guide-link-badge.is-new', css)
        self.assertIn('.portal-external-menu-panel', css)
        self.assertIn('.portal-external-menu-groups', css)
        self.assertIn('.portal-guide-link-copy small', css)
        self.assertNotIn('portal-hero-jump', template)
        self.assertIn('width: min(46rem, 72%)', css)
        self.assertIn('.portal-hero-search { width: 100%; }', css)

        navigation_data = self.client.get('/portal/api/navigation/').json()['data']
        self.assertNotIn('/#section-tools', {item['url'] for item in navigation_data['guide']})
        categories = {item['key']: item for item in navigation_data['categories']}
        self.assertEqual(categories['data']['name'], '数据中心')
        self.assertEqual(categories['tools']['name'], '站内工具')
        header_category_keys = {item['category'] for item in navigation_data['header']}
        guide_category_keys = {item['category'] for item in navigation_data['guide']}
        self.assertEqual(header_category_keys, guide_category_keys)
        self.assertEqual(header_category_keys, {'data', 'mythic', 'tools', 'community'})
        ordered_category_names = [
            item['name'] for item in navigation_data['categories']
            if item['key'] in header_category_keys
        ]
        self.assertEqual(ordered_category_names, ['数据中心', '大秘境', '站内工具', '资讯社区'])

    def test_dashboard_has_dedicated_group_and_item_navigation_editor(self):
        PortalNavigationGroup.objects.all().delete()
        admin = User.objects.create_user(username='portal-navigation-admin', password='test-pass', is_staff=True, is_superuser=True)
        self.client.force_login(admin)

        get_response = self.client.get('/api/dashboard/portal-navigation/')
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json()['records'], [])

        for table_name in ('PortalNavigationGroup', 'PortalNavigationItem'):
            crud_response = self.client.post(
                '/dashboard/',
                data=json.dumps({'action': 'get_table_data', 'table_name': table_name}),
                content_type='application/json',
            )
            self.assertEqual(crud_response.status_code, 200, crud_response.content)
            self.assertEqual(crud_response.json()['status'], 'success')

        save_response = self.client.patch(
            '/api/dashboard/portal-navigation/',
            data=json.dumps({'groups': [{
                'id': None, 'key': 'mythic-local', 'name': '大秘境站内入口',
                'description': '只包含 Portal 页面与板块', 'icon_key': 'chart', 'is_active': True,
                'items': [{
                    'id': None, 'name': 'DPS 排行榜', 'url': '/#section-mythicstats',
                    'desc': '跳转首页排行榜', 'icon_key': 'chart', 'badge': '新', 'badge_tone': 'new',
                    'show_in_header': False, 'show_in_home_guide': True, 'is_active': True,
                }],
            }]}),
            content_type='application/json',
        )
        self.assertEqual(save_response.status_code, 200, save_response.content)
        self.assertEqual(PortalNavigationGroup.objects.count(), 1)
        self.assertEqual(PortalNavigationItem.objects.get().url, '/#section-mythicstats')

        record = save_response.json()['records'][0]
        record['items'][0]['url'] = 'https://example.com/not-allowed'
        external_response = self.client.patch(
            '/api/dashboard/portal-navigation/', data=json.dumps({'groups': [record]}), content_type='application/json',
        )
        self.assertEqual(external_response.status_code, 400)
        self.assertIn('站内地址', external_response.json()['error'])

        dashboard_template = (ROOT / 'templates/dashboard/index.html').read_text(encoding='utf-8')
        dashboard_script = (ROOT / 'static/dashboard/js/portal_navigation_management.js').read_text(encoding='utf-8')
        shell_script = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')
        self.assertIn('id="portal-navigation"', dashboard_template)
        self.assertIn('首页导航管理', dashboard_template)
        self.assertIn('外部网址属于“外部快捷链接”', dashboard_template)
        self.assertIn('data-item-field="show_in_header"', dashboard_script)
        self.assertIn('data-item-field="show_in_home_guide"', dashboard_script)
        self.assertIn('window.loadPortalNavigationManagement', dashboard_script)
        self.assertIn("sectionId === 'portal-navigation'", shell_script)
        self.assertIn("{ value: 'data', label: '数据站点' }", shell_script)
        self.assertIn("{ value: 'tools', label: '实用工具' }", shell_script)

    def test_navigation_model_is_item_granular(self):
        group_fields = {field.name for field in PortalNavigationGroup._meta.fields}
        item_fields = {field.name for field in PortalNavigationItem._meta.fields}
        self.assertTrue({'key', 'name', 'description', 'icon_key', 'sort_order', 'is_active'}.issubset(group_fields))
        self.assertTrue({
            'group', 'name', 'url', 'desc', 'icon_key', 'badge', 'badge_tone',
            'sort_order', 'show_in_header', 'show_in_home_guide', 'is_active',
        }.issubset(item_fields))
