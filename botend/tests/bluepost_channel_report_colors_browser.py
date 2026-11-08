"""Offline Chromium regression, explicit UI dependency (requirements-simc-ui-test.txt).

Run from the project: .venv/bin/python -m botend.tests.bluepost_channel_report_colors_browser
Kept outside Django's test*.py discovery so ordinary ORM tests need no browser package.
"""
import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'LMonitor.settings_test_sqlite')
import django
django.setup()
from datetime import date
from pathlib import Path
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from botend.wow_daily_report.generator import render_daily_html
from botend.tests import test_bluepost_channel_report_colors as cases


class BluepostChannelReportBrowserTests(cases.BluepostChannelReportColorsTests):
    def test_browser_renders_exact_shared_css_three_colors_and_neutral_unknowns_offline(self):

        palette = (Path(__file__).resolve().parents[2] / 'static/shared/css/bluepost-channel.css').read_text(encoding='utf-8')
        expected = [
            ('rgb(29, 78, 216)', 'rgb(239, 246, 255)', 'rgb(191, 219, 254)'),
            ('rgb(109, 40, 217)', 'rgb(245, 243, 255)', 'rgb(221, 214, 254)'),
            ('rgb(146, 64, 14)', 'rgb(255, 251, 235)', 'rgb(253, 230, 138)'),
        ]
        with sync_playwright() as playwright, playwright.chromium.launch(headless=True) as browser:
            page = browser.new_page()
            requests = []
            page.route('**/*', lambda route: (requests.append(route.request.url), route.abort()))
            for report_day in [date(2026, 11, 2), date(2026, 11, 3)]:
                document = render_daily_html(report_day, self.sections())
                soup = BeautifulSoup(document, 'html.parser')
                shared_styles = [node for node in soup.select('head style') if node.get_text() == palette]
                self.assertEqual(len(shared_styles), 1, 'Inline the source CSS verbatim once, not a copied palette')
                self.assertEqual(document.count(palette), 1)
                for width in [1280, 390]:
                    with self.subTest(report_day=report_day, width=width):
                        page.set_viewport_size({'width': width, 'height': 900})
                        page.set_content(document, wait_until='load')
                        result = page.evaluate('''() => {
                            const colors = node => {
                                const css = getComputedStyle(node);
                                return [css.color, css.backgroundColor, css.borderTopColor];
                            };
                            const badges = [...document.querySelectorAll('.news-card .bluepost-channel-badge')];
                            const neutral = document.createElement('span');
                            neutral.className = 'bluepost-channel-badge';
                            neutral.textContent = '未知渠道';
                            document.querySelector('.meta').append(neutral);
                            return {
                                badges: badges.map(colors),
                                neutral: colors(neutral),
                                other: colors(document.querySelector('.context-status')),
                                overflow: document.documentElement.scrollWidth > innerWidth,
                                otherWithoutPalette: (() => {
                                    const shared = document.querySelector('#bluepost-channel-style');
                                    shared.sheet.disabled = true;
                                    const other = colors(document.querySelector('.context-status'));
                                    shared.sheet.disabled = false;
                                    return other;
                                })(),
                            };
                        }''')
                        self.assertEqual(result['badges'][:3], [list(colors) for colors in expected])
                        self.assertNotIn(result['neutral'], [list(colors) for colors in expected])
                        self.assertEqual(result['badges'][3:], [result['neutral']] * 5)
                        self.assertNotIn(result['other'], result['badges'])
                        self.assertEqual(result['other'], result['otherWithoutPalette'])
                        self.assertFalse(result['overflow'])
            self.assertEqual(requests, [], 'Rendering adds no HTTP requests or external stylesheet dependencies')


if __name__ == '__main__':
    import unittest
    unittest.main()
