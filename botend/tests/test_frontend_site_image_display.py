"""SSR exact-match and Chromium preload regression. No real image requests."""
import json
from pathlib import Path
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

ROOT = Path(__file__).resolve().parents[2]
UP = 'https://wow.zamimg.com/images/wow/icons/small/spell_fire.jpg'
MIRROR = 'https://oss.wowdaily.cn/wow_icons_oss/small/spell_fire.jpg'


class FrontendImageDisplayTests(SimpleTestCase):
    @patch('botend.services.site_image_assets._published', return_value={UP: MIRROR})
    def test_first_html_uses_published_avatar_but_not_navigation(self, _):
        profile = {'avatar': UP, 'name': 'A', 'links': [{'url': UP, 'label': 'source'}]}
        html = render_to_string('shared/class_guide_author.html', {'author_profile': profile})
        self.assertIn(f'src="{MIRROR}"', html)
        self.assertIn(f'href="{UP}"', html)
        profile['avatar'] += '?unpublished=1'
        html = render_to_string('shared/class_guide_author.html', {'author_profile': profile})
        self.assertNotIn('<img', html)
        self.assertIn(f'href="{UP}"', html)

    def test_rich_preview_maps_before_request_without_changing_text_or_links(self):
        if sync_playwright is None:
            self.skipTest('optional Chromium UI regression')
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            requests = []
            def intercept(route):
                requests.append(route.request.url)
                route.fulfill(content_type='image/svg+xml', body='<svg xmlns="http://www.w3.org/2000/svg"/>')
            page.route('**/*', intercept)
            page.set_content('<script type="application/json" id="published-image-map">' + json.dumps({UP: MIRROR}) + '</script><main></main>')
            page.add_script_tag(path=str(ROOT / 'static/shared/js/site-images.js'))
            candidates = f'data:image/png;base64,AAAA 1x, {UP} 2x'
            html = f'''<a href="{UP}">{UP}</a><picture><source srcset="{UP}, {UP} 2x"><img src="{UP}" srcset="{candidates}"></picture>
<svg xmlns:xlink="http://www.w3.org/1999/xlink"><image href="{UP}" xlink:href="{UP}"/></svg>
<div style="width:12px;height:12px;background-image:url('{UP}')!important"></div>'''
            page.evaluate('s => document.querySelector("main").innerHTML = window.publishedImageHtml(s)', html)
            page.wait_for_load_state('networkidle')
            self.assertEqual(page.locator('a').get_attribute('href'), UP)
            self.assertEqual(page.locator('a').inner_text(), UP)
            self.assertEqual(page.locator('img').get_attribute('srcset'), candidates.replace(UP, MIRROR))
            self.assertEqual(page.locator('source').get_attribute('srcset'), f'{MIRROR}, {MIRROR} 2x')
            for name in ('href', 'xlink:href'):
                self.assertEqual(page.locator('image').get_attribute(name), MIRROR)
            self.assertIn(MIRROR, page.locator('div').get_attribute('style'))
            self.assertEqual(set(requests), {MIRROR})
            browser.close()
