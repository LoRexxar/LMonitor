"""Overview cache keeps the public projection, not discarded detail JSON."""
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from botend.services.spec_overview_service import SpecOverviewService as Service


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                     'LOCATION': 'overview-projection-tests'}})
class OverviewProjectionCacheTests(SimpleTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings_override = override_settings(MEDIA_ROOT=self.temp.name)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.season = SimpleNamespace(id=99, mplus_encounters=[{'id': 11}], raid_encounters=[{'id': 22}])
        active = patch('botend.services.spec_overview_service.SpecStatsService.get_active_season',
                       return_value=self.season)
        active.start()
        self.addCleanup(active.stop)
        cache.clear()
        self.addCleanup(cache.clear)

    def write(self, module, payload):
        path = Path(self.temp.name) / 'aggregated' / str(self.season.id) / 'Mage' / 'Fire' / Service.FILES[module]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def payload(self, module):
        detail = {'last_updated': '2026-09-08T12:00:00Z', 'discarded_detail': ['x' * 1000] * 100}
        if module == 'mythic-plus':
            return {'dungeons': [{'dungeon_id': 11, 'sample_size': 10, 'dps': {'median': 42},
                                  'talent_usage': detail}]}
        zone = {'zone_id': 1, 'bosses': [{'boss_id': 22, 'sample_size': 20, 'talent_usage': detail}]}
        return {'zone_groups': [zone], 'difficulties': [{'difficulty': 5, 'label': 'Mythic',
                                                       'zone_groups': [zone]}]}

    def test_cache_compacts_details_and_resolves_nested_timestamp_only_on_miss(self):
        for module, method in [('mythic-plus', Service.mythic_plus), ('raid', Service.raid)]:
            with self.subTest(module=module):
                self.write(module, self.payload(module))
                with patch.object(Service, '_latest_timestamp', wraps=Service._latest_timestamp) as scan:
                    first = method('Mage', 'Fire')
                    with patch.object(Path, 'open', side_effect=AssertionError('cache hit reopened snapshot')):
                        second = method('Mage', 'Fire')
                    self.assertEqual(first, second)
                    self.assertEqual(first['updated_at'], '2026-09-08T12:00:00Z')
                    self.assertEqual(scan.call_count, 1)
                compact, _ = Service._aggregate(module, 'Mage', 'Fire')
                self.assertNotIn('discarded_detail', json.dumps(compact))
                self.assertLess(len(json.dumps(compact)), 2000)

    def test_atomic_replace_and_encounter_and_season_isolation(self):
        path = self.write('mythic-plus', self.payload('mythic-plus'))
        first = Service.mythic_plus('Mage', 'Fire')
        payload = self.payload('mythic-plus')
        payload['updated_at'] = 'replacement-time'
        replacement = path.with_suffix('.next')
        replacement.write_text(json.dumps(payload), encoding='utf-8')
        mtime = path.stat().st_mtime_ns + 1
        os.utime(replacement, ns=(mtime, mtime))
        os.replace(replacement, path)
        self.assertNotEqual(Service.mythic_plus('Mage', 'Fire'), first)
        self.assertEqual(Service.mythic_plus('Mage', 'Fire')['updated_at'], 'replacement-time')
        self.season.mplus_encounters = [{'id': 12}]
        self.assertEqual(Service.mythic_plus('Mage', 'Fire')['dungeons'], [])
        self.season.mplus_encounters = [{'id': 11}]
        self.season.id = 100
        self.assertEqual(Service.mythic_plus('Mage', 'Fire')['dungeons'], [])

    def test_explicit_timestamp_wins_and_difficulties_do_not_change_fallback(self):
        payload = self.payload('raid')
        payload['updated_at'] = 'explicit-time'
        self.write('raid', payload)
        with patch.object(Service, '_latest_timestamp', side_effect=AssertionError('unneeded scan')):
            self.assertEqual(Service.raid('Mage', 'Fire')['updated_at'], 'explicit-time')
        cache.clear()
        payload.pop('updated_at')
        payload['zone_groups'][0]['bosses'][0].pop('talent_usage')
        payload['difficulties'] = [{'difficulty': 5, 'zone_groups': [{'bosses': [
            {'boss_id': 22, 'last_updated': '2099-01-01'}]}]}]
        path = self.write('raid', payload)
        from datetime import datetime, timezone
        self.assertEqual(Service.raid('Mage', 'Fire')['updated_at'],
                         datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat())

    def test_empty_malformed_and_players_contracts(self):
        for payload in [{}, [], {'dungeons': 1}]:
            cache.clear()
            self.season.mplus_encounters = []
            self.write('mythic-plus', payload)
            self.assertEqual(Service.mythic_plus('Mage', 'Fire')['dungeons'], [])
        self.write('players', {'players': [{'id': 7, 'rank': 1}], 'total': 1})
        player = Service.players('Mage', 'Fire')['players'][0]
        self.assertEqual(player['detail_url'], '/portal/spec/Mage/Fire/player/7/')
