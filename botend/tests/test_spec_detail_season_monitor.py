from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from botend.controller.plugins.portal.SpecDetailSeasonMonitor import SpecDetailSeasonMonitor


class DummyTask:
    flag = ''

    def save(self):
        pass


class SpecDetailSeasonMonitorActivationTests(SimpleTestCase):
    def _monitor_for_season(self, season_key):
        monitor = SpecDetailSeasonMonitor(req=None, task=DummyTask())
        monitor._fetch_wcl_zones = lambda: [
            {'id': 100, 'name': 'Mythic+ Season Test'},
            {'id': 200, 'name': 'Test Raid'},
        ]
        monitor._find_latest_mplus_zone = lambda zones: {'id': 100, 'name': 'Mythic+ Season Test'}
        monitor._fetch_rio_season = lambda: f'season-{season_key.split("-s")[0]}-{season_key.split("-s")[1]}'
        monitor._find_all_raid_zones = lambda zones, season_key=None: [{'id': 200, 'name': 'Test Raid'}]
        monitor._fetch_encounters = lambda zone_id: [{'id': zone_id + 1, 'name': f'enc-{zone_id}'}]
        monitor._fetch_wcl_partition = lambda zone_id: 9
        return monitor

    def _mock_manager(self, existing):
        manager = MagicMock()
        filter_result = MagicMock()
        filter_result.first.return_value = existing
        manager.filter.return_value = filter_result
        manager.update_or_create.return_value = (SimpleNamespace(is_active=bool(existing and existing.is_active)), existing is None)
        return manager

    @patch('botend.controller.plugins.portal.SpecDetailSeasonMonitor.SeasonMeta')
    def test_new_detected_season_is_staged_inactive_and_does_not_deactivate_current(self, season_meta):
        season_meta.objects = self._mock_manager(existing=None)

        monitor = self._monitor_for_season('mn-s1')
        self.assertTrue(monitor.scan(''))

        season_meta.objects.filter.assert_called_once_with(season_key='mn-s1')
        _, kwargs = season_meta.objects.update_or_create.call_args
        self.assertEqual(kwargs['season_key'], 'mn-s1')
        self.assertFalse(kwargs['defaults']['is_active'])
        self.assertEqual(kwargs['defaults']['rio_season'], 'season-mn-1')
        # 不再执行 SeasonMeta.objects.filter(is_active=True).update(is_active=False)
        self.assertFalse(season_meta.objects.filter.return_value.update.called)

    @patch('botend.controller.plugins.portal.SpecDetailSeasonMonitor.SeasonMeta')
    def test_existing_active_season_stays_active_when_metadata_refreshes(self, season_meta):
        season_meta.objects = self._mock_manager(existing=SimpleNamespace(is_active=True))

        monitor = self._monitor_for_season('mn-s1')
        self.assertTrue(monitor.scan(''))

        _, kwargs = season_meta.objects.update_or_create.call_args
        self.assertTrue(kwargs['defaults']['is_active'])
        self.assertEqual(kwargs['defaults']['season_name'], 'Mythic+ Season Test')
