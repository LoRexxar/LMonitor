import json
import re
import tempfile
from datetime import datetime, timezone as dt_timezone
from importlib import import_module
from unittest.mock import MagicMock, patch

from django.apps import apps as django_apps
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from LMonitor import config
from botend.models import PortalNavigationItem
from botend.plugin_sync import monitor_default_wait_time


class MplusDpsRankingAggregationTests(SimpleTestCase):
    def setUp(self):
        self.encounters = [
            {'id': dungeon_id, 'name': f'Dungeon {dungeon_id}', 'short': f'D{dungeon_id}'}
            for dungeon_id in range(1, 9)
        ]
        self.season = {
            'id': 7,
            'season_key': 'test-s2',
            'season_name': 'Test Season 2',
            'mplus_encounters': self.encounters,
        }

    @staticmethod
    def _row(dungeon_id, class_name, spec_name, dps, player, level=10):
        return {
            'dungeon_id': dungeon_id,
            'class_name': class_name,
            'spec_name': spec_name,
            'dps': dps,
            'keystone_level': level,
            'region': 'cn',
            'realm': 'test',
            'character_name': player,
        }

    def test_total_is_sample_weighted_and_requires_all_eight_dungeons(self):
        from botend.services.mplus_dps_rankings_service import build_rankings_payload_from_rows

        rows = []
        for dungeon_id in range(1, 9):
            # Fury: each layer median is 150, so only the 200 row survives.
            rows.extend([
                self._row(dungeon_id, 'Warrior', 'Fury', 100, f'fury-low-{dungeon_id}'),
                self._row(dungeon_id, 'Warrior', 'Fury', 200, f'fury-high-{dungeon_id}'),
            ])
            if dungeon_id == 1:
                # Arcane D1 keeps 500 and 300 (2 samples): min/avg/max = 300/400/500.
                rows.extend([
                    self._row(1, 'mage', 'arcane', 100, 'arcane-low-1'),
                    self._row(1, 'mage', 'arcane', 300, 'arcane-mid-1'),
                    self._row(1, 'mage', 'arcane', 500, 'arcane-high-1'),
                ])
            else:
                # D2-D8 keep one 500 sample each.
                rows.extend([
                    self._row(dungeon_id, 'Mage', 'Arcane', 100, f'arcane-low-{dungeon_id}'),
                    self._row(dungeon_id, 'Mage', 'Arcane', 500, f'arcane-high-{dungeon_id}'),
                ])
            if dungeon_id < 8:
                rows.extend([
                    self._row(dungeon_id, 'Mage', 'Fire', 100, f'fire-low-{dungeon_id}'),
                    self._row(dungeon_id, 'Mage', 'Fire', 350, f'fire-high-{dungeon_id}'),
                ])
            # Healer and unknown identities must never leak into a DPS leaderboard.
            rows.append(self._row(dungeon_id, 'Priest', 'Holy', 999, f'holy-{dungeon_id}'))
            rows.append(self._row(dungeon_id, 'Unknown', 'Mystery', 9999, f'unknown-{dungeon_id}'))

        payload = build_rankings_payload_from_rows(
            season=self.season,
            rows=rows,
            generated_at=datetime(2026, 9, 5, 1, 2, 3, tzinfo=dt_timezone.utc),
        )

        self.assertEqual(payload['season']['key'], 'test-s2')
        self.assertEqual(len(payload['scopes']), 9)
        self.assertEqual(payload['scopes'][0]['key'], 'overall')
        self.assertEqual(payload['method']['sample_cap_per_spec_dungeon'], 100)

        overall = payload['rankings']['overall']
        self.assertEqual([(item['spec_name'], item['rank']) for item in overall], [('Arcane', 1), ('Fury', 2)])
        self.assertNotIn('Fire', [item['spec_name'] for item in overall])
        self.assertNotIn('Holy', [item['spec_name'] for item in overall])
        self.assertEqual(overall[0]['sample_size'], 9)
        self.assertEqual(overall[0]['dungeon_count'], 8)
        self.assertAlmostEqual(overall[0]['lower_dps'], (300 * 2 + 500 * 7) / 9, places=2)
        self.assertAlmostEqual(overall[0]['average_dps'], (400 * 2 + 500 * 7) / 9, places=2)
        self.assertAlmostEqual(overall[0]['highest_dps'], 500, places=2)
        self.assertEqual(overall[0]['detail_url'], '/portal/spec/Mage/Arcane/dungeons/')

        dungeon_one = payload['rankings']['dungeon:1']
        self.assertEqual(dungeon_one[0]['spec_name'], 'Arcane')
        self.assertEqual(dungeon_one[0]['sample_size'], 2)
        self.assertEqual(dungeon_one[0]['lower_dps'], 300)
        self.assertEqual(dungeon_one[0]['average_dps'], 400)
        self.assertEqual(dungeon_one[0]['highest_dps'], 500)

    def test_average_dps_tiers_use_five_percent_bands_from_scope_leader(self):
        from botend.services.mplus_dps_rankings_service import _rank

        averages = (100, 95, 90, 85, 80, 75, 70, 69.99)
        items = [
            {'class_name': 'Mage', 'spec_name': f'Spec {index}', 'average_dps': average}
            for index, average in enumerate(averages, start=1)
        ]

        ranked = _rank(items)

        self.assertEqual([item['tier'] for item in ranked], ['S', 'S', 'A', 'B', 'C', 'D', 'E', 'F'])
        self.assertEqual([item['average_ratio'] for item in ranked], list(averages))

    def test_invalid_season_manifest_is_rejected_before_publishing(self):
        from botend.services.mplus_dps_rankings_service import build_rankings_payload_from_rows

        invalid = dict(self.season)
        invalid['mplus_encounters'] = self.encounters[:7]
        with self.assertRaisesMessage(RuntimeError, '8 个唯一'):
            build_rankings_payload_from_rows(invalid, [])

        duplicate = dict(self.season)
        duplicate['mplus_encounters'] = self.encounters[:7] + [self.encounters[0]]
        with self.assertRaisesMessage(RuntimeError, '8 个唯一'):
            build_rankings_payload_from_rows(duplicate, [])

    def test_duplicate_character_is_counted_once_after_higher_dps_sort(self):
        from botend.services.mplus_dps_rankings_service import (
            build_rankings_payload_from_rows,
            select_representative_rows,
        )

        rows = [
            self._row(1, 'Mage', 'Arcane', 500, 'same-player', level=12),
            self._row(1, 'Mage', 'Arcane', 500, 'same-player', level=12),
            self._row(1, 'Mage', 'Arcane', 400, 'other-player', level=12),
            self._row(1, 'Mage', 'Arcane', 100, 'below-median', level=12),
            self._row(1, 'Mage', 'Arcane', 50, 'also-below-median', level=12),
        ]
        selected = select_representative_rows(rows, max_samples=100)
        self.assertEqual([row['dps'] for row in selected], [500, 400])

        production_rows = list(rows)
        for dungeon_id in range(2, 9):
            production_rows.extend([
                self._row(dungeon_id, 'Mage', 'Arcane', 500, f'high-{dungeon_id}', level=12),
                self._row(dungeon_id, 'Mage', 'Arcane', 100, f'low-{dungeon_id}', level=12),
            ])
        payload = build_rankings_payload_from_rows(self.season, production_rows)
        dungeon_one = payload['rankings']['dungeon:1'][0]
        self.assertEqual(dungeon_one['sample_size'], 2)
        self.assertEqual(dungeon_one['average_dps'], 450)

    def test_atomic_publish_failure_preserves_previous_snapshot(self):
        from botend.services.mplus_dps_rankings_service import atomic_write_payload, snapshot_path

        with tempfile.TemporaryDirectory() as temporary:
            with override_settings(MEDIA_ROOT=temporary):
                path = snapshot_path(self.season['id'])
                path.parent.mkdir(parents=True, exist_ok=True)
                previous = {'season': {'id': self.season['id']}, 'version': 'previous'}
                path.write_text(json.dumps(previous), encoding='utf-8')

                with patch(
                    'botend.services.mplus_dps_rankings_service.os.replace',
                    side_effect=OSError('replace failed'),
                ):
                    with self.assertRaises(OSError):
                        atomic_write_payload(path, {'version': 'new'})

                self.assertEqual(json.loads(path.read_text(encoding='utf-8')), previous)
                self.assertEqual(list(path.parent.glob('.tmp-mplus-dps-*.json')), [])

    def test_read_path_never_falls_back_to_request_thread_aggregation(self):
        from botend.services.mplus_dps_rankings_service import get_current_mplus_dps_rankings_payload

        season = type('Season', (), {'id': self.season['id']})()
        with (
            patch('botend.services.mplus_dps_rankings_service.active_season', return_value=season),
            patch(
                'botend.services.mplus_dps_rankings_service.load_published_mplus_dps_rankings',
                return_value=None,
            ),
            patch(
                'botend.services.mplus_dps_rankings_service.build_current_mplus_dps_rankings_payload'
            ) as build,
        ):
            with self.assertRaisesMessage(RuntimeError, '快照尚未生成'):
                get_current_mplus_dps_rankings_payload()
        build.assert_not_called()


class MplusDpsRankingRouteTests(TestCase):
    def test_page_and_api_are_new_independent_routes(self):
        payload = {
            'season': {'id': 7, 'key': 'test-s2', 'name': 'Test Season 2'},
            'generated_at': '2026-09-05T01:02:03+00:00',
            'scopes': [{'key': 'overall', 'label': '总计', 'dungeon_id': None}],
            'rankings': {'overall': []},
            'method': {},
        }
        page = self.client.get(reverse('portal_mplus_dps_rankings'))
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, '大秘境 DPS 榜单')
        self.assertContains(page, 'mplus-dps-rankings.js')
        html = page.content.decode('utf-8')
        css_version = re.search(r'mplus-dps-rankings\.css\?v=([^"\']+)', html)
        js_version = re.search(r'mplus-dps-rankings\.js\?v=([^"\']+)', html)
        self.assertIsNotNone(css_version)
        self.assertIsNotNone(js_version)
        self.assertEqual(css_version.group(1), js_version.group(1))

        with patch('botend.portal.api.get_current_mplus_dps_rankings_payload', return_value=payload):
            response = self.client.get(reverse('portal_mplus_dps_rankings_api'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), payload)

        with patch(
            'botend.portal.api.get_current_mplus_dps_rankings_payload',
            side_effect=RuntimeError('大秘境 DPS 榜单快照尚未生成'),
        ):
            unavailable = self.client.get(reverse('portal_mplus_dps_rankings_api'))
        self.assertEqual(unavailable.status_code, 503)

        self.assertEqual(self.client.get(reverse('portal_specs')).status_code, 200)

    def test_navigation_migration_is_idempotent_and_reversible(self):
        migration = import_module('botend.migrations.0203_mplus_dps_rankings_navigation')
        migration.add_mplus_dps_navigation(django_apps, None)
        migration.add_mplus_dps_navigation(django_apps, None)
        item_filter = {'group__key': 'data', 'url': '/portal/mplus/dps-rankings/'}
        self.assertEqual(PortalNavigationItem.objects.filter(**item_filter).count(), 1)

        migration.remove_mplus_dps_navigation(django_apps, None)
        self.assertEqual(PortalNavigationItem.objects.filter(**item_filter).count(), 0)
        migration.add_mplus_dps_navigation(django_apps, None)
        self.assertEqual(PortalNavigationItem.objects.filter(**item_filter).count(), 1)


class MplusDpsRankingMonitorContractTests(SimpleTestCase):
    def test_monitor_is_registered_as_hourly_without_reindexing_existing_plugins(self):
        self.assertEqual(monitor_default_wait_time('SpecDungeonDpsRankingMonitor'), 3600)
        self.assertEqual(config.Monitor_Type_BaseObject_List[-1].__name__, 'SpecDungeonDpsRankingMonitor')

    def test_monitor_publishes_snapshot_and_updates_flag(self):
        from botend.controller.plugins.portal.SpecDungeonDpsRankingMonitor import SpecDungeonDpsRankingMonitor

        task = MagicMock()
        payload = {
            'season': {'id': 7, 'key': 'test-s2'},
            'scopes': [{'key': 'overall'}],
            'rankings': {'overall': [{'spec_name': 'Arcane'}]},
        }
        with patch(
            'botend.controller.plugins.portal.SpecDungeonDpsRankingMonitor.publish_current_mplus_dps_rankings',
            return_value=payload,
        ) as publish:
            self.assertTrue(SpecDungeonDpsRankingMonitor(None, task).scan(''))

        publish.assert_called_once_with()
        self.assertTrue(task.flag.startswith('test-s2@specs=1@'))
        task.save.assert_called_once_with(update_fields=['flag'])
