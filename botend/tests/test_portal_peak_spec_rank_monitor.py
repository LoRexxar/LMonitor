from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalPeakSpecRankMonitor import PortalPeakSpecRankMonitor


class PortalPeakSpecRankMonitorSeasonTests(SimpleTestCase):
    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.SeasonMeta.objects.filter')
    def test_resolve_season_uses_active_season_metadata_rio_season(self, season_filter):
        season_filter.return_value.first.return_value = Mock(rio_season='season-mn-2')
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-2')
        season_filter.assert_called_once_with(is_active=True)

    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.SeasonMeta.objects.filter')
    def test_resolve_season_returns_empty_when_active_metadata_has_no_rio_season(self, season_filter):
        season_filter.return_value.first.return_value = Mock(rio_season='')
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), '')

    @patch.object(PortalPeakSpecRankMonitor, '_fetch_and_upsert')
    @patch.object(PortalPeakSpecRankMonitor, '_resolve_season', return_value='')
    def test_scan_fails_without_metadata_season_and_does_not_fetch_rankings(self, resolve_season, fetch):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertFalse(monitor.scan(''))
        fetch.assert_not_called()

    def test_empty_rankings_are_failure_and_do_not_replace_current_rows(self):
        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())
        response = Mock(status_code=200)
        response.json.return_value = {'rankings': {'rankedCharacters': []}}

        with patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get', return_value=response), \
                patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.PortalPeakSpecRankRow.objects') as objects:
            ok = monitor._fetch_and_upsert(
                season='season-mn-1', region='world', class_slug='death-knight', spec_slug='blood'
            )

        self.assertFalse(ok)
        objects.filter.assert_not_called()