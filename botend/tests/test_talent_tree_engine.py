# -*- coding: utf-8 -*-

from types import SimpleNamespace
from unittest.mock import patch

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from botend.services.spec_stats_service import SpecStatsService, _compute_talent_popularity_tree
from botend.wow.talents.adapters import build_tree_set_from_talents
from botend.wow.talents.layout import build_talent_tree_layout
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.models import (
    TalentBuildStateModel,
    TalentNodeModel,
    TalentTreeModel,
    TalentTreeSetModel,
)
from botend.wow.talents.render import build_talent_render_model
from botend.wow.talents.view_model import build_talent_view_model


class FakeRankingQuerySet:
    def __init__(self, records, first_row=None):
        self._records = list(records)
        self._first_row = first_row or SimpleNamespace()

    def exists(self):
        return bool(self._records)

    def aggregate(self, **kwargs):
        dps_values = [item.get('dps') for item in self._records if item.get('dps') is not None]
        avg = sum(dps_values) / len(dps_values) if dps_values else None
        return {
            'avg': avg,
            'max': max(dps_values) if dps_values else None,
            'min': min(dps_values) if dps_values else None,
            'stddev': 0,
        }

    def values_list(self, field_name, flat=False):
        return [item.get(field_name) for item in self._records]

    def values(self, *field_names):
        return [{field_name: item.get(field_name) for field_name in field_names} for item in self._records]

    def first(self):
        return self._first_row


class TalentTreeModelTests(SimpleTestCase):
    def test_talent_node_model_from_legacy_dict_normalizes_fields(self):
        node = TalentNodeModel.from_raw({
            'treeType': 'hero',
            'talentID': '101',
            'spellID': '202',
            'name': '',
            'points': '1',
            'tier': '3',
            'column': '2',
            'icon': 'spell_icon',
        })

        self.assertEqual(node.tree_type, 'hero')
        self.assertEqual(node.node_id, 101)
        self.assertEqual(node.talent_id, 101)
        self.assertEqual(node.spell_id, 202)
        self.assertEqual(node.points, 1)
        self.assertEqual(node.row, 3)
        self.assertEqual(node.column, 2)
        self.assertEqual(node.name, '技能ID 202')
        self.assertTrue(node.selected)
        self.assertEqual(node.key, 101)

    def test_talent_tree_model_builds_defaults_and_serializes_nodes(self):
        tree = TalentTreeModel(
            tree_type='build_code',
            nodes=['BwQAAAAAAAAAAAAAAAAAAAAA'],
        )

        self.assertEqual(tree.title, '导入代码')
        self.assertEqual(tree.grid_columns, 1)
        self.assertEqual(tree.grid_rows, 1)
        self.assertTrue(tree.synthetic_layout)
        self.assertEqual(len(tree.nodes), 1)
        self.assertIsInstance(tree.nodes[0], TalentNodeModel)
        self.assertEqual(tree.nodes[0].tree_type, 'build_code')
        self.assertEqual(tree.nodes[0].talent_code, 'BwQAAAAAAAAAAAAAAAAAAAAA')

        payload = tree.to_dict()
        self.assertEqual(payload['title'], '导入代码')
        self.assertEqual(payload['nodes'][0]['talent_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')
        self.assertTrue(payload['nodes'][0]['selected'])


class TalentTreeAdapterTests(SimpleTestCase):
    def test_adapter_groups_nodes_into_three_tree_types_and_build_state(self):
        class Provider:
            def merge_into_node(self, node, class_name='', spec_name=''):
                return dict(node)

        payload = [
            {
                'spell_id': 101,
                'talent_id': 101,
                'name': '职业节点',
                'tree_type': 'class',
                'row': 1,
                'column': 1,
            },
            {
                'spell_id': 202,
                'talent_id': 202,
                'name': '专精节点',
                'tree_type': 'spec',
                'row': 2,
                'column': 4,
                'points': 1,
            },
            {
                'spell_id': 303,
                'talent_id': 303,
                'name': '英雄节点',
                'tree_type': 'hero',
                'row': 1,
                'column': 2,
            },
        ]

        tree_set, build_state = build_tree_set_from_talents(
            payload,
            class_name='Monk',
            spec_name='Windwalker',
            metadata_provider=Provider(),
        )

        self.assertIsInstance(tree_set, TalentTreeSetModel)
        self.assertIsInstance(build_state, TalentBuildStateModel)
        self.assertEqual([tree.tree_type for tree in tree_set.trees], ['class', 'spec', 'hero'])
        self.assertEqual(tree_set.set_key, 'Monk:Windwalker')
        self.assertEqual(tree_set.layout_mode, 'three-column')
        self.assertEqual(tree_set.trees[0].title, '职业天赋')
        self.assertFalse(tree_set.trees[1].synthetic_layout)
        self.assertIsInstance(tree_set.trees[1].nodes[0], TalentNodeModel)
        self.assertIn('spec:202', build_state.selected_nodes)
        self.assertEqual(build_state.node_ranks['spec:202'], 1)
        self.assertEqual(build_state.source_id, 'Monk:Windwalker')

    def test_adapter_keeps_build_code_in_meta_and_build_state(self):
        class Provider:
            def merge_into_node(self, node, class_name='', spec_name=''):
                return dict(node)

        tree_set, build_state = build_tree_set_from_talents(
            [
                'BwQAAAAAAAAAAAAAAAAAAAAA',
                {
                    'spell_id': 101,
                    'talent_id': 101,
                    'name': '职业节点',
                    'tree_type': 'class',
                    'row': 1,
                    'column': 1,
                    'points': 2,
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
            metadata_provider=Provider(),
        )

        self.assertEqual(len(tree_set.trees), 1)
        self.assertIsInstance(tree_set.trees[0], TalentTreeModel)
        self.assertEqual(tree_set.meta['build_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')
        self.assertEqual(build_state.build_code, 'BwQAAAAAAAAAAAAAAAAAAAAA')
        self.assertEqual(build_state.node_ranks['class:101'], 2)

    def test_adapter_compresses_raw_metadata_coordinates_into_dense_layout_grid(self):
        class Provider:
            def merge_into_node(self, node, class_name='', spec_name=''):
                return dict(node)

        tree_set, _ = build_tree_set_from_talents(
            [
                {
                    'spell_id': 101,
                    'talent_id': 101,
                    'name': '节点一',
                    'tree_type': 'spec',
                    'row': 1500,
                    'column': 4200,
                },
                {
                    'spell_id': 102,
                    'talent_id': 102,
                    'name': '节点二',
                    'tree_type': 'spec',
                    'row': 2400,
                    'column': 6000,
                },
                {
                    'spell_id': 103,
                    'talent_id': 103,
                    'name': '节点三',
                    'tree_type': 'spec',
                    'row': 2400,
                    'column': 8700,
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
            metadata_provider=Provider(),
        )

        tree = tree_set.trees[0]
        self.assertEqual(tree.grid_rows, 2)
        self.assertEqual(tree.grid_columns, 8)
        self.assertEqual(
            [(node.row, node.column, node.layout_row, node.layout_column) for node in tree.nodes],
            [
                (1500, 4200, 1, 1),
                (2400, 6000, 2, 2),
                (2400, 8700, 2, 3),
            ],
        )

    def test_adapter_always_merges_metadata_for_structural_fields_when_ids_exist(self):
        class Provider:
            def merge_into_node(self, node, class_name='', spec_name=''):
                merged = dict(node)
                merged.update({
                    'row': 5100,
                    'column': 10800,
                    'parents': [9001],
                    'tree_type': 'hero',
                })
                return merged

        tree_set, _ = build_tree_set_from_talents(
            [
                {
                    'spell_id': 101,
                    'talent_id': 101,
                    'name': '运行态节点',
                    'tree_type': 'spec',
                    'row': 2,
                    'column': 4,
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
            metadata_provider=Provider(),
        )

        node = tree_set.trees[0].nodes[0]
        self.assertEqual(tree_set.trees[0].tree_type, 'hero')
        self.assertEqual(node.row, 5100)
        self.assertEqual(node.column, 10800)
        self.assertEqual(node.parents, [9001])
        self.assertEqual(node.layout_row, 1)
        self.assertEqual(node.layout_column, 1)


class TalentMetadataProviderTests(SimpleTestCase):
    def test_merge_into_node_uses_metadata_as_authoritative_for_structural_fields(self):
        provider = TalentMetadataProvider()
        provider.get_node_metadata = lambda **kwargs: {
            'tree_type': 'hero',
            'row': 5100,
            'column': 10800,
            'max_points': 2,
            'parents': [9001, 9002],
            'name': '静态元数据名称',
        }

        merged = provider.merge_into_node(
            {
                'tree_type': 'spec',
                'row': 2,
                'column': 4,
                'max_points': 1,
                'parents': [100],
                'name': '运行态名称',
            },
            class_name='Monk',
            spec_name='Windwalker',
        )

        self.assertEqual(merged['tree_type'], 'hero')
        self.assertEqual(merged['row'], 5100)
        self.assertEqual(merged['column'], 10800)
        self.assertEqual(merged['max_points'], 2)
        self.assertEqual(merged['parents'], [9001, 9002])
        self.assertEqual(merged['name'], '运行态名称')


class TalentTreeLayoutTests(SimpleTestCase):
    def test_build_talent_tree_layout_positions_panels_and_nodes(self):
        tree_set = TalentTreeSetModel(
            set_key='Monk:Windwalker',
            class_name='Monk',
            spec_name='Windwalker',
            trees=[
                TalentTreeModel(
                    tree_type='class',
                    grid_columns=8,
                    grid_rows=2,
                    nodes=[
                        TalentNodeModel(
                            tree_type='class',
                            node_id=101,
                            talent_id=101,
                            spell_id=1101,
                            name='职业节点',
                            layout_row=1,
                            layout_column=1,
                        )
                    ],
                ),
                TalentTreeModel(
                    tree_type='spec',
                    grid_columns=8,
                    grid_rows=4,
                    nodes=[
                        TalentNodeModel(
                            tree_type='spec',
                            node_id=202,
                            talent_id=202,
                            spell_id=2202,
                            name='专精节点',
                            layout_row=2,
                            layout_column=3,
                        )
                    ],
                ),
                TalentTreeModel(
                    tree_type='hero',
                    grid_columns=4,
                    grid_rows=3,
                    nodes=[
                        TalentNodeModel(
                            tree_type='hero',
                            node_id=303,
                            talent_id=303,
                            spell_id=3303,
                            name='英雄节点',
                            layout_row=1,
                            layout_column=2,
                        )
                    ],
                ),
            ],
        )
        build_state = TalentBuildStateModel(selected_nodes={'spec:202'})

        layout = build_talent_tree_layout(tree_set, build_state)

        self.assertEqual(layout.layout_mode, 'three-column')
        self.assertEqual(layout.width, 2128)
        self.assertEqual(layout.height, 488)
        self.assertEqual([(panel.tree_type, panel.x, panel.y) for panel in layout.panels], [
            ('class', 0, 0),
            ('spec', 848, 0),
            ('hero', 1696, 0),
        ])

        spec_panel = layout.panels[1]
        self.assertEqual((spec_panel.width, spec_panel.height), (816, 488))
        self.assertEqual(len(spec_panel.nodes), 1)
        self.assertEqual(
            (
                spec_panel.nodes[0].layout_row,
                spec_panel.nodes[0].layout_column,
                spec_panel.nodes[0].x,
                spec_panel.nodes[0].y,
                spec_panel.nodes[0].center_x,
                spec_panel.nodes[0].center_y,
                spec_panel.nodes[0].selected,
            ),
            (2, 3, 1076, 188, 1112, 224, True),
        )

    def test_build_talent_tree_layout_generates_minimal_svg_paths_from_parents(self):
        tree_set = TalentTreeSetModel(
            set_key='Monk:Windwalker',
            class_name='Monk',
            spec_name='Windwalker',
            trees=[
                TalentTreeModel(
                    tree_type='spec',
                    grid_columns=8,
                    grid_rows=4,
                    nodes=[
                        TalentNodeModel(
                            tree_type='spec',
                            node_id=10,
                            talent_id=10,
                            spell_id=1010,
                            name='起点',
                            layout_row=1,
                            layout_column=2,
                        ),
                        TalentNodeModel(
                            tree_type='spec',
                            node_id=20,
                            talent_id=20,
                            spell_id=2020,
                            name='垂直后继',
                            layout_row=3,
                            layout_column=2,
                            parents=[10],
                        ),
                        TalentNodeModel(
                            tree_type='spec',
                            node_id=30,
                            talent_id=30,
                            spell_id=3030,
                            name='横向后继',
                            layout_row=4,
                            layout_column=4,
                            parents=[20],
                        ),
                    ],
                )
            ],
        )

        layout = build_talent_tree_layout(tree_set)
        paths = layout.panels[0].paths

        self.assertEqual(len(paths), 2)
        self.assertEqual(
            [(path.parent_key, path.child_key, path.svg_path) for path in paths],
            [
                ('spec:10', 'spec:20', 'M 168 164 L 168 284'),
                ('spec:20', 'spec:30', 'M 168 356 L 168 368 L 360 368 L 360 380'),
            ],
        )


class TalentTreeRenderTests(SimpleTestCase):
    @patch('botend.wow.talents.adapters.TalentMetadataProvider')
    def test_build_talent_render_model_outputs_unified_render_payload(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        render_model = build_talent_render_model(
            [
                'BwQAAAAAAAAAAAAAAAAAAAAA',
                {
                    'spell_id': 101,
                    'talent_id': 101,
                    'name': '职业节点',
                    'tree_type': 'class',
                    'row': 1,
                    'column': 1,
                },
                {
                    'spell_id': 202,
                    'talent_id': 202,
                    'name': '专精节点',
                    'tree_type': 'spec',
                    'row': 2,
                    'column': 4,
                    'points': 1,
                    'parents': [101],
                },
                {
                    'spell_id': 303,
                    'talent_id': 303,
                    'name': '英雄节点',
                    'tree_type': 'hero',
                    'row': 1,
                    'column': 2,
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
        )

        payload = render_model.to_dict()
        self.assertEqual(payload['set_key'], 'Monk:Windwalker')
        self.assertEqual(payload['layout_mode'], 'three-column')
        self.assertEqual(payload['build_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')
        self.assertEqual([tree['tree_type'] for tree in payload['trees']], ['class', 'spec', 'hero', 'build_code'])
        self.assertEqual(
            [node['tree_type'] for node in payload['nodes']],
            ['class', 'spec', 'hero', 'build_code'],
        )

        spec_tree = payload['trees'][1]
        self.assertEqual(spec_tree['panel']['tree_type'], 'spec')
        self.assertEqual(len(spec_tree['nodes']), 1)
        self.assertEqual(
            {
                'layout_row': spec_tree['nodes'][0]['layout_row'],
                'layout_column': spec_tree['nodes'][0]['layout_column'],
                'selected': spec_tree['nodes'][0]['selected'],
                'node_key': spec_tree['nodes'][0]['node_key'],
            },
            {
                'layout_row': 2,
                'layout_column': 4,
                'selected': True,
                'node_key': 'spec:202',
            },
        )
        self.assertEqual(payload['build_state']['selected_nodes'], ['spec:202'])
        self.assertEqual(payload['tree_set']['trees'][0]['tree_type'], 'class')
        self.assertEqual(payload['layout']['panels'][2]['tree_type'], 'hero')
        self.assertEqual(payload['trees'][3]['nodes'][0]['talent_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')

    @patch('botend.wow.talents.adapters.TalentMetadataProvider')
    def test_build_talent_view_model_reuses_render_model_output(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        view_model = build_talent_view_model(
            [
                {
                    'spell_id': 1001,
                    'talent_id': 1001,
                    'name': '职业节点',
                    'tree_type': 'class',
                    'row': 1,
                    'column': 2,
                },
                {
                    'spell_id': 2002,
                    'talent_id': 2002,
                    'name': '专精节点',
                    'tree_type': 'spec',
                    'row': 3,
                    'column': 3,
                    'points': 2,
                },
            ],
            class_name='Mage',
            spec_name='Frost',
        )

        self.assertEqual(view_model['build_code'], view_model['render_model']['build_code'])
        self.assertEqual(view_model['nodes'], view_model['render_model']['nodes'])
        self.assertEqual(view_model['trees'], view_model['render_model']['trees'])
        self.assertEqual([tree['tree_type'] for tree in view_model['trees']], ['class', 'spec'])
        self.assertEqual(view_model['render_model']['class_name'], 'Mage')
        self.assertEqual(view_model['render_model']['spec_name'], 'Frost')
        self.assertEqual(view_model['nodes'][1]['node_key'], 'spec:2002')
        self.assertEqual(view_model['trees'][1]['nodes'][0]['selected'], True)


class SpecStatsTalentRenderTests(SimpleTestCase):
    @patch('botend.wow.talents.adapters.TalentMetadataProvider')
    @patch('botend.services.spec_stats_service.PlayerSpecTopPlayer.objects.filter')
    def test_get_player_detail_returns_render_model_and_keeps_legacy_fields(self, mock_filter, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        player = SimpleNamespace(
            id=7,
            rank=1,
            character_name='Zenwalker',
            realm='Stormrage',
            region='us',
            score=4123.45,
            faction='Alliance',
            race='Pandaren',
            gender='female',
            guild_name='Brew Squad',
            realm_rank=1,
            avatar_url='',
            profile_url='https://raider.io/characters/us/stormrage/Zenwalker',
            achievement_points=12345,
            item_level=684.2,
            gear_json=[{
                'slot': 'head',
                'id': 190001,
                'name': '测试头盔',
                'icon': 'inv_helmet_01',
                'itemLevel': 684,
            }],
            talents_json=[
                'BwQAAAAAAAAAAAAAAAAAAAAA',
                {
                    'spell_id': 101,
                    'talent_id': 101,
                    'name': '职业节点',
                    'tree_type': 'class',
                    'row': 1,
                    'column': 1,
                },
                {
                    'spell_id': 201,
                    'talent_id': 201,
                    'name': '专精前置节点',
                    'tree_type': 'spec',
                    'row': 1,
                    'column': 4,
                },
                {
                    'spell_id': 202,
                    'talent_id': 202,
                    'name': '专精节点',
                    'tree_type': 'spec',
                    'row': 2,
                    'column': 4,
                    'points': 1,
                    'parents': [201],
                },
            ],
            stats_json={'crit': {'pct': 25.5}},
            stats_crawl_status=1,
            class_name='Monk',
            spec_name='Windwalker',
            last_updated=None,
        )
        mock_filter.return_value.first.return_value = player

        detail = SpecStatsService.get_player_detail(player.id)

        self.assertEqual(detail['talent_render_model']['build_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')
        self.assertEqual(detail['talent_render_model']['nodes'], detail['talents'])
        self.assertEqual(detail['talent_render_model']['trees'], detail['talent_groups'])
        self.assertEqual(detail['talent_code'], detail['talent_render_model']['build_code'])
        self.assertEqual(detail['talent_render_model']['layout']['panels'][1]['tree_type'], 'spec')
        self.assertEqual(detail['talent_render_model']['trees'][1]['nodes'][0]['node_key'], 'spec:201')

    def test_player_detail_template_renders_dom_svg_talent_tree_from_render_model(self):
        render_model = build_talent_render_model(
            tree_set=TalentTreeSetModel(
                set_key='Monk:Windwalker',
                class_name='Monk',
                spec_name='Windwalker',
                trees=[
                    TalentTreeModel(
                        tree_type='spec',
                        grid_columns=8,
                        grid_rows=3,
                        nodes=[
                            TalentNodeModel(
                                tree_type='spec',
                                node_id=10,
                                talent_id=10,
                                spell_id=1010,
                                name='起点',
                                layout_row=1,
                                layout_column=2,
                            ),
                            TalentNodeModel(
                                tree_type='spec',
                                node_id=20,
                                talent_id=20,
                                spell_id=2020,
                                name='垂直后继',
                                layout_row=3,
                                layout_column=2,
                                points=1,
                                parents=[10],
                            ),
                        ],
                    ),
                ],
            ),
            build_state=TalentBuildStateModel(
                selected_nodes={'spec:20'},
                node_ranks={'spec:20': 1},
                build_code='BwQAAAAAAAAAAAAAAAAAAAAA',
            ),
        ).to_dict()
        player_detail = {
            'id': 7,
            'rank': 1,
            'character_name': 'Zenwalker',
            'realm': 'Stormrage',
            'region': 'us',
            'score': 4123.45,
            'faction': 'Alliance',
            'race': 'Pandaren',
            'gender': 'female',
            'guild_name': 'Brew Squad',
            'realm_rank': 1,
            'avatar_url': '',
            'profile_url': 'https://raider.io/characters/us/stormrage/Zenwalker',
            'achievement_points': 12345,
            'item_level': 684.2,
            'gear': [],
            'gear_source': '人物榜 Monitor 落库',
            'talents': render_model['nodes'],
            'talent_groups': render_model['trees'],
            'talent_code': render_model['build_code'],
            'talent_render_model': render_model,
            'stats': {},
            'stats_source': 'Battle.net 属性 Monitor 已采集',
            'last_updated': None,
        }

        html = render_to_string(
            'portal/spec_detail/player_detail.html',
            {
                'class_name': 'Monk',
                'spec_name': 'Windwalker',
                'nav': {
                    'class_name': 'Monk',
                    'spec_name': 'Windwalker',
                    'class_cn': '武僧',
                    'spec_cn': '踏风',
                    'spec_icon': 'ability_monk_flyingdragonkick',
                    'role': 'dps',
                },
                'player_detail': player_detail,
            },
        )

        self.assertIn('class="talent-render-stage"', html)
        self.assertIn('data-layout-mode="three-column"', html)
        self.assertIn('data-parent-key="spec:10"', html)
        self.assertIn('data-node-key="spec:20"', html)
        self.assertIn('data-child-key="spec:20"', html)
        self.assertIn('M 168 164 L 168 284', html)
        self.assertIn('https://www.wowhead.com/spell=2020', html)
        self.assertIn('BwQAAAAAAAAAAAAAAAAAAAAA', html)

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    @patch('botend.services.spec_stats_service.SpecDungeonRanking.objects.filter')
    def test_get_dungeon_detail_returns_popularity_talent_tree_render_model(self, mock_filter, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        records = [
            {
                'dps': 1500000,
                'keystone_level': 18,
                'clear_time': 1800000,
                'talents_json': [
                    {
                        'spell_id': 1010,
                        'talent_id': 10,
                        'name': '起点',
                        'tree_type': 'spec',
                        'row': 1,
                        'column': 2,
                    },
                    {
                        'spell_id': 2020,
                        'talent_id': 20,
                        'name': '热门节点',
                        'tree_type': 'spec',
                        'row': 3,
                        'column': 2,
                        'points': 1,
                        'parents': [10],
                    },
                ],
                'gear_json': [],
                'faction': 'Alliance',
            },
            {
                'dps': 1490000,
                'keystone_level': 17,
                'clear_time': 1815000,
                'talents_json': [
                    {
                        'spell_id': 1010,
                        'talent_id': 10,
                        'name': '起点',
                        'tree_type': 'spec',
                        'row': 1,
                        'column': 2,
                    },
                    {
                        'spell_id': 2020,
                        'talent_id': 20,
                        'name': '热门节点',
                        'tree_type': 'spec',
                        'row': 3,
                        'column': 2,
                        'points': 1,
                        'parents': [10],
                    },
                ],
                'gear_json': [],
                'faction': 'Alliance',
            },
        ]
        mock_filter.return_value = FakeRankingQuerySet(
            records,
            first_row=SimpleNamespace(dungeon_name='Ara-Kara, City of Echoes'),
        )

        detail = SpecStatsService.get_dungeon_detail(42, 'Monk', 'Windwalker', season_id=1)

        self.assertIsNotNone(detail['talent_popularity_tree'])
        render_model = detail['talent_popularity_tree']['render_model']
        self.assertEqual(render_model['layout_mode'], 'three-column')
        self.assertEqual(render_model['trees'][0]['paths'][0]['parent_key'], 'spec:10')
        self.assertEqual(render_model['trees'][0]['paths'][0]['child_key'], 'spec:20')
        self.assertEqual(render_model['trees'][0]['nodes'][1]['usage_pct'], 100.0)
        self.assertEqual(detail['talent_popularity_tree']['preserved_parent_edges'], 1)

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_dungeon_stats_template_renders_popularity_talent_tree(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        talent_tree = _compute_talent_popularity_tree(
            records=[
                {
                    'talents_json': [
                        {
                            'spell_id': 1010,
                            'talent_id': 10,
                            'name': '起点',
                            'tree_type': 'spec',
                            'row': 1,
                            'column': 2,
                        },
                        {
                            'spell_id': 2020,
                            'talent_id': 20,
                            'name': '热门节点',
                            'tree_type': 'spec',
                            'row': 3,
                            'column': 2,
                            'points': 1,
                            'parents': [10],
                        },
                    ],
                    'gear_json': [],
                    'faction': 'Alliance',
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
            top_n=10,
        )

        html = render_to_string(
            'portal/spec_detail/dungeon_stats.html',
            {
                'class_name': 'Monk',
                'spec_name': 'Windwalker',
                'nav': {
                    'class_name': 'Monk',
                    'spec_name': 'Windwalker',
                    'class_cn': '武僧',
                    'spec_cn': '踏风',
                    'spec_icon': 'ability_monk_flyingdragonkick',
                    'role': 'dps',
                },
                'dungeon_detail': {
                    'dungeon_name': '回响之城',
                    'sample_size': 1,
                    'talent_popularity_tree': talent_tree,
                    'talent_usage': [],
                },
            },
        )

        self.assertIn('热门天赋树', html)
        self.assertIn('class="talent-render-stage"', html)
        self.assertIn('data-parent-key="spec:10"', html)
        self.assertIn('data-child-key="spec:20"', html)
        self.assertIn('热门节点', html)
        self.assertIn('100.0%', html)

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_raid_stats_template_renders_popularity_talent_tree(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        talent_tree = _compute_talent_popularity_tree(
            records=[
                {
                    'talents_json': [
                        {
                            'spell_id': 3030,
                            'talent_id': 30,
                            'name': '团本前置',
                            'tree_type': 'hero',
                            'row': 1,
                            'column': 2,
                        },
                        {
                            'spell_id': 4040,
                            'talent_id': 40,
                            'name': '团本热门节点',
                            'tree_type': 'hero',
                            'row': 2,
                            'column': 2,
                            'points': 1,
                            'parents': [30],
                        },
                    ],
                    'gear_json': [],
                    'faction': 'Alliance',
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
            top_n=10,
        )

        html = render_to_string(
            'portal/spec_detail/raid_stats.html',
            {
                'class_name': 'Monk',
                'spec_name': 'Windwalker',
                'nav': {
                    'class_name': 'Monk',
                    'spec_name': 'Windwalker',
                    'class_cn': '武僧',
                    'spec_cn': '踏风',
                    'spec_icon': 'ability_monk_flyingdragonkick',
                    'role': 'dps',
                },
                'boss_detail': {
                    'boss_name': '测试首领',
                    'sample_size': 1,
                    'talent_popularity_tree': talent_tree,
                    'talent_usage': [],
                },
            },
        )

        self.assertIn('热门天赋树', html)
        self.assertIn('class="talent-render-stage"', html)
        self.assertIn('data-parent-key="hero:30"', html)
        self.assertIn('data-child-key="hero:40"', html)
        self.assertIn('团本热门节点', html)
        self.assertIn('100.0%', html)
