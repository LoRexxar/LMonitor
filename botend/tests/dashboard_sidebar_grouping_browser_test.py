"""Run with the Dashboard QA Python: real sidebar DOM and shared JavaScript."""
import re
import unittest
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]


class SidebarGroupingBrowserTests(unittest.TestCase):
    def test_grouped_navigation_preserves_entries_links_and_active_branch(self):
        template = (ROOT / 'templates/dashboard/index.html').read_text()
        sidebar = str(BeautifulSoup(template, 'html.parser').select_one('#sidebar'))
        # The all-access fixture includes every conditional entry, without a server
        # or API mocks; grouping consumes the real template nodes and real script.
        sidebar = re.sub(r'{%.*?%}', '', sidebar, flags=re.S)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(sidebar)
            before = page.locator('#dashboard-primary-nav a').count()
            page.add_script_tag(path=str(ROOT / 'static/dashboard/js/sidebar.js'))
            page.evaluate('DashboardSidebar.initGroups(); DashboardSidebar.bindSubmenus();')
            groups = page.locator('#dashboard-primary-nav > li').evaluate_all('''nodes => nodes.map(node => ({
                key: node.dataset.sidebarGroup || null,
                label: node.querySelector(':scope > .sidebar-group-label')?.textContent,
                entries: [...node.querySelectorAll(':scope > ul > li')].map(item =>
                    item.dataset.section || item.dataset.sidebarFolder || item.dataset.sidebarLink || item.dataset.dashboardTable)
            }))''')
            self.assertEqual(groups, [
                {'key': 'overview', 'label': '概览统计', 'entries': ['dashboard-home', 'site-analytics']},
                {'key': 'content', 'label': '内容运营', 'entries': ['class-guide-module', 'news', 'site']},
                {'key': 'tools', 'label': '游戏工具', 'entries': ['simc', 'mythic-planner', 'gear-builder-management', 'tools']},
                {'key': 'access', 'label': '账号权限', 'entries': ['user-management', 'user-groups', 'bilibili-binding']},
                {'key': 'operations', 'label': '运行维护', 'entries': ['MonitorTask', 'logs', 'database-tables']},
            ])
            # Three grouping folders add only their own header links; no entry lost.
            self.assertEqual(page.locator('#dashboard-primary-nav a').count(), before + 3)
            self.assertEqual(page.locator('[data-sidebar-link="bilibili-binding"] > a').get_attribute('href'), '/dashboard/bilibili-binding/')
            self.assertEqual(page.locator('#sidebar .has-submenu > a[aria-expanded="true"]').count(), 0)
            page.evaluate('''DashboardSidebar.reveal(document.querySelector('[data-dashboard-section="class-guides"]'))''')
            self.assertEqual(page.locator('[data-section="class-guide-module"] > a').get_attribute('aria-expanded'), 'true')
            self.assertEqual(page.locator('[data-dashboard-section="class-guides"] > a').get_attribute('aria-current'), 'page')
            self.assertEqual(page.locator('[data-section="database-tables"] > a').get_attribute('aria-expanded'), 'false')
            page.evaluate('DashboardSidebar.initGroups(); DashboardSidebar.bindSubmenus();')
            self.assertEqual(page.locator('#dashboard-primary-nav > [data-sidebar-group]').count(), 5)
            self.assertEqual(page.locator('#dashboard-primary-nav a').count(), before + 3)
            # Grouping after permission filtering must not create empty headings.
            page.set_content(sidebar)
            page.evaluate('''document.querySelectorAll('#dashboard-primary-nav > li').forEach(item => {
                if (item.dataset.section !== 'site-analytics') item.remove();
            });''')
            page.add_script_tag(path=str(ROOT / 'static/dashboard/js/sidebar.js'))
            page.evaluate('DashboardSidebar.initGroups();')
            self.assertEqual(page.locator('[data-sidebar-group]').count(), 1)
            self.assertEqual(page.locator('[data-sidebar-group]').get_attribute('data-sidebar-group'), 'overview')
            browser.close()


if __name__ == '__main__':
    unittest.main()
