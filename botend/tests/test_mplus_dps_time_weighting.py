"""Time weighting changes only the mean of the already selected WCL runs."""
import math
import random
import tempfile
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext

from botend.models import SpecDungeonRanking
from botend.services import mplus_dps_rankings_service as service


AS_OF = datetime(2026, 11, 3, 12, 34, 56, 123456, tzinfo=dt_timezone.utc)
SEASON = {
    'id': 7,
    'season_key': 'time-weight-test',
    'season_name': 'Time weighting',
    'mplus_encounters': [{'id': i, 'name': f'Dungeon {i}'} for i in range(1, 9)],
}


def row(dps, player, *, dungeon_id=1, level=12, started_at=AS_OF, **extra):
    return {
        'dungeon_id': dungeon_id,
        'class_name': 'Mage',
        'spec_name': 'Arcane',
        'dps': dps,
        'keystone_level': level,
        'region': 'eu',
        'realm': 'test',
        'character_name': player,
        'last_updated': AS_OF - timedelta(minutes=1),
        'fight_started_at': started_at,
        **extra,
    }


def build(rows, as_of=AS_OF):
    return service.build_rankings_payload_from_rows(SEASON, iter(rows), generated_at=as_of)


def dungeon_stat(payload, dungeon_id=1):
    return payload['rankings'][f'dungeon:{dungeon_id}'][0]


class MplusDpsTimeWeightingTests(SimpleTestCase):
    def test_selected_runs_use_seven_day_half_life_only_for_average(self):
        rows = [
            row(100, 'newest'),
            row(200, 'week-old', level=11, started_at=AS_OF - timedelta(days=7)),
            row(400, 'two-weeks-old', level=10, started_at=AS_OF - timedelta(days=14)),
        ]
        payload = build(rows)
        stat = dungeon_stat(payload)
        self.assertEqual(stat['sample_size'], 3)
        self.assertEqual(stat['lower_dps'], 100)
        self.assertEqual(stat['highest_dps'], 400)
        self.assertAlmostEqual(stat['average_dps'], (100 + 200 * .5 + 400 * .25) / 1.75)
        self.assertEqual(payload['source_updated_at'], (AS_OF - timedelta(minutes=1)).isoformat())
        self.assertEqual(payload['method']['dungeon_metrics']['average'], 'time_weighted_mean')
        self.assertEqual(payload['method']['time_weighting']['kind'], 'exponential')
        self.assertEqual(payload['method']['time_weighting']['half_life_days'], 7)
        self.assertEqual(payload['method']['time_weighting']['timestamp'], 'fight_started_at')
        self.assertEqual(payload['method']['overall_weight'], '各副本最终样本数')

    def test_fractional_days_microseconds_and_timezones_use_actual_elapsed_time(self):
        # The Europe/Paris DST transition lies inside this interval. Dates, local
        # calendar days and .days truncation are not the elapsed-time contract.
        paris = ZoneInfo('Europe/Paris')
        as_of = datetime(2026, 10, 28, 12, 0, 0, 234567, tzinfo=paris)
        newest = as_of.astimezone(dt_timezone.utc) - timedelta(hours=3)
        oldest = newest - timedelta(days=7, hours=6, microseconds=123456)
        rows = [
            row(800, 'old', started_at=oldest.astimezone(paris)),
            row(200, 'new', level=11, started_at=newest.astimezone(ZoneInfo('Asia/Shanghai'))),
        ]
        weight = 2 ** (-(newest - oldest).total_seconds() / (7 * 86400))
        stat = dungeon_stat(build(rows, as_of=as_of))
        self.assertAlmostEqual(stat['average_dps'], (200 + 800 * weight) / (1 + weight), places=10)

    def test_time_never_changes_existing_selector_identity_order_or_cap(self):
        rows = [row(1000 + i, f'high-{i}', level=13) for i in range(150)]
        rows += [row(2000 + i, f'lower-{i}', level=12) for i in range(150)]
        # Same normalized player: high layer wins even with lower DPS, and a
        # same-level tie retains its original entry/date, not the newer log.
        rows += [
            row(1149, ' HIGH-149 ', level=13, region=' EU ', realm=' TEST '),
            row(10000, 'high-149', level=12),
        ]
        random.Random(42).shuffle(rows)
        for i, entry in enumerate(rows):
            entry['fight_started_at'] = AS_OF - timedelta(days=(i * 17) % 150)
        original = service.select_representative_rows(rows)
        self.assertEqual(len(original), 100)
        expected_entries = [
            (entry['keystone_level'], float(entry['dps']), (
                entry['region'].strip().lower(), entry['realm'].strip().lower(),
                entry['character_name'].strip().lower(),
            ), entry['fight_started_at'])
            for entry in original
        ]
        grouped = [
            (entry['keystone_level'], float(entry['dps']), (
                entry['region'].strip().lower(), entry['realm'].strip().lower(),
                entry['character_name'].strip().lower(),
            ), entry['fight_started_at'])
            for entry in rows
        ]
        self.assertEqual(service._select_representative_entries(grouped), expected_entries)
        self.assertEqual(
            service._select_representative_values([entry[:3] for entry in grouped]),
            [entry[1] for entry in expected_entries],
        )
        for cap in (0, 1, 17, 100, 500):
            with self.subTest(cap=cap):
                baseline = service.select_representative_rows(rows, max_samples=cap)
                self.assertEqual(
                    service._select_representative_values(
                        [entry[:3] for entry in grouped], max_samples=cap,
                    ),
                    [entry['dps'] for entry in baseline],
                )
                self.assertEqual(
                    [entry[3] for entry in service._select_representative_entries(grouped, cap)],
                    [entry['fight_started_at'] for entry in baseline],
                )
        selected_ids = {id(entry) for entry in original}
        # Bad dates outside the final sample are not a new selection filter.
        for entry in rows:
            if id(entry) not in selected_ids:
                entry['fight_started_at'] = None
        with patch.object(
            service, '_select_representative_entries', wraps=service._select_representative_entries,
        ) as select:
            stat = dungeon_stat(build(rows))
        actual = service._select_representative_entries(select.call_args.args[0])
        self.assertEqual(actual, expected_entries)
        self.assertEqual(stat['sample_size'], 100)
        self.assertEqual(stat['lower_dps'], min(entry['dps'] for entry in original))
        self.assertEqual(stat['highest_dps'], max(entry['dps'] for entry in original))
        weights = [2 ** (-(AS_OF - entry['fight_started_at']).total_seconds() / (7 * 86400)) for entry in original]
        expected = math.fsum(entry['dps'] * weight for entry, weight in zip(original, weights)) / math.fsum(weights)
        self.assertAlmostEqual(stat['average_dps'], expected)

    def test_same_dps_with_different_dates_keeps_the_selected_entry_date(self):
        rows = [
            row(400, 'same', started_at=AS_OF - timedelta(days=14)),
            row(400, 'same', started_at=AS_OF),
            row(300, 'other', started_at=AS_OF),
            row(100, 'low'), row(50, 'lowest'),
        ]
        stat = dungeon_stat(build(rows))
        self.assertEqual(stat['sample_size'], 4)
        self.assertAlmostEqual(stat['average_dps'], (400 * .25 + 300 + 100 + 50) / 3.25)

    def test_overall_still_uses_dungeon_sample_sizes_not_time_weight_sums(self):
        rows = [
            row(100, 'd1-new'),
            row(500, 'd1-old', level=11, started_at=AS_OF - timedelta(days=7)),
        ]
        rows += [
            row(1000, f'd{i}', dungeon_id=i, started_at=AS_OF - timedelta(days=140))
            for i in range(2, 9)
        ]
        payload = build(rows)
        overall = payload['rankings']['overall'][0]
        d1_average = (100 + 500 * .5) / 1.5
        self.assertAlmostEqual(dungeon_stat(payload)['average_dps'], d1_average)
        self.assertEqual(overall['sample_size'], 9)
        self.assertEqual(overall['dungeon_count'], 8)
        self.assertAlmostEqual(overall['average_dps'], (d1_average * 2 + 1000 * 7) / 9)
        self.assertAlmostEqual(overall['lower_dps'], (100 * 2 + 1000 * 7) / 9)
        self.assertAlmostEqual(overall['highest_dps'], (500 * 2 + 1000 * 7) / 9)
        self.assertEqual(build(rows[:-1])['rankings']['overall'], [])

    def test_without_new_logs_advancing_as_of_never_decays_dps(self):
        rows = [
            row(100, 'new'),
            row(500, 'old', level=11, started_at=AS_OF - timedelta(days=14)),
        ]
        original = build(rows)
        later = build(rows, as_of=AS_OF + timedelta(days=800000))
        self.assertEqual(original['rankings'], later['rankings'])
        self.assertNotEqual(original['generated_at'], later['generated_at'])
        self.assertEqual(original['source_updated_at'], later['source_updated_at'])

    def test_rank_and_equal_range_tiers_follow_weighted_mean_not_peak(self):
        rows = []
        for dungeon_id in range(1, 9):
            rows.extend([
                row(100, 'arcane-new', dungeon_id=dungeon_id),
                row(400, 'arcane-old', dungeon_id=dungeon_id, level=11,
                    started_at=AS_OF - timedelta(days=14)),
                row(200, 'fire', dungeon_id=dungeon_id, spec_name='Fire'),
            ])
        payload = build(rows)
        for scope in ['overall'] + [f'dungeon:{i}' for i in range(1, 9)]:
            with self.subTest(scope=scope):
                fire, arcane = payload['rankings'][scope]
                self.assertEqual((fire['spec_name'], fire['tier']), ('Fire', 'S'))
                self.assertEqual((arcane['spec_name'], arcane['tier']), ('Arcane', 'F'))
                self.assertEqual(arcane['average_dps'], 160)
                self.assertEqual(arcane['highest_dps'], 400)
                self.assertEqual(arcane['average_ratio'], 80)

    def test_collection_time_changes_freshness_but_not_weight_or_dps(self):
        rows = [
            row(100, 'new'),
            row(500, 'old', level=11, started_at=AS_OF - timedelta(days=7)),
        ]
        original = build(rows)
        rows[0]['last_updated'] = AS_OF
        rows[1]['last_updated'] = AS_OF - timedelta(days=1000)
        refreshed = build(rows)
        self.assertEqual(original['rankings'], refreshed['rankings'])
        self.assertEqual(refreshed['source_updated_at'], AS_OF.isoformat())

    def test_ancient_logs_do_not_underflow_and_same_age_keeps_arithmetic_mean(self):
        ancient = datetime(1, 1, 1, tzinfo=dt_timezone.utc)
        for second_date, expected in (
            (ancient, 300), (ancient + timedelta(days=7), (100 + 500 * 2) / 3),
        ):
            with self.subTest(second_date=second_date):
                stat = dungeon_stat(build([
                    row(100, 'a', started_at=ancient),
                    row(500, 'b', level=11, started_at=second_date),
                ]))
                self.assertAlmostEqual(stat['average_dps'], expected)
                self.assertEqual(stat['highest_dps'], 500)

    def test_generated_at_is_frozen_once_for_lazy_input_and_all_groups(self):
        def lazy_rows():
            self.assertEqual(now.call_count, 1)
            for dungeon_id in range(1, 9):
                yield row(200, f'd{dungeon_id}', dungeon_id=dungeon_id)
        with patch.object(service.timezone, 'now', side_effect=[AS_OF, AS_OF - timedelta(days=1)]) as now:
            payload = service.build_rankings_payload_from_rows(SEASON, lazy_rows())
        self.assertEqual(now.call_count, 1)
        self.assertEqual(payload['generated_at'], AS_OF.isoformat())
        self.assertEqual(payload['rankings']['overall'][0]['average_dps'], 200)

    def test_invalid_selected_date_fails_whole_publish_and_preserves_snapshot(self):
        invalid_dates = [
            None, '', '2026-11-01T00:00:00Z', 1793443200000,
            AS_OF.replace(tzinfo=None), AS_OF + timedelta(microseconds=1),
        ]
        season = SimpleNamespace(**SEASON)
        for invalid in invalid_dates:
            with self.subTest(invalid=invalid), tempfile.TemporaryDirectory() as temporary:
                with override_settings(MEDIA_ROOT=temporary):
                    path = service.snapshot_path(season.id)
                    previous = {'season': {'id': season.id}, 'version': 'previous'}
                    service.atomic_write_payload(path, previous)
                    previous_bytes = path.read_bytes()
                    rows = [row(200, 'valid', dungeon_id=i) for i in range(1, 8)]
                    rows += [row(500, 'invalid', dungeon_id=8, started_at=invalid)]
                    with (
                        patch.object(service, 'iter_season_rows', return_value=iter(rows)),
                        patch.object(service.timezone, 'now', return_value=AS_OF),
                        patch.object(service, 'atomic_write_payload', wraps=service.atomic_write_payload) as write,
                    ):
                        with self.assertRaisesMessage(RuntimeError, 'fight_started_at'):
                            service.publish_current_mplus_dps_rankings(season)
                    write.assert_not_called()
                    self.assertEqual(path.read_bytes(), previous_bytes)
                    self.assertEqual(service.load_published_mplus_dps_rankings(season), previous)
                    self.assertEqual(list(path.parent.glob('.tmp-mplus-dps-*.json')), [])

    def test_missing_selected_date_is_not_replaced_by_collection_time(self):
        entry = row(400, 'missing')
        del entry['fight_started_at']
        with self.assertRaisesMessage(RuntimeError, 'fight_started_at'):
            build([entry])

    def test_legacy_snapshot_without_time_metadata_stays_readable_and_unmodified(self):
        season = SimpleNamespace(**SEASON)
        previous = {
            'season': {'id': season.id},
            'method': {'dungeon_metrics': {'average': 'mean'}},
            'rankings': {'overall': [{'average_dps': 300}]},
        }
        with tempfile.TemporaryDirectory() as temporary, override_settings(MEDIA_ROOT=temporary):
            path = service.snapshot_path(season.id)
            service.atomic_write_payload(path, previous)
            original_bytes = path.read_bytes()
            with (
                patch.object(service, 'active_season', return_value=season),
                patch.object(service, 'build_current_mplus_dps_rankings_payload') as aggregate,
            ):
                projected = service.get_current_mplus_dps_rankings_payload()
                self.assertEqual(set(projected), set(previous))
                self.assertEqual(projected['season'], previous['season'])
                self.assertEqual(projected['rankings'], {'overall': [{'average_dps': 300, 'tier': 'S'}]})
                self.assertEqual(projected['method']['tier_mode'], 'equal_range')
                self.assertEqual(projected['method']['tier_count'], 7)
                self.assertEqual(
                    {key: value for key, value in projected['method'].items() if not key.startswith('tier_')},
                    previous['method'],
                )
            aggregate.assert_not_called()
            self.assertEqual(path.read_bytes(), original_bytes)
            self.assertNotIn('time_weighting', service.load_published_mplus_dps_rankings(season)['method'])

    def test_naive_as_of_is_rejected_rather_than_assuming_a_timezone(self):
        with self.assertRaisesMessage(RuntimeError, 'generated_at'):
            build([row(200, 'valid')], as_of=AS_OF.replace(tzinfo=None))


class MplusDpsTimeWeightingQueryTests(TestCase):
    def test_orm_reads_are_lazy_thin_and_keep_datetime_attached_to_each_run(self):
        SpecDungeonRanking.objects.bulk_create([
            SpecDungeonRanking(
                season_id=7, dungeon_id=1, dungeon_name='D1', class_name='Mage',
                spec_name='Arcane', character_name=player, region='eu', realm='test',
                dps=dps, keystone_level=level, fight_started_at=started,
                last_updated=AS_OF - timedelta(minutes=1), gear_json=[{'unused': 'gear'}],
                talents_json=[{'unused': 'talents'}],
            )
            for player, dps, level, started in (
                ('old', 500, 12, AS_OF - timedelta(days=7)),
                ('new', 100, 11, AS_OF),
            )
        ])
        season = SimpleNamespace(**SEASON)
        with CaptureQueriesContext(connection) as queries:
            rows = service.iter_season_rows(season)
            self.assertEqual(len(queries), 0)
            payload = service.build_rankings_payload_from_rows(season, rows, generated_at=AS_OF)
        self.assertEqual(len(queries), 1)
        sql = queries[0]['sql'].lower()
        self.assertIn('fight_started_at', sql)
        self.assertNotIn('gear_json', sql)
        self.assertNotIn('talents_json', sql)
        self.assertNotIn('talent_build_code', sql)
        self.assertNotIn('order by', sql)
        self.assertEqual(service.ROW_FIELDS[-1], 'fight_started_at')
        self.assertAlmostEqual(dungeon_stat(payload)['average_dps'], (500 * .5 + 100) / 1.5)
        self.assertEqual(payload['source_updated_at'], (AS_OF - timedelta(minutes=1)).isoformat())
