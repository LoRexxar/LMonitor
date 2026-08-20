from pathlib import Path
from django.test import SimpleTestCase


class PortalMythicstatsDpsUiContractsTests(SimpleTestCase):
    def test_dps_rows_prioritize_rank_and_relative_average_over_bright_class_bars(self):
        source = (Path(__file__).resolve().parents[2] / 'static/portal/js/main.js').read_text(encoding='utf-8')

        self.assertNotIn('leaderAvg', source)
        self.assertIn('mythicstats-rank ${rankClass}', source)
        self.assertNotIn('距榜首', source)
        self.assertNotIn('榜首 · 100%', source)
        self.assertNotIn('relativeLabel', source)
        self.assertIn('mythicstats-dps-bar-average', source)

        self.assertIn('--mythicstats-accent-deep', source)
        self.assertNotIn('mythicstatsHexToRgba(color, 0.92)', source)

    def test_landing_page_cache_busts_portal_stylesheet(self):
        template = (Path(__file__).resolve().parents[2] / 'templates/portal/index.html').read_text(encoding='utf-8')

        self.assertRegex(template, r"portal/css/portal\.css' %}\?v=[^\"']+")
