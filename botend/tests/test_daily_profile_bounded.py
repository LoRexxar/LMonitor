"""Daily identity-bounded reads on the real ORM; transport is forbidden."""
import json
import threading
from contextlib import contextmanager
from unittest.mock import Mock, patch

from django.db import close_old_connections, connection
from django.test import TransactionTestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor
from botend.models import PlayerSpecTopPlayer, SeasonMeta, SpecDungeonRanking, SpecRaidRanking


class DailyProfileBoundedTests(TransactionTestCase):
    def setUp(self):
        self.season = SeasonMeta.objects.create(
            season_key='daily-bounded', season_name='Daily bounded',
            rio_season='season-daily-bounded', mplus_zone_id=1, raid_zone_id=2,
            is_active=True,
        )
        self.monitor = SpecDetailPlayerMonitor(Mock(), Mock())
        self.enterContext(patch('requests.sessions.Session.request',
                                side_effect=AssertionError('external transport forbidden')))
        self.enterContext(patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.CLASS_SPEC_MAP',
                                {'Mage': ['Frost']}))
        self.enterContext(patch('botend.controller.plugins.portal.SpecDetailPlayerMonitor.time.sleep'))
        self.enterContext(patch.object(self.monitor, '_crawl_battlenet_stats'))
        self.enterContext(patch('botend.services.character_bindings.refresh_bound_characters', return_value=0))

    def player(self, name, **extra):
        fields = dict(season_id=self.season.pk, region='eu', realm='Hyjal',
                      character_name=name, class_name='Mage', spec_name='Frost',
                      rank=4, score=4000, stats_json={'retained': 123},
                      stats_crawl_status=1, last_updated=timezone.now(),
                      gear_json=[{'source': 'raiderio_profile', 'id': 123}],
                      talent_build_code='already-complete')
        fields.update(extra)
        return PlayerSpecTopPlayer.objects.create(**fields)

    def history(self, count=1000):
        PlayerSpecTopPlayer.objects.bulk_create([
            PlayerSpecTopPlayer(season_id=self.season.pk, region='eu', realm='Hyjal',
                character_name=f'History{i}', class_name='Mage' if i % 2 else 'DeathKnight',
                spec_name='Frost', gear_json=[{'padding': 'x' * 4096}],
                talents_json=[{'padding': 'y' * 8192}], stats_json={'padding': 'z' * 4096})
            for i in range(count)
        ], batch_size=100)

    @contextmanager
    def capture_profiles(self):
        loaded = []
        from_db = PlayerSpecTopPlayer.from_db
        def record(*args, **kwargs):
            row = from_db(*args, **kwargs)
            loaded.append(row.pk)
            return row
        with patch.object(PlayerSpecTopPlayer, 'from_db', side_effect=record), \
                CaptureQueriesContext(connection) as queries:
            yield loaded, queries

    def daily(self, players):
        with patch.object(self.monitor, '_fetch_top_players', return_value=players), \
                patch.object(self.monitor, '_backfill_ranking_sample_profiles'):
            self.assertTrue(self.monitor.scan(''))

    def incoming(self, name, **extra):
        fields = {'region': 'eu', 'realm': 'Hyjal', 'name': name,
                  'gear': [{'id': 321}], 'talents': [], 'talent_build_code': 'updated',
                  'rank': 19, 'score': 1}
        fields.update(extra)
        return fields

    def assert_bounded_sql(self, queries, label, loaded, max_selects):
        selects = [q['sql'] for q in queries if 'wow_spec_top_player' in q['sql']
                   and q['sql'].lstrip().startswith(('SELECT', '('))]
        self.assertLessEqual(len(selects), max_selects)
        for sql in selects:
            if 'gear_json' in sql:
                self.assertRegex(sql, r'(?i)(\bid[`\"]? IN|character_name[`\"]? =)')
            else:
                for field in ('region', 'realm', 'character_name', 'spec_name', 'season_id'):
                    self.assertIn(field, sql)
        plans = []
        if connection.vendor == 'mysql':
            with connection.cursor() as cursor:
                for sql in selects:
                    cursor.execute('EXPLAIN ' + sql)
                    plan = [dict(zip([c[0] for c in cursor.description], row))
                            for row in cursor.fetchall()]
                    plans.append(plan)
                    for step in plan:
                        if step['table'] == 'wow_spec_top_player':
                            self.assertIn(step['type'], ('const', 'range'))
                            self.assertIsNotNone(step['key'])
        print('DAILY_BOUND_EVIDENCE', json.dumps({
            'label': label, 'backend': connection.vendor, 'hydrated_rows': len(loaded),
            'profile_selects': len(selects), 'sql': selects, 'explain': plans,
        }, default=str, ensure_ascii=False), flush=True)

    def test_top20_reads_only_incoming_and_repairs_class_without_touching_rank_score(self):
        self.history()
        targets = [self.player(f'Target{i}', class_name='DeathKnight' if i == 0 else 'Mage')
                   for i in range(19)]
        decoys = [self.player('Target0', realm='Other'), self.player('Target0', region='us'),
                  self.player('Target0', spec_name='Arcane'),
                  self.player('Target0', season_id=self.season.pk + 100)]
        before = list(PlayerSpecTopPlayer.objects.filter(pk__in=[p.pk for p in decoys]).values())
        incoming = [self.incoming(p.character_name) for p in targets] + [self.incoming('New')]
        incoming[1]['_profile_refresh_failed'] = True
        with self.capture_profiles() as (loaded, queries):
            self.daily(incoming)
        self.assertEqual(set(loaded), {p.pk for p in targets})
        self.assertEqual(len(loaded), len(targets))
        self.assert_bounded_sql(queries, 'top20', loaded, 2)
        for p in targets:
            p.refresh_from_db()
            self.assertEqual((p.class_name, p.rank, p.score), ('Mage', 4, 4000))
            self.assertEqual((p.stats_json, p.stats_crawl_status), ({'retained': 123}, 1))
        self.assertEqual(targets[1].gear_json, [{'source': 'raiderio_profile', 'id': 123}])
        new = PlayerSpecTopPlayer.objects.get(character_name='New')
        self.assertEqual((new.rank, new.score), (None, None))
        self.assertEqual(before, list(PlayerSpecTopPlayer.objects.filter(
            pk__in=[p.pk for p in decoys]).values()))
        with CaptureQueriesContext(connection) as repeated:
            self.daily(incoming)
        self.assertFalse(any(q['sql'].startswith(('UPDATE', 'INSERT', 'DELETE'))
                             and 'wow_spec_top_player' in q['sql'] for q in repeated))

    def ranking(self, model, name, scope_id, dps, **extra):
        scope = {'dungeon_id': scope_id} if model is SpecDungeonRanking else {'boss_id': scope_id}
        fields = dict(season_id=self.season.pk, region='eu', realm='Hyjal',
                      character_name=name, class_name='Mage', spec_name='Frost',
                      dps=dps, guild_name='Sample guild', **scope)
        fields.update(extra)
        return model.objects.create(**fields)

    def test_samples_keep_per_scope_limit_and_only_hydrate_union_of_selected_identities(self):
        self.history()
        targets = []
        for model, scope in ((SpecDungeonRanking, 1), (SpecDungeonRanking, 2), (SpecRaidRanking, 3)):
            for i in range(100):
                name = f'Sample{scope}x{i}'
                targets.append(self.player(name, class_name='DeathKnight' if i == 0 else 'Mage'))
                self.ranking(model, name, scope, 1000 - i)
            self.ranking(model, 'Excluded', scope, 0)
        excluded = self.player('Excluded')
        # Same character in another boss: keep the pre-existing sample identity de-duplication.
        self.ranking(SpecRaidRanking, targets[0].character_name, 4, 2000)
        with self.capture_profiles() as (loaded, queries), \
                patch.object(self.monitor, '_enrich_profile_model_from_raiderio',
                             side_effect=AssertionError('complete profiles must not refetch')):
            updated = self.monitor._backfill_ranking_sample_profiles(self.season.pk, 'Mage', 'Frost')
        self.assertEqual(updated, 300)
        self.assertEqual(set(loaded), {p.pk for p in targets})
        self.assertEqual(len(loaded), len(targets))
        self.assert_bounded_sql(queries, 'samples-300', loaded, 6)
        self.assertEqual(PlayerSpecTopPlayer.objects.filter(guild_name='Sample guild').count(), 300)
        excluded.refresh_from_db()
        self.assertIsNone(excluded.guild_name)
        for p in targets:
            p.refresh_from_db()
            self.assertEqual((p.rank, p.score, p.stats_json), (4, 4000, {'retained': 123}))
            self.assertEqual(p.class_name, 'DeathKnight' if p.character_name.endswith('x0') else 'Mage')
        with CaptureQueriesContext(connection) as repeated:
            self.assertEqual(self.monitor._backfill_ranking_sample_profiles(
                self.season.pk, 'Mage', 'Frost'), 0)
        self.assertFalse(any(q['sql'].startswith(('UPDATE', 'INSERT', 'DELETE')) for q in repeated))

    def test_mysql_aliases_use_database_collation_and_reuse_one_loaded_object(self):
        if connection.vendor != 'mysql':
            self.skipTest('MySQL collation contract')
        self.history(20)
        player = self.player('Straße', class_name='DeathKnight')
        with connection.cursor() as cursor:
            cursor.execute('SELECT %s = %s', ['Straße', 'Strasse'])
            self.assertEqual(cursor.fetchone()[0], 1)
        with self.capture_profiles() as (loaded, queries):
            self.daily([self.incoming('Strasse', _profile_refresh_failed=True)])
        self.assertEqual(loaded, [player.pk])
        self.assert_bounded_sql(queries, 'collation-daily', loaded, 2)
        player.refresh_from_db()
        self.assertEqual((player.class_name, player.stats_json, player.rank, player.score),
                         ('Mage', {'retained': 123}, 4, 4000))
        self.ranking(SpecDungeonRanking, 'Strasse', 1, 2000)
        self.ranking(SpecDungeonRanking, 'Straße', 1, 1000)
        with self.capture_profiles() as (loaded, queries), \
                patch.object(self.monitor, '_enrich_profile_model_from_raiderio',
                             side_effect=AssertionError('collation alias must reuse existing data')):
            self.assertEqual(self.monitor._backfill_ranking_sample_profiles(
                self.season.pk, 'Mage', 'Frost'), 1)
        self.assertEqual(loaded, [player.pk])
        self.assert_bounded_sql(queries, 'collation-sample', loaded, 2)
        self.assertEqual(PlayerSpecTopPlayer.objects.filter(character_name='Strasse').count(), 1)

    def test_mysql_concurrent_insert_conflict_is_exact_and_preserves_rank_score(self):
        if connection.vendor != 'mysql':
            self.skipTest('independent InnoDB connection contract')
        self.history(20)
        winner_ids, errors = [], []
        def insert_winner():
            close_old_connections()
            try:
                winner_ids.append(self.player('Straße', class_name='DeathKnight').pk)
            except BaseException as exc:
                errors.append(exc)
            finally:
                close_old_connections()
        def intercept(execute, sql, params, many, context):
            if sql.startswith('INSERT INTO `wow_spec_top_player`') and not winner_ids:
                thread = threading.Thread(target=insert_winner, daemon=True)
                thread.start()
                thread.join(8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(errors, [])
            return execute(sql, params, many, context)
        candidate = PlayerSpecTopPlayer(season_id=self.season.pk, region='eu', realm='Hyjal',
            character_name='Strasse', class_name='Mage', spec_name='Frost',
            rank=None, score=None, gear_json=[{'id': 321}], last_updated=timezone.now())
        with self.capture_profiles() as (loaded, queries), connection.execute_wrapper(intercept):
            saved = self.monitor._save_profile_safely(candidate)
        self.assertEqual(loaded, winner_ids)
        self.assert_bounded_sql(queries, 'insert-conflict', loaded, 1)
        saved.refresh_from_db()
        self.assertEqual((saved.pk, saved.class_name, saved.rank, saved.score, saved.gear_json),
                         (winner_ids[0], 'Mage', 4, 4000, [{'id': 321}]))

    def test_empty_samples_do_not_read_profiles(self):
        self.history(20)
        with self.capture_profiles() as (loaded, queries):
            self.assertEqual(self.monitor._backfill_ranking_sample_profiles(
                self.season.pk, 'Mage', 'Frost'), 0)
        self.assertEqual(loaded, [])
        self.assertFalse(any('wow_spec_top_player' in q['sql'] for q in queries))
