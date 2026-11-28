from pathlib import Path
from unittest import TestCase

from playwright.sync_api import sync_playwright


class HomeRecoveryPartialTests(TestCase):
    def test_single_rendered_mplus_section_does_not_reload_homepage(self):
        scripts = Path(__file__).resolve().parents[2] / 'static/portal/js'
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for section in ('mplus-cutoffs', 'mplus-rankings'):
                with self.subTest(section=section):
                    page = browser.new_page()
                    page.route('https://home.test/**', lambda r: r.fulfill(
                        content_type='text/html', body=f'<div id="{section}"></div>'))
                    page.goto('https://home.test/')
                    page.add_script_tag(path=str(scripts / 'main.js'))
                    if section == 'mplus-cutoffs':
                        page.evaluate("renderMplusCutoffs('mplus-cutoffs', {items:[{region:'CN',cutoff_1:4200}]})")
                    else:
                        page.evaluate("renderMplusRuns('mplus-rankings', [{dungeon:'test',level:20}])")
                    self.assertEqual(page.locator(f'#{section} tbody tr').count(), 1)
                    attempted = page.evaluate('''src => {
                        let check; const urls = [];
                        new Function('setTimeout', 'location', src)(
                          fn => {check = fn;}, {href:location.href, replace:u=>urls.push(u)});
                        check(); return urls;
                    }''', (scripts / 'home-recovery.js').read_text())
                    self.assertEqual(attempted, [])
                    page.close()
            browser.close()
