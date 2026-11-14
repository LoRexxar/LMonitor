from bs4 import BeautifulSoup
from django.templatetags.static import static
from django.template.loader import render_to_string
from django.test import SimpleTestCase
from urllib.parse import urlsplit


class GuideTooltipAssetsTests(SimpleTestCase):
    def test_published_guide_shell_loads_shared_item_tooltip_styles_and_controller(self):
        html = render_to_string('portal/class_guide_base.html', {'snapshot_header_slot': 'Header'})
        soup = BeautifulSoup(html, 'html.parser')
        styles = [urlsplit(x['href']).path for x in soup.select('link[rel="stylesheet"][href]')]
        scripts = [urlsplit(x['src']).path for x in soup.select('script[src]')]
        self.assertEqual(styles.count(urlsplit(static('shared/css/wow-item-tooltip.css')).path), 1)
        self.assertEqual(scripts.count(urlsplit(static('shared/js/wow-item-tooltip.js')).path), 1)
