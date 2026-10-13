"""Focused source contracts; independent TestCase and in-memory Django database."""
import copy
import json
from datetime import timedelta
from unittest.mock import Mock, patch

from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from botend.models import PlayerSpecTopPlayer, SeasonMeta, RaiderIOCharacterActivity, RaiderIORunRoster
from botend.raiderio_identity import activity_identity_digest
from botend.services.player_raiderio_activity import player_identity
from botend.services.raiderio_run_roster import validate_details
from botend.tests.test_player_raiderio_activity import payload
from botend.tests.test_raiderio_run_roster import details


def scored(character_id=100):
    return {'ui': {'affixes': 'all', 'characterId': character_id, 'date': 'all',
                   'mode': 'scored', 'role': 'all', 'season': 'season-mn-2'},
            'dungeons': [{'dungeon': {'id': 1, 'name': "Kings' Rest", 'slug': 'kings-rest'},
                          'keystone_run_id': 123, 'mythic_level': 20, 'score': 401.2,
                          'num_chests': 0, 'completed_at': '2026-10-10T12:00:00Z',
                          'role': 'healer', 'alternate': None, 'keystonePace': {'huge': True}}]}


def roster(run_id=123):
    data = details(run_id)
    for i, member in enumerate(data['roster']):
        member['character']['id'] = 100 + i
    return data


class ObservationValidationTests(SimpleTestCase):
    def test_character_id_retained_strictly_and_legacy_supported(self):
        run = payload()['mythic_plus_best_runs'][0]
        result = validate_details(roster(), run, 'season-mn-2')
        self.assertEqual(result['roster'][0]['character']['id'], 100)
        self.assertNotIn('id', validate_details(details(), run, 'season-mn-2')['roster'][0]['character'])
        for bad in (True, '100', 0, -1, None, 1.5):
            data = roster(); data['roster'][0]['character']['id'] = bad
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_details(data, run, 'season-mn-2')

    def test_strict_scored_scope_and_complete_minimal_records(self):
        from botend.services.player_observation_sources import validate_scored_runs
        clean = validate_scored_runs(scored(), 'season-mn-2', 100)
        self.assertNotIn('keystonePace', json.dumps(clean))
        self.assertIn('characterId=100', clean['source_url'])
        for key in scored()['ui']:
            data = scored(); del data['ui'][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_scored_runs(data, 'season-mn-2', 100)
            data = scored(); data['ui'][key] = 'wrong'
            with self.subTest(wrong=key), self.assertRaises(ValueError):
                validate_scored_runs(data, 'season-mn-2', 100)
        for key in scored()['dungeons'][0]:
            if key == 'keystonePace':
                continue
            data = scored(); del data['dungeons'][0][key]
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_scored_runs(data, 'season-mn-2', 100)
        for changes in ({'score': float('nan')}, {'score': True}, {'mythic_level': '20'},
                        {'num_chests': -1}, {'keystone_run_id': True}, {'role': 'all'},
                        {'completed_at': '2026-10-10'}, {'alternate': []},
                        {'dungeon': {'id': 1, 'name': "Kings' Rest"}}):
            data = scored(); data['dungeons'][0].update(changes)
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                validate_scored_runs(data, 'season-mn-2', 100)
        data = scored(); data['dungeons'] *= 2
        with self.assertRaises(ValueError):
            validate_scored_runs(data, 'season-mn-2', 100)
        data = scored(); data['ui']['specId'] = 1
        with self.assertRaises(ValueError):
            validate_scored_runs(data, 'season-mn-2', 100)

    def test_alternate_is_validated_not_copied_as_unbounded_json(self):
        from botend.services.player_observation_sources import validate_scored_runs
        data = scored()
        alt = copy.deepcopy(data['dungeons'][0]); alt.pop('alternate'); alt['keystone_run_id'] = 124
        data['dungeons'][0]['alternate'] = alt
        clean = validate_scored_runs(data, 'season-mn-2', 100)
        self.assertEqual(clean['dungeons'][0]['alternate']['keystone_run_id'], 124)
        self.assertNotIn('keystonePace', json.dumps(clean))
        data['dungeons'][0]['alternate']['score'] = None
        with self.assertRaises(ValueError):
            validate_scored_runs(data, 'season-mn-2', 100)


class ObservationSelectionTests(SimpleTestCase):
    def test_character_budget_order_matches_evidence_digest_order(self):
        from botend.services.player_observation_sources import _characters
        subject = activity_identity_digest('eu', 'test-realm', 'Member0')
        facts = []
        for run_id in range(18):
            fact = roster(123 + run_id)
            for index, member in enumerate(fact['roster'][1:], 1):
                cid = 200 + run_id * 4 + index
                name = f'Member{cid}'
                member['character'].update(id=cid, name=name, path=f'/characters/eu/test-realm/{name}')
            facts.append(fact)
        chosen, conflicts = _characters(facts, subject)
        digests = [activity_identity_digest(**row['identity']) for row in chosen]
        self.assertEqual(conflicts, 0)
        self.assertEqual(len(digests), 73)
        self.assertEqual(digests[:40], sorted(digests, key=lambda key: (key != subject, key))[:40])


class ObservationSourcesTests(TestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk,
            region='eu', realm='Test Realm', character_name='Member4', class_name='Priest',
            spec_name='Holy', rank=1, profile_url='https://raider.io/characters/eu/test-realm/Member4',
            gear_json=[{'id': 1}], stats_json={'haste': 2}, talents_json=[{'id': 3}])
        self.run = payload()['mythic_plus_best_runs'][0]
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(self.player), payload_json={'mythic_plus_best_runs': [self.run],
                'mythic_plus_recent_runs': [self.run]}, fetched_at=timezone.now(),
            status='ready', next_attempt_at=timezone.now() + timedelta(hours=24))
        self.row = RaiderIORunRoster.objects.create(rio_season=self.season.rio_season, run_id=123,
            payload_json=roster(), roster_visible=True, fetched_at=timezone.now(),
            status='ready', next_attempt_at=timezone.now() + timedelta(days=7))
        self.calls = []
        self.http_status = 200
        self.bad_scored = False

    def transport(self, url, **kwargs):
        self.calls.append((url, kwargs))
        self.assertEqual(kwargs['timeout'], 25)
        params = kwargs['params']
        if url.endswith('/profile'):
            self.assertNotIn('gear', params['fields']); self.assertNotIn('talents', params['fields'])
            data = payload('Member4')
        elif url.endswith('/run-details'):
            data = roster(params['id'])
        else:
            data = scored(params['characterId'])
            if self.bad_scored:
                data['ui']['characterId'] += 1
        return Mock(status_code=self.http_status, headers={}, json=Mock(return_value=data))

    def refresh(self, **kwargs):
        from botend.services.player_observation_sources import refresh_observation_sources
        with patch('botend.services.player_observation_sources.requests.Session.get', side_effect=self.transport):
            return refresh_observation_sources(self.player, **kwargs)

    def test_reuses_fresh_roster_subject_first_dedup_and_minimal_central_storage(self):
        before = PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk)
        result = self.refresh(max_characters=2)
        self.assertEqual(result['api_requests'], 2)
        self.assertEqual([c[1]['params']['characterId'] for c in self.calls], [104, 101])
        self.assertEqual(len(result['runs']), 1)
        self.assertEqual(len(result['characters']), 2)
        self.activity.refresh_from_db()
        self.assertEqual(self.activity.scored_runs_json['ui']['characterId'], 104)
        self.assertIsNotNone(self.activity.scored_runs_fetched_at)
        self.assertNotIn('keystonePace', json.dumps(self.activity.scored_runs_json))
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 2)
        self.assertEqual(PlayerSpecTopPlayer.objects.values().get(pk=self.player.pk), before)
        json.dumps(result, allow_nan=False)
        self.calls.clear()
        self.assertEqual(self.refresh(max_characters=2)['api_requests'], 0)
        self.assertEqual(self.calls, [])

    def test_non_top20_and_inactive_never_create_or_request_even_force(self):
        for changes in ({'rank': 21}, {'rank': None}):
            PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(**changes)
            self.player.refresh_from_db()
            self.assertEqual(self.refresh(force=True)['status'], 'not_top20')
        PlayerSpecTopPlayer.objects.filter(pk=self.player.pk).update(rank=1)
        self.player.refresh_from_db()
        self.season.is_active = False; self.season.save()
        self.assertEqual(self.refresh(force=True)['status'], 'wrong_season')
        self.assertEqual(self.calls, [])
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 1)

    def test_precise_legacy_repair_and_force_respects_tombstone_backoff_lease(self):
        self.row.payload_json = details(); self.row.save()
        result = self.refresh(max_characters=1)
        self.assertEqual(result['api_requests'], 2)
        self.row.refresh_from_db()
        self.assertEqual(self.row.payload_json['roster'][0]['character']['id'], 100)
        for status, visible in (('unavailable', False), ('error', True), ('fetching', True)):
            self.calls.clear()
            self.row.payload_json = details(); self.row.status = status; self.row.roster_visible = visible
            self.row.next_attempt_at = timezone.now() + timedelta(hours=1); self.row.save()
            self.refresh(force=True)
            self.assertEqual(self.calls, [])

    def test_429_stops_entire_batch_and_invalid_scored_preserves_last_good(self):
        self.refresh(max_characters=1)
        self.activity.refresh_from_db()
        old, stamp = self.activity.scored_runs_json, self.activity.scored_runs_fetched_at
        self.calls.clear(); self.bad_scored = True
        result = self.refresh(force=True, max_characters=1)
        self.assertEqual(result['characters'][0]['status'], 'invalid_payload')
        self.activity.refresh_from_db()
        self.assertEqual((self.activity.scored_runs_json, self.activity.scored_runs_fetched_at), (old, stamp))
        self.calls.clear(); self.http_status = 429
        result = self.refresh(force=True)
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(len(self.calls), 1)

    def test_activity_refresh_counts_api_and_does_not_auto_enrich_outside_budget(self):
        self.activity.next_attempt_at = timezone.now() - timedelta(seconds=1); self.activity.save()
        self.row.delete()
        result = self.refresh(max_runs=1, max_characters=1)
        self.assertEqual(result['api_requests'], 3)
        self.assertEqual([c[0].rsplit('/', 1)[-1] for c in self.calls],
                         ['profile', 'run-details', 'mythic-plus-scored-runs'])

    def test_missing_paths_invalid_rosters_and_conflicting_character_ids_not_guessed(self):
        data = roster(); data['roster'][4]['character']['path'] = ''
        self.row.payload_json = data; self.row.save()
        result = self.refresh(max_characters=1)
        self.assertEqual(result['characters'][0]['character_id'], 101)
        self.assertEqual(self.activity.scored_runs_json, {})
        self.calls.clear()
        data['roster'][0]['character']['path'] = '/characters/eu/WRONG/Member0'
        self.row.payload_json = data; self.row.save()
        self.refresh(force=True)
        self.assertEqual(self.calls, [])

    def test_run_and_character_caps_are_real_union_budgets(self):
        best, recent = [], []
        for number in range(123, 148):
            run = copy.deepcopy(self.run)
            run['url'] = f'https://raider.io/mythic-plus-runs/season-mn-2/{number}-20-kings-rest'
            (best if number < 139 else recent).append(run)
        self.activity.payload_json = {'mythic_plus_best_runs': best, 'mythic_plus_recent_runs': recent + best[:1]}
        self.activity.save()
        self.row.delete()
        original_transport = self.transport

        def unique_rosters(url, **kwargs):
            response = original_transport(url, **kwargs)
            if url.endswith('/run-details'):
                data = roster(kwargs['params']['id'])
                for i, member in enumerate(data['roster']):
                    character = member['character']
                    character['id'] = kwargs['params']['id'] * 10 + i
                    character['name'] = f"Run{kwargs['params']['id']}Member{i}"
                    character['path'] = f"/characters/eu/test-realm/{character['name']}"
                response.json.return_value = data
            return response

        self.transport = unique_rosters
        with patch('botend.services.player_observation_sources._pace', return_value=True), \
                patch('botend.services.raiderio_run_roster._pace', return_value=True):
            result = self.refresh()
        self.assertEqual(len(result['runs']), 18)
        self.assertEqual(len(result['characters']), 40)
        self.assertEqual(result['api_requests'], 58)
        self.assertTrue(result['runs_truncated'])
        self.assertTrue(result['characters_truncated'])
        self.assertFalse(result['complete_season'])
        self.assertEqual(len({c[1]['params']['id'] for c in self.calls if c[0].endswith('/run-details')}), 18)

    def test_rate_limit_at_activity_or_roster_stops_before_scored(self):
        self.http_status = 429
        self.activity.next_attempt_at = timezone.now() - timedelta(seconds=1); self.activity.save()
        result = self.refresh()
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(len(self.calls), 1)
        self.calls.clear()
        self.activity.status = 'ready'
        self.activity.next_attempt_at = timezone.now() + timedelta(hours=24); self.activity.save()
        self.row.delete()
        result = self.refresh()
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(result['api_requests'], 1)
        self.assertEqual(result['characters'], [])

    def test_conflicting_ids_and_unverified_privacy_are_not_character_sources(self):
        data = roster(); data['roster'][0]['character']['id'] = 104
        self.row.payload_json = data; self.row.save()
        result = self.refresh()
        self.assertEqual(result['identity_conflicts'], 2)
        self.assertNotIn(104, [c['character_id'] for c in result['characters']])
        self.calls.clear()
        data = roster(); data['deleted_at'] = '2026-10-10T12:00:00Z'
        self.row.payload_json = data; self.row.save()
        result = self.refresh(force=True)
        self.assertEqual(result['characters'], [])
        self.assertEqual(self.calls, [])

    def test_scored_busy_and_backoff_preserve_independent_activity_state(self):
        teammate_identity = {'region': 'eu', 'realm_slug': 'test-realm', 'character_name': 'member1'}
        teammate = RaiderIOCharacterActivity.objects.create(season=self.season, **teammate_identity,
            status='fetching', next_attempt_at=timezone.now() + timedelta(minutes=2))
        result = self.refresh(force=True, max_characters=2)
        self.assertEqual(result['characters'][1]['status'], 'busy')
        teammate.refresh_from_db()
        self.assertEqual(teammate.status, 'fetching')
        self.assertEqual(teammate.scored_runs_json, {})
        teammate.status = 'error'; teammate.save()
        self.calls.clear()
        result = self.refresh(force=True, max_characters=2)
        self.assertEqual(result['characters'][1]['status'], 'backoff')
        self.assertEqual(len(self.calls), 1)

    def test_all_requests_use_existing_proxy_configuration(self):
        from django.test import override_settings
        with override_settings(REQUEST_CONFIG={'enable_proxy': True, 'proxies': {'https': 'http://proxy.invalid'}}):
            self.refresh(max_characters=1)
        self.assertEqual(self.calls[0][1]['proxies'], {'https': 'http://proxy.invalid'})

    def test_limits_rejected_not_silently_expanded(self):
        for kwargs in ({'max_runs': 19}, {'max_characters': 41}, {'max_runs': True},
                       {'max_characters': 0}, {'max_runs': -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                self.refresh(**kwargs)
        self.assertEqual(self.calls, [])
