"""统计快照边界、限流、来源校验与公开零回源验证。"""
from copy import deepcopy
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, RequestFactory
from django.utils import timezone

from botend.portal.rio_statistics import RioStatisticsAPIView
from botend.services import rio_statistics as rio


FLOOR = {'id': 394, 'uiMapId': 2514, 'name': '测试楼层', 'default': 1,
         'ingameMinX': 6000, 'ingameMinY': 4000, 'ingameMaxX': 4500, 'ingameMaxY': 5000}
GRID = {'numRuns': 10, 'minSamplesRequired': 1, 'maxSamplesInGrid': 8,
        'gridsByFloor': {'394': {'5000,4500': 8}}}
OVERVIEW = {'season': 'season-mn-2', 'region': 'cn', 'snapshotDate': '2026-11-05',
            'updatedAt': '2026-11-05T01:00:00Z',
            'populations': [{'faction': 'all', 'population': 10,
                             'percentiles': [{'percentile': 1, 'cutoffScore': 3000}]}],
            'history': [{'date': '2026-11-04', 'population': 8, 'cutoffs': [{'percentile': 1, 'score': None}]},
                        {'date': '2026-11-05', 'population': 10, 'cutoffs': [{'percentile': 1, 'score': 3000}]}]}


class RioStatisticsTests(SimpleTestCase):
    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix='rio-statistics-tests-'))
        self.override = self.settings(RIO_STATISTICS_ROOT=self.directory)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.meta = {'schema': 1, 'updated_at': timezone.now().isoformat(), 'current_season': 'season-mn-2',
                     'seasons': [{'slug': 'season-mn-2', 'name': '午夜第二赛季',
                                  'starts': {'cn': '2026-08-19T23:00:00Z'}, 'ends': {'cn': '2030-01-01T00:00:00Z'},
                                  'dungeons': [{'slug': 'den-of-nalorakk', 'name': '测试副本', 'challenge_mode_id': 586}]}],
                     'raids': [{'slug': 'the-unbinding-of-kithix', 'name': '测试团本', 'bosses': 8,
                                'starts': {'cn': '2026-10-14T23:00:00Z'}, 'ends': {'cn': '2030-01-01T00:00:00Z'}}],
                     'floors': {'586': [deepcopy(FLOOR)]},
                     'affixes': [], 'periods': [{'region': 'cn', 'previous': {'period': 1087}}]}
        self.scope = {'token': 'a' * 64, 'id': 1, 'season_key': 'mn-s2', 'season_name': '午夜第二赛季',
                      'rio_season': 'season-mn-2', 'game_build': '12.1.5.70000', 'expansion_id': 11,
                      'version_key': 'retail', 'journal_release_id': None}
        self.meta['scope'] = deepcopy(self.scope)
        self.meta['maps'] = {}
        scope_patch = patch.object(rio, 'active_scope', return_value=self.scope)
        scope_patch.start()
        self.addCleanup(scope_patch.stop)
        names_patch = patch.object(rio, 'localize_catalog', side_effect=lambda data, scope: data)
        names_patch.start()
        self.addCleanup(names_patch.stop)
        rio._write(self.directory / 'catalog.json', self.meta)
        self.factory = RequestFactory()

    def overview_coordinate(self, region='cn'):
        return rio.coordinate('overview', {'season': 'season-mn-2', 'region': region})

    def heat_coordinate(self, **extra):
        return rio.coordinate('heatmaps', {'season': 'season-mn-2', 'region': 'cn',
                                          'dungeon': 'den-of-nalorakk', **extra})

    def response(self, data, status=200, **headers):
        return Mock(status_code=status, headers=headers, json=Mock(return_value=data), raise_for_status=Mock())

    def test_public_requests_never_fetch_or_register_jobs(self):
        c = self.overview_coordinate()
        rio._publish(c, rio.normalize_overview(OVERVIEW, c['season'], c['region']))
        before = rio._index()
        with patch('requests.get', side_effect=AssertionError('公开请求不得回源')), \
                patch('requests.Session.request', side_effect=AssertionError('公开请求不得回源')):
            for module, params in [('catalog', {}), ('overview', {'region': 'cn'}),
                                   ('overview', {'region': 'world'}), ('heatmaps', {'dungeon': 'den-of-nalorakk'})]:
                response = RioStatisticsAPIView.as_view()(self.factory.get('/', params), module=module)
                self.assertEqual(response.status_code, 200)
        self.assertEqual(rio._index(), before)
        self.assertFalse((self.directory / 'queries.json').exists())

    def test_failure_preserves_good_payload_and_cross_region_does_not_fallback(self):
        c = self.overview_coordinate()
        rio._publish(c, {'population': 10})
        rio._failure(c, '读取超时')
        self.assertEqual(rio.read_entry(c)['data'], {'population': 10})
        self.assertEqual(rio.read_entry(c)['snapshot']['state'], 'stale')
        self.assertNotIn('data', rio.read_entry(self.overview_coordinate('world')))

    def test_heatmap_has_matching_floors_and_finite_coordinates(self):
        result = rio.normalize_heatmap(GRID, [FLOOR])
        self.assertEqual(result['floors'][0]['points'], [[5000.0, 4500.0, 8]])
        bad = deepcopy(GRID)
        bad['gridsByFloor'] = {'999': {}}
        with self.assertRaises(ValueError): rio.normalize_heatmap(bad, [FLOOR])
        bad['gridsByFloor'] = {'394': {'nan,4500': 1}}
        with self.assertRaises(ValueError): rio.normalize_heatmap(bad, [FLOOR])
        bad['gridsByFloor'] = {'394': {'5000,4500': -1}}
        with self.assertRaises(ValueError): rio.normalize_heatmap(bad, [FLOOR])

    def test_last_reset_identity_rolls_over_instead_of_showing_previous_week(self):
        c = self.heat_coordinate(period='last-reset')
        rio._publish(c, rio.normalize_heatmap(GRID, [FLOOR]))
        changed = deepcopy(self.meta)
        changed['periods'][0]['previous']['period'] = 1088
        rio._write(self.directory / 'catalog.json', changed)
        current = self.heat_coordinate(period='last-reset')
        self.assertNotEqual(rio.key_for(c), rio.key_for(current))
        self.assertEqual(rio.read_entry(current)['snapshot']['state'], 'pending')

    def test_invalid_scope_is_rejected_without_guessing(self):
        for params in [{'season': '../../index'}, {'region': 'invalid'}]:
            with self.assertRaises(ValueError): rio.coordinate('overview', params)
        for params in [{'dungeon': 'unknown'}, {'layer': 'arbitrary'}, {'level': '999'},
                       {'zero': '1', 'layer': 'player_death'}]:
            with self.assertRaises(ValueError): self.heat_coordinate(**params)
        response = RioStatisticsAPIView.as_view()(self.factory.get('/', {'region': 'invalid'}), module='overview')
        self.assertEqual(response.status_code, 400)

    def test_source_identity_null_history_and_duplicate_dates(self):
        result = rio.normalize_overview(OVERVIEW, 'season-mn-2', 'cn')
        self.assertIsNone(result['history'][0]['cutoffs'][0]['score'])
        with self.assertRaises(ValueError): rio.normalize_overview(OVERVIEW, 'season-mn-2', 'world')
        bad = deepcopy(OVERVIEW)
        bad['history'].append(bad['history'][0])
        with self.assertRaises(ValueError): rio.normalize_overview(bad, 'season-mn-2', 'cn')

    def test_rate_limit_stops_batch_and_persists_cooldown_across_collectors(self):
        transport = Mock(return_value=self.response({}, status=429, **{'Retry-After': '120'}))
        result = rio.Collector(transport=transport).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual(transport.call_count, 1)
        self.assertEqual(result['state'], 'backoff')
        next_transport = Mock()
        next_result = rio.Collector(transport=next_transport, force=True).run(modules=['overview'])
        self.assertEqual(next_result['state'], 'backoff')
        next_transport.assert_not_called()

    def test_connection_retry_is_once_and_counts_towards_shared_budget(self):
        import requests
        transport = Mock(side_effect=[requests.ConnectionError('连接重置'), self.response(OVERVIEW)])
        collector = rio.Collector(max_requests=2, transport=transport)
        self.assertEqual(collector.get('/api/test')['season'], 'season-mn-2')
        self.assertEqual((collector.requests, transport.call_count), (2, 2))
        with self.assertRaises(RuntimeError): collector.get('/api/test')
        limited = Mock(side_effect=requests.Timeout('超时'))
        with self.assertRaises(requests.Timeout): rio.Collector(max_requests=1, transport=limited).get('/api/test')
        limited.assert_called_once()

    def test_budget_is_bounded_and_unchanged_ready_data_is_not_refetched(self):
        def source(url, **kwargs):
            result = deepcopy(OVERVIEW)
            result['region'] = kwargs['params']['region']
            return self.response(result)
        transport = Mock(side_effect=source)
        first = rio.Collector(max_requests=1, transport=transport).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual((first['requests'], first['ready']), (1, 1))
        second = rio.Collector(max_requests=1, transport=transport).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual((second['requests'], second['ready']), (1, 1))
        third = rio.Collector(max_requests=1, transport=transport).run(modules=['overview'], regions=['cn', 'world'])
        self.assertEqual(third['requests'], 0)

    def test_spell_zero_deaths_and_period_filter_are_sent_to_public_api(self):
        c = self.heat_coordinate(layer='player_spell', zero='1', period='last-reset')
        transport = Mock(return_value=self.response(GRID))
        payload, _ = rio.Collector(transport=transport).fetch(c, self.meta)
        url = transport.call_args.args[0]
        params = transport.call_args.kwargs['params']
        self.assertEqual(url, 'https://raider.io/api/v1/live-tracking/heatmaps/grid')
        self.assertEqual((params['region'], params['minPeriod'], params['maxPeriod'], params['maxPlayerDeaths']), ('cn', 1087, 1087, 0))
        self.assertIn('2825', params['includePlayerSpellIds'])
        self.assertEqual(payload['num_runs'], 10)

    def test_level_ranges_and_exact_levels_are_isolated_and_sent_inclusively(self):
        transport = Mock(return_value=self.response(GRID))
        collector = rio.Collector(transport=transport)
        for level, expected in [('level-2', (2, 2)), ('level-12', (12, 12)),
                                ('level-30', (30, 30)), ('10-14', (10, 14)), ('25-plus', (25, 99))]:
            with self.subTest(level=level):
                coordinate = self.heat_coordinate(level=level)
                collector.fetch(coordinate, self.meta)
                params = transport.call_args.kwargs['params']
                self.assertEqual((params['minMythicLevel'], params['maxMythicLevel']), expected)
                self.assertNotEqual(rio.key_for(coordinate), rio.key_for(self.heat_coordinate(level='10-plus')))
        rio._publish(self.heat_coordinate(level='10-plus'), {'num_runs': 10})
        self.assertEqual(rio.read_entry(self.heat_coordinate(level='level-12'))['snapshot']['state'], 'pending')
        for invalid in ['level-1', 'level-31', '14-10', '10,12']:
            with self.assertRaises(ValueError): self.heat_coordinate(level=invalid)

    def test_nightly_default_includes_ranges_and_single_levels(self):
        transport = Mock(return_value=self.response(GRID))
        with patch.object(rio, '_publish'), patch('botend.services.rio_statistics_maps.collect_maps'):
            result = rio.Collector(transport=transport).run(modules=['heatmaps'], regions=['cn'], periods=['season'])
        pairs = {(call.kwargs['params']['minMythicLevel'], call.kwargs['params']['maxMythicLevel'])
                 for call in transport.call_args_list}
        self.assertTrue({(2, 2), (12, 12), (30, 30), (10, 14), (25, 99)} <= pairs)
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(result['ready'], len(rio.DEFAULT_LEVELS) * 4)

    def test_database_version_change_hides_old_snapshot_immediately(self):
        c = self.overview_coordinate()
        rio._publish(c, {'population': 10})
        changed = dict(self.scope, token='b' * 64, game_build='12.1.5.70001')
        with patch.object(rio, 'active_scope', return_value=changed):
            self.assertEqual(rio.current_catalog()['scope_state'], 'updating')
            new_coordinate = self.overview_coordinate()
            self.assertEqual(rio.read_entry(new_coordinate)['snapshot']['state'], 'pending')
            self.assertNotEqual(new_coordinate, c)

    def test_version_change_during_collection_cannot_publish(self):
        transport = Mock(return_value=self.response(OVERVIEW))
        collector = rio.Collector(transport=transport)
        changed = dict(self.scope, token='b' * 64)
        scopes = iter([self.scope])
        with patch.object(rio, 'active_scope', side_effect=lambda: next(scopes, changed)):
            result = collector.run(modules=['overview'], regions=['cn'])
        self.assertEqual(result['ready'], 0)
        self.assertEqual(rio.read_entry(self.overview_coordinate())['snapshot']['state'], 'pending')

    def test_missing_database_binding_does_not_choose_upstream_current_season(self):
        from botend.services.rio_statistics_scope import ScopeUnavailable
        with patch.object(rio, 'active_scope', side_effect=ScopeUnavailable('未配置赛季标识')):
            self.assertEqual(rio.public_catalog()['current_season'], '')
            self.assertEqual(rio.public_catalog()['scope_state'], 'unconfigured')
            transport = Mock()
            with self.assertRaises(ScopeUnavailable): rio.Collector(transport=transport).run()
            transport.assert_not_called()

    def test_empty_ready_grid_is_distinct_from_missing_snapshot(self):
        empty = deepcopy(GRID)
        empty['numRuns'] = 0
        empty['gridsByFloor'] = {'394': {}}
        c = self.heat_coordinate()
        rio._publish(c, rio.normalize_heatmap(empty, [FLOOR]))
        result = rio.read_entry(c)
        self.assertEqual(result['snapshot']['state'], 'ready')
        self.assertEqual(result['data']['floors'][0]['points'], [])

    def test_no_event_grid_preserves_known_floor_for_empty_level_map(self):
        empty = dict(GRID, numRuns=0, gridsByFloor={})
        payload = rio.normalize_heatmap(empty, [FLOOR])
        self.assertEqual(payload['num_runs'], 0)
        self.assertEqual(payload['floors'][0]['id'], '394')
        self.assertEqual(payload['floors'][0]['points'], [])
