"""Real collectors/ORM; only HTTP and pacing are replaced by fixture boundaries."""
import copy
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase
from django.utils import timezone

from botend.models import RaiderIOCharacterActivity
from botend.services.player_analysis_recheck import Blocked, require_sources
from botend.services.player_observation_sources import refresh_observation_sources
from botend.services.player_raiderio_activity import mplus_modules_ready, project_player_activity



class MplusModuleReadinessTests(TestCase):
    def setUp(self):
        from botend.tests.test_player_raid_progression import RaidProgressionTests
        RaidProgressionTests.setUp(self)
        RaidProgressionTests.bind(self)
        self.row.payload_json = {}
        self.row.save(update_fields=['payload_json'])
        self.data = copy.deepcopy(self.data)
        self.data['mythic_plus_best_runs'] = []
        self.data['mythic_plus_recent_runs'] = []
        self.data['mythic_plus_dungeon_run_counts'] = []
        self.data['raid_progression']['the-venomous-abyss']['mythic_bosses_killed'] = None

    def collect(self, data=None, status=200):
        response = Mock(status_code=status, headers={}, json=lambda: copy.deepcopy(self.data if data is None else data))
        with patch('requests.Session.get', return_value=response) as http, patch('botend.services.player_observation_sources._pace', return_value=True):
            result = refresh_observation_sources(self.player, progressive=True, refresh_recent=True)
        self.assertLessEqual(http.call_count, 1)
        self.row.refresh_from_db()
        return result

    def test_raid_unknown_keeps_central_partial_but_mplus_is_ready(self):
        result = self.collect()
        require_sources(result)
        self.assertEqual(result['activity']['status'], 'ready')
        self.assertEqual(result['activity']['global_status'], 'partial')
        self.assertEqual(self.row.status, 'partial')
        self.assertIsNone(self.row.payload_json['raid_progression']['the-venomous-abyss'].get('mythic_bosses_killed'))
        raid_view = project_player_activity(self.player)['raid_progression']
        self.assertEqual(raid_view['status'], 'partial')
        self.assertIsNone(next(row for row in raid_view['rows'] if row['name'] == '烈毒之渊')['mythic'])

    def test_raid_partial_second_refresh_reuses_fresh_mplus_without_http(self):
        require_sources(self.collect())
        retry_at = self.row.next_attempt_at
        attempt_at = self.row.last_attempt_at
        with patch('requests.Session.get', side_effect=AssertionError('unexpected HTTP')) as http:
            result = refresh_observation_sources(self.player, progressive=True, refresh_recent=True)
        require_sources(result)
        self.assertEqual(result['activity']['status'], 'fresh')
        self.assertEqual(result['activity']['global_status'], 'partial')
        self.assertEqual(result['activity']['collector_status'], 'backoff')
        self.assertEqual(result['api_requests'], 0)
        http.assert_not_called()
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, 'partial')
        self.assertEqual(self.row.next_attempt_at, retry_at)
        self.assertEqual(self.row.last_attempt_at, attempt_at)

    def test_partial_cache_reuse_rejects_errors_stale_missing_and_new_attempt(self):
        require_sources(self.collect())
        original = RaiderIOCharacterActivity.objects.get(pk=self.row.pk)
        cases = [
            {'last_error': 'http_429'},
            {'last_attempt_at': timezone.now() + timedelta(seconds=1)},
            {'fetched_at': timezone.now() - timedelta(days=2)},
            {'status': 'fetching'}, {'status': 'error'},
        ]
        for field in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_dungeon_run_counts'):
            clocks = copy.deepcopy(original.module_fetched_at)
            clocks[field] = (timezone.now() - timedelta(days=2)).isoformat()
            cases.append({'module_fetched_at': clocks})
            payload = copy.deepcopy(original.payload_json)
            del payload[field]
            cases.append({'payload_json': payload})
        for updates in cases:
            with self.subTest(updates=updates):
                values = {key: getattr(original, key) for key in (
                    'status', 'last_error', 'last_attempt_at', 'fetched_at', 'module_fetched_at', 'payload_json')}
                RaiderIOCharacterActivity.objects.filter(pk=original.pk).update(**{**values, **updates})
                with patch('requests.Session.get', side_effect=AssertionError('unexpected HTTP')) as http:
                    result = refresh_observation_sources(self.player, progressive=True, refresh_recent=True)
                with self.assertRaises(Blocked):
                    require_sources(result)
                http.assert_not_called()
                self.assertEqual(result['api_requests'], 0)

    def test_backoff_reuse_requires_matching_collector_row_and_status(self):
        require_sources(self.collect())
        for state in ({'status': 'backoff'}, {'status': 'backoff', 'activity_id': self.row.pk + 1},
                      *({'status': status, 'activity_id': self.row.pk} for status in
                        ('busy', 'rate_limited', 'invalid_payload', 'unresolved', 'wrong_season'))):
            with self.subTest(state=state), patch(
                    'botend.services.player_observation_sources._ActivityOnlyCollector.refresh', return_value=state), patch(
                    'requests.Session.get', side_effect=AssertionError('unexpected HTTP')) as http:
                result = refresh_observation_sources(self.player, progressive=True)
                with self.assertRaises(Blocked):
                    require_sources(result)
                http.assert_not_called()

    def test_no_central_binding_does_not_block_mplus_or_invent_raid_facts(self):
        from botend.journal_models import JournalState
        JournalState.objects.update(active_release=None)
        result = self.collect()
        require_sources(result)
        self.assertEqual(self.row.status, 'partial')
        self.assertEqual(project_player_activity(self.player)['raid_progression']['status'], 'unresolved')

    def test_missing_module_rejects_even_with_retained_last_good(self):
        require_sources(self.collect())
        for field in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_dungeon_run_counts'):
            with self.subTest(field=field):
                RaiderIOCharacterActivity.objects.filter(pk=self.row.pk).update(next_attempt_at=None)
                data = copy.deepcopy(self.data)
                del data[field]
                result = self.collect(data)
                self.assertIn(field, self.row.payload_json)
                self.assertFalse(mplus_modules_ready(self.row))
                with self.assertRaises(Blocked):
                    require_sources(result)

    def test_invalid_identity_season_modules_and_http_errors_still_block(self):
        require_sources(self.collect())
        invalid = []
        for field in ('mythic_plus_best_runs', 'mythic_plus_recent_runs', 'mythic_plus_dungeon_run_counts'):
            data = copy.deepcopy(self.data); data[field] = 'invalid'; invalid.append(data)
        data = copy.deepcopy(self.data); data['name'] = 'Wrong'; invalid.append(data)
        data = copy.deepcopy(self.data); data['mythic_plus_scores_by_season'] = [{'season': 'season-old'}]; invalid.append(data)
        for data, status in [(d, 200) for d in invalid] + [(self.data, 503), (self.data, 429)]:
            with self.subTest(status=status, data=data):
                RaiderIOCharacterActivity.objects.filter(pk=self.row.pk).update(next_attempt_at=None)
                with self.assertRaises(Blocked):
                    require_sources(self.collect(data, status))
                self.assertFalse(mplus_modules_ready(self.row))
                self.assertTrue(self.row.last_error)
                retry_at = self.row.next_attempt_at
                with patch('requests.Session.get', side_effect=AssertionError('unexpected HTTP')) as http:
                    deferred = refresh_observation_sources(self.player, progressive=True, refresh_recent=True)
                self.assertEqual(deferred['activity']['status'], 'backoff')
                with self.assertRaises(Blocked):
                    require_sources(deferred)
                http.assert_not_called()
                self.row.refresh_from_db()
                self.assertEqual(self.row.next_attempt_at, retry_at)
                self.assertTrue(self.row.last_error)

    def test_uncollected_empty_expired_clock_and_active_lease_not_ready(self):
        require_sources(self.collect())
        for updates in ({'module_fetched_at': {}}, {'payload_json': {}},
                        {'fetched_at': timezone.now() - timedelta(days=2)},
                        {'status': 'fetching'}, {'last_error': 'transport_error'}):
            with self.subTest(updates=updates):
                row = RaiderIOCharacterActivity.objects.get(pk=self.row.pk)
                for key, value in updates.items():
                    setattr(row, key, value)
                self.assertFalse(mplus_modules_ready(row))
        for status, expected in (('partial', 'backoff'), ('fetching', 'busy'), ('error', 'backoff')):
            RaiderIOCharacterActivity.objects.filter(pk=self.row.pk).update(status=status, last_error='transport_error', next_attempt_at=timezone.now() + timedelta(minutes=5))
            result = self.collect()
            self.assertEqual(result['activity']['status'], expected)
            with self.assertRaises(Blocked):
                require_sources(result)
