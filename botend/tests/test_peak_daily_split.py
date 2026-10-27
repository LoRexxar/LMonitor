"""Real ORM + real loopback HTTP contracts (synthetic source payloads).

Only transport routing, the spec directory and sleep are replaced. Parsers,
collectors, TTL/backoff, persistence and both public snapshots are real.
"""
import copy
import json
import tempfile
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from unittest.mock import patch

import requests
from django.contrib.auth import get_user_model
from django.db import close_old_connections
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from botend.controller.plugins.portal.PortalPeakSpecRankMonitor import PortalPeakSpecRankMonitor
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.models import (MonitorTask, PlayerSpecTopPlayer, RaiderIOCharacterActivity,
                           RaiderIORunRoster, SeasonMeta)
from botend.services.rio_rankings_snapshot import read_rankings
from botend.services.character_bindings import bind_character
from botend.tests.test_player_raiderio_activity import payload
from botend.tests.test_raiderio_run_roster import details


class PeakDailySplitTests(TransactionTestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(season_key='mn-s2', season_name='S2',
            rio_season='season-mn-2', mplus_zone_id=1, raid_zone_id=2, is_active=True)
        self.task = MonitorTask.objects.create(name='peak split loopback', target='')
        self.rankings = [{'score': 4000 - i, 'character': {
            'name': f'Player{i}', 'path': f'/characters/eu/test-realm/Player{i}',
            'class': {'name': 'Priest'}, 'spec': {'name': 'Holy', 'role': 'healer'},
            'realm': {'slug': 'test-realm', 'name': 'Test Realm'},
            'region': {'slug': 'eu'},
        }} for i in range(20)]
        self.requests = []
        self.rejected = []
        self.profile_failures = set()
        self.profile_scores = {}
        self.stats_failures = set()
        self.pause_profile = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                self.do_GET()

            def do_GET(self):
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                owner.requests.append((parsed.path, query))
                status = 200
                if parsed.path.endswith('/rankings/specs'):
                    body = {'rankings': {'rankedCharacters': copy.deepcopy(owner.rankings)}}
                elif parsed.path.endswith('/characters/profile'):
                    if owner.pause_profile:
                        owner.entered.set()
                        if not owner.release.wait(15):
                            status = 504
                    name = query['name'][0]
                    body = payload(name)
                    if name.lower() in owner.profile_scores:
                        body['mythic_plus_scores_by_season'][0]['scores'] = {
                            'all': owner.profile_scores[name.lower()]}
                    body['gear'] = {'item_level_equipped': 300,
                        'items': {'head': {'item_id': 999, 'item_level': 300}}}
                    body['race'] = 'Human'
                    if name.lower() in owner.profile_failures:
                        status = 503
                elif parsed.path.endswith('/run-details'):
                    body = details(int(query['id'][0]))
                elif parsed.path == '/token':
                    body = {'access_token': 'loopback-only', 'expires_in': 3600}
                elif parsed.path.endswith('/statistics'):
                    body = {'melee_crit': {'rating_normalized': 1239, 'value': 32.9},
                            'melee_haste': {'rating_normalized': 626, 'value': 18.8},
                            'mastery': {'rating_normalized': 994, 'value': 59.2},
                            'versatility': 143, 'versatility_damage_done_bonus': 2.6}
                    if parsed.path.split('/')[-2] in owner.stats_failures:
                        status = 503
                else:
                    owner.rejected.append(self.path)
                    status, body = 404, {}
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def stop():
            self.release.set()
            server.shutdown()
            server.server_close()
            thread.join(5)
        self.addCleanup(stop)
        send = requests.adapters.HTTPAdapter.send

        def loopback(adapter, request, **kwargs):
            original = urlsplit(request.url)
            if original.hostname not in {'raider.io', 'oauth.battle.net', 'eu.api.blizzard.com'}:
                owner.rejected.append(request.url)
                raise AssertionError('unexpected external transport')
            request.url = urlunsplit(('http', f'127.0.0.1:{server.server_port}',
                                     original.path, original.query, ''))
            kwargs['proxies'] = {}
            return send(adapter, request, **kwargs)

        self.enterContext(patch.object(requests.adapters.HTTPAdapter, 'send', loopback))
        self.enterContext(patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.CLASS_SPEC_MAP',
                                {'Priest': ['Holy']}))
        self.enterContext(patch('time.sleep'))
        media = tempfile.TemporaryDirectory()
        self.addCleanup(media.cleanup)
        self.root = Path(media.name)
        self.enterContext(override_settings(MEDIA_ROOT=media.name,
            BATTLENET_CONFIG={'client_id': 'loopback', 'client_secret': 'loopback'}))

    def short(self):
        monitor = PortalPeakSpecRankMonitor(None, self.task)
        with patch.object(monitor, '_spec_list', return_value=[{'class_slug': 'priest', 'spec_slug': 'holy'}]):
            self.assertTrue(monitor.scan(''))
        self.assertEqual(self.rejected, [])

    def daily(self):
        self.assertTrue(SpecDetailPlayerMonitor(None, self.task).scan(''))
        self.assertEqual(self.rejected, [])

    def projected(self):
        return json.loads((self.root / 'aggregated' / str(self.season.pk) /
                           'Priest/Holy/leaderboard.json').read_text())

    def test_short_is_ranking_only_for_new_fresh_stale_and_failed_profiles(self):
        for i, age in enumerate((0, 2, 4)):
            PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
                realm='Test Realm', character_name=f'Player{i}', class_name='Priest', spec_name='Holy',
                rank=10, score=100, gear_json=[{'id': 123}], stats_json={'old': 1},
                stats_crawl_status=-1 if i == 2 else 1,
                last_updated=timezone.now() - timedelta(days=age))
        before = {p.pk: p for p in PlayerSpecTopPlayer.objects.all()}
        self.short()
        self.assertEqual([path for path, _ in self.requests], ['/api/mythic-plus/rankings/specs'])
        self.assertEqual(PlayerSpecTopPlayer.objects.filter(last_updated=None).count(), 17)
        self.assertEqual(RaiderIOCharacterActivity.objects.count(), 0)
        for p in before.values():
            old = (p.gear_json, p.stats_json, p.stats_crawl_status, p.last_updated)
            p.refresh_from_db()
            self.assertEqual((p.gear_json, p.stats_json, p.stats_crawl_status, p.last_updated), old)
        self.assertEqual(len(self.projected()['players']), 20)
        self.assertIsNotNone(read_rankings('peak', season=self.season.rio_season, region='world'))
        _, query = self.requests[0]
        self.assertEqual({k: query[k] for k in ('season', 'region', 'class', 'spec', 'page', 'pageSize')},
            {'season': ['season-mn-2'], 'region': ['world'], 'class': ['priest'],
             'spec': ['holy'], 'page': ['0'], 'pageSize': ['20']})

    def test_daily_initializes_placeholders_activity_roster_stats_and_preserves_failures(self):
        self.short()
        self.profile_failures = {'player0'}
        self.stats_failures = {'player1'}
        old = PlayerSpecTopPlayer.objects.get(character_name='Player0')
        old.gear_json = [{'id': 123}]
        old.save(update_fields=['gear_json'])
        self.daily()
        old.refresh_from_db()
        self.assertEqual(old.gear_json, [{'id': 123}])
        self.assertIsNone(old.last_updated)
        good = PlayerSpecTopPlayer.objects.get(character_name='Player2')
        self.assertEqual(good.gear_json[0]['id'], 999)
        self.assertEqual(good.stats_crawl_status, 1)
        self.assertIsNotNone(good.last_updated)
        failed_stats = PlayerSpecTopPlayer.objects.get(character_name='Player1')
        self.assertEqual(failed_stats.stats_crawl_status, -1)
        self.assertEqual(failed_stats.stats_json, {})
        self.assertEqual(RaiderIORunRoster.objects.get().status, 'ready')
        self.assertEqual(RaiderIOCharacterActivity.objects.filter(status='ready').count(), 19)
        count = len(self.requests)
        self.short()
        self.assertEqual([p for p, _ in self.requests[count:]], ['/api/mythic-plus/rankings/specs'])
        # A second daily invocation must respect source and Battle.net backoff.
        count = len(self.requests)
        self.daily()
        self.assertFalse(any('/player1/statistics' in p for p, _ in self.requests[count:]))
        old.refresh_from_db()
        self.assertIsNone(old.last_updated)
        self.assertEqual(old.gear_json, [{'id': 123}])

    def test_daily_inflight_old_rankings_cannot_undo_short_rank_score_or_departure(self):
        for i in range(20):
            PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
                realm='Test Realm', character_name=f'Player{i}', class_name='Priest', spec_name='Holy',
                rank=i + 1, score=4000 - i, last_updated=None)
        self.pause_profile = True
        errors = []
        def run():
            close_old_connections()
            try:
                self.daily()
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.entered.wait(10), 'daily never reached real HTTP barrier')
            self.rankings = list(reversed(self.rankings))
            self.rankings[0] = copy.deepcopy(self.rankings[0])
            self.rankings[0]['character']['name'] = 'NewArrival'
            self.rankings[0]['character']['path'] = '/characters/eu/test-realm/NewArrival'
            for i, row in enumerate(self.rankings):
                row['score'] = 5000 - i
            self.short()  # must publish while daily's profile HTTP is still blocked
            self.assertTrue(thread.is_alive())
            expected = list(PlayerSpecTopPlayer.objects.order_by('pk').values_list('pk', 'rank', 'score'))
            projection = self.projected()
        finally:
            self.release.set()
            thread.join(20)
        self.assertFalse(thread.is_alive(), 'daily did not terminate')
        self.assertEqual(errors, [])
        self.assertEqual(list(PlayerSpecTopPlayer.objects.order_by('pk').values_list('pk', 'rank', 'score')), expected)
        self.assertEqual(self.projected(), projection)
        self.assertIsNone(PlayerSpecTopPlayer.objects.get(character_name='Player19').rank)

    def bound_player(self, name='Player0', rank=None):
        player = PlayerSpecTopPlayer.objects.create(season_id=self.season.pk, region='eu',
            realm='Test Realm', character_name=name, class_name='Priest', spec_name='Holy',
            profile_url=f'https://raider.io/characters/eu/test-realm/{name}',
            rank=rank, score=100, gear_json=[{'id': 123}], last_updated=None)
        user = get_user_model().objects.create_user(f'binding-{name}')
        return player, bind_character(user, name, 'test-realm', 'eu')

    def binding_score_race(self, rank):
        player, binding = self.bound_player(rank=rank)
        incoming = copy.deepcopy(self.rankings)
        self.rankings = []  # daily must reach the binding fallback, not the Top20 path
        self.pause_profile = True
        errors = []

        def run():
            close_old_connections()
            try:
                self.daily()
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(self.entered.wait(10), 'binding never reached real HTTP barrier')
            self.assertEqual([q['name'] for p, q in self.requests
                              if p.endswith('/characters/profile')], [['Player0']])
            self.rankings = incoming
            self.short()
            self.assertTrue(thread.is_alive())
            player.refresh_from_db()
            self.assertEqual((player.rank, player.score), (1, 4000))
        finally:
            self.release.set()
            thread.join(20)
        self.assertFalse(thread.is_alive(), 'binding daily did not terminate')
        self.assertEqual(errors, [])
        player.refresh_from_db()
        binding.refresh_from_db()
        self.assertEqual(binding.player_id, player.pk)
        self.assertEqual(player.gear_json[0]['id'], 999)
        self.assertIsNotNone(player.last_updated)
        self.assertEqual((player.rank, player.score), (1, 4000))

    def test_daily_binding_promotion_during_http_preserves_short_score(self):
        self.profile_scores['player0'] = 2500
        self.binding_score_race(rank=None)

    def test_daily_ranked_binding_without_score_cannot_restore_stale_score(self):
        self.binding_score_race(rank=5)

    def test_daily_nonrank_binding_updates_valid_scores_and_keeps_last_good(self):
        self.rankings = []
        for index, (score, expected) in enumerate(((2500.5, 2500.5), (0, 0),
                (None, 100), (True, 100), ('2500', 100), (-1, 100))):
            with self.subTest(score=score):
                name = f'Personal{index}'
                player, binding = self.bound_player(name)
                self.profile_scores[name.lower()] = score
                self.daily()
                player.refresh_from_db()
                self.assertIsNone(player.rank)
                self.assertEqual(player.score, expected)
                self.assertEqual(player.gear_json[0]['id'], 999)
                self.assertIsNotNone(player.last_updated)
                binding.delete()

    def test_daily_insert_conflict_preserves_short_owned_fields(self):
        # Reachable when a WCL sample was absent at discovery, then short inserts
        # the same identity while the sample's profile HTTP is in flight.
        candidate = PlayerSpecTopPlayer(season_id=self.season.pk, region='eu', realm='Test Realm',
            character_name='Player0', class_name='Priest', spec_name='Holy', rank=None,
            score=None, gear_json=[{'id': 999}], last_updated=timezone.now())
        self.short()
        saved = SpecDetailPlayerMonitor(None, self.task)._save_profile_safely(candidate)
        saved.refresh_from_db()
        self.assertEqual((saved.rank, saved.score), (1, 4000))
        self.assertEqual(saved.gear_json, [{'id': 999}])
