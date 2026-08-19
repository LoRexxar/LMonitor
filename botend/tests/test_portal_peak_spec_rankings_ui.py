from pathlib import Path
import unittest
from unittest.mock import Mock

from botend.portal.api import _peak_row_to_dict


class PortalPeakSpecRankingsUIContractTests(unittest.TestCase):
    def test_peak_api_normalizes_raider_slug_to_canonical_aggregate_url(self):
        row = Mock(
            rank=1,
            character_name='Kazzok',
            score=4000,
            score_color='#fff',
            character_path='',
            realm_name='Test',
            rio_region_slug='us',
            class_slug='death-knight',
            spec_slug='blood',
        )

        payload = _peak_row_to_dict(row)

        self.assertEqual(payload['aggregate_url'], '/portal/spec/DeathKnight/Blood/dungeons/')
        self.assertNotIn('profile_url', payload)

    def test_peak_rank_renderer_uses_server_authoritative_aggregate_url(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / 'static/portal/js/main.js').read_text(encoding='utf-8')
        start = source.index('function renderPeakSpecGrid(')
        end = source.index('function renderMplusRuns(', start)
        renderer = source[start:end]

        self.assertIn('const aggregateHref = sanitizeHref(spec?.aggregate_url || "");', renderer)
        self.assertIn('href="${escapeHtml(aggregateHref)}"', renderer)
        self.assertNotIn('profile_url', renderer)
        self.assertNotIn('target="_blank"', renderer)
        self.assertIn('const top3 = filtered.slice(0, 3);', renderer)
        self.assertIn('const rows = top3.map((x) => {', renderer)
        self.assertNotIn('filtered.slice(0, 20)', renderer)
