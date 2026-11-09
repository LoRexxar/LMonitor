"""日报只读参与观察：真实 ORM 范围、中央 SQLite 与公开发布链路。"""
from contextlib import ExitStack
from datetime import date, datetime
import hashlib
from pathlib import Path
import tempfile
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.db import connection
from django.test import TestCase
from django.utils import timezone

from botend.models import SeasonMeta, WowTalentVersion
from botend.services import rio_statistics as rio
from botend.services import rio_statistics_views as views
from botend.services.rio_statistics_scope import active_scope
from botend.services.rio_statistics_storage import StorageError, encoded, key_for


class WowDailyRioSectionTests(TestCase):
    day = date(2026, 11, 5)
    fetched = '2026-11-05T01:00:00+00:00'
    upstream = '2026-11-04T23:00:00Z'

    def setUp(self):
        SeasonMeta.objects.update(is_active=False)
        WowTalentVersion.objects.update(is_active=False)
        self.season = SeasonMeta.objects.create(season_key='mn-s2-daily-rio', season_name='午夜第二赛季',
            rio_season='season-mn-2', game_build='12.1.5.70000', is_active=True,
            mplus_zone_id=1, raid_zone_id=2)
        self.version = WowTalentVersion.objects.create(key='retail-daily-rio', branch='retail',
            is_active=True, current_build='12.1.5.70000')
        temporary = tempfile.TemporaryDirectory(prefix='daily-rio-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / 'central'
        config = self.settings(RIO_STATISTICS_ROOT=self.root)
        config.enable(); self.addCleanup(config.disable)
        clock = patch.object(timezone, 'now', return_value=datetime.fromisoformat('2026-11-05T02:00:00+00:00'))
        clock.start(); self.addCleanup(clock.stop)
        network = patch('requests.sessions.Session.request', side_effect=AssertionError('不得联网'))
        network.start(); self.addCleanup(network.stop)
        views.clear_cache(); self.addCleanup(views.clear_cache)

    def collect(self, published_ext=None, day=None):
        from botend.wow_daily_report.rio_section import collect_rio_section
        return collect_rio_section(day or self.day, published_ext=published_ext)

    def metadata(self):
        scope = active_scope()
        meta = {'schema': 1, 'updated_at': self.fetched, 'scope': rio.public_scope(scope),
            'current_season': scope['rio_season'],
            'seasons': [{'slug': scope['rio_season'], 'name': scope['season_name'], 'dungeons': []}],
            'periods': [], 'raids': [], 'floors': {}, 'maps': {}}
        rio._write(self.root / 'catalog.json', meta)
        return meta

    def publish(self, region='cn', rows=None, fetched=None, refresh=True, data_changes=None):
        meta = self.metadata()
        coordinate = rio.coordinate('overview', {'region': region}, meta=meta)
        data = {'season': coordinate['season'], 'region': region, 'snapshot_date': '2026-11-06',
            'populations': [{'faction': 'all', 'population': 999999, 'percentiles': []}],
            'history': [{'date': d, 'population': p, 'cutoffs': []} for d, p in
                (rows if rows is not None else [('2026-10-29', 70), ('2026-11-04', 90),
                                                ('2026-11-05', 100), ('2026-11-06', 9999)])]}
        data.update(data_changes or {})
        rio.store().publish(coordinate, data, fetched_at=fetched or self.fetched,
            upstream_at=self.upstream, next_at='2026-11-06T01:00:00+00:00')
        if refresh:
            views.refresh_views()
        return coordinate

    @staticmethod
    def published(section):
        # Exactly the persisted contract: no numerical report facts or overview mirror.
        return {'sections': {'rio': {'references': [item['reference'] for item in section['items']]}}}

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes() for p in self.root.rglob('*') if p.is_file()}

    def test_public_cn_world_history_projection_is_readonly_and_reference_only(self):
        self.publish('cn'); self.publish('world', [('2026-10-29', 100), ('2026-11-04', 200), ('2026-11-05', 180)])
        before = self.files()
        def no_write(execute, sql, params, many, context):
            self.assertTrue(sql.lstrip().upper().startswith('SELECT'), sql)
            return execute(sql, params, many, context)
        with ExitStack() as stack:
            for target in [(rio.store(), 'ensure'), (rio.store(), 'publish'), (views, 'refresh_views'),
                           (Path, 'write_text'), (Path, 'write_bytes'), (Path, 'mkdir')]:
                stack.enter_context(patch.object(*target, side_effect=AssertionError('只读投影不得写入')))
            stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('不得网络连接')))
            stack.enter_context(connection.execute_wrapper(no_write))
            section = self.collect()
        self.assertEqual(self.files(), before)
        self.assertEqual((section['key'], section['title']), ('rio', '大秘境参与观察'))
        self.assertTrue(section['notice'])
        self.assertEqual([item['region'] for item in section['items']], ['cn', 'world'])
        for item, values in zip(section['items'], [(100, 10, 30), (180, -20, 80)]):
            self.assertEqual(set(item), {'region', 'region_label', 'season', 'season_name', 'data_date',
                'population', 'previous_date', 'day_delta', 'week_date', 'week_delta',
                'fetched_at', 'upstream_at', 'stale', 'url', 'heatmap_url', 'reference'})
            self.assertEqual((item['population'], item['day_delta'], item['week_delta']), values)
            self.assertEqual((item['data_date'], item['previous_date'], item['week_date']),
                ('2026-11-05', '2026-11-04', '2026-10-29'))
            self.assertEqual(item['region_label'], rio.REGIONS[item['region']])
            self.assertEqual(item['season_name'], '午夜第二赛季')
            self.assertEqual((item['fetched_at'], item['upstream_at'], item['stale']),
                (self.fetched, self.upstream, False))
            for field, tab in [('url', 'overview'), ('heatmap_url', 'heatmaps')]:
                parsed = urlsplit(item[field])
                self.assertEqual((parsed.scheme, parsed.netloc, parsed.path), ('', '', '/portal/rio-statistics/'))
                self.assertEqual(parse_qs(parsed.query), {'tab': [tab], 'season': ['season-mn-2'],
                    'region': [item['region']], 'days': ['14']})
            reference = item['reference']
            self.assertNotIn('population', reference)
            self.assertNotIn('data', reference)
            self.assertNotIn('day_delta', reference)
            self.assertEqual(reference['archive_day'], '2026-11-05')
            self.assertEqual(reference['data_date'], item['data_date'])
            body = rio.store().read_reference(reference)
            self.assertEqual(reference['digest'], hashlib.sha256(encoded(body)).hexdigest())

    def test_baseline_uses_latest_valid_historical_integer_and_exact_days(self):
        self.publish(rows=[('2026-10-27', 1), ('2026-10-28', 0), ('2026-10-30', 5),
            ('2026-11-03', 0), ('2026-11-04', 12), ('2026-11-05', None), ('2026-11-06', 999)])
        item = self.collect()['items'][0]
        self.assertEqual((item['data_date'], item['population'], item['day_delta'], item['week_delta']),
            ('2026-11-04', 12, 12, 12))
        self.assertEqual((item['previous_date'], item['week_date']), ('2026-11-03', '2026-10-28'))

    def test_missing_days_are_unknown_not_nearest_row_or_seventh_row(self):
        self.publish(rows=[('2026-10-26', 1), ('2026-10-27', 2), ('2026-10-28', 3),
            ('2026-10-30', 4), ('2026-10-31', 5), ('2026-11-01', 6), ('2026-11-02', 7), ('2026-11-05', 10)])
        item = self.collect()['items'][0]
        self.assertIsNone(item['day_delta']); self.assertIsNone(item['week_delta'])

    def test_zero_is_fact_but_none_boolean_negative_fraction_string_are_not(self):
        for invalid in [None, True, False, -1, 3.5, 3.0, '4']:
            with self.subTest(population=invalid):
                self.publish(rows=[('2026-10-28', invalid), ('2026-11-03', invalid),
                    ('2026-11-04', 0), ('2026-11-05', invalid)])
                item = self.collect()['items'][0]
                self.assertEqual((item['data_date'], item['population']), ('2026-11-04', 0))
                self.assertIsNone(item['day_delta']); self.assertIsNone(item['week_delta'])
        self.publish(rows=[('2026-11-05', None), ('2026-11-06', 10)])
        self.assertEqual(self.collect()['items'], [])

    def test_nonfinite_cannot_enter_central_encoded_publication(self):
        c = self.publish()
        for invalid in [float('nan'), float('inf'), float('-inf')]:
            with self.subTest(population=invalid), self.assertRaises(ValueError):
                rio.store().publish(c, {'season': c['season'], 'region': 'cn',
                    'history': [{'date': '2026-11-05', 'population': invalid}]},
                    fetched_at=self.fetched, upstream_at=self.upstream, next_at=None)
        self.assertEqual(self.collect()['items'][0]['population'], 100)

    def test_duplicate_dates_bad_dates_and_wrong_identity_are_not_accepted(self):
        cases = [({'history': [{'date': '2026-11-05', 'population': 1}] * 2}),
            {'history': [{'date': '2026-11-5', 'population': 10}]},
            {'region': 'world'}, {'season': 'season-mn-3'}, {'history': {}},
            {'history': [{'date': '2026-11-05', 'population': 10, 'region': 'world'}]}]
        for changes in cases:
            with self.subTest(changes=changes):
                self.publish(data_changes=changes)
                self.assertEqual(self.collect()['items'], [])

    def test_published_references_freeze_body_day_name_and_stale_across_updates_and_seasons(self):
        c = self.publish()
        rio.store().failure(c, 'temporary', attempted_at=self.fetched, next_at=self.fetched)
        views.refresh_views()
        first = self.collect()
        self.assertTrue(first['items'][0]['stale'])
        ext = self.published(first)
        self.publish(rows=[('2026-10-29', 10), ('2026-11-04', 30), ('2026-11-05', 500)],
            fetched='2026-11-06T01:00:00+00:00')
        self.assertEqual(self.collect(ext), first)
        self.season.rio_season = 'season-mn-3'
        self.season.season_name = '午夜第三赛季'
        self.season.save(update_fields=['rio_season', 'season_name'])
        self.publish(rows=[('2026-11-05', 999)])
        before = self.files()
        with patch.object(rio, 'active_scope', side_effect=AssertionError('冻结引用不得依赖当前范围')), \
                patch.object(views, 'read_public', side_effect=AssertionError('历史重建不得读当前展示')):
            self.assertEqual(self.collect(ext), first)
        self.assertEqual(self.files(), before)

    def test_same_archive_day_overwrite_or_missing_history_refuses_rebuild(self):
        self.publish()
        ext = self.published(self.collect())
        self.publish(rows=[('2026-11-05', 999)], fetched='2026-11-05T03:00:00+00:00')
        with self.assertRaises(StorageError): self.collect(ext)
        self.publish(fetched='2026-11-06T01:00:00+00:00')
        ext = self.published(self.collect())
        self.publish(fetched='2026-11-07T01:00:00+00:00')
        with rio.store().connection() as db:
            db.execute('DELETE FROM history WHERE day=?', ('2026-11-06',))
        with self.assertRaises(StorageError): self.collect(ext)

    def test_archive_day_is_fetched_shanghai_date_not_upstream_or_data_date(self):
        self.publish(fetched='2026-11-04T17:00:00+00:00')
        item = self.collect()['items'][0]
        self.assertEqual(item['reference']['archive_day'], '2026-11-05')
        self.assertEqual(rio.store().read_reference(item['reference'])['fetched_at'], '2026-11-04T17:00:00+00:00')

    def test_reference_validation_rejects_injected_hash_wrong_coordinates_and_dates(self):
        self.publish()
        reference = self.collect()['items'][0]['reference']
        invalid = [dict(reference, key='../../snapshots'), dict(reference, digest='../' + 'a' * 61),
            dict(reference, digest='0' * 64), dict(reference, archive_day='2026-11-04'),
            dict(reference, data_date='20261105'), dict(reference, kind='other'), dict(reference, stale=1)]
        for changes in [{'scope': 'wrong'}, {'scope': 'b' * 64}, {'module': 'runs'},
                        {'region': 'us'}, {'season': '../../season'}, {'dungeon': 'x'}]:
            invalid.append(dict(reference, coordinate={**reference['coordinate'], **changes}))
        for ref in invalid:
            with self.subTest(reference=ref), self.assertRaises(StorageError): rio.store().read_reference(ref)

    def test_corrupt_central_payload_or_row_metadata_refuses_reference(self):
        self.publish()
        reference = self.collect()['items'][0]['reference']
        # Corrupt both possible locations; a checksum hit must still use _unpack.
        with rio.store().connection() as db:
            db.execute('UPDATE snapshots SET raw_bytes=raw_bytes+1')
            db.execute('UPDATE history SET raw_bytes=raw_bytes+1')
        with self.assertRaises(StorageError): self.collect(self.published({'items': [{'reference': reference}]}))

    def test_unconfigured_pending_and_private_only_sources_do_not_initialize(self):
        self.assertEqual(self.collect()['items'], [])
        self.assertFalse(self.root.exists())
        self.season.is_active = False
        self.season.save(update_fields=['is_active'])
        self.assertEqual(self.collect()['items'], [])
        self.assertFalse(self.root.exists())
        self.season.is_active = True
        self.season.save(update_fields=['is_active'])
        self.publish(refresh=False)
        before = self.files()
        self.assertEqual(self.collect()['items'], [])
        self.assertEqual(self.files(), before)

    def test_publication_race_can_skip_unavailable_digest_but_never_use_private_new_value(self):
        self.publish()
        self.publish(rows=[('2026-11-05', 999)], fetched='2026-11-05T03:00:00+00:00', refresh=False)
        self.assertEqual(self.collect()['items'], [])
        views.refresh_views()
        self.assertEqual(self.collect()['items'][0]['population'], 999)

    def test_missing_reference_database_is_never_created_and_canonical_identity_is_checked_before_open(self):
        self.publish()
        ext = self.published(self.collect())
        original = ext['sections']['rio']['references'][0]
        for season in ['season-mn-02', 'season-mn-２', '../../season']:
            reference = {**original, 'coordinate': {**original['coordinate'], 'season': season}}
            reference['key'] = key_for(reference['coordinate'])
            with self.subTest(season=season), \
                    patch('botend.services.rio_statistics_storage.sqlite3.connect',
                          side_effect=AssertionError('非法引用必须在打开数据库前拒绝')), \
                    self.assertRaises(StorageError):
                rio.store().read_reference(reference)
        rio.store().path.unlink()
        before = self.files()
        with self.assertRaises(StorageError): self.collect(ext)
        self.assertFalse(rio.store().path.exists())
        self.assertEqual(self.files(), before)

    def test_reference_row_scope_timestamp_coordinate_and_naive_clock_are_validated(self):
        for column, value in [('scope', 'b' * 64), ('fetched_at', '2026-11-01T00:00:00Z'),
                              ('upstream_at', None)]:
            self.publish()
            reference = self.collect()['items'][0]['reference']
            with rio.store().connection() as db:
                for table in ('snapshots', 'history'):
                    db.execute(f'UPDATE {table} SET {column}=?', (value,))
            with self.subTest(column=column), self.assertRaises(StorageError):
                rio.store().read_reference(reference)
        c = self.publish()
        reference = self.collect()['items'][0]['reference']
        with rio.store().connection() as db:
            db.execute('UPDATE snapshots SET coordinate=?', ('{}',))
        with self.assertRaises(StorageError): rio.store().read_reference(reference)
        # publish accepts this legacy-shaped clock, but a reference must reject its ambiguity.
        rio.store().publish(c, {'season': c['season'], 'region': 'cn', 'history': []},
            fetched_at='2026-11-05T01:00:00', upstream_at=self.upstream, next_at=None)
        body = rio.store().read(c)[0]
        reference = {**reference, 'digest': hashlib.sha256(encoded(body)).hexdigest()}
        with self.assertRaises(StorageError): rio.store().read_reference(reference)

    def test_invalid_published_reference_container_or_duplicate_region_refuses_rebuild(self):
        self.publish()
        ext = self.published(self.collect())
        for references in [None, {}, ['not-a-reference'], ext['sections']['rio']['references'] * 2]:
            with self.subTest(references=references), self.assertRaises(StorageError):
                self.collect({'sections': {'rio': {'references': references}}})
        self.assertEqual(self.collect({'sections': {'rio': {'references': []}}})['items'], [])


    def test_complete_generator_freezes_only_citations_and_preserves_source_storage(self):
        import json
        from botend.models import WowDailyReport
        from botend.wow_daily_report.generator import generate_wow_daily_report
        from bs4 import BeautifulSoup
        self.publish('cn'); self.publish('world')
        before = self.files()
        with tempfile.TemporaryDirectory() as output, self.settings(BASE_DIR=output):
            result = generate_wow_daily_report(report_date=self.day, use_llm=False)
            body = Path(result['full_path']).read_text()
            dom = BeautifulSoup(body, 'html.parser')
            self.assertEqual(dom.select_one('.daily-section')['data-section'], 'rio')
            self.assertEqual(result['ext']['sections']['rio']['count'], 2)
            self.assertEqual(result['ext']['sections']['rio']['summary_error'], 'summary_disabled')
            self.assertEqual(len(result['ext']['sections']['rio']['references']), 2)
            self.assertEqual(self.files(), before)
            published = self.collect(published_ext=result['ext'])
            self.assertEqual([i['population'] for i in published['items']], [100, 100])
            old_row = WowDailyReport.objects.values().get(report_date=self.day)
            self.publish('cn', [('2026-11-05', 500)], fetched='2026-11-05T04:00:00+00:00')
            with self.assertRaises(StorageError):
                generate_wow_daily_report(report_date=self.day, use_llm=False)
            self.assertEqual(WowDailyReport.objects.values().get(report_date=self.day), old_row)
            self.assertEqual(Path(result['full_path']).read_text(), body)

    def test_published_count_mismatch_cannot_silently_drop_a_region(self):
        with self.assertRaises(StorageError):
            self.collect({'sections': {'rio': {'count': 2, 'references': []}}})
