from copy import deepcopy
from datetime import timedelta
import re

from django.core.exceptions import ValidationError
from django.db import IntegrityError, connection, transaction
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from botend.controller.plugins.portal.SpecDetailRankingMonitor import SpecDetailRankingMonitor
from botend.models import SpecDungeonRanking, SpecRaidRanking


class SpecDetailRankingMonitorDifferentialUpdateTests(TestCase):
    def _sync(self, records, model=SpecRaidRanking):
        if model is SpecRaidRanking:
            filters = {'season_id': 1, 'boss_id': 100, 'difficulty': 5}
            key_fields = SpecDetailRankingMonitor.RAID_KEY_FIELDS
            update_fields = SpecDetailRankingMonitor.RAID_UPDATE_FIELDS
        else:
            filters = {'season_id': 1, 'dungeon_id': 100}
            key_fields = SpecDetailRankingMonitor.DUNGEON_KEY_FIELDS
            update_fields = SpecDetailRankingMonitor.DUNGEON_UPDATE_FIELDS
        return SpecDetailRankingMonitor._sync_ranking_records(
            model=model, filters=filters, records=records,
            key_fields=key_fields, update_fields=update_fields,
        )

    def _dml(self, queries):
        return [query['sql'] for query in queries
                if query['sql'].lstrip().split()[0].upper() in {'INSERT', 'UPDATE', 'DELETE'}]

    def _updated_columns(self, sql):
        self.assertTrue(sql.startswith('UPDATE '), sql)
        assignments = sql.split(' SET ', 1)[1].rsplit(' WHERE ', 1)[0]
        return set(re.findall(r'"([a-z_]+)" = CASE', assignments))

    def test_numeric_scalar_strings_round_trip_without_repeated_dml(self):
        old_time = timezone.now() - timedelta(days=1)
        for model in (SpecRaidRanking, SpecDungeonRanking):
            with self.subTest(model=model.__name__):
                if model is SpecRaidRanking:
                    incoming = self._record(
                        report_code='numeric', fight_id=1, dps='100.25', last_updated=old_time,
                    )
                    incoming.kill_time = '100000'
                    incoming.raid_zone_id = '10'
                else:
                    incoming = SpecDungeonRanking(
                        season_id=1, dungeon_id=100, dungeon_name='Dungeon',
                        class_name='Warrior', spec_name='Fury', character_name='Player',
                        realm='Kazzak', region='eu', report_code='numeric', fight_id=1,
                        dps='100.25', keystone_level='12', clear_time='100000', score='123.5',
                        affixes=[{'id': '10'}], talents_json=[{'node_id': '1'}],
                        gear_json=[{'item_id': '1'}], last_updated=old_time,
                    )
                incoming.faction = '1'
                payload = deepcopy(incoming)
                self.assertEqual(self._sync([deepcopy(payload)], model)['created'], 1)
                stored = model.objects.get(report_code='numeric')
                payload.last_updated = timezone.now()
                for repeat in range(2):
                    with self.subTest(repeat=repeat), CaptureQueriesContext(connection) as queries:
                        result = self._sync([deepcopy(payload)], model)
                    self.assertEqual(self._dml(queries), [], self._dml(queries))
                    self.assertEqual(result, {'created': 0, 'updated': 0, 'deleted': 0, 'unchanged': 1})
                stored.refresh_from_db()
                self.assertEqual(stored.last_updated, old_time)
                self.assertEqual(stored.dps, 100.25)
                self.assertEqual(stored.faction, 1)
                self.assertEqual(stored.gear_json, payload.gear_json)
                self.assertEqual(stored.talents_json, payload.talents_json)

    def test_bulk_updates_group_only_the_actual_changed_columns(self):
        old_time = timezone.now() - timedelta(days=1)
        rows = [self._record(report_code=str(i), fight_id=i, dps=100, last_updated=old_time)
                for i in range(1, 6)]
        SpecRaidRanking.objects.bulk_create(rows)
        incoming = deepcopy(rows)
        for row in incoming:
            row.last_updated = timezone.now()
        incoming[0].dps = 101
        incoming[1].dps = 102
        incoming[2].talents_json = [{'talentID': 123, 'spellID': None, 'entry_id': 456}]
        incoming[3].dps = 103
        incoming[3].kill_time = 90000
        with CaptureQueriesContext(connection) as queries:
            result = self._sync(incoming)
        self.assertEqual(result, {'created': 0, 'updated': 4, 'deleted': 0, 'unchanged': 1})
        writes = self._dml(queries)
        self.assertCountEqual([self._updated_columns(sql) for sql in writes], [
            {'dps', 'last_updated'}, {'talents_json', 'last_updated'},
            {'dps', 'kill_time', 'last_updated'},
        ], writes)
        for sql in writes:
            self.assertNotIn('gear_json', sql)
        for original, expected in zip(rows, incoming):
            original.refresh_from_db()
            self.assertEqual(original.dps, expected.dps)
            self.assertEqual(original.kill_time, expected.kill_time)
            self.assertEqual(original.talents_json, expected.talents_json)
            self.assertEqual(original.gear_json, expected.gear_json)
            self.assertEqual(original.last_updated, old_time if original.pk == rows[-1].pk
                             else expected.last_updated)
        with CaptureQueriesContext(connection) as queries:
            result = self._sync(deepcopy(incoming))
        self.assertEqual(self._dml(queries), [])
        self.assertEqual(result, {'created': 0, 'updated': 0, 'deleted': 0, 'unchanged': 5})

    def test_json_semantics_and_empty_build_code_are_not_normalized(self):
        old_time = timezone.now() - timedelta(days=1)
        row = self._record(report_code='json', fight_id=1, dps=100, last_updated=old_time)
        row.gear_json = [{'item_id': 1, 'slot': 0}, {'item_id': 2, 'slot': 1}]
        row.save()
        incoming = deepcopy(row)
        incoming.last_updated = timezone.now()
        incoming.gear_json = [{'slot': 0, 'item_id': 1}, {'slot': 1, 'item_id': 2}]
        with CaptureQueriesContext(connection) as queries:
            self.assertEqual(self._sync([incoming])['unchanged'], 1)
        self.assertEqual(self._dml(queries), [])
        for field, value in (
            ('gear_json', list(reversed(incoming.gear_json))),
            ('talents_json', [{'node_id': '1'}]),
            ('talent_build_code', ''),
        ):
            with self.subTest(field=field):
                setattr(incoming, field, value)
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self._sync([incoming])['updated'], 1)
                self.assertEqual([self._updated_columns(sql) for sql in self._dml(queries)],
                                 [{field, 'last_updated'}])
                row.refresh_from_db()
                self.assertEqual(getattr(row, field), value)
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self._sync([deepcopy(incoming)])['unchanged'], 1)
                self.assertEqual(self._dml(queries), [])

    def test_invalid_numeric_conversion_fails_before_any_snapshot_dml(self):
        now = timezone.now()
        stale = self._record(report_code='stale', fight_id=1, dps=100, last_updated=now)
        stale.save()
        invalid = self._record(report_code='invalid', fight_id=2, dps=200, last_updated=now)
        for field, value in (('kill_time', 'not-an-integer'), ('kill_time', '1.5'),
                             ('kill_time', 1.5), ('dps', 'nan'), ('dps', 'inf'),
                             ('kill_time', True), ('dps', False)):
            with self.subTest(field=field, value=value):
                incoming = deepcopy(invalid)
                setattr(incoming, field, value)
                with CaptureQueriesContext(connection) as queries:
                    with self.assertRaises((ValidationError, ValueError)), transaction.atomic():
                        self._sync([incoming])
                self.assertEqual(self._dml(queries), [], self._dml(queries))
                self.assertEqual(list(SpecRaidRanking.objects.values_list('pk', flat=True)), [stale.pk])

    def test_caller_transaction_rolls_back_delete_insert_and_prior_update_group(self):
        now = timezone.now()
        rows = [self._record(report_code=str(i), fight_id=i, dps=100, last_updated=now)
                for i in range(1, 4)]
        SpecRaidRanking.objects.bulk_create(rows)
        before = list(SpecRaidRanking.objects.order_by('pk').values())
        incoming = deepcopy(rows[:2])
        incoming[0].dps = 101
        incoming[1].dps = None  # Real NOT NULL violation, not a mocked ORM failure.
        incoming[1].kill_time = 90000  # A different field group, after the successful DPS group.
        incoming.append(self._record(report_code='new', fight_id=4, dps=200, last_updated=now))
        with CaptureQueriesContext(connection) as queries:
            with self.assertRaises(IntegrityError), transaction.atomic():
                self._sync(incoming)
        self.assertTrue(any(sql.startswith('DELETE ') for sql in self._dml(queries)))
        self.assertTrue(any(sql.startswith('INSERT ') for sql in self._dml(queries)))
        self.assertEqual(list(SpecRaidRanking.objects.order_by('pk').values()), before)

    def test_existing_report_fight_and_anonymous_dps_identity_is_preserved(self):
        now = timezone.now()
        cases = [('report_code', 'other'), ('fight_id', 2), ('realm', 'kazzak'),
                 ('dps', 100.01)]
        for field, value in cases:
            with self.subTest(field=field):
                SpecRaidRanking.objects.all().delete()
                row = self._record(report_code='identity', fight_id=1, dps=100, last_updated=now)
                if field == 'dps':
                    row.character_name, row.realm, row.region = 'Anonymous', '', ''
                row.save()
                incoming = deepcopy(row)
                incoming.pk = None
                setattr(incoming, field, value)
                self.assertEqual(self._sync([incoming]),
                                 {'created': 1, 'updated': 0, 'deleted': 1, 'unchanged': 0})
                stored = SpecRaidRanking.objects.get()
                self.assertNotEqual(stored.pk, row.pk)
                self.assertEqual(getattr(stored, field), value)
                with CaptureQueriesContext(connection) as queries:
                    self.assertEqual(self._sync([deepcopy(incoming)])['unchanged'], 1)
                self.assertEqual(self._dml(queries), [])

    def _record(self, *, report_code, fight_id, dps, last_updated):
        return SpecRaidRanking(
            season_id=1,
            boss_id=100,
            boss_name='Boss',
            raid_zone_id=10,
            raid_zone_name='Raid',
            class_name='Warrior',
            spec_name='Fury',
            character_name='Player',
            realm='Kazzak',
            region='eu',
            dps=dps,
            kill_time=100000,
            talents_json=[{'node_id': 1}],
            talent_build_code='build',
            gear_json=[{'item_id': 1}],
            faction=1,
            guild_name='Guild',
            report_code=report_code,
            fight_id=fight_id,
            last_updated=last_updated,
        )

    def test_sync_ranking_records_only_creates_updates_and_deletes_differences(self):
        old_time = timezone.now() - timedelta(days=1)
        new_time = timezone.now()
        unchanged = self._record(report_code='same', fight_id=1, dps=100, last_updated=old_time)
        changed = self._record(report_code='changed', fight_id=2, dps=200, last_updated=old_time)
        stale = self._record(report_code='stale', fight_id=3, dps=300, last_updated=old_time)
        SpecRaidRanking.objects.bulk_create([unchanged, changed, stale])

        incoming = [
            self._record(report_code='same', fight_id=1, dps=100, last_updated=new_time),
            self._record(report_code='changed', fight_id=2, dps=250, last_updated=new_time),
            self._record(report_code='new', fight_id=4, dps=400, last_updated=new_time),
        ]

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=incoming,
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {'created': 1, 'updated': 1, 'deleted': 1, 'unchanged': 1})
        unchanged.refresh_from_db()
        changed.refresh_from_db()
        self.assertEqual(unchanged.last_updated, old_time)
        self.assertEqual(changed.dps, 250)
        self.assertEqual(changed.last_updated, new_time)
        self.assertFalse(SpecRaidRanking.objects.filter(report_code='stale').exists())
        self.assertTrue(SpecRaidRanking.objects.filter(report_code='new').exists())

    def test_sync_mythic_does_not_delete_heroic_records_for_the_same_boss(self):
        now = timezone.now()
        heroic = self._record(report_code='heroic', fight_id=1, dps=100, last_updated=now)
        heroic.difficulty = 4
        SpecRaidRanking.objects.bulk_create([heroic])

        mythic = self._record(report_code='mythic', fight_id=2, dps=200, last_updated=now)
        mythic.difficulty = 5
        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100, 'difficulty': 5},
            records=[mythic],
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {'created': 1, 'updated': 0, 'deleted': 0, 'unchanged': 0})
        self.assertEqual(
            list(SpecRaidRanking.objects.order_by('difficulty').values_list('difficulty', 'report_code')),
            [(4, 'heroic'), (5, 'mythic')],
        )

    def test_sync_ranking_records_merges_duplicate_named_player_from_wcl_pages(self):
        now = timezone.now()
        duplicate_records = [
            self._record(report_code='same', fight_id=1, dps=100, last_updated=now),
            self._record(report_code='same', fight_id=1, dps=200, last_updated=now),
        ]

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=duplicate_records,
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {
            'created': 1,
            'updated': 0,
            'deleted': 0,
            'unchanged': 0,
        })
        self.assertEqual(SpecRaidRanking.objects.count(), 1)
        self.assertEqual(SpecRaidRanking.objects.get().dps, 100)

    def test_sync_ranking_records_keeps_distinct_anonymous_players_in_same_fight(self):
        now = timezone.now()
        first = self._record(report_code='a:report', fight_id=4, dps=100, last_updated=now)
        second = self._record(report_code='a:report', fight_id=4, dps=200, last_updated=now)
        for row in (first, second):
            row.character_name = 'Anonymous'
            row.realm = ''
            row.region = ''

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=[first, second],
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {
            'created': 2,
            'updated': 0,
            'deleted': 0,
            'unchanged': 0,
        })
        self.assertEqual(
            list(SpecRaidRanking.objects.order_by('dps').values_list('dps', flat=True)),
            [100.0, 200.0],
        )

    def test_sync_ranking_records_removes_existing_duplicate_business_keys(self):
        now = timezone.now()
        first = self._record(report_code='same', fight_id=1, dps=100, last_updated=now)
        duplicate = self._record(report_code='same', fight_id=1, dps=100, last_updated=now)
        SpecRaidRanking.objects.bulk_create([first, duplicate])

        result = SpecDetailRankingMonitor._sync_ranking_records(
            model=SpecRaidRanking,
            filters={'season_id': 1, 'boss_id': 100},
            records=[self._record(report_code='same', fight_id=1, dps=100, last_updated=now)],
            key_fields=SpecDetailRankingMonitor.RAID_KEY_FIELDS,
            update_fields=SpecDetailRankingMonitor.RAID_UPDATE_FIELDS,
        )

        self.assertEqual(result, {
            'created': 0,
            'updated': 0,
            'deleted': 1,
            'unchanged': 1,
        })
        self.assertEqual(SpecRaidRanking.objects.count(), 1)
