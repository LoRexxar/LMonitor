from importlib import import_module
from pathlib import Path

from bs4 import BeautifulSoup
from django.apps import apps
from django.test import SimpleTestCase, TestCase
from django.urls import reverse

from botend.models import PortalNavigationGroup, PortalNavigationItem


ROOT = Path(__file__).resolve().parents[2]


class PortalWowUpdatesPageTests(SimpleTestCase):
    def test_hotfix_table_uses_three_columns_and_inline_wrapping_fields(self):
        html = BeautifulSoup(self.client.get(reverse('portal_wow_updates')).content, 'html.parser')
        self.assertEqual(len(html.select('.wow-hotfix-table thead th')), 3)
        css = (ROOT / 'static/portal/css/wow-updates.css').read_text(encoding='utf-8')
        self.assertIn('.wow-hotfix-changes', css)
        self.assertIn('flex-wrap: wrap', css)
        self.assertIn('.wow-hotfix-table { width: 100%; min-width: 0;', css)
        script = (ROOT / 'static/portal/js/wow-updates.js').read_text(encoding='utf-8')
        self.assertIn("element('details', 'wow-hotfix-source-details')", script)
        self.assertNotIn('左右滑动表格', html.get_text(' ', strip=True))

    def test_hotfix_page_defaults_to_latest_and_keeps_priority_sort_optional(self):
        html = BeautifulSoup(self.client.get(reverse('portal_wow_updates')).content, 'html.parser')
        sort = html.select_one('#wow-hotfix-sort')
        self.assertIsNotNone(sort)
        self.assertEqual(sort.select_one('option[selected]')['value'], 'latest')
        self.assertIsNotNone(sort.select_one('option[value="changes_first"]'))
        self.assertIsNotNone(html.select_one('#wow-hotfix-breakdown'))
        script = (ROOT / 'static/portal/js/wow-updates.js').read_text(encoding='utf-8')
        self.assertIn('sort: hotfixSort.value', script)
        self.assertIn('counts.change', script)

    def test_compact_rows_keep_complete_source_and_spell_identity_on_expand(self):
        script = (ROOT / 'static/portal/js/wow-updates.js').read_text(encoding='utf-8')
        for value in ('`build ${item.build', 'item.region_name', 'item.locale',
                      'item.time', '`SpellID ${item.spell_id}`',
                      "addLink(links, '报告'", "addLink(links, 'Wago'",
                      "sourceDetails.append(element('summary', '', '来源详情'), sourceMeta, links)"):
            self.assertIn(value, script)
        css = (ROOT / 'static/portal/css/wow-updates.css').read_text(encoding='utf-8')
        self.assertIn('#wow-updates-panel-hotfix { max-width: 1040px;', css)
        self.assertIn('.wow-hotfix-source-meta', css)

    def test_advanced_filters_are_grouped_without_hiding_type_or_sort(self):
        html = BeautifulSoup(self.client.get(reverse('portal_wow_updates')).content, 'html.parser')
        advanced = html.select_one('#wow-hotfix-advanced')
        self.assertIsNotNone(advanced)
        self.assertTrue(advanced.has_attr('open'))
        self.assertEqual({x['id'] for x in advanced.select('select')},
                         {'wow-hotfix-branch', 'wow-hotfix-build', 'wow-hotfix-table'})
        for control in ('wow-hotfix-search', 'wow-hotfix-mode', 'wow-hotfix-sort'):
            self.assertIsNone(html.select_one(f'#{control}').find_parent(id='wow-hotfix-advanced'))
        script = (ROOT / 'static/portal/js/wow-updates.js').read_text(encoding='utf-8')
        self.assertIn("matchMedia('(max-width: 760px)')", script)
        self.assertIn('更多筛选（已启用）', script)

    def test_standalone_page_is_public_and_preserves_content_containers(self):
        response = self.client.get(reverse('portal_wow_updates'))
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, 'portal/wow_updates.html')
        for text in ('魔兽世界更新数据挖掘', 'wow-skill-diff-states', 'wow-skill-diff-list', 'wow-updates-search'):
            self.assertContains(response, text)
        for text in ('data-updates-tab="build"', 'data-updates-tab="hotfix"',
                     'id="wow-hotfix-list"', 'id="wow-hotfix-search"', 'id="wow-hotfix-branch"',
                     'id="wow-hotfix-build"', 'id="wow-hotfix-table"', 'id="wow-hotfix-mode"',
                     'id="wow-hotfix-pagination"', 'id="wow-updates-states-section"',
                     '<option value="values" selected>',
                     '<table', '<tbody id="wow-hotfix-list"',
                     '物理记录 ID', '原始字段 / 含义'):
            self.assertContains(response, text)
        self.assertNotContains(response, "portal/js/main.js")

    def test_hotfix_list_uses_record_endpoint_and_safe_text_rendering(self):
        script = (ROOT / 'static/portal/js/wow-updates.js').read_text(encoding='utf-8')
        self.assertIn('/portal/api/hotfix-entries/?', script)
        self.assertNotIn('/portal/api/hotfix-reports/?', script)
        self.assertIn("mode: hotfixMode.value", script)
        self.assertIn("statesSection.hidden = name === 'hotfix'", script)
        self.assertIn("params.set('build'", script)
        self.assertIn("params.set('table'", script)
        self.assertIn('AbortController', script)
        self.assertIn('textContent = text', script)
        self.assertNotIn('innerHTML', script)

    def test_home_removes_section_and_requests_but_keeps_legacy_redirect(self):
        response = self.client.get('/')
        self.assertNotContains(response, 'id="section-wow-skill-diff"')
        source = (ROOT / 'static/portal/js/main.js').read_text(encoding='utf-8')
        self.assertNotIn('/portal/api/wow-skill', source)
        self.assertNotIn('renderWowSkillDiff', source)
        self.assertIn('window.location.hash === "#section-wow-skill-diff"', source)
        self.assertIn('window.location.replace("/portal/wow-updates/")', source)


class PortalWowUpdatesNavigationTests(TestCase):
    def test_migration_is_reversible_and_preserves_custom_navigation(self):
        migration = import_module('botend.migrations.0205_move_wow_updates_navigation')
        group = PortalNavigationGroup.objects.create(key='updates-test', name='测试')
        item = PortalNavigationItem.objects.create(
            group=group, name='自定义版本入口', url=migration.OLD_URL,
            desc='保留说明', sort_order=7, is_active=False,
        )
        migration.move_navigation(apps, None)
        migration.move_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, migration.NEW_URL)
        self.assertEqual((item.name, item.desc, item.sort_order, item.is_active), ('自定义版本入口', '保留说明', 7, False))
        migration.restore_navigation(apps, None)
        item.refresh_from_db()
        self.assertEqual(item.url, migration.OLD_URL)
