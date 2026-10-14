"""Raid-source contracts. Full captured responses can be replayed via RIO_RAID_REPLAY_DIR."""
import copy
import io
import json
import os
from datetime import datetime, timedelta, timezone as dt_timezone
from pathlib import Path
from unittest.mock import Mock, patch

from django.core.management import call_command, CommandError
from django.test import TestCase

from botend.models import PlayerSpecTopPlayer, RaiderIOCharacterActivity, SeasonMeta
from botend.services.player_raiderio_activity import player_identity
from botend.services.player_raid_observation_sources import refresh_raid_sources

NOW = datetime(2026, 10, 13, 12, tzinfo=dt_timezone.utc)
BASE = 'https://raider.io/characters/cn/shadowmourne/%E7%A5%9E%E5%A5%87%E7%9A%84%E9%98%BF%E8%AF%BA'
CATALOG = 'https://cdn.raiderio.net/assets/data/siteMeta-test.json'
API = BASE.replace('/characters/', '/api/characters/') + '?season=season-mn-2&tier=36'


def fixtures():
    """Small explicit fixture; IDs/order/timestamps are from the captured response."""
    ids = [3470, 3445, 3497, 3455, 3420, 3421, 3429, 3492]
    slugs = ['nekzali-the-soulcoiler', 'entombed-sentinels', 'the-lost-explorers',
             'vashnik-the-malignant', 'sszorak', 'the-twin-fangs', 'the-coiled-altar', 'ulatek']
    encounters = [dict(ordinal=i, wowEncounterId=eid, encounterId=197163+i,
                       slug=slugs[i], name=slugs[i]) for i, eid in enumerate(ids)]
    meta = {'raids': [dict(id=16915, slug='the-venomous-abyss', name='The Venomous Abyss',
                           difficulty='mythic', encounters=encounters)]}
    details = {'characterDetails': {'character': {'id': 230181563, 'name': '神奇的阿诺',
        'region': {'slug': 'cn'}, 'realm': {'slug': 'shadowmourne'},
        'path': '/characters/cn/shadowmourne/神奇的阿诺'}, 'tier': '36', 'isPrivate': False},
        'characterMythicPlusProgress': {'season': 'season-mn-2', 'seasonSlug': 'season-mn-2'},
        'characterRaidProgress': {'tier': '36', 'raidProgress': [{
            'raid': 'the-venomous-abyss', 'encountersDefeated': {'mythic': [
                {'slug': slugs[5], 'firstDefeated': '2026-09-17T07:55:47.000Z', 'numKills': 2,
                 'lastDefeated': '2026-09-24T06:55:40.000Z'},
                {'slug': slugs[6], 'firstDefeated': '2026-09-24T07:50:18.000Z', 'numKills': 3},
                {'slug': slugs[7], 'firstDefeated': '2026-09-24T10:46:21.000Z', 'numKills': 3}]}}]}}
    html = ('<script>window.__RIO_INITIAL_DATA = ' + json.dumps(json.dumps(
        {'siteConfig': {'siteMetaUrl': CATALOG}})) + ';</script>'
        '<link rel="preload" as="fetch" href="' + API.replace('&', '&amp;') + '">')
    central = [{'id': eid, 'index': 100-i} for i, eid in enumerate(ids)]
    return html, meta, details, central


class RaidSourceTests(TestCase):
    def setUp(self):
        self.html, self.meta, self.details, central = fixtures()
        self.catalog_url = CATALOG
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=53, raid_encounters=central)
        self.player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='cn',
            realm='Shadowmourne', character_name='神奇的阿诺', profile_url=BASE,
            class_name='DeathKnight', spec_name='Blood', rank=1)
        self.activity = RaiderIOCharacterActivity.objects.create(season=self.season,
            **player_identity(self.player), payload_json={'mythic_plus_best_runs': [{'untouched': True}]},
            status='ready', fetched_at=NOW, module_fetched_at={'best_runs': NOW.isoformat()},
            scored_runs_json={'ui': {'season': 'season-mn-2', 'characterId': 230181563}})
        self.calls = []
        self.pacer = patch('botend.services.player_raid_observation_sources._pace', return_value=True)
        self.pacer.start()
        self.addCleanup(self.pacer.stop)

    def transport(self, url, **kwargs):
        self.calls.append(url)
        self.assertEqual(kwargs['timeout'], 25)
        self.assertIs(kwargs['allow_redirects'], False)
        if url == BASE:
            return Mock(status_code=200, headers={}, text=self.html)
        data = self.meta if url == self.catalog_url else self.details
        self.assertIn(url, (self.catalog_url, API))
        return Mock(status_code=200, headers={}, json=Mock(return_value=copy.deepcopy(data)))

    def refresh(self, **kwargs):
        return refresh_raid_sources(self.player, transport=self.transport, now=kwargs.get('now', NOW))

    def module(self):
        self.activity.refresh_from_db()
        return self.activity.payload_json['raid_first_kills']

    def test_three_gets_minimal_facts_and_cross_spec_freshness(self):
        before = RaiderIOCharacterActivity.objects.values().get(pk=self.activity.pk)
        self.assertEqual(self.refresh(), {'status': 'ready', 'activity_id': self.activity.pk, 'requests': 3})
        module = self.module()
        self.assertEqual([x['index'] for x in module['raids'][0]['encounters']], [6, 7, 8])
        self.assertEqual(module['raids'][0]['encounters'][0]['firstDefeated'], '2026-09-17T07:55:47.000Z')
        self.assertEqual(module['source']['character_id'], 230181563)
        self.assertEqual(module['next_attempt_at'], (NOW + timedelta(hours=24)).isoformat())
        after = RaiderIOCharacterActivity.objects.values().get(pk=self.activity.pk)
        old_payload = before.pop('payload_json'); new_payload = after.pop('payload_json')
        self.assertEqual(before, after)
        self.assertEqual({k:v for k,v in new_payload.items() if k != 'raid_first_kills'}, old_payload)
        self.assertNotIn('lastDefeated', json.dumps(module))
        self.player.pk = None; self.player.spec_name = 'Frost'; self.player.save()
        self.assertEqual(self.refresh()['status'], 'fresh')
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 1)

    def test_missing_first_kill_never_uses_last_kill_and_empty_is_valid(self):
        kills = self.details['characterRaidProgress']['raidProgress'][0]['encountersDefeated']['mythic']
        kills.pop()
        kills[0].pop('firstDefeated')
        kills[0].pop('numKills')
        self.assertEqual(self.refresh()['status'], 'ready')
        bosses = self.module()['raids'][0]['encounters']
        self.assertIsNone(bosses[0]['firstDefeated'])
        self.assertIsNone(bosses[0]['numKills'])
        self.assertIsNone(bosses[2]['firstDefeated'])
        self.season.raid_encounters = []
        self.season.save(update_fields=['raid_encounters'])
        self.assertEqual(self.refresh(now=NOW + timedelta(days=2))['status'], 'ready')
        self.assertEqual(self.module()['raids'], [])

    def test_bad_facts_fail_closed_keep_last_good_and_backoff(self):
        self.assertEqual(self.refresh()['status'], 'ready')
        good = copy.deepcopy(self.module())
        original = copy.deepcopy(self.details)
        for field, bad in [('numKills', True), ('numKills', '3'), ('numKills', 0), ('numKills', None),
                           ('numKills', -1), ('firstDefeated', '2026-09-24'),
                           ('firstDefeated', '2026-09-24T10:00:00'),
                           ('firstDefeated', '2099-09-24T10:00:00Z')]:
            with self.subTest(field=field, bad=bad):
                self.details = copy.deepcopy(original)
                self.details['characterRaidProgress']['raidProgress'][0]['encountersDefeated']['mythic'][0][field] = bad
                self.activity.payload_json['raid_first_kills'] = good
                self.activity.save(update_fields=['payload_json'])
                later = NOW + timedelta(days=2)
                self.assertEqual(self.refresh(now=later)['status'], 'invalid_payload')
                current = self.module()
                self.assertEqual(current['raids'], good['raids'])
                self.assertEqual(current['fetched_at'], good['fetched_at'])
                self.assertEqual(current['next_attempt_at'], (later + timedelta(minutes=30)).isoformat())
                self.assertEqual(self.refresh(now=later)['status'], 'backoff')

    def test_catalog_full_unique_contiguous_and_current_subset(self):
        original = copy.deepcopy(self.meta)
        changes = [lambda es: es.append(copy.deepcopy(es[-1])),
                   lambda es: es[0].update(ordinal=True),
                   lambda es: es[1].update(ordinal=17),
                   lambda es: es[1].update(wowEncounterId=es[0]['wowEncounterId']),
                   lambda es: es[0].update(wowEncounterId=999999),
                   lambda es: es[0].update(encounterId='197163')]
        for change in changes:
            self.meta = copy.deepcopy(original)
            change(self.meta['raids'][0]['encounters'])
            self.activity.payload_json = {}
            self.activity.save(update_fields=['payload_json'])
            with self.subTest(change=change):
                self.assertNotEqual(self.refresh()['status'], 'ready')
                self.assertFalse(self.module()['visible'])
        self.meta = original
        self.meta['raids'][0]['encounters'].reverse()
        self.activity.payload_json = {}
        self.activity.save(update_fields=['payload_json'])
        self.assertEqual(self.refresh()['status'], 'ready')
        self.assertEqual([e['wowEncounterId'] for e in self.module()['raids'][0]['encounters']], [3421, 3429, 3492])

    def test_duplicate_or_unknown_progress_raid_and_kills_rejected(self):
        original = copy.deepcopy(self.details)
        for kind in ('raid', 'kill', 'unknown_kill'):
            self.details = copy.deepcopy(original)
            rows = self.details['characterRaidProgress']['raidProgress']
            if kind == 'raid':
                rows.append(copy.deepcopy(rows[0]))
            elif kind == 'kill':
                rows[0]['encountersDefeated']['mythic'] *= 2
            else:
                rows[0]['encountersDefeated']['mythic'][0]['slug'] = 'unrelated-boss'
            self.activity.payload_json = {}
            self.activity.save(update_fields=['payload_json'])
            with self.subTest(kind=kind):
                self.assertEqual(self.refresh()['status'], 'invalid_payload')

    def test_private_and_identity_tombstone_survive_later_transport_error(self):
        self.assertEqual(self.refresh()['status'], 'ready')
        self.details['characterDetails']['isPrivate'] = True
        later = NOW + timedelta(days=2)
        self.assertEqual(self.refresh(now=later)['status'], 'private')
        self.assertFalse(self.module()['visible'])
        calls = len(self.calls)
        self.assertEqual(self.refresh(now=later)['status'], 'private')
        self.assertEqual(len(self.calls), calls)
        import requests
        failed = Mock(side_effect=requests.Timeout())
        result = refresh_raid_sources(self.player, transport=failed, now=later + timedelta(days=2))
        self.assertEqual(result['status'], 'transport_error')
        self.assertFalse(self.module()['visible'])
        self.details['characterDetails']['isPrivate'] = False
        self.details['characterDetails']['character']['id'] += 1
        self.assertEqual(self.refresh(now=later + timedelta(days=4))['status'], 'identity_mismatch')
        self.assertFalse(self.module()['visible'])
        self.details['characterDetails']['character']['id'] -= 1
        self.assertEqual(self.refresh(now=later + timedelta(days=6))['status'], 'ready')
        self.assertTrue(self.module()['visible'])

    def test_identity_scope_and_tier_validation(self):
        original = copy.deepcopy(self.details)
        mutations = [
            ('identity_mismatch', lambda d: d['characterDetails']['character'].update(id=True)),
            ('identity_mismatch', lambda d: d['characterDetails']['character'].update(name='Other')),
            ('identity_mismatch', lambda d: d['characterDetails']['character']['realm'].update(slug='other')),
            ('identity_mismatch', lambda d: d['characterDetails']['character'].update(path='/characters/cn/other/神奇的阿诺')),
            ('wrong_tier', lambda d: d['characterDetails'].update(tier=36)),
            ('wrong_tier', lambda d: d['characterRaidProgress'].update(tier='35')),
            ('wrong_season', lambda d: d['characterMythicPlusProgress'].update(season='season-mn-1')),
            ('invalid_privacy', lambda d: d['characterDetails'].update(isPrivate=0)),
        ]
        for expected, mutate in mutations:
            self.details = copy.deepcopy(original); mutate(self.details)
            self.activity.payload_json = {}; self.activity.save(update_fields=['payload_json'])
            with self.subTest(expected=expected, mutate=mutate):
                self.assertEqual(self.refresh()['status'], expected)

    def test_untrusted_discovery_rejected_without_following(self):
        original = self.html
        for old, new in [(CATALOG, 'https://evil.example/meta.json'),
                         (CATALOG, CATALOG + '?redirect=1'),
                         (CATALOG, 'https://cdn.raiderio.net@evil.example/assets/data/siteMeta-x.json'),
                         ('tier=36', 'tier=36&amp;tier=37'),
                         ('tier=36', 'tier=0'), ('season=season-mn-2', 'season=season-mn-1'),
                         ('/api/characters/cn/shadowmourne/', '/api/characters/cn/other/')]:
            self.html = original.replace(old, new)
            self.activity.payload_json = {}; self.activity.save(update_fields=['payload_json'])
            before = len(self.calls)
            with self.subTest(new=new):
                self.assertNotEqual(self.refresh()['status'], 'ready')
                self.assertEqual(len(self.calls) - before, 1)

    def test_429_retry_after_stops_and_redirect_is_not_followed(self):
        for code, retry in [(429, '7200'), (429, 'Tue, 13 Oct 2026 15:00:00 GMT'), (302, '')]:
            self.activity.payload_json = {}; self.activity.save(update_fields=['payload_json'])
            transport = Mock(return_value=Mock(status_code=code, headers={'Retry-After': retry}))
            result = refresh_raid_sources(self.player, transport=transport, now=NOW)
            self.assertEqual(result['requests'], 1)
            self.assertEqual(result['status'], 'rate_limited' if code == 429 else 'http_302')
            self.assertEqual(transport.call_count, 1)
            due = datetime.fromisoformat(self.module()['next_attempt_at'])
            self.assertEqual(due, NOW + (timedelta(hours=2) if retry == '7200' else
                timedelta(hours=3) if retry else timedelta(minutes=30)))
            self.assertEqual(self.refresh()['status'], 'backoff')

    def test_lease_cas_and_merge_preserve_concurrent_module_update(self):
        original_transport = self.transport
        nested = []
        def transport(url, **kwargs):
            if url == BASE:
                nested.append(self.refresh())
                row = RaiderIOCharacterActivity.objects.get(pk=self.activity.pk)
                row.payload_json['concurrent_mplus'] = {'ok': True}
                row.save(update_fields=['payload_json'])
            return original_transport(url, **kwargs)
        result = refresh_raid_sources(self.player, transport=transport, now=NOW)
        self.assertEqual(result['status'], 'ready')
        self.assertEqual(nested[0]['status'], 'busy')
        self.assertEqual(nested[0]['requests'], 0)
        self.assertIsNone(self.module()['lease_token'])
        self.assertEqual(self.activity.payload_json['concurrent_mplus'], {'ok': True})
        def supersede(url, **kwargs):
            row = RaiderIOCharacterActivity.objects.get(pk=self.activity.pk)
            row.payload_json['raid_first_kills']['lease_token'] = 'new-owner'
            row.save(update_fields=['payload_json'])
            return original_transport(url, **kwargs)
        result = refresh_raid_sources(self.player, transport=supersede, now=NOW + timedelta(days=2))
        self.assertEqual(result['status'], 'superseded')
        self.assertEqual(self.module()['lease_token'], 'new-owner')

    def test_only_current_saved_top20_and_unknown_scored_id_is_allowed(self):
        self.player.rank = 21; self.player.save(update_fields=['rank'])
        self.assertEqual(self.refresh()['status'], 'not_top20')
        self.player.rank = 1; self.player.save(update_fields=['rank'])
        self.season.is_active = False; self.season.save(update_fields=['is_active'])
        self.assertEqual(self.refresh()['status'], 'wrong_season')
        self.assertEqual(self.calls, [])
        self.season.is_active = True; self.season.save(update_fields=['is_active'])
        self.activity.scored_runs_json = {}; self.activity.save(update_fields=['scored_runs_json'])
        self.assertEqual(self.refresh()['status'], 'ready')

    def test_management_default_dry_run_has_no_writes_or_network(self):
        before = RaiderIOCharacterActivity.objects.values().get(pk=self.activity.pk)
        out = io.StringIO()
        with patch('botend.services.player_raid_observation_sources.requests.Session.get') as get:
            call_command('refresh_player_raid_observation', player_id=self.player.pk, stdout=out)
            self.assertFalse(get.called)
        self.assertEqual(json.loads(out.getvalue())['status'], 'dry_run')
        self.assertEqual(before, RaiderIOCharacterActivity.objects.values().get(pk=self.activity.pk))
        out = io.StringIO()
        with patch('botend.services.player_raid_observation_sources.requests.Session.get', side_effect=self.transport):
            call_command('refresh_player_raid_observation', player_id=self.player.pk, apply=True, stdout=out)
        self.assertEqual(json.loads(out.getvalue())['status'], 'ready')
        self.player.rank = 21; self.player.save(update_fields=['rank'])
        with self.assertRaises(CommandError):
            call_command('refresh_player_raid_observation', player_id=self.player.pk, apply=True, stdout=io.StringIO())

    def test_private_malformed_scope_still_hides_previous_facts(self):
        self.assertEqual(self.refresh()['status'], 'ready')
        self.details['characterDetails'].update(isPrivate=True, tier='wrong')
        self.assertEqual(self.refresh(now=NOW + timedelta(days=2))['status'], 'wrong_tier')
        self.assertFalse(self.module()['visible'])
        self.assertEqual(self.refresh(now=NOW + timedelta(days=2))['status'], 'backoff')

    def test_dynamic_tier_and_short_raid_not_global_wcl_numbers(self):
        from botend.services.player_raid_observation_sources import discover_sources, validate_catalog
        identity = player_identity(self.player)
        source = discover_sources(self.html.replace('tier=36', 'tier=91'), identity, self.season.rio_season)
        self.assertEqual(source['tier'], '91')
        self.assertTrue(source['api_url'].endswith('tier=91'))
        self.meta['raids'][0]['encounters'] = self.meta['raids'][0]['encounters'][:7]
        _, selected = validate_catalog(self.meta, self.season)
        self.assertEqual(selected, [])
        # An unrelated legacy raid's one-based ordinal is never repurposed as
        # the current raid's order (observed in the full authoritative catalog).
        self.meta['raids'].append(dict(id=999, slug='old-raid', name='Old', difficulty='mythic',
            encounters=[dict(ordinal=1, slug='old-boss', wowEncounterId=999, encounterId=999)]))
        _, selected = validate_catalog(self.meta, self.season)
        self.assertEqual(selected, [])

    def test_expired_lease_can_be_reclaimed_and_second_request_429_stops(self):
        original = self.transport
        def rate_limited(url, **kwargs):
            if url == CATALOG:
                return Mock(status_code=429, headers={'Retry-After': '1'})
            return original(url, **kwargs)
        result = refresh_raid_sources(self.player, transport=rate_limited, now=NOW)
        self.assertEqual(result['status'], 'rate_limited')
        self.assertEqual(result['requests'], 2)
        self.assertEqual(self.module()['next_attempt_at'], (NOW + timedelta(minutes=30)).isoformat())
        self.activity.payload_json['raid_first_kills'].update(status='fetching', lease_token='expired-owner',
            next_attempt_at=(NOW - timedelta(seconds=1)).isoformat())
        self.activity.save(update_fields=['payload_json'])
        self.assertEqual(self.refresh()['status'], 'ready')
        self.assertIsNone(self.module()['lease_token'])

    def test_full_captured_real_response_offline_replay(self):
        root = os.environ.get('RIO_RAID_REPLAY_DIR')
        if not root:
            self.skipTest('Set RIO_RAID_REPLAY_DIR for the external captured-response acceptance replay')
        root = Path(root)
        live = json.loads((root / 'raid-firstkill-live-responses.json').read_text())
        central = json.loads((root / 'raid-firstkill-central.json').read_text())
        self.assertIsNone(live['error'])
        calls = live['calls'][:3]
        self.assertTrue(all(c['status'] == 200 for c in calls))
        self.html, self.meta, self.details = calls[0]['body'], calls[1]['json'], calls[2]['json']
        self.catalog_url = calls[1]['url']
        self.season.raid_encounters = central['season']['raid_encounters']
        self.season.save(update_fields=['raid_encounters'])
        self.assertEqual(self.refresh()['status'], 'ready')
        self.assertEqual(self.calls, [c['url'] for c in calls])
        module = self.module()
        self.assertEqual(module['source']['character_id'], central['activity']['scored_ui']['characterId'])
        self.assertEqual(module['source']['tier'], '36')
        bosses = module['raids'][0]['encounters']
        self.assertEqual([(e['index'], e['wowEncounterId']) for e in bosses], [(6, 3421), (7, 3429), (8, 3492)])
        self.assertEqual([e['firstDefeated'] for e in bosses], ['2026-09-17T07:55:47.000Z',
            '2026-09-24T07:50:18.000Z', '2026-09-24T10:46:21.000Z'])
        from zoneinfo import ZoneInfo
        dates = [datetime.fromisoformat(e['firstDefeated'].replace('Z', '+00:00')).astimezone(
            ZoneInfo('Asia/Shanghai')).date().isoformat() for e in bosses]
        self.assertEqual(dates, ['2026-09-17', '2026-09-24', '2026-09-24'])
        from botend.services.player_raid_observation import project_raid_observation
        observation = project_raid_observation(self.activity, self.season, player_identity(self.player), now=NOW)
        self.assertTrue(observation['supported'])
        self.assertIn('7、8', observation['reason'])
        self.assertIn('2026-09-24', observation['reason'])
