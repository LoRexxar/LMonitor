"""Bounded short reconciliation on real ORM/InnoDB; no external transport."""
import json
import os
import threading
from unittest.mock import Mock, patch

from django.db import close_old_connections, connection, transaction, OperationalError
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext

from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.models import PlayerSpecTopPlayer, SeasonMeta


class PeakRankBoundedTests(TransactionTestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='bounded', season_name='Bounded', rio_season='season-bounded',
            mplus_zone_id=1, raid_zone_id=1, is_active=True,
        )
        self.monitor = SpecDetailPlayerMonitor(Mock(), Mock())
        self.enterContext(patch(
            'botend.controller.plugins.portal.SpecDetailAggregationMonitor.'
            'SpecDetailAggregationMonitor.refresh_leaderboard_projection'))
        self.enterContext(patch('requests.sessions.Session.request',
                                side_effect=AssertionError('short task used transport')))

    def player(self, name, **extra):
        fields = dict(season_id=self.season.pk, region='eu', realm='Hyjal',
                      character_name=name, class_name='Mage', spec_name='Arcane',
                      rank=None, score=100, gear_json=[{'preserved': True}])
        fields.update(extra)
        return PlayerSpecTopPlayer.objects.create(**fields)

    def preload(self, names):
        return self.monitor.preload_peak_rankings(
            rio_season=self.season.rio_season, class_name='Mage', spec_name='Arcane',
            rankings=[{'score': 4000 - i, 'character': {
                'name': name, 'region': {'slug': 'EU'}, 'realm': {'name': ' Hyjal '},
            }} for i, name in enumerate(names)],
        )

    def independent(self, fn):
        result, errors = [], []
        def run():
            close_old_connections()
            try:
                result.append(fn())
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join(8)
        self.assertFalse(thread.is_alive(), 'independent DB connection did not terminate')
        if errors:
            raise errors[0]
        return result[0]

    def test_history_never_hydrates_and_twenty_candidates_keep_scope_and_content(self):
        count = int(os.environ.get('PEAK_HISTORY_ROWS', '2000'))
        PlayerSpecTopPlayer.objects.bulk_create([
            PlayerSpecTopPlayer(season_id=self.season.pk, region='eu', realm='Hyjal',
                character_name=f'History{i}', class_name='Mage', spec_name='Arcane',
                rank=None, score=100)
            for i in range(count)
        ], batch_size=1000)
        ranked = [self.player(f'Ranked{i}', rank=i + 1) for i in range(20)]
        returning = self.player('Mägé')
        repaired = self.player('Repair', class_name='Warlock')
        decoys = [self.player('New', realm='Other'), self.player('New', region='us'),
                  self.player('New', spec_name='Fire'),
                  self.player('New', season_id=self.season.pk + 100)]
        before = list(PlayerSpecTopPlayer.objects.filter(
            pk__in=[p.pk for p in [*ranked, returning, repaired, *decoys]]).values())
        loaded = []
        from_db = PlayerSpecTopPlayer.from_db
        def record(*args, **kwargs):
            row = from_db(*args, **kwargs)
            loaded.append(row.pk)
            return row
        names = [f'Ranked{i}' for i in range(17)] + ['Mägé', 'Repair', 'New']
        with patch.object(PlayerSpecTopPlayer, 'from_db', side_effect=record), \
                CaptureQueriesContext(connection) as queries:
            result = self.preload(names)
        expected_loaded = {p.pk for p in [*ranked, returning, repaired]}
        self.assertEqual(set(loaded), expected_loaded)
        self.assertEqual(len(loaded), len(expected_loaded))
        self.assertEqual(result, {'new': 1, 'initialized': 0, 'updated': 19, 'departed': 3})
        self.assertEqual(PlayerSpecTopPlayer.objects.filter(
            season_id=self.season.pk, class_name='Mage', spec_name='Arcane',
            rank__isnull=False).count(), 20)
        for old in before:
            after = PlayerSpecTopPlayer.objects.values().get(pk=old['id'])
            for field, value in old.items():
                if field not in ('class_name', 'rank', 'score'):
                    self.assertEqual(after[field], value)
            if old['id'] in {p.pk for p in decoys}:
                self.assertEqual(after, old)
        repaired.refresh_from_db()
        self.assertEqual((repaired.class_name, repaired.rank), ('Mage', 19))
        returning.refresh_from_db()
        self.assertEqual(returning.rank, 18)
        new = PlayerSpecTopPlayer.objects.get(season_id=self.season.pk, region='eu',
            realm='Hyjal', character_name='New', spec_name='Arcane')
        self.assertEqual((new.rank, new.gear_json, new.last_updated), (20, [], None))
        selects = [q['sql'] for q in queries if q['sql'].startswith('SELECT')
                   and 'wow_spec_top_player' in q['sql']]
        self.assertEqual(len(selects), 4)  # current ranks + three exact unique-key probes
        for sql in selects:
            self.assertNotIn('gear_json', sql)
            if connection.vendor == 'mysql':
                self.assertIn('FOR UPDATE', sql)
        if connection.vendor == 'mysql':
            plans = []
            with transaction.atomic(), connection.cursor() as cursor:
                for sql in selects:
                    cursor.execute('EXPLAIN ' + sql)
                    plans.append([dict(zip([c[0] for c in cursor.description], row))
                                  for row in cursor.fetchall()])
            self.assertEqual(plans[0][0]['key'], 'idx_peak_current_rank')
            self.assertLessEqual(plans[0][0]['rows'], 40)
            self.assertTrue(all(plan[0]['type'] == 'const' for plan in plans[1:]))
            print('PEAK_BOUND_EVIDENCE', json.dumps({
                'history_rows': count, 'hydrated_rows': len(loaded),
                'candidate_count': len(names), 'sql': selects, 'explain': plans,
            }, ensure_ascii=False, default=str), flush=True)
        with CaptureQueriesContext(connection) as repeated:
            self.assertEqual(self.preload(names),
                {'new': 0, 'initialized': 0, 'updated': 0, 'departed': 0})
        self.assertFalse(any(q['sql'].startswith(('UPDATE', 'INSERT', 'DELETE')) for q in repeated))

    def test_mysql_collation_alias_does_not_depart_or_overwrite_first_candidate(self):
        if connection.vendor != 'mysql':
            self.skipTest('MySQL collation contract')
        # 0900_ai_ci equates these, but Python's NFKD accent stripping does not.
        player = self.player('Straße', rank=7)
        with connection.cursor() as cursor:
            cursor.execute('SELECT %s = %s', ['Straße', 'Strasse'])
            self.assertEqual(cursor.fetchone()[0], 1)
        self.assertEqual(self.preload(['Strasse', 'Straße']),
            {'new': 0, 'initialized': 0, 'updated': 1, 'departed': 0})
        player.refresh_from_db()
        self.assertEqual((player.rank, player.score, player.gear_json),
                         (1, 4000, [{'preserved': True}]))

    def test_candidate_and_current_rows_are_locked_but_unrelated_history_is_not(self):
        if connection.vendor != 'mysql':
            self.skipTest('InnoDB independent-connection row-lock contract')
        current = self.player('Current', rank=1)
        returning = self.player('Returning')
        history = self.player('History')
        # Represent the history-heavy production table. On a three-row table
        # MySQL may prefer a table scan (and lock scanned records) over any index.
        PlayerSpecTopPlayer.objects.bulk_create([
            PlayerSpecTopPlayer(season_id=self.season.pk, region='eu', realm='Hyjal',
                character_name=f'History{i}', class_name='Mage', spec_name='Arcane',
                rank=None, score=100)
            for i in range(2000)
        ], batch_size=1000)
        checked = []
        save = self.monitor._save_changed_profile
        def probe(pk):
            try:
                with transaction.atomic():
                    PlayerSpecTopPlayer.objects.select_for_update(nowait=True).get(pk=pk)
                return False
            except OperationalError as exc:
                if exc.args[0] not in (1205, 3572):
                    raise
                return True
        def check(profile, values):
            self.assertTrue(self.independent(lambda: probe(profile.pk)))
            self.assertFalse(self.independent(lambda: probe(history.pk)))
            checked.append(profile.pk)
            return save(profile, values)
        with patch.object(self.monitor, '_save_changed_profile', side_effect=check):
            self.preload(['Current', 'Returning'])
        self.assertEqual(checked, [current.pk, returning.pk])

    def test_concurrent_unique_insert_reuses_locked_winner_without_content_overwrite(self):
        if connection.vendor != 'mysql':
            self.skipTest('InnoDB concurrent unique insert contract')
        winner_ids = []
        def intercept(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO `wow_spec_top_player`') and not winner_ids:
                winner_ids.append(self.independent(lambda: self.player(
                    'Race', class_name='Warlock', gear_json=[{'winner': True}]).pk))
            return execute(sql, params, many, context)
        with connection.execute_wrapper(intercept):
            result = self.preload(['Race'])
        self.assertEqual(result, {'new': 0, 'initialized': 0, 'updated': 1, 'departed': 0})
        winner = PlayerSpecTopPlayer.objects.get(pk=winner_ids[0])
        self.assertEqual((winner.class_name, winner.rank, winner.score, winner.gear_json),
                         ('Mage', 1, 4000, [{'winner': True}]))
        self.assertEqual(PlayerSpecTopPlayer.objects.count(), 1)
