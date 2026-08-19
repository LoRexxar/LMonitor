from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.PortalMplusRunMonitor import PortalMplusRunMonitor
from botend.portal.api import _mplus_to_dict


class PortalMplusRunMonitorSeasonTests(SimpleTestCase):
    @patch('botend.controller.plugins.portal.PortalMplusRunMonitor.SeasonMeta.objects.filter')
    def test_uses_active_season_metadata_instead_of_s1_literal(self, season_filter):
        season_filter.return_value.first.return_value = SimpleNamespace(rio_season='season-mn-2')
        monitor = PortalMplusRunMonitor(Mock(), Mock())

        self.assertEqual(monitor._resolve_season(), 'season-mn-2')
        season_filter.assert_called_once_with(is_active=True)

    def test_top_run_uses_chinese_dungeon_name_from_shared_catalog(self):
        run = SimpleNamespace(
            rank=1,
            dungeon='Voidscar Arena',
            dungeon_slug='voidscar-arena',
            level=22,
            time_seconds=1200,
            score=400.0,
            party_json=None,
            dps_json=None,
            tank='',
            healer='',
            run_url='',
            source='raiderio',
            season='season-mn-2',
            region='world',
        )

        self.assertEqual(_mplus_to_dict(run)['dungeon_cn'], '虚痕竞技场')
