"""TEST SYNTHESIS: website statistics contract over real loopback HTTP.

No external Raider.IO calls: exercise Collector → requests → bounded local HTTP
→ normalization → SQLite snapshot → published reader, with explicit source data.
"""
from copy import deepcopy
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

import requests
from django.test import SimpleTestCase
from django.db import InterfaceError
from django.utils import timezone

from botend.services import rio_statistics as rio
from botend.services.rio_statistics_dimensions import week_options
from botend.services.rio_statistics_views import read_public
from botend.tests import test_rio_statistics as fixtures


def read_view(c):
    return json.loads(read_public('runs', {k: v for k, v in c.items() if k not in ('module', 'scope')}).raw)['data']


class AutomaticRunStatisticsTests(SimpleTestCase):
    def setUp(self):
        fixtures.RioStatisticsTests.setUp(self)
        # Two explicitly synthetic weeks for every UI region; no production data.
        start = timezone.now().replace(microsecond=0) - timedelta(days=8)
        self.meta['periods'] = []
        for region in rio.REGIONS:
            if region == 'world':
                continue
            self.meta['seasons'][0]['starts'][region] = start.isoformat()
            self.meta['periods'].append({'region': region,
                'previous': {'period': 1077},
                'current': {'period': 1078, 'start': (start + timedelta(days=7)).isoformat(),
                            'end': (start + timedelta(days=14)).isoformat()}})
        rio._write(self.directory / 'catalog.json', self.meta)
        self.received = []
        self.mode = 'ready'
        self.unavailable_regions = set()
        self.overview_failures = {}
        self.mutate = lambda data: None
        case = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                parsed = urlsplit(self.path)
                q = {key: values[0] for key, values in parse_qs(parsed.query).items()}
                case.received.append((parsed.path, q))
                if parsed.path.endswith('/summary'):
                    payload = dict(deepcopy(fixtures.OVERVIEW), region=q['region'])
                    status, headers = case.overview_failures.get(q['region'], 200), {}
                elif parsed.path == '/api/v1/mythic-plus/static-data':
                    payload, status, headers = {'seasons': case.meta['seasons']}, 200, {}
                elif parsed.path == '/api/v1/raiding/static-data':
                    payload, status, headers = {'raids': []}, 200, {}
                elif parsed.path == '/api/v1/live-tracking/heatmaps/floors':
                    payload, status, headers = case.meta['floors'], 200, {}
                elif parsed.path == '/api/v1/periods':
                    payload, status, headers = {'periods': case.meta['periods']}, 200, {}
                elif parsed.path == '/api/v1/mythic-plus/affixes':
                    payload, status, headers = {}, 200, {}
                else:
                    payload = case.source(q)
                    case.mutate(payload)
                    status = (429 if case.mode == 'limited'
                              else 503 if q.get('region', 'world') in case.unavailable_regions else 200)
                    headers = {'Retry-After': '180'} if status == 429 else {}
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.session = requests.Session()
        self.session.trust_env = False
        self.addCleanup(self.session.close)

    def source(self, query):
        q = deepcopy(query)
        for key in ('minMythicLevel', 'maxMythicLevel', 'seasonWeekStart', 'seasonWeekEnd', 'version'):
            if key in q:
                q[key] = int(q[key])
        q.update(force=False, uniqueCharacters=False, timedOnly=False, viewTypePosition='enemy_position')
        region = q.get('region', 'world')
        weeks = week_options(self.meta, region)
        return {'type': 'runs-over-time', 'status': self.mode,
                'query': q, 'meta': {}, 'aggregated_at': '2026-11-01T00:00:00Z',
                'data': [{'period': week['period'], 'quantity': 5, 'timedRuns': 3,
                          'nonTimedRuns': 2} for week in weeks]}

    def transport(self, url, **kwargs):
        self.assertEqual(urlsplit(url).netloc, 'raider.io')
        return self.session.get(f'http://127.0.0.1:{self.server.server_port}' + urlsplit(url).path,
                                params=kwargs['params'], timeout=kwargs['timeout'])

    def collect(self, **kwargs):
        return rio.Collector(transport=self.transport, **kwargs)

    def test_nightly_collects_all_ui_run_ranges_without_expanding_other_regions(self):
        result = self.collect(max_requests=24).run(modules=['overview', 'runs'], maintain=True)
        run_requests = [(path, q) for path, q in self.received if path == '/api/statistics/get-data']
        self.assertEqual(len(run_requests), 18)
        self.assertEqual({(q.get('region', 'world'), int(q['minMythicLevel']), int(q['maxMythicLevel']))
                          for _, q in run_requests},
                         {(region, low, high) for region in rio.REGIONS
                          for low, high, _ in rio.LEVELS.values()})
        self.assertEqual({q['region'] for path, q in self.received if path.endswith('/summary')}, {'cn', 'world'})
        self.assertEqual((result['state'], result['ready'], result['requests']), ('complete', 20, 20))
        for _, q in run_requests:
            self.assertEqual((q['type'], q['groupBy'], q['version']), ('runs-over-time', 'period', '4'))
            self.assertEqual((q['seasonWeekStart'], q['seasonWeekEnd'], q['force']), ('1', '2', 'false'))
        for region in rio.REGIONS:
            for level in rio.LEVELS:
                c = rio.coordinate('runs', {'region': region, 'level': level}, meta=self.meta)
                entry = read_view(c)
                self.assertEqual(entry['snapshot']['state'], 'ready')
                self.assertEqual(entry['data']['source_kind'], 'raiderio-statistics')
                self.assertEqual(entry['data']['coverage'], {'first_week': 1, 'last_week': 2})
                self.assertEqual([r['quantity'] for r in entry['data']['rows']], [5, 5])
                self.assertEqual(entry['snapshot']['upstream_at'], '2026-11-01T00:00:00+00:00')
        count = len(self.received)
        again = self.collect().run(modules=['runs'], maintain=True)
        self.assertEqual((again['state'], again['requests'], len(self.received)), ('complete', 0, count))

    def test_mismatched_response_identity_or_week_window_never_publishes(self):
        c = rio.coordinate('runs', {'region': 'cn'}, meta=self.meta)
        mutations = [lambda d: d['query'].update(region='eu'),
                     lambda d: d['query'].update(region=''),
                     lambda d: d['query'].update(season='season-mn-1'),
                     lambda d: d['query'].update(minMythicLevel=16, maxMythicLevel=99),
                     lambda d: d['query'].update(seasonWeekStart=2),
                     lambda d: d['query'].update(seasonWeekEnd=1),
                     lambda d: d['query'].update(timedOnly=True),
                     lambda d: d['query'].update(force=True),
                     lambda d: d['query'].update(viewTypePosition='player_position'),
                     lambda d: d['data'][0].update(period=9999),
                     lambda d: d['data'].append(deepcopy(d['data'][0]))]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                self.mutate = mutate
                with self.assertRaises(ValueError):
                    self.collect().fetch(c, self.meta)
        self.assertNotIn('data', rio.read_entry(c))

    def test_generating_retains_last_good_and_waits_for_normal_retry(self):
        c = rio.coordinate('runs', {'region': 'cn'}, meta=self.meta)
        self.collect().run(modules=['runs'], regions=['cn'], levels=['10-plus'])
        before, _ = rio.store().read(c)
        self.mode = 'generating'
        result = self.collect(force=True).run(modules=['runs'], regions=['cn'], levels=['10-plus'])
        self.assertEqual((result['state'], result['ready'], result['failed']), ('partial', 0, 1))
        after, _ = rio.store().read(c)
        self.assertEqual(after, before)
        self.assertEqual(read_view(c)['snapshot']['state'], 'stale')
        retry = self.collect().run(modules=['runs'], regions=['cn'], levels=['10-plus'])
        self.assertEqual((retry['state'], retry['requests'], retry['remaining']), ('partial', 0, 1))

    def test_runs_use_shared_budget_and_persist_429_cooldown(self):
        first = self.collect(max_requests=1).run(modules=['runs'], regions=['cn'])
        self.assertEqual((first['requests'], first['ready'], first['remaining']), (1, 1, 2))
        self.mode = 'limited'
        limited = self.collect().run(modules=['runs'], regions=['cn'])
        self.assertEqual((limited['state'], limited['requests']), ('backoff', 1))
        count = len(self.received)
        later = self.collect(force=True).run(modules=['runs'], regions=['cn'])
        self.assertEqual((later['state'], later['requests'], len(self.received)), ('backoff', 0, count))

    def test_three_unavailable_run_ranges_do_not_stop_other_ranges(self):
        self.unavailable_regions = {'cn', 'world', 'us'}
        with patch('utils.log.logger.warning') as warning:
            result = self.collect(max_requests=4).run(
                modules=['runs'], regions=['cn', 'world', 'us', 'eu'], levels=['10-plus'])
        self.assertEqual((result['state'], result['requests'], result['failed'], result['ready']),
                         ('partial', 4, 3, 1))
        self.assertFalse((self.directory / 'cooldown.json').exists())
        messages = [call.args[0] % call.args[1:] for call in warning.call_args_list]
        for region in ('cn', 'world', 'us', 'eu'):
            c = rio.coordinate('runs', {'region': region}, meta=self.meta)
            available = region not in self.unavailable_regions
            self.assertEqual(read_view(c)['snapshot']['state'], 'ready' if available else 'pending')
            if not available:
                _, entry = rio.store().read(c)
                self.assertTrue(entry['error'])
                self.assertTrue(any(f'region={region} ' in message and 'http_status=503' in message
                                    for message in messages))

    def test_local_scope_or_display_failure_stops_further_source_requests(self):
        for broken, error in (('guard_scope', InterfaceError('not logged')),
                              ('publish_views', rio.StorageError('not logged'))):
            with self.subTest(broken=broken):
                self.received.clear()
                collector = self.collect(force=False)
                with patch.object(collector, broken, side_effect=error), patch.object(collector, 'metadata',
                        side_effect=lambda: (setattr(collector, 'scope', self.scope) or self.meta)):
                    result = collector.run(modules=['runs'], regions=['cn'])
                self.assertEqual(result['requests'], 1)
                self.assertEqual(len(self.received), 1)
                self.assertGreater(result['remaining'], 0)

    def test_overview_503_preserves_old_snapshots_and_continues_normal_and_forced_refresh(self):
        regions = ('cn', 'world', 'us', 'eu')
        for force in (False, True):
            with self.subTest(force=force), TemporaryDirectory() as directory, self.settings(RIO_STATISTICS_ROOT=directory):
                root = Path(directory)
                rio._write(root / 'catalog.json', self.meta)
                self.received.clear()
                self.overview_failures = {region: 503 for region in regions[:3]}
                old_time = '2026-11-01T00:00:00Z'
                old_payloads = {}
                for region in ('cn', 'eu'):
                    coord = rio.coordinate('overview', {'region': region}, meta=self.meta)
                    payload = rio.normalize_overview(dict(fixtures.OVERVIEW, region=region), coord['season'], region)
                    rio.store().publish(coord, payload, fetched_at=old_time, upstream_at=None, next_at=old_time)
                    old_payloads[region], _ = rio.store().read(coord)
                result = self.collect(max_requests=12, force=force).run(modules=['overview'], regions=regions)
                requests_made = [q['region'] for url, q in self.received if url.endswith('/summary')]
                self.assertEqual((result['state'], result['failed'], result['ready'], result['remaining']),
                                 ('partial', 3, 1, 0), requests_made)
                self.assertEqual(requests_made, ['world', 'us', 'cn', 'eu'])
                self.assertFalse((root / 'cooldown.json').exists())
                actual, entry = rio.store().read(rio.coordinate('overview', {'region': 'cn'}, meta=self.meta))
                self.assertEqual(actual, old_payloads['cn'])
                self.assertTrue(entry['error'])
                old_view = json.loads(read_public('overview', {'region': 'cn'}).raw)['data']
                self.assertEqual((old_view['snapshot']['state'], old_view['fetched_at'], old_view['data']),
                                 ('stale', old_time, old_payloads['cn']['data']))
                new_view = json.loads(read_public('overview', {'region': 'eu'}).raw)['data']
                self.assertEqual(new_view['snapshot']['state'], 'ready')
                self.assertEqual(new_view['data']['region'], 'eu')
                self.assertGreater(rio._stamp(new_view['fetched_at']), rio._stamp(old_time))

    def test_other_consecutive_server_errors_still_trip_existing_breaker(self):
        regions = ('cn', 'world', 'us', 'eu')
        self.overview_failures = {region: 502 for region in regions}
        result = self.collect(max_requests=5).run(modules=['overview'], regions=regions)
        self.assertEqual((result['state'], result['requests'], result['failed'], result['remaining']),
                         ('backoff', 3, 3, 1))
        self.assertTrue((self.directory / 'cooldown.json').is_file())

    def test_older_source_is_rejected_and_equal_ready_source_can_recover_failure(self):
        c = rio.coordinate('runs', {'region': 'cn'}, meta=self.meta)
        self.collect().run(modules=['runs'], regions=['cn'], levels=['10-plus'])
        before, _ = rio.store().read(c)
        self.mutate = lambda d: d.update(aggregated_at='2026-10-31T00:00:00Z')
        older = self.collect(force=True).run(modules=['runs'], regions=['cn'], levels=['10-plus'])
        self.assertEqual((older['state'], older['ready'], older['failed']), ('partial', 0, 1))
        after, _ = rio.store().read(c)
        self.assertEqual(after, before)
        self.mutate = lambda d: None
        equal = self.collect(force=True).run(modules=['runs'], regions=['cn'], levels=['10-plus'])
        self.assertEqual((equal['state'], equal['ready'], equal['failed']), ('complete', 1, 0))
        current, entry = rio.store().read(c)
        self.assertEqual(current['data'], before['data'])
        self.assertEqual(current['upstream_at'], before['upstream_at'])
        self.assertFalse(entry['error'])
        self.assertEqual(read_view(c)['snapshot']['state'], 'ready')
        self.assertTrue(all(q['force'] == 'false' for path, q in self.received
                            if path == '/api/statistics/get-data'))

    def test_source_missing_weeks_are_not_fabricated_as_zero(self):
        c = rio.coordinate('runs', {'region': 'kr', 'level': '20-plus'}, meta=self.meta)
        self.mutate = lambda d: d['data'].pop(0)
        payload, _ = self.collect().fetch(c, self.meta)
        self.assertEqual([row['period'] for row in payload['rows']], [1078])
        self.mutate = lambda d: d['data'][0].pop('timedRuns')
        with self.assertRaises(ValueError):
            self.collect().fetch(c, self.meta)

    def test_manual_runs_scope_stays_explicit_and_only_queries_does_not_expand(self):
        result = self.collect().run(modules=['runs'], regions=['eu'], levels=['15-plus'])
        self.assertEqual((result['ready'], result['requests']), (1, 1))
        self.assertEqual(self.received[0][1]['region'], 'eu')
        result = self.collect().run(modules=['runs'], only_queries=True, maintain=True)
        self.assertEqual(result['requests'], 0)
