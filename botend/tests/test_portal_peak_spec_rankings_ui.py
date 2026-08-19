from pathlib import Path
import unittest


class PortalPeakSpecRankingsUIContractTests(unittest.TestCase):
    def test_peak_rank_entries_link_to_the_matching_internal_mplus_aggregate(self):
        root = Path(__file__).resolve().parents[2]
        source = (root / 'static/portal/js/main.js').read_text(encoding='utf-8')
        start = source.index('function renderPeakSpecGrid(')
        end = source.index('function renderMplusRuns(', start)
        renderer = source[start:end]

        self.assertIn(
            'const aggregateHref = `/portal/spec/${encodeURIComponent(classSlug)}/${encodeURIComponent(specSlug)}/dungeons/`;',
            renderer,
        )
        self.assertIn('href="${escapeHtml(aggregateHref)}"', renderer)
        self.assertNotIn('profile_url', renderer)
        self.assertNotIn('target="_blank"', renderer)
