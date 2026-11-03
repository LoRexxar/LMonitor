"""Real ORM + private work files, no external WCL calls."""
import io
from unittest.mock import patch

from django.core.management import call_command
from django.db import connection
from django.test import TestCase, SimpleTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.models import SeasonMeta, SpecDungeonRanking
from botend.tests.aggregation_cache_helpers import isolate_work_cache
from botend.services import spec_aggregation_cache as cache
from botend.services.wcl_ranking_time import parse_ranking_fight_started_at
from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor as Monitor

MS = 1793630083252

RANKING = 'botend.controller.plugins.portal.SpecDetailRankingMonitor'


class RankingTimeParserTests(SimpleTestCase):
    def test_absolute_utc_milliseconds_only(self):
        actual = parse_ranking_fight_started_at({'startTime': MS, 'report': {'startTime': 1793620958979}})
        self.assertEqual(actual.timestamp(), MS / 1000)
        self.assertEqual(actual.utcoffset().total_seconds(), 0)
        self.assertIsNone(parse_ranking_fight_started_at({'report': {'startTime': MS}}))
        self.assertIsNone(parse_ranking_fight_started_at({'startTime': None}))
        for bad in (True, False, 0, '1793630083252', [], {}, float('nan'), float('inf'), 10**100, -1):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                parse_ranking_fight_started_at({'startTime': bad})


class RankingTimePersistenceTests(TestCase):
    def setUp(self):
        self.root = isolate_work_cache(self)
        self.season = SeasonMeta.objects.create(season_key='time-test', season_name='Time',
            mplus_zone_id=1, raid_zone_id=2,
            mplus_encounters=[{'id': 101, 'name': 'Dungeon'}])
        self.raw = {'name': 'Exact', 'amount': 123.25, 'hardModeLevel': 20, 'duration': 123456,
            'report': {'code': 'Report', 'fightID': 7, 'startTime': MS - 100000},
            'server': {'name': 'Realm', 'region': 'EU'}, 'startTime': MS}
        self.monitor = Monitor(None, None)
        self.enterContext(patch(RANKING + '.time.sleep'))
        self.enterContext(patch(RANKING + '.CLASS_SPEC_MAP', {'Mage': ['Arcane']}))
        self.enterContext(patch('requests.sessions.Session.request', side_effect=AssertionError('HTTP forbidden')))

    def row(self, **overrides):
        values = dict(season_id=self.season.pk, dungeon_id=101, dungeon_name='Dungeon',
            class_name='Mage', spec_name='Arcane', character_name='Exact', realm='Realm', region='EU',
            report_code='Report', fight_id=7, dps=123.25, keystone_level=20, clear_time=123456,
            medal='', guild_name='', last_updated=timezone.now())
        values.update(overrides)
        return SpecDungeonRanking.objects.create(**values)

    def save(self, rows=None, page=1, more=False, fingerprint='cycle'):
        scope = f'ranking-page:101:Mage:Arcane:dps:None:{page}'
        cache.save_work(self.season.pk, scope, fingerprint,
            {'rankings': rows if rows is not None else [self.raw], 'hasMorePages': more})
        return cache.work_path(self.season.pk, scope)

    def command(self, apply=False):
        output = io.StringIO()
        call_command('backfill_mplus_ranking_times', season=str(self.season.pk),
            dungeon=101, class_name='Mage', spec='Arcane', apply=apply, stdout=output)
        return output.getvalue()

    def test_dry_run_no_writes_apply_only_date_and_existing_preserved(self):
        row = self.row(gear_json=[{'large': 'unchanged'}], talents_json=[1])
        existing = self.row(character_name='Old', fight_started_at=timezone.now())
        existing_date = existing.fight_started_at
        before = SpecDungeonRanking.objects.filter(pk=row.pk).values().get()
        path = self.save()
        file_before = path.read_bytes(), path.stat().st_mtime_ns
        with CaptureQueriesContext(connection) as queries:
            output = self.command()
        self.assertIn('dry-run', output)
        self.assertFalse(any(q['sql'].lstrip().upper().startswith(('UPDATE', 'INSERT', 'DELETE')) for q in queries))
        row.refresh_from_db(); self.assertIsNone(row.fight_started_at)
        with CaptureQueriesContext(connection) as queries:
            output = self.command(True)
        row.refresh_from_db(); existing.refresh_from_db()
        self.assertEqual(row.fight_started_at.timestamp(), MS / 1000)
        self.assertEqual(existing.fight_started_at, existing_date)
        after = SpecDungeonRanking.objects.filter(pk=row.pk).values().get()
        before.pop('fight_started_at'); after.pop('fight_started_at')
        self.assertEqual(before, after)
        self.assertEqual(file_before, (path.read_bytes(), path.stat().st_mtime_ns))
        updates = [q['sql'] for q in queries if q['sql'].lstrip().upper().startswith('UPDATE')]
        self.assertEqual(len(updates), 1)
        self.assertNotIn('last_updated', updates[0])
        self.assertIn('"updated": 1', output)
        self.assertIn('"already": 1', output)
        self.assertTrue(all('gear_json' not in q['sql'] and 'talents_json' not in q['sql'] for q in queries))

    def test_exact_identity_and_stats_missing_date_or_report_never_guessed(self):
        self.row()
        for field, value in [('dps', 124), ('clear_time', 123), ('region', 'eu'),
                             ('realm', 'realm'), ('report_code', ''), ('fight_id', None)]:
            self.row(**{field: value})
        self.save()
        output = self.command(True)
        self.assertEqual(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).count(), 1)
        self.assertIn('partial', output)
        SpecDungeonRanking.objects.update(fight_started_at=None)
        for raw in ({**self.raw, 'startTime': None}, {**self.raw, 'report': None}):
            self.save([raw]); self.command(True)
            self.assertFalse(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).exists())

    def test_event_date_with_omitted_server_does_not_invent_character_identity(self):
        row = self.row(region='', realm='', character_name='Anonymous')
        before = SpecDungeonRanking.objects.filter(pk=row.pk).values().get()
        self.save([{**self.raw, 'server': {}, 'name': 'Anonymous'}])
        self.assertIn('"updated": 1', self.command(True))
        after = SpecDungeonRanking.objects.filter(pk=row.pk).values().get()
        self.assertEqual(after.pop('fight_started_at').timestamp(), MS / 1000)
        before.pop('fight_started_at')
        self.assertEqual(before, after)
        self.assertEqual(after['region'], '')
        self.assertEqual(after['realm'], '')

    def test_missing_date_preserves_only_the_exact_existing_log_date(self):
        date = parse_ranking_fight_started_at(self.raw)
        row = self.row(fight_started_at=date)
        missing = {key: value for key, value in self.raw.items() if key != 'startTime'}
        def collect(raw):
            with patch.object(self.monitor, '_wcl_graphql', return_value={'worldData': {'encounter': {
                    'characterRankings': {'rankings': [raw], 'hasMorePages': False}}}}):
                self.assertTrue(self.monitor._collect_dungeon_rankings(self.season))
        collect(missing)
        row.refresh_from_db()
        self.assertEqual(row.fight_started_at, date)
        collect({**missing, 'report': {'code': 'Report', 'fightID': 8}})
        replacement = SpecDungeonRanking.objects.get()
        self.assertNotEqual(replacement.pk, row.pk)
        self.assertIsNone(replacement.fight_started_at)

    def test_conflicting_exact_key_and_corrupt_cache_refused(self):
        self.row()
        self.save([self.raw, {**self.raw, 'startTime': MS + 1000}])
        output = self.command(True)
        self.assertIn('"conflict": 1', output)
        self.assertFalse(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).exists())
        path = self.save(); path.write_bytes(b'corrupt gzip')
        self.assertIn('partial', self.command(True))
        self.assertFalse(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).exists())

    def test_normal_base_json_to_collector_persists_time_and_bad_page_preserves(self):
        # Only the GraphQL transport is replaced: Base JSON parser, collector and ORM are real.
        with patch.object(self.monitor, '_wcl_graphql', return_value={'worldData': {'encounter': {
                'characterRankings': {'rankings': [self.raw], 'hasMorePages': False}}}}):
            self.assertTrue(self.monitor._collect_dungeon_rankings(self.season))
        row = SpecDungeonRanking.objects.get()
        self.assertEqual(row.fight_started_at.timestamp(), MS / 1000)
        for bad in (True, 'bad', float('nan')):
            payload = {'rankings': [{**self.raw, 'startTime': bad}], 'hasMorePages': False}
            self.assertFalse(Monitor._valid_ranking_page(payload))
            with patch.object(self.monitor, '_wcl_graphql', return_value={'worldData': {'encounter': {
                    'characterRankings': payload}}}):
                self.assertFalse(self.monitor._collect_dungeon_rankings(self.season))
            self.assertEqual(SpecDungeonRanking.objects.get().pk, row.pk)

    def test_three_real_hundred_row_pages_bounded_bulk_lightweight(self):
        for page in range(1, 4):
            raws = [{**self.raw, 'name': f'Player{i}'} for i in range((page - 1) * 100, page * 100)]
            self.save(raws, page, more=True)
            for raw in raws:
                self.row(character_name=raw['name'])
        with CaptureQueriesContext(connection) as queries:
            output = self.command(True)
        self.assertIn('"updated": 300', output)
        self.assertEqual(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).count(), 300)
        self.assertEqual(sum(q['sql'].lstrip().startswith('UPDATE') for q in queries), 1)
        self.assertLess(len(queries), 12)
        self.assertTrue(all('gear_json' not in q['sql'] for q in queries))

    def test_current_row_rechecked_after_file_read_before_atomic(self):
        from botend.management.commands import backfill_mplus_ranking_times as command
        stale = self.row()
        self.save()
        original = command.read_dates
        def race(scope):
            result = original(scope)
            SpecDungeonRanking.objects.filter(pk=stale.pk).update(dps=999)
            return result
        with patch.object(command, 'read_dates', side_effect=race):
            self.assertIn('"unknown": 1', self.command(True))
        stale.refresh_from_db()
        self.assertEqual(stale.dps, 999)
        self.assertIsNone(stale.fight_started_at)

    def test_file_read_race_preserves_new_nonnull_and_deleted_or_moved_rows(self):
        from botend.management.commands import backfill_mplus_ranking_times as command
        row = self.row()
        self.save()
        original = command.read_dates
        stamp = timezone.now()
        for change in ('date', 'move', 'delete'):
            SpecDungeonRanking.objects.filter(pk=row.pk).update(fight_started_at=None, dungeon_id=101)
            def race(scope):
                result = original(scope)
                query = SpecDungeonRanking.objects.filter(pk=row.pk)
                if change == 'date':
                    query.update(fight_started_at=stamp)
                elif change == 'move':
                    query.update(dungeon_id=102)
                else:
                    query.delete()
                return result
            with patch.object(command, 'read_dates', side_effect=race):
                self.assertIn('"updated": 0', self.command(True))
            if change == 'date':
                row.refresh_from_db(); self.assertEqual(row.fight_started_at, stamp)
            elif change == 'move':
                row.refresh_from_db(); self.assertIsNone(row.fight_started_at)
            else:
                self.assertFalse(SpecDungeonRanking.objects.filter(pk=row.pk).exists())

    def test_valid_gzip_bad_hash_and_wrong_schema_refused(self):
        import gzip
        import hashlib
        import json
        self.row()
        path = self.save()
        envelope = json.loads(gzip.decompress(path.read_bytes()))
        envelope['data']['schema'] = 999
        body = json.dumps(envelope['data'], separators=(',', ':')).encode()
        raw = b'{"data":' + body + b',"digest":"' + hashlib.sha256(body).hexdigest().encode() + b'"}'
        path.write_bytes(gzip.compress(raw))
        self.assertIn('partial', self.command(True))
        path = self.save()
        raw = gzip.decompress(path.read_bytes()).replace(b'Exact', b'Wrong')
        path.write_bytes(gzip.compress(raw))
        self.assertIn('partial', self.command(True))
        self.assertFalse(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).exists())

    def test_bad_date_cache_schema_and_digest_are_not_reused(self):
        self.row()
        self.save([{**self.raw, 'startTime': 'bad'}])
        self.command(True)
        self.assertFalse(SpecDungeonRanking.objects.filter(fight_started_at__isnull=False).exists())
        from botend.models import WclCollectionCycle
        from botend.services.spec_stats_snapshot import content_digest
        self.monitor._cycle = WclCollectionCycle.objects.create(season=self.season, slot=timezone.now(), owner='time')
        scope = 'ranking-page:101:Mage:Arcane:dps:None:1'
        bad = {'rankings': [{**self.raw, 'startTime': True}], 'hasMorePages': False}
        cache.save_work(self.season.pk, scope, content_digest(['ranking-page-v1', self.monitor._cycle.pk]), bad)
        good = {'rankings': [self.raw], 'hasMorePages': False}
        with patch.object(self.monitor, '_wcl_graphql', return_value={'worldData': {'encounter': {'characterRankings': good}}}) as transport:
            self.assertTrue(self.monitor._collect_dungeon_rankings(self.season))
            self.assertEqual(transport.call_count, 1)
        self.assertEqual(SpecDungeonRanking.objects.get().fight_started_at.timestamp(), MS / 1000)
