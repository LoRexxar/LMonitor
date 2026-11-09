"""Due-only maintenance: real ORM, collector and parser; HTTP fixtures only."""
import copy
import json
from datetime import timedelta
from unittest.mock import patch

import requests
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import MonitorTask, PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_raiderio_activity import (
    ActivityCollector, MODULES, TTL, player_identity, refresh_due_top20_activity,
)
from botend.tests.test_player_raiderio_activity import activity_facts, expire_activity_modules


class FixtureHTTP:
    """Exercise ActivityCollector's actual HTTP response handling/publication."""
    def __init__(self, status=200, partial=False, bad_identity=False):
        self.calls = []
        self.status = status
        self.partial = partial
        self.bad_identity = bad_identity
        self.best_runs = []
        self.roster_status = 200
        self.roster_data = None
        self.failure = None
        self.on_response = None

    def __call__(self, url, **kwargs):
        self.calls.append((url, copy.deepcopy(kwargs)))
        if self.failure:
            raise self.failure
        if url == 'https://raider.io/api/v1/mythic-plus/run-details':
            assert kwargs['params'] == {'season': 'season-mn-2', 'id': 123}
            response = requests.Response()
            response.status_code = self.roster_status
            response.headers['Retry-After'] = '900'
            response._content = json.dumps(self.roster_data).encode()
            return response
        assert url == 'https://raider.io/api/v1/characters/profile', url
        params = kwargs['params']
        assert 'gear' not in params['fields'] and 'talents' not in params['fields']
        name = 'Wrong' if self.bad_identity else params['name']
        data = {'name': name, 'region': params['region'], 'realm': 'Test Realm',
                'profile_url': f'https://raider.io/characters/{params["region"]}/{params["realm"]}/{name}',
                'last_crawled_at': '2026-10-10T13:00:00Z',
                'mythic_plus_scores_by_season': [{'season': 'season-mn-2'}],
                'mythic_plus_best_runs': self.best_runs, 'mythic_plus_recent_runs': [],
                'mythic_plus_dungeon_run_counts': [], 'raid_progression': {},
                'gear': {'items': {'head': {'id': 999}}}, 'talents': [{'id': 999}]}
        if self.partial:
            del data['mythic_plus_recent_runs']
        if self.on_response:
            self.on_response()
        response = requests.Response()
        response.status_code = self.status
        response.headers['Retry-After'] = '900'
        response._content = json.dumps(data).encode()
        return response


class DueTop20ActivityTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.http = FixtureHTTP()
        self.collector = ActivityCollector(transport=self.http)

    def player(self, name='Tester', spec='Holy', rank=1, **kwargs):
        return PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
            realm='Test Realm', character_name=name, class_name='Priest', spec_name=spec,
            rank=rank, score=4000, gear_json=[{'id': 123}], talents_json=[{'id': 7}],
            stats_json={'haste': 999}, last_updated=self.now,
            profile_url=f'https://raider.io/characters/eu/test-realm/{name}', **kwargs)

    def activity(self, player, **kwargs):
        defaults = dict(status='ready', fetched_at=self.now - TTL - timedelta(minutes=1),
                        next_attempt_at=self.now - timedelta(minutes=1))
        defaults.update(kwargs)
        return RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(player), **defaults)

    def run_due(self, **kwargs):
        return refresh_due_top20_activity(self.season, collector=self.collector, **kwargs)

    def test_fresh_during_daily_becomes_due_in_day_without_profile_changes(self):
        player = self.player()
        fetched = self.now - timedelta(hours=23)
        row = self.activity(player, fetched_at=fetched, next_attempt_at=fetched + TTL)
        before = PlayerSpecTopPlayer.objects.values().get(pk=player.pk)
        with patch('botend.services.player_raiderio_activity.timezone.now', return_value=self.now):
            first = self.run_due()
        self.assertEqual((first['attempted'], first['fresh']), (0, 1))
        self.assertEqual(self.http.calls, [])
        with patch('botend.services.player_raiderio_activity.timezone.now', return_value=self.now + timedelta(hours=2)):
            second = self.run_due()
        self.assertEqual((second['attempted'], second['ready'], second['remaining_due']), (1, 1, 0))
        row.refresh_from_db()
        self.assertEqual(row.fetched_at, self.now + timedelta(hours=2))
        self.assertEqual(PlayerSpecTopPlayer.objects.values().get(pk=player.pk), before)
        self.assertNotIn('gear', row.payload_json)
        self.assertNotIn('talents', row.payload_json)
        self.assertEqual(len(self.http.calls), 1)

    def test_missing_activity_is_created_once_across_specs(self):
        player = self.player()
        self.player(name='TESTER', spec='Discipline')
        result = self.run_due()
        self.assertEqual((result['attempted'], result['ready']), (1, 1))
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 1)
        self.assertEqual(RaiderIOCharacterActivity.objects.get().identity_digest,
                         activity_identity_digest(**player_identity(player)))

    def test_future_ready_error_and_live_lease_never_call_collector_or_http(self):
        for name, status in [('Fresh', 'ready'), ('Error', 'error'), ('Lease', 'fetching')]:
            self.activity(self.player(name), status=status, next_attempt_at=self.now + timedelta(minutes=2))
        with patch.object(self.collector, 'refresh', side_effect=AssertionError('must prefilter')):
            with CaptureQueriesContext(connection) as queries:
                result = self.run_due()
        self.assertEqual((result['attempted'], result['fresh'], result['busy'], result['backoff']), (0, 1, 1, 1))
        self.assertEqual(self.http.calls, [])
        selects = [q['sql'] for q in queries if q['sql'].startswith('SELECT')]
        self.assertFalse(any(any(field in sql for field in ('payload_json', 'gear_json', 'talents_json', 'stats_json')) for sql in selects))
        activity_sql = [sql for sql in selects if 'wow_raiderio_character_activity' in sql]
        self.assertTrue(activity_sql)
        for sql in activity_sql:
            self.assertIn('identity_digest', sql.split('WHERE')[1])
            self.assertIn('season_id', sql.split('WHERE')[1])

    def test_due_error_and_expired_lease_retry_normally(self):
        for name, status in [('Error', 'error'), ('Lease', 'fetching')]:
            self.activity(self.player(name), status=status, last_error='transport_error')
        result = self.run_due()
        self.assertEqual((result['attempted'], result['ready'], result['error']), (2, 2, 0))
        self.assertEqual(len(self.http.calls), 2)
        self.assertFalse(RaiderIOCharacterActivity.objects.exclude(last_error='').exists())

    def test_snapshot_races_are_rechecked_by_original_collector(self):
        for status, expected in [('fetching', 'busy'), ('error', 'backoff'), ('ready', 'fresh_race')]:
            with self.subTest(status=status):
                RaiderIOCharacterActivity.objects.all().delete()
                PlayerSpecTopPlayer.objects.all().delete()
                row = self.activity(self.player())
                original = self.collector.refresh
                def race(player, season, **kwargs):
                    self.assertEqual(kwargs, {'force': False, 'include_profile': False})
                    RaiderIOCharacterActivity.objects.filter(pk=row.pk).update(
                        status=status, next_attempt_at=self.now + timedelta(minutes=2))
                    return original(player, season, **kwargs)
                with patch.object(self.collector, 'refresh', side_effect=race):
                    result = self.run_due()
                self.assertEqual(result[expected], 1)
                self.assertEqual(result['remaining_due'], 0)
        self.assertEqual(self.http.calls, [])

    def test_limit_oldest_due_across_specs_then_identity_not_pk(self):
        newer = self.player('Newer')
        oldest = self.player('Oldest', spec='Shadow')
        self.player('OLDEST', spec='Discipline')
        self.activity(newer, next_attempt_at=self.now - timedelta(minutes=1))
        self.activity(oldest, next_attempt_at=self.now - timedelta(hours=5))
        result = self.run_due(limit=1)
        self.assertEqual(self.http.calls[0][1]['params']['name'], 'Oldest')
        self.assertEqual((result['due'], result['attempted'], result['remaining_due']), (2, 1, 1))
        result = self.run_due(limit=1)
        self.assertEqual(self.http.calls[1][1]['params']['name'], 'Newer')
        self.assertEqual(result['remaining_due'], 0)

    def test_missing_equal_priority_is_identity_stable(self):
        players = [self.player('Zulu'), self.player('Alpha', spec='Shadow')]
        expected = min(players, key=lambda p: activity_identity_digest(**player_identity(p)))
        self.run_due(limit=1)
        self.assertEqual(self.http.calls[0][1]['params']['name'], expected.character_name)

    def test_non_top20_old_season_unknown_spec_and_anonymous_excluded(self):
        for rank in (None, 0, 21):
            self.player(f'Rank{rank}', rank=rank)
        self.player('Unknown', spec='Unknown')
        old = self.player('Old')
        PlayerSpecTopPlayer.objects.filter(pk=old.pk).update(season_id=self.season.pk + 100)
        anon = self.player('Anonymous')
        PlayerSpecTopPlayer.objects.filter(pk=anon.pk).update(realm='anonymous', profile_url='')
        result = self.run_due()
        self.assertEqual((result['attempted'], result['unresolved']), (0, 1))
        self.assertEqual(self.http.calls, [])
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 0)

    def test_429_stops_batch_without_writing_unattempted_errors(self):
        for name in ('One', 'Two', 'Three'):
            self.player(name)
        self.http.status = 429
        result = self.run_due()
        self.assertEqual((result['attempted'], result['error'], result['remaining_due']), (1, 1, 2))
        self.assertTrue(result['stopped'])
        self.assertEqual(result['stop'], 'rate_limited')
        self.assertEqual(len(self.http.calls), 1)
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 1)
        row = RaiderIOCharacterActivity.objects.get()
        self.assertEqual(row.last_error, 'rate_limited')
        self.assertGreater(row.next_attempt_at, self.now + timedelta(minutes=10))

    def test_failure_keeps_lastgood_and_partial_is_not_ready(self):
        player = self.player()
        good = {field: ([] if field != 'raid_progression' else {}) for field in MODULES.values()}
        row = self.activity(player, payload_json=good)
        self.http.bad_identity = True
        result = self.run_due()
        self.assertEqual((result['error'], result['ready']), (1, 0))
        row.refresh_from_db()
        self.assertEqual(activity_facts(row), good)
        self.assertEqual(row.last_error, 'invalid_payload')
        self.assertEqual(row.fetched_at, self.now - TTL - timedelta(minutes=1))
        self.http.bad_identity = False
        self.http.partial = True
        expire_activity_modules(row)
        result = self.run_due()
        self.assertEqual((result['partial'], result['ready']), (1, 0))
        row.refresh_from_db()
        self.assertEqual(row.status, 'partial')
        self.assertEqual(row.payload_json['mythic_plus_recent_runs'], [])

    def test_http403_and_transport_failure_remain_errors_and_backoff(self):
        player = self.player()
        row = self.activity(player, payload_json={'lastgood': ['kept']})
        for state in ('http_403', 'transport_error'):
            with self.subTest(state=state):
                row.refresh_from_db()
                expire_activity_modules(row)
                self.http.status = 403 if state == 'http_403' else 200
                self.http.failure = requests.ConnectionError('fixture disconnect') if state == 'transport_error' else None
                result = self.run_due()
                self.assertEqual((result['error'], result['ready'], result['states']), (1, 0, {state: 1}))
                row.refresh_from_db()
                self.assertEqual(row.last_error, state)
                self.assertEqual(activity_facts(row), {'lastgood': ['kept']})
                self.assertGreater(row.next_attempt_at, self.now)
                calls = len(self.http.calls)
                self.assertEqual(self.run_due()['backoff'], 1)
                self.assertEqual(len(self.http.calls), calls)

    def test_collector_publication_cas_rejects_winner_during_http(self):
        row = self.activity(self.player())
        winner = {'winner': ['new-facts']}
        def publish_winner():
            RaiderIOCharacterActivity.objects.filter(pk=row.pk).update(status='ready',
                last_attempt_at=self.now + timedelta(seconds=10), payload_json=winner,
                next_attempt_at=self.now + TTL)
        self.http.on_response = publish_winner
        result = self.run_due()
        self.assertEqual(result['states'], {'superseded': 1})
        self.assertEqual(result['ready'], 0)
        row.refresh_from_db()
        self.assertEqual(row.payload_json, winner)
        self.assertEqual(row.last_attempt_at, self.now + timedelta(seconds=10))

    def test_real_roster_maintenance_privacy_and_429_keep_module_outcomes_separate(self):
        from botend.models import RaiderIORunRoster
        from botend.tests.test_player_raiderio_activity import payload
        from botend.tests.test_raiderio_run_roster import details
        player = self.player()
        run = payload()['mythic_plus_best_runs'][0]
        self.http.best_runs = [run]
        self.http.roster_data = details()
        result = self.run_due()
        self.assertEqual((result['ready'], result['roster_error']), (1, 0))
        roster = RaiderIORunRoster.objects.get()
        self.assertTrue(roster.roster_visible)
        self.assertEqual(len(roster.payload_json['roster']), 5)
        lastgood = copy.deepcopy(roster.payload_json)
        activity = RaiderIOCharacterActivity.objects.get()
        for roster_status in (403, 200, 429):
            activity.refresh_from_db()
            expire_activity_modules(activity)
            RaiderIOCharacterActivity.objects.filter(pk=activity.pk).update(next_attempt_at=self.now - timedelta(seconds=2))
            RaiderIORunRoster.objects.filter(pk=roster.pk).update(next_attempt_at=self.now)
            self.http.roster_status = roster_status
            if roster_status == 200:
                self.http.roster_data['runPrivacyMode'] = 'private'
            if roster_status == 429:
                other = self.player('Unattempted', spec='Shadow')
                unattempted = self.activity(other, next_attempt_at=self.now - timedelta(seconds=1))
            result = self.run_due()
            self.assertEqual(result['ready'], 1)
            self.assertEqual(result['error'], 0)
            self.assertEqual(result['roster_error'], int(roster_status != 200))
            roster.refresh_from_db()
            self.assertEqual(roster.payload_json, lastgood)
            if roster_status == 200:
                self.assertFalse(roster.roster_visible)
            if roster_status == 429:
                self.assertTrue(result['stopped'])
                self.assertEqual(result['stop'], 'rate_limited')
                self.assertEqual((result['attempted'], result['remaining_due']), (1, 1))
                unattempted.refresh_from_db()
                self.assertIsNone(unattempted.last_attempt_at)
                self.assertEqual(unattempted.last_error, '')
                self.assertFalse(roster.roster_visible)  # error cannot revive private facts

    def raid_http(self, *, status=200, private=False):
        from botend.tests.test_player_raid_observation_sources import fixtures, API, CATALOG
        html, catalog, details, _ = fixtures()
        base = 'https://raider.io/characters/eu/test-realm/tester'
        api = base.replace('/characters/', '/api/characters/') + '?season=season-mn-2&tier=36'
        html = html.replace(API.replace('&', '&amp;'), api.replace('&', '&amp;'))
        details['characterDetails']['isPrivate'] = private
        details['characterDetails']['character'].update(name='Tester', region={'slug': 'eu'},
            realm={'slug': 'test-realm'}, path='/characters/eu/test-realm/tester')
        def transport(url, **kwargs):
            if url == 'https://raider.io/api/v1/characters/profile':
                return self.http(url, **kwargs)
            self.http.calls.append((url, copy.deepcopy(kwargs)))
            response = requests.Response()
            response.status_code = status
            response.headers['Retry-After'] = '2700'
            if url == base:
                response._content = html.encode()
            else:
                self.assertIn(url, (CATALOG, api))
                response._content = json.dumps(catalog if url == CATALOG else details).encode()
            return response
        return transport

    def test_private_raid_is_valid_nonpublic_outcome_not_monitor_failure(self):
        from botend.controller.plugins.portal.PlayerRaiderIOActivityMonitor import PlayerRaiderIOActivityMonitor
        row = self.activity(self.player(), payload_json={'raid_first_kills': {}})
        task = MonitorTask.objects.create(name='PlayerRaiderIOActivityMonitor', type=0, target='')
        with patch('requests.get', self.raid_http()), patch('django.utils.timezone.now', return_value=self.now):
            self.assertTrue(PlayerRaiderIOActivityMonitor(None, task).scan(''))
            self.assertTrue(PlayerRaiderIOActivityMonitor(None, task).scan(''))
        row.refresh_from_db()
        self.assertTrue(row.payload_json['raid_first_kills']['visible'])
        later = self.now + timedelta(days=2)
        with patch('requests.get', self.raid_http(private=True)), patch('django.utils.timezone.now', return_value=later):
            self.assertTrue(PlayerRaiderIOActivityMonitor(None, task).scan(''))
            self.assertTrue(PlayerRaiderIOActivityMonitor(None, task).scan(''))
        row.refresh_from_db()
        self.assertEqual(row.payload_json['raid_first_kills']['status'], 'private')
        self.assertFalse(row.payload_json['raid_first_kills']['visible'])
        task.refresh_from_db()
        self.assertEqual(json.loads(task.flag)['raid_error'], 0)
        # Later failure retains the privacy tombstone, never revives last-good.
        with patch('requests.get', self.raid_http(status=403)), patch('django.utils.timezone.now', return_value=later + timedelta(days=2)):
            self.assertTrue(PlayerRaiderIOActivityMonitor(None, task).scan(''))
            self.assertFalse(PlayerRaiderIOActivityMonitor(None, task).scan(''))
        row.refresh_from_db()
        self.assertFalse(row.payload_json['raid_first_kills']['visible'])

    def test_roster_and_raid_rate_limits_gate_new_instances_using_causal_clock(self):
        from botend.controller.plugins.portal.PlayerRaiderIOActivityMonitor import PlayerRaiderIOActivityMonitor
        from botend.models import CharacterEnrichmentTask, RaiderIOSourceBackoff
        from botend.tests.test_player_raiderio_activity import payload
        for source, seconds in [('roster', 900), ('raid', 2700)]:
            with self.subTest(source=source):
                CharacterEnrichmentTask.objects.all().delete()
                RaiderIOSourceBackoff.objects.all().delete()
                RaiderIOCharacterActivity.objects.all().delete()
                PlayerSpecTopPlayer.objects.all().delete()
                self.http = FixtureHTTP()
                row = self.activity(self.player(), payload_json={'raid_first_kills': {}} if source == 'raid' else {})
                if source == 'roster':
                    self.http.best_runs = payload()['mythic_plus_best_runs']
                    self.http.roster_status = 429
                transport = self.raid_http(status=429) if source == 'raid' else self.http
                task = MonitorTask.objects.create(name=f'limited-{source}', type=0, target='')
                with patch('requests.get', transport), patch('django.utils.timezone.now', return_value=self.now):
                    self.assertTrue(PlayerRaiderIOActivityMonitor(None, task).scan(''))
                    self.assertFalse(PlayerRaiderIOActivityMonitor(None, task).scan(''))
                self.assertEqual(len(self.http.calls), 2)
                task.refresh_from_db()
                summary = json.loads(task.flag)
                self.assertEqual(timezone.datetime.fromisoformat(summary['rate_limited_until']), self.now + timedelta(seconds=seconds))
                self.assertEqual(summary['rate_limit_clock_error'], '')
                row.refresh_from_db()
                self.assertEqual(row.status, 'ready')  # 活动成功状态独立于后续队伍/团本失败。
                self.player('Other', spec='Shadow')
                with patch('requests.get', transport), patch('django.utils.timezone.now', return_value=self.now + timedelta(seconds=600)):
                    self.assertTrue(PlayerRaiderIOActivityMonitor(None, MonitorTask.objects.get(pk=task.pk)).scan(''))
                self.assertEqual(len(self.http.calls), 2)
                task.refresh_from_db()
                deferred = json.loads(task.flag)
                self.assertEqual((deferred['attempted'], deferred['ready'], deferred['remaining_due']), (0, 0, 1))
                self.http.best_runs = []
                from botend.tests.test_raiderio_run_roster import details
                self.http.roster_status, self.http.roster_data = 200, details()
                resumed_http = self.raid_http() if source == 'raid' else self.http
                with patch('requests.get', resumed_http), patch('django.utils.timezone.now', return_value=self.now + timedelta(seconds=seconds + 1)):
                    self.assertTrue(PlayerRaiderIOActivityMonitor(None, MonitorTask.objects.get(pk=task.pk)).scan(''))
                self.assertEqual(len(self.http.calls), 6 if source == 'raid' else 4)

    def test_changed_causal_clock_fails_closed_across_new_monitor_instances(self):
        from botend.controller.plugins.portal.PlayerRaiderIOActivityMonitor import PlayerRaiderIOActivityMonitor
        row = self.activity(self.player())
        task = MonitorTask.objects.create(name='limited-race', type=0, target='')
        self.http.status = 429
        def replace_clock():
            RaiderIOCharacterActivity.objects.filter(pk=row.pk).update(
                status='ready', last_error='', next_attempt_at=self.now + TTL)
        self.http.on_response = replace_clock
        with patch('requests.get', self.http), patch('django.utils.timezone.now', return_value=self.now):
            self.assertFalse(PlayerRaiderIOActivityMonitor(None, task).scan(''))
        task.refresh_from_db()
        summary = json.loads(task.flag)
        self.assertIsNone(summary['rate_limited_until'])
        self.assertEqual(summary['rate_limit_clock_error'], 'causal_429_clock_missing_or_changed')
        self.player('Unattempted', spec='Shadow')
        self.http.on_response = None
        self.http.status = 200
        with patch('requests.get', self.http), patch('django.utils.timezone.now', return_value=self.now + timedelta(hours=2)):
            self.assertFalse(PlayerRaiderIOActivityMonitor(None, MonitorTask.objects.get(pk=task.pk)).scan(''))
        self.assertEqual(len(self.http.calls), 1)
        task.refresh_from_db()
        summary = json.loads(task.flag)
        self.assertEqual((summary['attempted'], summary['ready'], summary['remaining_due']), (0, 0, 1))
        self.assertEqual(summary['rate_limit_clock_error'], 'causal_429_clock_missing_or_changed')

    def test_service_rejects_missing_or_nonunique_active_season(self):
        self.player()
        with self.assertRaises(ValueError):
            refresh_due_top20_activity(None, collector=self.collector)
        other = SeasonMeta.objects.create(season_key='other', season_name='Other',
            rio_season='other', mplus_zone_id=1, raid_zone_id=2)
        with self.assertRaises(ValueError):
            self.run_due()
        other.delete()
        SeasonMeta.objects.filter(pk=self.season.pk).update(is_active=False)
        with self.assertRaises(ValueError):
            self.run_due()  # stale caller's active=True cannot bypass live metadata
        self.assertEqual(self.http.calls, [])

    def test_monitor_runs_real_collector_and_never_reports_http_failure_ready(self):
        from botend.controller.plugins.portal.PlayerRaiderIOActivityMonitor import PlayerRaiderIOActivityMonitor
        self.player()
        task = MonitorTask.objects.create(name='PlayerRaiderIOActivityMonitor', type=0, target='')
        monitor = PlayerRaiderIOActivityMonitor(None, task)
        self.assertFalse(monitor.requires_browser)
        self.assertTrue(monitor.default_is_active)
        self.http.status = 403
        with patch('botend.services.player_raiderio_activity.requests.get', side_effect=self.http):
            self.assertFalse(monitor.scan(''))
        task.refresh_from_db()
        summary = json.loads(task.flag)
        self.assertEqual((summary['attempted'], summary['ready'], summary['error']), (1, 0, 1))
        self.assertEqual(summary['states'], {'http_403': 1})
        self.assertNotIn('results', summary)
        self.http.status = 200
        expire_activity_modules(RaiderIOCharacterActivity.objects.get())
        with patch('botend.services.player_raiderio_activity.requests.get', side_effect=self.http):
            self.assertTrue(monitor.scan(''))
        task.refresh_from_db()
        summary = json.loads(task.flag)
        self.assertEqual((summary['attempted'], summary['ready'], summary['error']), (1, 1, 0))

    def test_invalid_season_or_limit_rejected_without_requests(self):
        self.player()
        for value in (0, -1, 101, True):
            with self.subTest(limit=value), self.assertRaises(ValueError):
                self.run_due(limit=value)
        self.season.is_active = False
        with self.assertRaises(ValueError):
            self.run_due()
        self.assertEqual(self.http.calls, [])


class DueActivityMonitorTests(TestCase):
    def test_monitor_requires_unique_active_season_and_records_real_outcomes(self):
        from botend.controller.plugins.portal.PlayerRaiderIOActivityMonitor import PlayerRaiderIOActivityMonitor
        task = MonitorTask.objects.create(name='PlayerRaiderIOActivityMonitor', type=0, target='')
        monitor = PlayerRaiderIOActivityMonitor(None, task)
        self.assertEqual(monitor.default_wait_time, 600)
        service = 'botend.controller.plugins.portal.PlayerRaiderIOActivityMonitor.refresh_due_top20_activity'
        with patch(service) as refresh:
            self.assertFalse(monitor.scan(''))
            refresh.assert_not_called()
        season = SeasonMeta.objects.create(season_key='s1', season_name='S1', rio_season='season-mn-2',
            mplus_zone_id=1, raid_zone_id=2)
        partial = dict(attempted=1, ready=0, partial=1, fresh=0, fresh_race=0, busy=0,
            backoff=0, error=0, remaining_due=0, unresolved=1, stopped=False, stop='',
            roster_error=0, raid_error=0)
        with patch(service, return_value=partial) as refresh:
            self.assertFalse(monitor.scan(''))
            self.assertEqual(refresh.call_args.args, (season,))
            self.assertEqual(refresh.call_args.kwargs['limit'], 20)
            self.assertTrue(refresh.call_args.kwargs['collector'].defer_enrichment)
            self.assertTrue(callable(refresh.call_args.kwargs['should_continue']))
        task.refresh_from_db()
        summary = json.loads(task.flag)
        self.assertEqual((summary['attempted'], summary['ready'], summary['partial']), (1, 0, 1))
        SeasonMeta.objects.create(season_key='s2', season_name='S2', rio_season='season-other',
            mplus_zone_id=1, raid_zone_id=2)
        with patch(service) as refresh:
            self.assertFalse(monitor.scan(''))
            refresh.assert_not_called()
