"""维度隔离、凌晨订阅、完成记录口径与导入门禁回归。"""
from copy import deepcopy
import json
from io import StringIO
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, Client, override_settings
from django.urls import path
from django.core.management import call_command

from botend.portal.rio_statistics import RioStatisticsPrepareView
from botend.services import rio_statistics as rio
from botend.services.rio_statistics_dimensions import identities, week_options
from botend.services.rio_statistics_queries import subscribe, pending_queries
from botend.services.rio_statistics_runs import normalize_runs, import_file, import_feeds, StatisticsPending
from botend.tests import test_rio_statistics as rio_fixtures
GRID = rio_fixtures.GRID

urlpatterns = [path('prepare/', RioStatisticsPrepareView.as_view())]


class RioDimensionTests(SimpleTestCase):
    def setUp(self):
        rio_fixtures.RioStatisticsTests.setUp(self)
        self.meta['seasons'][0]['starts']['us'] = '2026-08-18T15:00:00Z'
        self.meta['affixes'] = [{'id': 9, 'name': '残暴'}, {'id': 10, 'name': '强韧'}]
        self.meta['periods'][0].update(current={'period': 1088, 'start': '2026-11-04T23:00:00Z', 'end': '2026-11-11T23:00:00Z'})
        rio._write(self.directory / 'catalog.json', self.meta)

    def c(self, **params):
        return rio.coordinate('heatmaps', {'season': 'season-mn-2', 'region': 'cn', 'dungeon': 'den-of-nalorakk', **params}, meta=self.meta)

    def test_id_sets_are_canonical_and_do_not_hit_broad_snapshot(self):
        first = self.c(include_classes='8,1,8', include_specs='63,62')
        second = self.c(include_classes='1,8', include_specs=[62, 63])
        self.assertEqual(first, second)
        self.assertNotEqual(rio.key_for(first), rio.key_for(self.c()))
        self.assertNotIn('filters', self.c())

    def test_conflicting_unknown_or_wrong_layer_filters_are_rejected(self):
        for params in ({'include_classes': '8', 'exclude_classes': '8'}, {'death_classes': '8', 'layer': 'player_spell'},
                       {'include_affixes': '999'}, {'include_specs': '999'}, {'includeClassIds': '8'},
                       {'zero': '1', 'layer': 'player_spell', 'deaths_min': '1'}, {'timer_min': '1.5'},
                       {'item_min': 'nan'}, {'deaths_max': '1.5'}, {'item_min': '200', 'item_max': '100'}):
            with self.subTest(params=params), self.assertRaises(ValueError): self.c(**params)

    def test_default_timer_is_equivalent_but_other_bounds_change_identity(self):
        self.assertEqual(self.c(), self.c(timer_max='1.0'))
        self.assertEqual(self.c(), self.c(timer_min='0', item_min='0', deaths_min='0'))
        self.assertNotEqual(self.c(), self.c(timer_max='2'))
        self.assertEqual(self.c(item_min='200.0')['filters']['item_min'], 200)

    def test_custom_levels_validate_both_ends(self):
        c = self.c(level='custom', min_level='31', max_level='39')
        self.assertEqual((c['min_level'], c['max_level']), (31, 39))
        self.assertEqual(self.c(level='custom', min_level='12', max_level='12'), self.c(level='level-12'))
        for low, high in [('1', '20'), ('15', '14'), ('2', '100'), ('nan', '20')]:
            with self.assertRaises(ValueError): self.c(level='custom', min_level=low, max_level=high)

    def test_regions_weeks_and_custom_range_use_real_period_ids(self):
        weeks = week_options(self.meta, 'cn')
        self.assertEqual((weeks[0]['period'], weeks[-1]['period']), (1077, 1088))
        self.assertEqual(weeks[0]['date'], '2026-08-20')
        self.assertIn('08/20', weeks[0]['label'])
        for params, expected in [({'period': 'this-reset'}, (1088, 1088)),
                                  ({'period': 'last-2-resets'}, (1086, 1087)),
                                  ({'period': 'custom', 'week_start': '2', 'week_end': '3'}, (1078, 1079))]:
            c = self.c(**params)
            self.assertEqual((c['min_period'], c['max_period']), expected)
        with self.assertRaises(ValueError): self.c(period='custom', week_start='3', week_end='2')

    def test_advanced_filter_values_are_forwarded_to_the_public_api(self):
        transport = Mock(return_value=Mock(status_code=200, headers={}, json=Mock(return_value=GRID)))
        c = self.c(include_classes='8', death_specs='63', include_affixes='9', item_min='200', timer_max='1.5',
                   period='custom', week_start='2', week_end='3', level='custom', min_level='11', max_level='13')
        rio.Collector(transport=transport).fetch(c, self.meta)
        params = transport.call_args.kwargs['params']
        self.assertEqual((params['includeClassIds'], params['includePlayerDeathSpecIds'], params['includeAffixIds']), ('8', '63', '9'))
        self.assertEqual((params['minMythicLevel'], params['maxMythicLevel'], params['minPeriod'], params['maxPeriod']), (11, 13, 1078, 1079))
        self.assertEqual((params['minItemLevel'], params['maxTimerFraction']), (200, 1.5))

    def test_class_and_spec_labels_are_chinese(self):
        data = identities()
        self.assertEqual(len(data['classes']), 13)
        for row in data['classes'] + data['specs']:
            self.assertRegex(row['label'], r'[\u3400-\u9fff]')

    def test_subscription_does_not_fetch_and_night_prioritizes_it_within_budget(self):
        c = self.c(include_classes='8')
        with patch('requests.Session.request', side_effect=AssertionError('登记不得回源')):
            self.assertEqual(subscribe(c, client='test')['state'], 'queued')
        self.assertEqual(pending_queries(self.meta), [c])
        transport = Mock(return_value=Mock(status_code=200, headers={}, json=Mock(return_value=GRID)))
        with patch('botend.services.rio_statistics_maps.collect_maps'):
            result = rio.Collector(max_requests=1, transport=transport).run(modules=['heatmaps'], regions=['cn'], levels=['10-plus'], periods=['season'])
        self.assertEqual(result['ready'], 1)
        self.assertEqual(transport.call_args.kwargs['params']['includeClassIds'], '8')

    def test_command_can_collect_only_an_explicit_advanced_query(self):
        path = self.directory / 'queries-input.json'
        rio._write(path, [{'dungeon': 'den-of-nalorakk', 'include_classes': '8'}])
        source = Mock(status_code=200, headers={}, json=Mock(return_value=GRID))
        with patch('requests.Session.get', return_value=source) as request, patch('botend.services.rio_statistics_maps.collect_maps'):
            call_command('sync_rio_statistics', query_file=str(path), only_queries=True, modules=['heatmaps'],
                         regions=['cn'], max_requests=8, allow_partial=True, stdout=StringIO())
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs['params']['includeClassIds'], '8')

    def test_subscription_has_per_client_and_global_bounds(self):
        for cls in range(1, 13): subscribe(self.c(include_classes=str(cls)), client='one')
        subscribe(self.c(include_classes='1'), client='one')
        with self.assertRaises(ValueError): subscribe(self.c(include_classes='13'), client='one')
        with patch('botend.services.rio_statistics_queries.MAX_QUERIES', 12):
            with self.assertRaises(ValueError): subscribe(self.c(include_classes='13'), client='two')

    def test_old_database_scope_subscription_is_never_collected(self):
        subscribe(self.c(include_classes='8'), client='test')
        meta = deepcopy(self.meta); meta['scope']['token'] = 'b' * 64
        self.assertEqual(pending_queries(meta), [])

    def test_version_change_releases_subscription_capacity(self):
        with patch('botend.services.rio_statistics_queries.MAX_QUERIES', 1):
            subscribe(self.c(include_classes='8'), client='test')
            new = self.c(include_classes='9'); new['scope'] = 'b' * 64
            with patch.object(rio, 'active_scope', return_value=dict(self.scope, token='b' * 64)):
                subscribe(new, client='test')
        rows = rio._load(self.directory / 'queries.json', {})['queries']
        self.assertEqual(len(rows), 1)
        self.assertEqual(next(iter(rows.values()))['coordinate']['scope'], 'b' * 64)

    def test_late_old_scope_registration_cannot_erase_new_scope_subscriptions(self):
        old = self.c(include_classes='8')
        new = dict(old, scope='b' * 64)
        with patch.object(rio, 'active_scope', return_value=dict(self.scope, token='b' * 64)):
            subscribe(new, client='test')
            with self.assertRaises(ValueError): subscribe(old, client='test')
        rows = rio._load(self.directory / 'queries.json', {})['queries']
        self.assertIn(rio.key_for(new), rows)

    @override_settings(ROOT_URLCONF=__name__)
    def test_prepare_endpoint_requires_csrf_and_only_registers_valid_queries(self):
        client = Client(enforce_csrf_checks=True)
        body = {'module': 'heatmaps', 'params': {'dungeon': 'den-of-nalorakk', 'include_classes': '8'}}
        self.assertEqual(client.post('/prepare/', data=json.dumps(body), content_type='application/json').status_code, 403)
        client.cookies['csrftoken'] = 'a' * 32
        with patch('requests.Session.request', side_effect=AssertionError('公开登记不得回源')):
            response = client.post('/prepare/', data=json.dumps(body), content_type='application/json', HTTP_X_CSRFTOKEN='a' * 32)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(pending_queries(self.meta)), 1)

    def source(self):
        # 最小、独立的真实形状，不依赖被忽略的调研文件是否随仓库发布。
        return {'type': 'runs-over-time', 'status': 'ready', 'aggregated_at': '2026-11-04T23:13:46Z',
                'query': {'type': 'runs-over-time', 'season': 'season-mn-2', 'region': 'cn', 'groupBy': 'period',
                          'minMythicLevel': 2, 'maxMythicLevel': 99, 'seasonWeekStart': 1, 'seasonWeekEnd': 12,
                          'version': 4, 'uniqueCharacters': False, 'timedOnly': False},
                'data': [{'period': 1077, 'quantity': 1540251, 'timedRuns': 1295572, 'nonTimedRuns': 244679}]}

    def test_run_counts_and_rate_keep_source_identity_and_denominator(self):
        c, payload, stamp = normalize_runs(self.source(), self.meta)
        self.assertEqual((c['module'], c['level']), ('runs', 'all'))
        self.assertEqual(payload['rows'][0]['quantity'], 1540251)
        self.assertAlmostEqual(payload['rows'][0]['timed_rate'], 1295572 / 1540251 * 100)
        self.assertEqual(payload['rows'][0]['week'], 1)

    def test_run_counts_reject_generating_different_scope_and_wrong_totals(self):
        source = self.source(); source['status'] = 'generating'
        with self.assertRaises(StatisticsPending): normalize_runs(source, self.meta)
        for change in ('season', 'uniqueCharacters', 'quantity', 'duplicate', 'unknown_filter'):
            source = self.source()
            if change == 'season': source['query']['season'] = 'season-mn-1'
            elif change == 'uniqueCharacters': source['query'][change] = True
            elif change == 'quantity': source['data'][0][change] += 1
            elif change == 'duplicate': source['data'].append(source['data'][0])
            else: source['query']['includeClassIds'] = '8'
            with self.subTest(change=change), self.assertRaises(ValueError): normalize_runs(source, self.meta)

    def test_import_is_local_idempotent_and_generating_cannot_replace_good_data(self):
        path = self.directory / 'source.json'; rio._write(path, self.source())
        with patch('botend.services.rio_statistics_scope.active_scope', return_value=self.scope), \
                patch('requests.Session.request', side_effect=AssertionError('导入不得请求未公开接口')):
            self.assertTrue(import_file(path, self.meta))
            self.assertFalse(import_file(path, self.meta))
            result = import_feeds(self.meta)
        self.assertEqual((result['imported'], result['skipped']), (0, 1))
        c = rio.coordinate('runs', {'level': 'all'}, meta=self.meta)
        good = rio.read_entry(c)['data']
        source = self.source(); source['status'] = 'generating'; rio._write(path, source)
        with self.assertRaises(StatisticsPending): import_file(path, self.meta)
        self.assertEqual(rio.read_entry(c)['data'], good)

    def test_import_scope_change_cannot_publish(self):
        path = self.directory / 'source.json'; rio._write(path, self.source())
        with patch('botend.services.rio_statistics_scope.active_scope', return_value=dict(self.scope, token='b' * 64)):
            with self.assertRaises(ValueError): import_file(path, self.meta)
        self.assertEqual(rio.read_entry(rio.coordinate('runs', {'level': 'all'}, meta=self.meta))['snapshot']['state'], 'pending')
