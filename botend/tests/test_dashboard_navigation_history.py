from pathlib import Path
import re
from unittest import TestCase


ROOT = Path(__file__).resolve().parents[2]
MAIN_JS = (ROOT / 'static/dashboard/js/main.js').read_text(encoding='utf-8')


def function_source(name):
    match = re.search(
        rf'function {re.escape(name)}\([^)]*\) \{{(?P<body>.*?)\n\}}',
        MAIN_JS,
        re.S,
    )
    if not match:
        raise AssertionError(f'function {name} not found')
    return match.group('body')


class DashboardNavigationHistoryTests(TestCase):
    def test_dashboard_navigation_writes_restorable_browser_history(self):
        sync_source = function_source('syncDashboardLocation')
        navigation_source = function_source('initNavigation')

        self.assertIn("url.searchParams.delete('section')", sync_source)
        self.assertIn("url.searchParams.delete('tool')", sync_source)
        self.assertIn("url.searchParams.delete('table')", sync_source)
        self.assertIn('window.history.pushState', sync_source)
        self.assertIn("syncDashboardLocation({ section: sectionId })", navigation_source)
        self.assertIn("syncDashboardLocation({ section: dashboardSection })", navigation_source)
        self.assertIn("syncDashboardLocation({ tool: toolName })", navigation_source)
        self.assertIn("syncDashboardLocation({ table: tableName })", navigation_source)

    def test_browser_history_restores_section_and_dashboard_home(self):
        activation_source = function_source('activateDashboardLocation')

        self.assertIn("document.querySelector('.nav-item[data-section=\"dashboard-home\"]')", activation_source)
        self.assertIn("window.addEventListener('popstate', activateDashboardLocation)", MAIN_JS)
