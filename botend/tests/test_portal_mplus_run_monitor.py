from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalMplusRunMonitor import PortalMplusRunMonitor


class PortalMplusRunMonitorSeasonTests(SimpleTestCase):
    @patch('botend.controller.plugins.portal.PortalMplusRunMonitor.SeasonMeta.objects.filter')
    def test_uses_active_season_metadata_instead_of_s1_literal(self, season_filter):
        season_filter.return_value.first.return_value = SimpleNamespace(rio_season='season-mn-2')
        monitor = PortalMplusRunMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-2')
        season_filter.assert_called_once_with(is_active=True)
