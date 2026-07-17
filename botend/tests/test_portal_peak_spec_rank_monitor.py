from datetime import datetime, timezone as dt_timezone
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalPeakSpecRankMonitor import PortalPeakSpecRankMonitor


class PortalPeakSpecRankMonitorSeasonTests(SimpleTestCase):
    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.timezone.now')
    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get')
    def test_resolve_season_uses_started_current_season_not_first_future_season(self, get, now):
        now.return_value = datetime(2026, 7, 17, tzinfo=dt_timezone.utc)
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            'seasons': [
                {'slug': 'season-mn-2', 'starts': {'us': '2026-12-16T15:00:00Z'}, 'ends': {'us': '2030-01-01T00:00:00Z'}},
                {'slug': 'season-mn-1-break-the-meta', 'starts': {'us': '2026-07-14T15:00:00Z'}, 'ends': {'us': '2026-07-21T15:00:00Z'}},
                {'slug': 'season-mn-1', 'starts': {'us': '2026-03-24T15:00:00Z'}, 'ends': {'us': '2026-12-16T15:00:00Z'}},
            ]
        }

        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-1')

    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.timezone.now')
    @patch('botend.controller.plugins.portal.PortalPeakSpecRankMonitor.requests.get')
    def test_resolve_season_accepts_naive_api_datetimes(self, get, now):
        now.return_value = datetime(2026, 7, 17, tzinfo=dt_timezone.utc)
        get.return_value.status_code = 200
        get.return_value.json.return_value = {
            'seasons': [
                {'slug': 'season-mn-1', 'starts': {'us': '2026-03-24T15:00:00'}, 'ends': {'us': '2026-12-16T15:00:00'}},
            ]
        }

        monitor = PortalPeakSpecRankMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-1')

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