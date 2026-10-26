"""Real-ORM read-path contracts; no production database or mocked querysets."""

from django.db import connection
from django.db.models import Prefetch
from django.test import TestCase
from django.test.utils import CaptureQueriesContext

from botend.models import (
    MythicDungeon, MythicDungeonAbility, MythicDungeonDataVersion,
    MythicDungeonDefaultRoute, MythicDungeonEnemy, MythicDungeonFloor,
    MythicDungeonPoi, MythicDungeonSpawn, MythicDungeonSpell,
)
from botend.mythic_planner import mdt_route_codec as codec
from botend.mythic_planner import services


class MythicPlannerReadPathTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.version = MythicDungeonDataVersion.objects.create(key='readpaths', is_active=True)
        cls.dungeon = MythicDungeon.objects.create(
            data_version=cls.version, key='one', name='One', external_index=901,
            metadata={'selection_groups': [], 'large': 'd' * 10000},
        )
        cls.floor = MythicDungeonFloor.objects.create(
            dungeon=cls.dungeon, key='floor', floor_index=2, name='Floor',
            metadata={'large': 'f' * 10000},
        )
        cls.enemy = MythicDungeonEnemy.objects.create(
            dungeon=cls.dungeon, key='enemy', name='Enemy',
            metadata={'source_enemy_index': 7, 'large': 'e' * 10000},
        )
        cls.spawn = MythicDungeonSpawn.objects.create(
            enemy=cls.enemy, floor=cls.floor, key='spawn',
            metadata={'source_clone_index': 3, 'large': 's' * 10000},
        )
        cls.payload = {
            'version': 1, 'dungeon_key': 'one', 'data_version_key': 'old',
            'name': 'Route', 'current_floor_key': 'floor', 'dungeon_level': 12,
            'pulls': [{'id': 'p', 'name': 'Pull', 'color': '#abcdef',
                       'spawn_uids': ['enemy:spawn']}],
            'annotations': [{'id': 'n', 'type': 'note', 'floor_key': 'floor',
                             'x': 50, 'y': 50, 'text': 'A & B < C', 'color': '#123456'}],
        }
        cls.route = MythicDungeonDefaultRoute.objects.create(
            dungeon=cls.dungeon, name='Route', route_data=cls.payload,
        )

    @staticmethod
    def queries_for(queries, table):
        return [q['sql'] for q in queries if f'FROM "{table}"' in q['sql']]

    def test_catalog_reuses_maps_per_dungeon_without_large_join_projections(self):
        expected = services.serialize_default_route(self.route)
        with CaptureQueriesContext(connection) as first_queries:
            first = services.serialize_catalog()
        self.assertEqual(first['default_routes'], [expected])
        for index in range(3):
            MythicDungeonDefaultRoute.objects.create(
                dungeon=self.dungeon, name=f'Copy {index}', route_data=self.payload,
            )
        with CaptureQueriesContext(connection) as queries:
            result = services.serialize_catalog()
        self.assertEqual(len(result['default_routes']), 4)
        for row in result['default_routes']:
            self.assertEqual(row['route_code'], expected['route_code'])
            self.assertTrue(row['is_valid'])
        self.assertEqual(len(queries), len(first_queries), 'SQL must not grow per route')
        self.assertEqual(len(queries), 7)
        spawn_queries = self.queries_for(queries, 'mythic_dungeon_spawn')
        self.assertEqual(len(spawn_queries), 1)
        self.assertEqual(len(self.queries_for(queries, 'mythic_dungeon_floor')), 1)
        select = spawn_queries[0].split(' FROM ')[0]
        self.assertNotIn('"mythic_dungeon_floor"."metadata"', select)
        self.assertNotIn('"mythic_dungeon_spawn"."patrol"', select)
        self.assertIn('source_enemy_index', select)
        self.assertIn('source_clone_index', select)

    def test_maps_are_scoped_to_requested_dungeon_and_identity(self):
        other = MythicDungeon.objects.create(
            data_version=self.version, key='other', name='Other', external_index=902,
        )
        other_floor = MythicDungeonFloor.objects.create(dungeon=other, key='floor', name='Other')
        other_enemy = MythicDungeonEnemy.objects.create(
            dungeon=other, key='enemy', name='Other', metadata={'source_enemy_index': 99},
        )
        MythicDungeonSpawn.objects.create(
            enemy=other_enemy, floor=other_floor, key='spawn', metadata={'source_clone_index': 99},
        )
        with CaptureQueriesContext(connection) as queries:
            catalog = services.serialize_catalog()
        self.assertEqual(len(catalog['dungeons']), 2)
        spawn_sql = self.queries_for(queries, 'mythic_dungeon_spawn')
        self.assertEqual(len(spawn_sql), 1)
        self.assertIn(f'"mythic_dungeon_enemy"."dungeon_id" = {self.dungeon.pk}', spawn_sql[0])
        context = codec.DungeonRouteContext(self.dungeon)
        with self.assertNumQueries(2):
            preset = codec.route_payload_to_preset(self.payload, route_context=context)
        self.assertEqual(preset['value']['pulls'][0][7], [3])
        with self.assertNumQueries(0):
            with self.assertRaisesRegex(ValueError, '所属地下城'):
                services.validate_route_payload(self.payload, other, route_context=context)
        MythicDungeonDefaultRoute.objects.all().delete()
        with CaptureQueriesContext(connection) as queries:
            services.serialize_catalog()
        self.assertEqual(self.queries_for(queries, 'mythic_dungeon_spawn'), [])

    def test_catalog_rejects_invalid_references_and_reloads_next_request(self):
        for model, pk in ((MythicDungeonSpawn, self.spawn.pk),
                          (MythicDungeonEnemy, self.enemy.pk),
                          (MythicDungeonFloor, self.floor.pk)):
            with self.subTest(inactive=model.__name__):
                self.assertTrue(services.serialize_catalog()['default_routes'][0]['is_valid'])
                model.objects.filter(pk=pk).update(is_active=False)
                result = services.serialize_catalog()['default_routes'][0]
                self.assertFalse(result['is_valid'])
                self.assertIn('不存在的怪物刷新点', result['invalid_reason'])
                model.objects.filter(pk=pk).update(is_active=True)
        variants = [
            ({'dungeon_key': 'other'}, '所属地下城'),
            ({'pulls': [{'spawn_uids': ['enemy:spawn', 'enemy:spawn']}]}, '重复'),
            ({'pulls': [{'spawn_uids': ['enemy:unknown']}]}, '不存在'),
            ({'annotations': [{'type': 'unsupported'}]}, '不支持'),
        ]
        for replacement, error in variants:
            with self.subTest(replacement=replacement):
                payload = {**self.payload, **replacement}
                MythicDungeonDefaultRoute.objects.filter(pk=self.route.pk).update(route_data=payload)
                row = services.serialize_catalog()['default_routes'][0]
                self.assertFalse(row['is_valid'])
                self.assertIn(error, row['invalid_reason'])
        MythicDungeonDefaultRoute.objects.filter(pk=self.route.pk).update(route_data=self.payload)
        MythicDungeonSpawn.objects.filter(pk=self.spawn.pk).update(metadata={})
        row = services.serialize_catalog()['default_routes'][0]
        self.assertFalse(row['is_valid'])
        self.assertIn('缺少 MDT source index', row['invalid_reason'])

    def test_codec_projection_preserves_index_types_and_active_version_lookup(self):
        # Another active version lacking this dungeon must not shadow its lookup.
        MythicDungeonDataVersion.objects.create(key='newer', is_active=True)
        for value in (3, '3', True, False, None, 3.5, 'true', [], {}):
            with self.subTest(index=value):
                MythicDungeonSpawn.objects.filter(pk=self.spawn.pk).update(
                    metadata={'source_clone_index': value, 'large': 'x' * 10000},
                )
                if isinstance(value, int):
                    preset = codec.route_payload_to_preset(self.payload)
                    actual = preset['value']['pulls'][0][7][0]
                    self.assertIs(type(actual), type(value))
                    self.assertEqual(actual, value)
                    self.assertEqual(codec.preset_to_route_payload(preset)['pulls'], self.payload['pulls'])
                else:
                    with self.assertRaisesRegex(ValueError, '缺少 MDT source index'):
                        codec.route_payload_to_preset(self.payload)

    def test_dungeon_detail_matches_full_relations_without_repeated_large_rows(self):
        spell = MythicDungeonSpell.objects.create(
            data_version=self.version, spell_id=100, name='Spell', name_zh='法术',
            description='', description_zh='', aura_description='Aura', aura_description_zh='光环',
            icon_url='spell-icon', source_branch='wow', source_locale='zhCN',
            snapshot_build='build', icon_file_data_id=999,
            metadata={'description_source': 'source', 'client_build': 'build',
                      'difficulty_id': 0, 'large': 't' * 10000},
        )
        for index in range(3):
            enemy = MythicDungeonEnemy.objects.create(
                dungeon=self.dungeon, key=f'e{index}', name=f'E{index}',
            )
            MythicDungeonAbility.objects.create(
                enemy=enemy, spell_record=spell, spell_id=100, name='Fallback',
                name_zh='覆盖' if index == 1 else '', description='fallback',
                description_zh='回退', icon_url='override', interruptible=True,
                metadata={'manual_override_fields': ['name_zh', 'icon_url'] if index == 1 else [],
                          'custom': [index]},
            )
            MythicDungeonSpawn.objects.create(enemy=enemy, floor=self.floor, key=f's{index}')
        MythicDungeonAbility.objects.create(
            enemy=self.enemy, spell_id=101, name='No snapshot', description_zh='无快照',
        )
        MythicDungeonPoi.objects.create(
            floor=self.floor, key='poi', metadata={'source': {'info': {'spellId': 100}}},
        )
        # Independent baseline loader reproduces the pre-optimization full graph.
        baseline = MythicDungeon.objects.select_related('data_version').get(pk=self.dungeon.pk)
        baseline_floors = MythicDungeonFloor.objects.filter(is_active=True).prefetch_related(
            Prefetch('pois', queryset=MythicDungeonPoi.objects.filter(is_active=True)),
        )
        baseline_enemies = MythicDungeonEnemy.objects.filter(is_active=True).prefetch_related(
            Prefetch('abilities', queryset=MythicDungeonAbility.objects.filter(is_active=True).select_related('spell_record')),
            Prefetch('spawns', queryset=MythicDungeonSpawn.objects.filter(is_active=True, floor__is_active=True).select_related('floor')),
        )
        from django.db.models import prefetch_related_objects
        prefetch_related_objects([baseline], Prefetch('floors', queryset=baseline_floors),
                                 Prefetch('enemies', queryset=baseline_enemies))
        expected = services.serialize_dungeon(baseline)
        with CaptureQueriesContext(connection) as queries:
            actual = services.serialize_dungeon(services.get_active_dungeon('one'))
        self.assertEqual(actual, expected)
        self.assertEqual(len(queries), 9)
        ability_sql = self.queries_for(queries, 'mythic_dungeon_ability')[0]
        spawn_sql = self.queries_for(queries, 'mythic_dungeon_spawn')[0]
        self.assertNotIn('"mythic_dungeon_spell"."metadata"', ability_sql)
        self.assertNotIn('"mythic_dungeon_floor"."metadata"', spawn_sql)
        self.assertEqual(len(self.queries_for(queries, 'mythic_dungeon_spell')), 1)
