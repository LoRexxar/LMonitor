"""周三特刊：真实 ORM 范围、公开发布、中央精确引用与只读七天投影。"""
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
import sqlite3
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.test import TestCase

from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views as views
from botend.services.rio_statistics_storage import StorageError
from botend.tests import test_wow_daily_rio_section as daily_fixtures


class WowDailyRioWeeklyTests(TestCase):
    day = date(2026, 11, 10)  # 内容日周二；发布日周三，不取执行机器今天。
    fetched = '2026-11-05T01:00:00+00:00'
    upstream = '2026-11-04T23:00:00Z'

    # Reuse real ORM/central-publication fixtures, not their daily test methods.
    setUp = daily_fixtures.WowDailyRioSectionTests.setUp
    metadata = daily_fixtures.WowDailyRioSectionTests.metadata
    publish = daily_fixtures.WowDailyRioSectionTests.publish
    published = staticmethod(daily_fixtures.WowDailyRioSectionTests.published)
    files = daily_fixtures.WowDailyRioSectionTests.files

    def collect(self, published_ext=None, day=None, *, weekly=True):
        from botend.wow_daily_report.rio_section import collect_rio_section
        return collect_rio_section(day or self.day, published_ext, weekly=weekly)

    def rows(self, populations=None, *, day=None):
        day = day or self.day
        populations = populations or { -14: 50, -7: 70, -6: 75, -5: 73, -4: 80,
            -3: 85, -2: 88, -1: 95, 0: 100, 1: 9999 }
        return [((day + timedelta(days=offset)).isoformat(), population)
                for offset, population in populations.items()]

    def test_complete_week_keeps_daily_fields_and_independent_cn_world(self):
        self.publish('cn', self.rows())
        self.publish('world', self.rows({-14: 200, -7: 180, -6: 180, -5: 170,
            -4: 160, -3: 160, -2: 155, -1: 150, 0: 140}))
        daily = self.collect(weekly=False)
        section = self.collect()
        self.assertEqual((section['key'], section['title']), ('rio', '大秘境一周观察'))
        for phrase in ['角色', '非独立玩家', '国服与全球不相加', '7', '缺失不补']:
            self.assertIn(phrase, section['notice'])
        self.assertEqual([item['region'] for item in section['items']], ['cn', 'world'])
        for item, daily_item in zip(section['items'], daily['items']):
            self.assertEqual({k: v for k, v in item.items() if k != 'weekly'}, daily_item)
        expected = {'period_start': '2026-11-04', 'period_end': '2026-11-10',
            'baseline_date': '2026-11-03', 'previous_baseline_date': '2026-10-27',
            'start_population': 70, 'end_population': 100, 'net_change': 30,
            'previous_net_change': 20, 'change_vs_previous': 10, 'coverage_days': 7,
            'days': [{'date': d, 'population': p, 'change': change} for d, p, change in [
                ('2026-11-04', 75, 5), ('2026-11-05', 73, -2), ('2026-11-06', 80, 7),
                ('2026-11-07', 85, 5), ('2026-11-08', 88, 3), ('2026-11-09', 95, 7),
                ('2026-11-10', 100, 5)]]}
        self.assertEqual(section['items'][0]['weekly'], expected)
        world = section['items'][1]['weekly']
        self.assertEqual((world['net_change'], world['previous_net_change'], world['change_vs_previous']),
                         (-40, -20, -20))
        self.assertEqual([d['population'] for d in world['days']], [180, 170, 160, 160, 155, 150, 140])
        ext = self.published(section)
        self.assertEqual(set(ext['sections']['rio']), {'references'})
        for reference in ext['sections']['rio']['references']:
            self.assertEqual(set(reference), {'kind', 'coordinate', 'key', 'digest', 'archive_day',
                'data_date', 'season_name', 'stale'})

    def test_calendar_boundaries_include_exact_seven_dates(self):
        for day, dates, baseline, previous in [
            (date(2027, 1, 5), ['2026-12-30', '2026-12-31', '2027-01-01', '2027-01-02',
                '2027-01-03', '2027-01-04', '2027-01-05'], '2026-12-29', '2026-12-22'),
            (date(2024, 3, 5), ['2024-02-28', '2024-02-29', '2024-03-01', '2024-03-02',
                '2024-03-03', '2024-03-04', '2024-03-05'], '2024-02-27', '2024-02-20'),
            (date(2026, 12, 1), ['2026-11-25', '2026-11-26', '2026-11-27', '2026-11-28',
                '2026-11-29', '2026-11-30', '2026-12-01'], '2026-11-24', '2026-11-17'),
        ]:
            with self.subTest(day=day):
                self.publish(rows=self.rows(day=day))
                week = self.collect(day=day)['items'][0]['weekly']
                self.assertEqual([row['date'] for row in week['days']], dates)
                self.assertEqual((week['period_start'], week['period_end'], week['baseline_date'],
                    week['previous_baseline_date']), (dates[0], dates[-1], baseline, previous))
                self.assertEqual((week['net_change'], week['coverage_days']), (30, 7))

    def test_missing_tuesday_never_replaces_period_end_with_monday_source(self):
        self.publish(rows=self.rows({-14: 40, -7: 50, -6: 55, -5: 60, -4: 65,
            -3: 70, -2: 75, -1: 80, 1: 9999}))
        item = self.collect()['items'][0]
        self.assertEqual((item['data_date'], item['reference']['data_date'], item['population']),
                         ('2026-11-09', '2026-11-09', 80))
        week = item['weekly']
        self.assertEqual((week['period_start'], week['period_end'], week['coverage_days']),
                         ('2026-11-04', '2026-11-10', 6))
        self.assertEqual(week['start_population'], 50)
        self.assertIsNone(week['end_population']); self.assertIsNone(week['net_change'])
        self.assertEqual(week['previous_net_change'], 10)
        self.assertIsNone(week['change_vs_previous'])
        self.assertEqual(week['days'][-1], {'date': '2026-11-10', 'population': None, 'change': None})

    def test_holes_allow_endpoint_net_but_never_bridge_daily_gaps(self):
        self.publish(rows=self.rows({-14: 20, -7: 50, -6: 55, -4: 60, -1: 70, 0: 80}))
        week = self.collect()['items'][0]['weekly']
        self.assertEqual((week['coverage_days'], week['net_change'], week['previous_net_change'],
                          week['change_vs_previous']), (4, 30, 30, 0))
        self.assertEqual([r['change'] for r in week['days']], [5, None, None, None, None, None, 10])
        self.assertEqual([r['population'] for r in week['days']], [55, None, 60, None, None, 70, 80])

    def test_missing_either_baseline_and_both_endpoints_are_unknown(self):
        for populations, expected in [
            ({-14: 20, -8: 49, -6: 51, 0: 80}, (None, 80, None, None, None, 2)),
            ({-8: 49, -7: 50, -6: 51, 0: 80}, (50, 80, 30, None, None, 2)),
            ({-14: 20, -8: 49, -6: 51, -1: 70}, (None, None, None, None, None, 2)),
            ({-14: 20, -7: 50}, (50, None, None, 30, None, 0)),
        ]:
            with self.subTest(populations=populations):
                self.publish(rows=self.rows(populations))
                week = self.collect()['items'][0]['weekly']
                self.assertEqual(tuple(week[k] for k in ['start_population', 'end_population',
                    'net_change', 'previous_net_change', 'change_vs_previous', 'coverage_days']), expected)
                self.assertIsNone(week['days'][1]['change'])

    def test_zero_valid_invalid_scalars_not_facts_or_coverage(self):
        for invalid in [None, True, False, -1, 1.5, 3.0, '4']:
            with self.subTest(population=invalid):
                self.publish(rows=self.rows({-14: invalid, -7: 0, -6: 0, -5: invalid,
                    -4: 0, -3: 0, -2: 0, -1: 0, 0: invalid}))
                item = self.collect()['items'][0]
                self.assertEqual((item['data_date'], item['population']), ('2026-11-09', 0))
                week = item['weekly']
                self.assertEqual((week['coverage_days'], week['start_population']), (5, 0))
                self.assertIsNone(week['end_population']); self.assertIsNone(week['net_change'])
                self.assertIsNone(week['previous_net_change']); self.assertIsNone(week['change_vs_previous'])
                self.assertEqual([r['change'] for r in week['days']], [0, None, None, 0, 0, 0, None])
        self.publish(rows=self.rows({-14: 0, -7: 0, **{offset: 0 for offset in range(-6, 1)}}))
        week = self.collect()['items'][0]['weekly']
        self.assertEqual((week['coverage_days'], week['net_change'], week['previous_net_change'],
                          week['change_vs_previous']), (7, 0, 0, 0))

    def test_only_tuesday_content_date_is_legal_without_reading_sources(self):
        for offset in range(1, 7):
            with self.subTest(offset=offset), \
                    patch.object(views, 'read_public', side_effect=AssertionError('日期拒绝必须在读源前')), \
                    patch.object(rio, 'store', side_effect=AssertionError('日期拒绝必须在读中央前')), \
                    self.assertRaises(ValueError):
                self.collect(day=self.day + timedelta(days=offset))
        self.assertFalse(self.root.exists())

    def test_projection_function_is_pure_and_reuses_history_validation(self):
        from botend.wow_daily_report.rio_weekly import project_weekly
        import copy
        coordinate = self.publish(rows=self.rows())
        body = rio.store().read(coordinate)[0]
        original = copy.deepcopy(body)
        expected = self.collect()['items'][0]['weekly']
        with patch.object(rio, 'store', side_effect=AssertionError('纯投影不得读中央')), \
                patch.object(views, 'read_public', side_effect=AssertionError('纯投影不得读公开源')):
            self.assertEqual(project_weekly(body, self.day), expected)
            with self.assertRaises(ValueError): project_weekly(body, date(2026, 11, 11))
            for changes in [{'season': 'season-mn-3'}, {'region': 'world'},
                    {'history': [{'date': '2026-11-10', 'population': 3, 'season': 'season-mn-3'}]},
                    {'history': [{'date': '2026-11-10', 'population': 3}] * 2}]:
                with self.subTest(changes=changes), self.assertRaises(StorageError):
                    project_weekly({**body, 'data': {**body['data'], **changes}}, self.day)
        self.assertEqual(body, original)

    def test_frozen_week_survives_new_body_season_and_source_urls(self):
        coordinate = self.publish(rows=self.rows())
        rio.store().failure(coordinate, 'temporary', attempted_at=self.fetched, next_at=self.fetched)
        views.refresh_views()
        first = self.collect()
        self.assertTrue(first['items'][0]['stale'])
        ext = self.published(first)
        self.publish(rows=self.rows({-7: 1000, 0: 9000}), fetched='2026-11-06T01:00:00+00:00')
        self.season.rio_season = 'season-mn-3'
        self.season.season_name = '午夜第三赛季'
        self.season.save(update_fields=['rio_season', 'season_name'])
        self.publish(rows=self.rows({-7: 10000, 0: 90000}), fetched='2026-11-07T01:00:00+00:00')
        before = self.files()
        with patch.object(rio, 'active_scope', side_effect=AssertionError('冻结不得读当前赛季')), \
                patch.object(rio, 'catalog', side_effect=AssertionError('冻结不得读当前元数据')), \
                patch.object(views, 'read_public', side_effect=AssertionError('冻结不得读当前展示')):
            self.assertEqual(self.collect(ext), first)
        self.assertEqual(self.files(), before)
        for field in ['url', 'heatmap_url']:
            self.assertEqual(parse_qs(urlsplit(first['items'][0][field]).query)['season'], ['season-mn-2'])

    def test_readonly_no_source_initialization_private_state_or_publication_race_fallback(self):
        self.assertEqual(self.collect()['items'], [])
        self.assertFalse(self.root.exists())
        self.publish(rows=self.rows(), refresh=False)
        before = self.files()
        self.assertEqual(self.collect()['items'], [])
        self.assertEqual(self.files(), before)
        views.refresh_views()
        self.assertEqual(self.collect()['items'][0]['weekly']['net_change'], 30)
        self.publish(rows=self.rows({-7: 1000, 0: 9000}),
                     fetched='2026-11-05T03:00:00+00:00', refresh=False)
        before = self.files()
        self.assertEqual(self.collect()['items'], [])
        self.assertEqual(self.files(), before)
        views.refresh_views()
        self.assertEqual(self.collect()['items'][0]['weekly']['net_change'], 8000)

    def test_collection_and_frozen_rebuild_use_one_readonly_central_body_per_region(self):
        self.publish('cn', self.rows()); self.publish('world', self.rows())
        before = self.files()
        real_connect = sqlite3.connect
        central_connections = []
        def readonly_connect(database, *args, **kwargs):
            self.assertTrue(str(database).endswith('?mode=ro'), database)
            self.assertTrue(kwargs.get('uri'))
            db = real_connect(database, *args, **kwargs)
            db.set_authorizer(lambda action, *rest: sqlite3.SQLITE_DENY if action in {
                sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_ALTER_TABLE} else sqlite3.SQLITE_OK)
            central_connections.append(str(database))
            return db
        def no_orm_write(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with ExitStack() as stack:
            for target in [(rio.store(), 'ensure'), (rio.store(), 'publish'), (views, 'refresh_views'),
                    (Path, 'write_text'), (Path, 'write_bytes'), (Path, 'mkdir')]:
                stack.enter_context(patch.object(*target, side_effect=AssertionError('只读投影不得写入')))
            stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('不得网络连接')))
            stack.enter_context(patch('botend.services.rio_statistics_storage.sqlite3.connect',
                                      side_effect=readonly_connect))
            stack.enter_context(connection.execute_wrapper(no_orm_write))
            section = self.collect()
            self.assertEqual(len(central_connections), 2)
            self.assertEqual(self.collect(self.published(section)), section)
            self.assertEqual(len(central_connections), 4)
        self.assertEqual(self.files(), before)

    def test_lost_exact_digest_refuses_frozen_rebuild_not_private_or_nearest_fallback(self):
        self.publish(rows=self.rows())
        ext = self.published(self.collect())
        self.publish(rows=self.rows({-7: 1000, 0: 9000}), fetched='2026-11-05T03:00:00+00:00')
        before = self.files()
        with self.assertRaises(StorageError): self.collect(ext)
        self.assertEqual(self.files(), before)

    def test_public_history_never_merges_other_season_region_or_population_total(self):
        for changes in [{'season': 'season-mn-3'}, {'region': 'world'},
                {'history': [{'date': '2026-11-10', 'population': 100, 'season': 'season-mn-3'}]},
                {'history': []}]:
            with self.subTest(changes=changes):
                self.publish(rows=self.rows(), data_changes=changes)
                self.assertEqual(self.collect()['items'], [])
