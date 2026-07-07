# -*- coding: utf-8 -*-

from types import SimpleNamespace
from unittest.mock import patch, MagicMock

from django.template.loader import render_to_string
from django.test import SimpleTestCase

from botend.services.spec_stats_service import (
    SpecStatsService,
    _compute_enchant_popularity,
    _compute_gear_popularity,
    _compute_talent_build_popularity,
    _compute_talent_popularity_tree,
)
from botend.management.commands.backfill_talent_spell_names import Command as BackfillTalentSpellNamesCommand
from botend.management.commands.init_talent_metadata import Command as InitTalentMetadataCommand
from botend.management.commands.normalize_talent_metadata import Command as NormalizeTalentMetadataCommand
from botend.wow.talents.adapters import build_tree_set_from_talents
from botend.wow.talents.layout import build_talent_tree_layout
from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.versioning import TalentVersionResolver
from botend.wow.talents.models import (
    TalentBuildStateModel,
    TalentNodeModel,
    TalentTreeModel,
    TalentTreeSetModel,
)
from botend.wow.talents.render import build_talent_render_model
from botend.wow.talents.service import TalentBuildCodeService
from botend.wow.talents.build_code import TalentBuildCodeDecoder, TalentBuildCodeEncoder, _ImportBitWriter
from botend.portal.talent_simulator import _merge_nodes_for_simulator
from botend.wow.talents.view_model import build_talent_view_model
from botend.controller.plugins.portal.SpecDetailPlayerMonitor import SpecDetailPlayerMonitor


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




class TalentVersionResolverTests(SimpleTestCase):
    def test_serialize_returns_stable_payload_for_version_object(self):
        version = SimpleNamespace(
            key='retail-12.0.7',
            label='正式服 12.0.7',
            branch='retail',
            major_version='12.0.7',
            current_build='12.0.7.68367',
            status='active',
            is_active=True,
        )

        payload = TalentVersionResolver.serialize(version)

        self.assertEqual(payload['key'], 'retail-12.0.7')
        self.assertEqual(payload['label'], '正式服 12.0.7')
        self.assertEqual(payload['branch'], 'retail')
        self.assertEqual(payload['major_version'], '12.0.7')
        self.assertEqual(payload['current_build'], '12.0.7.68367')
        self.assertEqual(payload['status'], 'active')
        self.assertTrue(payload['is_active'])

    @patch('botend.wow.talents.versioning.WowTalentVersion')
    def test_get_default_uses_usage_specific_flag_before_retail_fallback(self, version_model):
        default_version = SimpleNamespace(key='retail-12.0.7')
        version_model.objects.filter.return_value.order_by.return_value.first.return_value = default_version

        resolved = TalentVersionResolver.get_default(TalentVersionResolver.USAGE_STATS)

        self.assertIs(resolved, default_version)
        version_model.objects.filter.assert_called_with(is_active=True, is_default_stats=True)

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
        self.assertEqual([tree.tree_type for tree in tree_set.trees], ['class', 'hero', 'spec'])
        self.assertEqual(tree_set.set_key, 'Monk:Windwalker')
        self.assertEqual(tree_set.layout_mode, 'three-column')
        self.assertEqual(tree_set.trees[0].title, '职业天赋')
        spec_tree = next(t for t in tree_set.trees if t.tree_type == 'spec')
        self.assertFalse(spec_tree.synthetic_layout)
        self.assertIsInstance(spec_tree.nodes[0], TalentNodeModel)
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
        # Raw DB2 coords are now used directly as layout values
        self.assertEqual(tree.grid_rows, 2400)
        self.assertEqual(tree.grid_columns, 8700)
        self.assertEqual(
            [(node.row, node.column, node.layout_row, node.layout_column) for node in tree.nodes],
            [
                (1500, 4200, 1500, 4200),
                (2400, 6000, 2400, 6000),
                (2400, 8700, 2400, 8700),
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
        # Raw DB2 coords are now used directly as layout values
        self.assertEqual(node.layout_row, 5100)
        self.assertEqual(node.layout_column, 10800)

    def test_adapter_does_not_remerge_complete_authoritative_db2_nodes(self):
        class Provider:
            def merge_into_node(self, node, class_name='', spec_name=''):
                raise AssertionError('authoritative DB2 metadata should not be merged again')

        tree_set, _ = build_tree_set_from_talents(
            [
                {
                    'node_id': 10101,
                    'spell_id': 101,
                    'talent_id': 101,
                    'name': 'DB2 节点',
                    'tree_type': 'spec',
                    'row': 1500,
                    'column': 4200,
                    'parents': [10001],
                    'db2_subtree_id': 123,
                    'description': 'original desc',
                    'description_zh': '原始描述',
                    'source': 'db2_backfill',
                },
            ],
            class_name='Monk',
            spec_name='Windwalker',
            metadata_provider=Provider(),
        )

        node = tree_set.trees[0].nodes[0]
        self.assertEqual(node.name, 'DB2 节点')
        self.assertEqual(node.layout_row, 1500)
        self.assertEqual(node.layout_column, 4200)
        self.assertEqual(node.parents, [10001])
        self.assertEqual(node.db2_subtree_id, 123)
        self.assertEqual(node.description, 'original desc')
        self.assertEqual(node.description_zh, '原始描述')


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

    @patch('botend.wow.talents.metadata.TalentVersionResolver')
    @patch('botend.wow.talents.metadata.WowTalentNodeMetadata')
    def test_full_tree_nodes_filters_by_resolved_talent_version(self, metadata_model, resolver):
        version = SimpleNamespace(id=7, key='retail-12.0.7')
        resolver.resolve.return_value = version
        query = MagicMock()
        query.exclude.return_value.order_by.return_value.iterator.return_value = []
        metadata_model.objects.filter.return_value = query

        provider = TalentMetadataProvider(version_key='retail-12.0.7')
        provider.get_full_tree_nodes('DeathKnight', 'Blood')

        resolver.resolve.assert_called_with(
            version_key='retail-12.0.7',
            usage=TalentVersionResolver.USAGE_SIMULATOR,
        )
        metadata_model.objects.filter.assert_called_with(
            class_name='DeathKnight',
            spec_name='Blood',
            source__in={'db2_backfill', 'db2_repair', 'db2'},
            talent_version=version,
        )

    @patch('botend.wow.talents.service.TalentMetadataProvider')
    def test_build_code_service_passes_version_to_provider(self, provider_cls):
        provider = provider_cls.return_value
        provider.get_full_tree_nodes.return_value = []

        TalentBuildCodeService.build_full_payload(
            class_name='DeathKnight',
            spec_name='Blood',
            talent_build_code='',
            version_key='ptr-12.1.0',
            usage=TalentVersionResolver.USAGE_PLAYER_TREE,
        )

        provider_cls.assert_called_with(
            talent_version=None,
            version_key='ptr-12.1.0',
            usage=TalentVersionResolver.USAGE_PLAYER_TREE,
        )



class TalentMetadataBackfillTests(SimpleTestCase):
    def test_resolve_metadata_row_keeps_layout_coordinates_when_spell_name_resolves(self):
        command = BackfillTalentSpellNamesCommand()
        command._resolve_trait_layout = lambda monitor, build, raw_id: {
            'row': 2100,
            'column': 12000,
            'tree_type': 'spec',
        }
        command._resolve_trait_parents = lambda monitor, build, raw_id: [124865]
        command._resolve_spell_name = lambda monitor, build, spell_id: '活血酒'
        command._resolve_spell_icon = lambda monitor, build, spell_id, definition: 'inv_misc_beer_06'

        class Monitor:
            def _fetch_db2_row_by_id_requests(self, table, build, raw_id, locale_override=None):
                if table == 'TraitNodeEntry':
                    return {'TraitDefinitionID': 1, 'MaxRanks': 1, 'TraitSubTreeID': 0}
                if table == 'TraitDefinition':
                    return {'VisibleSpellID': 119582}
                return {}

        row = SimpleNamespace(
            node_id=124838,
            talent_id=124838,
            spell_id=124838,
            display_spell_id=None,
            row=None,
            column=None,
            max_points=1,
            tree_type='spec',
            name='',
            name_zh='',
            icon='',
        )

        resolved = command._resolve_metadata_row(Monitor(), '12.0.5.67823', row)

        self.assertEqual(resolved['row'], 2100)
        self.assertEqual(resolved['column'], 12000)
        self.assertEqual(resolved['parents_json'], [124865])
        self.assertEqual(resolved['name_zh'], '活血酒')
        self.assertEqual(resolved['icon'], 'inv_misc_beer_06')

    def test_backfill_parents_should_merge_with_existing_parents_instead_of_overwriting(self):
        command = BackfillTalentSpellNamesCommand()
        existing = [124865, 124866]
        resolved = [124866, 124867]

        merged = sorted(
            {
                command._coerce_int(parent_id, 0) or 0
                for parent_id in list(existing or []) + list(resolved or [])
                if command._coerce_int(parent_id, 0)
            }
        )

        self.assertEqual(merged, [124865, 124866, 124867])


class TalentMetadataInitCommandTests(SimpleTestCase):
    def test_build_targets_supports_class_and_spec_filters(self):
        command = InitTalentMetadataCommand()

        monk_targets = command._build_targets('Monk', '')
        self.assertIn(('Monk', 'Brewmaster'), monk_targets)
        self.assertIn(('Monk', 'Mistweaver'), monk_targets)
        self.assertIn(('Monk', 'Windwalker'), monk_targets)

        single_target = command._build_targets('Monk', 'Windwalker')
        self.assertEqual(single_target, [('Monk', 'Windwalker')])



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
        self.assertEqual(layout.width, 2048)
        self.assertEqual(layout.height, 472)
        self.assertEqual([(panel.tree_type, panel.x, panel.y) for panel in layout.panels], [
            ('class', 0, 0),
            ('spec', 816, 0),
            ('hero', 1632, 0),
        ])

        spec_panel = layout.panels[1]
        self.assertEqual((spec_panel.width, spec_panel.height), (800, 472))
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
            (2, 3, 1036, 180, 1072, 216, True),
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
                ('spec:10', 'spec:20', 'M 160 156 L 160 276'),
                ('spec:20', 'spec:30', 'M 160 348 L 352 372'),
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
        self.assertEqual([tree['tree_type'] for tree in payload['trees']], ['class', 'hero', 'spec', 'build_code'])
        self.assertEqual(
            [node['tree_type'] for node in payload['nodes']],
            ['class', 'hero', 'spec', 'build_code'],
        )

        spec_tree = next(t for t in payload['trees'] if t['tree_type'] == 'spec')
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
        self.assertEqual(payload['layout']['panels'][1]['tree_type'], 'hero')
        build_code_tree = next(t for t in payload['trees'] if t['tree_type'] == 'build_code')
        self.assertEqual(build_code_tree['nodes'][0]['talent_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')

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


class TalentSimulatorBuildCodeTests(SimpleTestCase):
    DEATH_KNIGHT_REFERENCE_CODE = 'CoPAAAAAAAAAAAAAAAAAAAAAAAAAAAAMAAAAAAAAAAAAAAA'
    FULL_NODES = [
        {'tree_type': 'class', 'node_id': 96200, 'talent_id': 96200, 'spell_id': 100200, 'max_points': 1},
        {'tree_type': 'class', 'node_id': 96196, 'talent_id': 96196, 'spell_id': 100196, 'max_points': 1},
        {
            'tree_type': 'class',
            'node_id': 96203,
            'talent_id': 96203,
            'spell_id': 391546,
            'display_spell_id': 391546,
            'max_points': 1,
            'choice_options': [
                {'talent_id': 76074, 'spell_id': 391546, 'display_spell_id': 391546, 'name': '黑暗行军'},
                {'talent_id': 76074, 'spell_id': 212552, 'display_spell_id': 212552, 'name': '幻影步'},
            ],
        },
    ]

    @patch('botend.wow.talents.service.TalentMetadataProvider')
    def test_encoder_preserves_choice_selection_for_choice_node(self, mock_provider_cls):
        mock_provider_cls.return_value.get_decoder_node_list.return_value = list(self.FULL_NODES)
        selected_nodes = [
            {'tree_type': 'class', 'node_id': 96200, 'talent_id': 96200, 'points': 1},
            {'tree_type': 'class', 'node_id': 96196, 'talent_id': 96196, 'points': 1},
            {'tree_type': 'class', 'node_id': 96203, 'talent_id': 96203, 'points': 1, 'choice_selection': 1},
        ]

        build_code = TalentBuildCodeService.encode_build_code_from_nodes(
            selected_nodes,
            class_name='DeathKnight',
            spec_name='Blood',
            reference_build_code=self.DEATH_KNIGHT_REFERENCE_CODE,
        )
        decoded = TalentBuildCodeDecoder.decode_node_states(build_code, self.FULL_NODES)

        self.assertTrue(build_code)
        self.assertEqual(decoded['class:96203']['choice_selection'], 1)
        self.assertEqual(decoded['class:96203']['points'], 1)

    def test_decoder_treats_selected_unpurchased_node_as_granted_zero_points(self):
        decoder_nodes = [
            {
                'tree_type': 'class',
                'node_id': 112182,
                'talent_id': 90325,
                'spell_id': 123000,
                'max_points': 1,
            }
        ]
        writer = _ImportBitWriter()
        writer.write(0, TalentBuildCodeDecoder.HEADER_VERSION_BITS)
        writer.write(0, TalentBuildCodeDecoder.SPEC_ID_BITS)
        for _ in range(16):
            writer.write(0, 8)
        writer.write(1, 1)  # selected/granted by default
        writer.write(0, 1)  # not purchased, should not consume a point
        build_code = writer.to_string()

        decoded = TalentBuildCodeDecoder.decode_node_states(build_code, decoder_nodes)

        self.assertEqual(decoded['class:112182']['points'], 0)
        self.assertTrue(decoded['class:112182']['selected'])
        self.assertFalse(decoded['class:112182']['purchased'])

    def test_decoder_does_not_sum_regular_choice_option_points(self):
        decoder_nodes = [
            {
                'tree_type': 'hero',
                'node_id': 117414,
                'talent_id': 94817,
                'spell_id': 123001,
                'max_points': 1,
                'is_choice_node': True,
                'choice_options': [
                    {'node_id': 117414, 'talent_id': 94817, 'spell_id': 123001, 'max_points': 1},
                    {'node_id': 117415, 'talent_id': 94817, 'spell_id': 123002, 'max_points': 1},
                ],
            }
        ]
        writer = _ImportBitWriter()
        writer.write(0, TalentBuildCodeDecoder.HEADER_VERSION_BITS)
        writer.write(0, TalentBuildCodeDecoder.SPEC_ID_BITS)
        for _ in range(16):
            writer.write(0, 8)
        writer.write(1, 1)  # selected
        writer.write(1, 1)  # purchased
        writer.write(0, 1)  # full rank, should mean max_points of the base node
        writer.write(1, 1)  # choice node
        writer.write(1, 2)  # second option
        build_code = writer.to_string()

        decoded = TalentBuildCodeDecoder.decode_node_states(build_code, decoder_nodes)

        self.assertEqual(decoded['hero:117414']['points'], 1)
        self.assertEqual(decoded['hero:117414']['choice_selection'], 1)

    def test_decoder_sums_apex_entry_point_pool(self):
        decoder_nodes = [
            {
                'tree_type': 'spec',
                'node_id': 137002,
                'talent_id': 110412,
                'spell_id': 1269310,
                'max_points': 1,
                'is_apex_talent': True,
                'choice_options': [
                    {'node_id': 137004, 'talent_id': 110412, 'spell_id': 1269308, 'max_points': 1},
                    {'node_id': 137003, 'talent_id': 110412, 'spell_id': 1269309, 'max_points': 2},
                    {'node_id': 137002, 'talent_id': 110412, 'spell_id': 1269310, 'max_points': 1},
                ],
            }
        ]
        build_code = TalentBuildCodeEncoder.encode_node_states(
            self.DEATH_KNIGHT_REFERENCE_CODE,
            decoder_nodes,
            [{'tree_type': 'spec', 'node_id': 137002, 'talent_id': 110412, 'points': 4}],
        )

        decoded = TalentBuildCodeDecoder.decode_node_states(build_code, decoder_nodes)

        self.assertEqual(decoded['spec:137002']['points'], 4)

    def test_decoder_sums_hero_anchor_apex_choice_options(self):
        decoder_nodes = [
            {
                'tree_type': 'hero_anchor',
                'node_id': 137002,
                'talent_id': 110412,
                'spell_id': 1269310,
                'max_points': 1,
                'db2_subtree_id': 61,
                'choice_options': [
                    {'node_id': 137004, 'talent_id': 110412, 'spell_id': 1269308, 'max_points': 1},
                    {'node_id': 137003, 'talent_id': 110412, 'spell_id': 1269309, 'max_points': 2},
                    {'node_id': 137002, 'talent_id': 110412, 'spell_id': 1269310, 'max_points': 1},
                ],
            }
        ]
        build_code = TalentBuildCodeEncoder.encode_node_states(
            self.DEATH_KNIGHT_REFERENCE_CODE,
            decoder_nodes,
            [{'tree_type': 'hero_anchor', 'node_id': 137002, 'talent_id': 110412, 'points': 4}],
        )

        decoded = TalentBuildCodeDecoder.decode_node_states(build_code, decoder_nodes)

        self.assertEqual(decoded['hero_anchor:137002']['points'], 4)

    def test_decoder_node_list_excludes_content_script_hero_anchor_noise(self):
        self.assertFalse(TalentMetadataProvider._include_in_decoder_node_list({
            'tree_type': 'hero_anchor',
            'talent_id': 99851,
            'node_id': 123388,
            'name': 'Plant Scallions',
            'db2_subtree_id': 0,
        }))
        self.assertTrue(TalentMetadataProvider._include_in_decoder_node_list({
            'tree_type': 'hero_anchor',
            'talent_id': 101235,
            'node_id': 125051,
            'name': '天神御师',
            'db2_subtree_id': 64,
        }))
        self.assertTrue(TalentMetadataProvider._include_in_decoder_node_list({
            'tree_type': 'spec',
            'talent_id': 110412,
            'node_id': 137002,
            'name': '怒火狂战',
            'db2_subtree_id': 0,
        }))

    def test_simulator_merge_applies_imported_choice_option_display(self):
        merged = _merge_nodes_for_simulator(
            self.FULL_NODES,
            decoded_states={'class:96203': {'selected': True, 'points': 1, 'choice_selection': 1}},
        )
        choice_node = next(node for node in merged if node.get('node_id') == 96203)

        self.assertEqual(choice_node['choice_selection'], 1)
        self.assertEqual(choice_node['name'], '幻影步')
        self.assertEqual(choice_node['display_spell_id'], 212552)
        self.assertEqual(choice_node['points'], 1)

    def test_simulator_merge_hides_hero_nodes_until_subtree_selected(self):
        full_nodes = [
            {'tree_type': 'class', 'node_id': 1001, 'talent_id': 1001, 'spell_id': 2001},
            {'tree_type': 'hero', 'node_id': 3001, 'talent_id': 3001, 'spell_id': 4001, 'db2_subtree_id': 11},
            {'tree_type': 'hero', 'node_id': 3002, 'talent_id': 3002, 'spell_id': 4002, 'db2_subtree_id': 22},
            {'tree_type': 'spec', 'node_id': 5001, 'talent_id': 5001, 'spell_id': 6001},
        ]

        merged_without_choice = _merge_nodes_for_simulator(full_nodes, active_hero_subtree=0)
        merged_with_choice = _merge_nodes_for_simulator(full_nodes, active_hero_subtree=22)

        self.assertEqual([node['tree_type'] for node in merged_without_choice], ['class', 'spec'])
        self.assertEqual(
            [(node['tree_type'], node['node_id']) for node in merged_with_choice],
            [('class', 1001), ('hero', 3002), ('spec', 5001)],
        )

    def test_encoder_maps_entry_and_spell_aliases_to_decoder_nodes(self):
        decoder_nodes = [
            {'tree_type': 'spec', 'node_id': 76042, 'talent_id': 76042, 'spell_id': 108199, 'max_points': 1,
             'is_choice_node': True, 'choice_options': [
                 {'talent_id': 76042, 'spell_id': 108199, 'display_spell_id': 108199},
                 {'talent_id': 136213, 'spell_id': 1263569, 'display_spell_id': 1263569},
             ]},
        ]
        selected_nodes = [
            {'tree_type': 'spec', 'node_id': 136213, 'talent_id': 136213, 'spell_id': 1263569, 'points': 1},
        ]

        lookup = TalentBuildCodeEncoder._build_selected_lookup(selected_nodes, decoder_nodes)

        self.assertIn('spec:76042', lookup)
        self.assertEqual(lookup['spec:76042']['points'], 1)
        self.assertEqual(lookup['spec:76042']['choice_selection'], 1)

    def test_build_full_payload_falls_back_to_structured_talents_when_build_code_loses_hero(self):
        decoder_nodes = [
            {'tree_type': 'class', 'node_id': 1001, 'talent_id': 1001, 'spell_id': 2001, 'max_points': 1},
            {'tree_type': 'hero', 'node_id': 3001, 'talent_id': 3001, 'spell_id': 4001, 'db2_subtree_id': 11, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5001, 'talent_id': 5001, 'spell_id': 6001, 'max_points': 1},
        ]
        selected_nodes = [
            {'tree_type': 'class', 'node_id': 1001, 'talent_id': 1001, 'points': 1},
            {'tree_type': 'hero', 'node_id': 3001, 'talent_id': 3001, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5001, 'talent_id': 5001, 'points': 1},
        ]
        stale_decoded = {'class:1001': {'selected': True, 'points': 1}}

        merged_states = TalentBuildCodeService._prefer_structured_nodes_when_build_code_looks_stale(
            stale_decoded,
            selected_nodes,
            decoder_nodes,
            class_name='DeathKnight',
            spec_name='Blood',
        )

        self.assertEqual(merged_states['class:1001']['points'], 1)
        self.assertEqual(merged_states['hero:3001']['points'], 1)
        self.assertEqual(merged_states['spec:5001']['points'], 1)

    def test_render_model_marks_bottom_multi_entry_node_as_apex_pool(self):
        render_model = build_talent_render_model(
            [
                {'tree_type': 'spec', 'node_id': 5001, 'talent_id': 5001, 'spell_id': 6001, 'name': '普通专精', 'row': 6000, 'column': 10200, 'max_points': 1, 'points': 1},
                {
                    'tree_type': 'spec', 'node_id': 5101, 'talent_id': 9001, 'spell_id': 6101,
                    'name': '顶峰节点', 'row': 7800, 'column': 12600, 'max_points': 1, 'points': 0,
                    'choice_options': [
                        {'tree_type': 'spec', 'node_id': 5101, 'talent_id': 9001, 'spell_id': 6101, 'name': '顶峰一', 'row': 7800, 'column': 12600, 'max_points': 1, 'points': 0},
                        {'tree_type': 'spec', 'node_id': 5102, 'talent_id': 9001, 'spell_id': 6102, 'name': '顶峰二', 'row': 7800, 'column': 12600, 'max_points': 2, 'points': 0},
                        {'tree_type': 'spec', 'node_id': 5103, 'talent_id': 9001, 'spell_id': 6103, 'name': '顶峰三', 'row': 7800, 'column': 12600, 'max_points': 1, 'points': 0},
                    ],
                },
            ],
            class_name='DeathKnight',
            spec_name='Blood',
            metadata_provider=type('Provider', (), {'merge_into_node': lambda self, node, class_name='', spec_name='': dict(node)})(),
        ).to_dict()

        spec_tree = next(tree for tree in render_model['trees'] if tree['tree_type'] == 'spec')
        apex_nodes = [node for node in spec_tree['nodes'] if node.get('is_apex_talent')]
        normal_nodes = [node for node in spec_tree['nodes'] if not node.get('is_apex_talent')]

        self.assertEqual([node['node_id'] for node in apex_nodes], [5101])
        self.assertEqual(apex_nodes[0]['point_pool'], 'apex')
        self.assertEqual(normal_nodes[0]['point_pool'], 'spec')
        self.assertEqual(spec_tree['point_pools']['apex']['max_points'], 4)
        self.assertEqual(spec_tree['point_pools']['apex']['points'], 0)
        self.assertEqual(spec_tree['point_pools']['spec']['points'], 1)

    def test_raiderio_loadout_preserves_apex_rank_when_build_code_omits_it(self):
        loadout = {
            'loadout_text': 'CgEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgGDDjZ2WmZmZmxMmZMjZmZWmZGjxsMmZGAAIMwGssZ0YGQmNMjFAzgxAgZGADzMzMMYA',
            'loadout': [
                {
                    'node': {
                        'id': 110412,
                        'type': 1,
                        'entries': [
                            {'id': 137004, 'type': 13, 'maxRanks': 1, 'spell': {'id': 1269308, 'name': 'Rampaging Berserker', 'icon': 'inv12_apextalent_warrior_rampagingberserker2'}},
                            {'id': 137003, 'type': 13, 'maxRanks': 2, 'spell': {'id': 1269309, 'name': 'Rampaging Berserker', 'icon': 'inv12_apextalent_warrior_rampagingberserker2'}},
                            {'id': 137002, 'type': 13, 'maxRanks': 1, 'spell': {'id': 1269310, 'name': 'Rampaging Berserker', 'icon': 'inv12_apextalent_warrior_rampagingberserker2'}},
                        ],
                        'posX': 12000,
                        'posY': 7050,
                        'row': 0,
                        'col': 14,
                    },
                    'entryIndex': 2,
                    'rank': 4,
                },
            ],
        }

        parsed = SpecDetailPlayerMonitor._parse_rio_talent_loadout_nodes(loadout['loadout'])
        self.assertEqual(parsed[0]['node_id'], 137002)
        self.assertEqual(parsed[0]['talent_id'], 110412)
        self.assertEqual(parsed[0]['points'], 4)
        self.assertEqual(parsed[0]['max_points'], 4)

        decoder_nodes = [
            {
                'tree_type': 'spec',
                'node_id': 137002,
                'talent_id': 110412,
                'spell_id': 1269310,
                'display_spell_id': 1269310,
                'name': 'Rampaging Berserker',
                'row': 7050,
                'column': 12000,
                'max_points': 4,
                'choice_options': [
                    {'node_id': 137004, 'talent_id': 110412, 'spell_id': 1269308, 'max_points': 1},
                    {'node_id': 137003, 'talent_id': 110412, 'spell_id': 1269309, 'max_points': 2},
                    {'node_id': 137002, 'talent_id': 110412, 'spell_id': 1269310, 'max_points': 1},
                ],
            },
        ]
        decoded_states = {}
        merged_states = TalentBuildCodeService._prefer_structured_nodes_when_build_code_looks_stale(
            decoded_states,
            parsed,
            decoder_nodes,
            class_name='Warrior',
            spec_name='Fury',
        )
        merged_nodes = TalentBuildCodeService._merge_full_tree_nodes(
            decoder_nodes,
            parsed,
            decoded_states=merged_states,
            has_build_code=True,
        )
        render_model = build_talent_render_model(
            merged_nodes,
            class_name='Warrior',
            spec_name='Fury',
            metadata_provider=type('Provider', (), {'merge_into_node': lambda self, node, class_name='', spec_name='': dict(node)})(),
        ).to_dict()
        spec_tree = next(tree for tree in render_model['trees'] if tree['tree_type'] == 'spec')
        apex_node = next(node for node in spec_tree['nodes'] if node.get('talent_id') == 110412)

        self.assertTrue(apex_node['selected'])
        self.assertEqual(apex_node['points'], 4)
        self.assertEqual(apex_node['max_points'], 4)
        self.assertEqual(spec_tree['point_pools']['apex']['points'], 4)
        self.assertEqual(spec_tree['point_pools']['apex']['max_points'], 4)

    @patch('botend.wow.talents.service.TalentMetadataProvider')
    def test_build_api_view_keeps_profile_build_code_when_structured_nodes_lack_apex(self, mock_provider_cls):
        stale_structured_nodes = [
            {'tree_type': 'class', 'node_id': 112182, 'talent_id': 112182, 'spell_id': 386196, 'name': '狂暴姿态', 'points': 1, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 112292, 'talent_id': 112292, 'spell_id': 383295, 'name': '熟能生巧', 'points': 2, 'max_points': 2},
        ]

        apex_render_node = {
            'tree_type': 'spec',
            'node_id': 137002,
            'talent_id': 110412,
            'spell_id': 1269310,
            'display_spell_id': 1269310,
            'name': 'Rampaging Berserker',
            'row': 10000,
            'column': 12000,
            'max_points': 4,
            'is_apex_talent': True,
            'point_pool': 'apex',
            'apex_entries': [
                {'node_id': 137004, 'talent_id': 110412, 'spell_id': 1269308, 'max_points': 1},
                {'node_id': 137003, 'talent_id': 110412, 'spell_id': 1269309, 'max_points': 2},
                {'node_id': 137002, 'talent_id': 110412, 'spell_id': 1269310, 'max_points': 1},
            ],
        }
        apex_decoder_node = dict(apex_render_node, tree_type='hero_anchor', max_points=1)
        apex_decoder_node['choice_options'] = list(apex_render_node['apex_entries'])
        build_code = TalentBuildCodeEncoder.encode_node_states(
            self.DEATH_KNIGHT_REFERENCE_CODE,
            [apex_decoder_node],
            [{'tree_type': 'hero_anchor', 'node_id': 137002, 'talent_id': 110412, 'points': 4}],
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [apex_render_node]
        mock_provider_cls.return_value.get_decoder_node_list.return_value = [apex_decoder_node]
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': dict(node)
        )

        payload = TalentBuildCodeService.build_full_payload(
            class_name='Warrior',
            spec_name='Fury',
            talent_build_code=build_code,
            talents_json=stale_structured_nodes,
        )

        self.assertEqual(payload[0]['talent_code'], build_code)
        apex_node = next(node for node in payload if node.get('talent_id') == 110412)
        self.assertTrue(apex_node['selected'])
        self.assertEqual(apex_node['points'], 4)
        self.assertEqual(apex_node['max_points'], 4)

    def test_prefers_structured_nodes_when_build_code_decodes_wrong_nodes_with_similar_total(self):
        decoder_nodes = [
            {'tree_type': 'class', 'node_id': 1001, 'talent_id': 1001, 'spell_id': 2001, 'max_points': 1},
            {'tree_type': 'class', 'node_id': 1002, 'talent_id': 1002, 'spell_id': 2002, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5001, 'talent_id': 5001, 'spell_id': 6001, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5002, 'talent_id': 5002, 'spell_id': 6002, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5003, 'talent_id': 5003, 'spell_id': 6003, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5004, 'talent_id': 5004, 'spell_id': 6004, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5005, 'talent_id': 5005, 'spell_id': 6005, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5006, 'talent_id': 5006, 'spell_id': 6006, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5007, 'talent_id': 5007, 'spell_id': 6007, 'max_points': 1},
            {'tree_type': 'spec', 'node_id': 5008, 'talent_id': 5008, 'spell_id': 6008, 'max_points': 1},
        ]
        structured_nodes = [
            {'tree_type': 'class', 'node_id': 1001, 'talent_id': 1001, 'spell_id': 2001, 'points': 1},
            {'tree_type': 'class', 'node_id': 1002, 'talent_id': 1002, 'spell_id': 2002, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5001, 'talent_id': 5001, 'spell_id': 6001, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5002, 'talent_id': 5002, 'spell_id': 6002, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5003, 'talent_id': 5003, 'spell_id': 6003, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5004, 'talent_id': 5004, 'spell_id': 6004, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5005, 'talent_id': 5005, 'spell_id': 6005, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5006, 'talent_id': 5006, 'spell_id': 6006, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5007, 'talent_id': 5007, 'spell_id': 6007, 'points': 1},
            {'tree_type': 'spec', 'node_id': 5008, 'talent_id': 5008, 'spell_id': 6008, 'points': 1},
        ]
        misaligned_decoded_states = {
            'class:1001': {'selected': True, 'points': 1},
            'class:1002': {'selected': True, 'points': 1},
            'spec:5001': {'selected': True, 'points': 1},
            'spec:5002': {'selected': True, 'points': 1},
            'spec:5003': {'selected': True, 'points': 1},
            # Same total as the structured payload, but the remaining points are
            # shifted into nodes not present in the structured/current render tree.
            'spec:9001': {'selected': True, 'points': 1},
            'spec:9002': {'selected': True, 'points': 1},
            'spec:9003': {'selected': True, 'points': 1},
            'spec:9004': {'selected': True, 'points': 1},
            'spec:9005': {'selected': True, 'points': 1},
        }

        merged_states = TalentBuildCodeService._prefer_structured_nodes_when_build_code_looks_stale(
            misaligned_decoded_states,
            structured_nodes,
            decoder_nodes,
            class_name='Warrior',
            spec_name='Fury',
        )

        self.assertEqual(sum(state['points'] for state in merged_states.values()), 10)
        self.assertIn('spec:5008', merged_states)
        self.assertNotIn('spec:9005', merged_states)

    def test_merge_maps_structured_choice_option_state_to_render_choice_node(self):
        full_nodes = [
            {
                'tree_type': 'spec',
                'node_id': 112284,
                'talent_id': 90415,
                'spell_id': 227847,
                'display_spell_id': 227847,
                'name': '剑刃风暴',
                'max_points': 1,
                'choice_options': [
                    {'node_id': 112284, 'talent_id': 90415, 'spell_id': 227847, 'display_spell_id': 227847, 'name': '剑刃风暴'},
                    {'node_id': 112285, 'talent_id': 90415, 'spell_id': 107574, 'display_spell_id': 107574, 'name': '天神下凡'},
                ],
            },
        ]
        structured_nodes = [
            {
                'tree_type': 'spec',
                'node_id': 112285,
                'talent_id': 112285,
                'spell_id': 107574,
                'display_spell_id': 107574,
                'name': '天神下凡',
                'points': 1,
                'max_points': 1,
            },
        ]
        structured_states = {
            'spec:112285': {
                'selected': True,
                'points': 1,
                'is_choice_node': False,
                'choice_selection': 0,
            },
        }

        merged = TalentBuildCodeService._merge_full_tree_nodes(
            full_nodes,
            structured_nodes,
            decoded_states=structured_states,
            has_build_code=True,
            decoder_nodes=full_nodes,
        )

        self.assertEqual(merged[0]['points'], 1)
        self.assertTrue(merged[0]['selected'])
        self.assertEqual(merged[0]['name'], '天神下凡')
        self.assertEqual(merged[0]['spell_id'], 107574)


class SpecStatsTalentRenderTests(SimpleTestCase):
    def test_enchant_popularity_groups_by_slot_and_formats_display_label(self):
        records = [
            {
                'gear_json': [
                    {'slot': 'head', 'enchants_detail': [{'id': 1001, 'name': 'Enchant Helm - Blessing of Speed', 'icon': 'spell_speed'}]},
                    {'slot': 'finger1', 'enchants_detail': [{'id': 2001, 'name': 'Enchant Ring - Eyes of the Eagle', 'icon': 'spell_eagle'}]},
                ],
            },
            {
                'gear_json': [
                    {'slot': 'HEAD', 'enchants_detail': [{'id': 1001, 'name': 'Enchant Helm - Blessing of Speed', 'icon': 'spell_speed'}]},
                    {'slot': 'finger2', 'enchants_detail': [{'id': 2001, 'name': 'Enchant Ring - Eyes of the Eagle', 'icon': 'spell_eagle'}]},
                    {'slot': 'hands', 'enchants_detail': [{'id': 2001, 'name': 'Enchant Ring - Eyes of the Eagle', 'icon': 'spell_eagle'}]},
                ],
            },
        ]

        result = _compute_enchant_popularity(records, top_n=10)

        self.assertEqual([group['slot_label'] for group in result], ['头盔', '手套', '戒指'])
        by_slot = {group['slot_label']: group['enchants'] for group in result}
        self.assertEqual(by_slot['头盔'][0]['display_label'], '头盔：附魔头盔：速度祝福')
        self.assertEqual(by_slot['头盔'][0]['count'], 2)
        self.assertEqual(by_slot['戒指'][0]['display_label'], '戒指：附魔戒指：鹰眼')
        self.assertEqual(by_slot['戒指'][0]['count'], 2)
        self.assertEqual(by_slot['手套'][0]['display_label'], '手套：附魔戒指：鹰眼')
        self.assertEqual(by_slot['手套'][0]['count'], 1)
        self.assertEqual(by_slot['戒指'][0]['slot_label'], '戒指')

    def test_gear_popularity_merges_ring_trinket_slots_and_filters_cosmetic_slots(self):
        records = [
            {
                'gear_json': [
                    {'slot': 'finger1', 'id': 101, 'name': '急速戒指', 'icon': 'inv_ring_01'},
                    {'slot': 'finger2', 'id': 102, 'name': '精通戒指', 'icon': 'inv_ring_02'},
                    {'slot': 'trinket1', 'id': 201, 'name': '主动饰品', 'icon': 'inv_trinket_01'},
                    {'slot': 'trinket2', 'id': 202, 'name': '被动饰品', 'icon': 'inv_trinket_02'},
                    {'slot': 'shirt', 'id': 301, 'name': '衬衫', 'icon': 'inv_shirt_01'},
                    {'slot': 'tabard', 'id': 302, 'name': '战袍', 'icon': 'inv_tabard_01'},
                ],
            },
            {
                'gear_json': [
                    {'slot': 'FINGER_1', 'id': 101, 'name': '急速戒指', 'icon': 'inv_ring_01'},
                    {'slot': 'TRINKET_2', 'id': 201, 'name': '主动饰品', 'icon': 'inv_trinket_01'},
                ],
            },
        ]

        result = _compute_gear_popularity(records, top_n=10)

        self.assertIn('戒指', result)
        self.assertNotIn('戒指1', result)
        self.assertNotIn('戒指2', result)
        self.assertEqual(
            [(item['itemID'], item['count']) for item in result['戒指']],
            [(101, 2), (102, 1)],
        )
        self.assertIn('饰品', result)
        self.assertNotIn('饰品1', result)
        self.assertNotIn('饰品2', result)
        self.assertEqual(
            [(item['itemID'], item['count']) for item in result['饰品']],
            [(201, 2), (202, 1)],
        )
        self.assertNotIn('衬衫', result)
        self.assertNotIn('战袍', result)

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_talent_build_popularity_uses_most_common_code_as_template(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        records = [
            {
                'talent_build_code': 'CODE_A',
                'character_name': 'TemplateOne',
                'realm': 'RealmA',
                'region': 'cn',
                'dps': 1000,
                'talents_json': [
                    {'node_id': 1, 'spell_id': 101, 'name': '通用天赋', 'tree_type': 'class', 'points': 1},
                    {'node_id': 2, 'spell_id': 102, 'name': '模板天赋', 'tree_type': 'spec', 'points': 1},
                ],
            },
            {
                'talent_build_code': 'CODE_A',
                'character_name': 'TemplateTwo',
                'realm': 'RealmA',
                'region': 'cn',
                'dps': 2000,
                'talents_json': [
                    {'node_id': 1, 'spell_id': 101, 'name': '通用天赋', 'tree_type': 'class', 'points': 1},
                    {'node_id': 2, 'spell_id': 102, 'name': '模板天赋', 'tree_type': 'spec', 'points': 1},
                ],
            },
            {
                'talent_build_code': 'CODE_B',
                'character_name': 'VariantOne',
                'realm': 'RealmB',
                'region': 'cn',
                'dps': 3000,
                'talents_json': [
                    {'node_id': 1, 'spell_id': 101, 'name': '通用天赋', 'tree_type': 'class', 'points': 1},
                    {'node_id': 3, 'spell_id': 103, 'name': '变体天赋', 'tree_type': 'spec', 'points': 1},
                ],
            },
        ]

        result = _compute_talent_build_popularity(records, 'Monk', 'Windwalker', top_n=10)

        self.assertEqual(result['total'], 3)
        self.assertEqual(result['template_code'], 'CODE_A')
        self.assertEqual([item['code'] for item in result['builds']], ['CODE_A', 'CODE_B'])
        self.assertTrue(result['builds'][0]['is_template'])
        self.assertEqual(result['builds'][0]['diff_count'], 0)
        self.assertEqual(
            [player['name'] for player in result['builds'][0]['top_players']],
            ['TemplateTwo', 'TemplateOne'],
        )
        self.assertEqual(result['builds'][1]['count'], 1)
        self.assertEqual(result['builds'][1]['pct'], 33.3)
        self.assertEqual(
            [player['name'] for player in result['builds'][1]['top_players']],
            ['VariantOne'],
        )
        self.assertEqual([node['name'] for node in result['builds'][1]['added_talents']], ['变体天赋'])
        self.assertEqual([node['name'] for node in result['builds'][1]['missing_talents']], ['模板天赋'])
        self.assertEqual(result['builds'][1]['added_talents'][0]['top_players'][0]['name'], 'VariantOne')
        self.assertEqual(
            [player['name'] for player in result['builds'][1]['missing_talents'][0]['top_players']],
            ['TemplateTwo', 'TemplateOne'],
        )

    @patch('botend.services.spec_stats_service.WowTalentNodeMetadata.objects.filter')
    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_talent_build_popularity_summarizes_hero_choice_without_diff(self, mock_provider_cls, mock_anchor_filter):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_anchor_filter.return_value.exclude.return_value.values.return_value = [
            {'db2_subtree_id': 11, 'name': 'Deathbringer', 'name_zh': ''},
            {'db2_subtree_id': 12, 'name': "San'layn", 'name_zh': ''},
        ]
        records = [
            {
                'talent_build_code': 'CODE_A',
                'talents_json': [
                    {'node_id': 1, 'spell_id': 101, 'name': '通用天赋', 'tree_type': 'class', 'points': 1},
                    {'node_id': 2, 'spell_id': 102, 'name': '专精天赋', 'tree_type': 'spec', 'points': 1},
                    {'node_id': 11, 'spell_id': 201, 'name': '英雄A', 'tree_type': 'hero', 'db2_subtree_id': 11, 'points': 1},
                ],
            },
            {
                'talent_build_code': 'CODE_B',
                'talents_json': [
                    {'node_id': 1, 'spell_id': 101, 'name': '通用天赋', 'tree_type': 'class', 'points': 1},
                    {'node_id': 2, 'spell_id': 102, 'name': '专精天赋', 'tree_type': 'spec', 'points': 1},
                    {'node_id': 12, 'spell_id': 202, 'name': '英雄B', 'tree_type': 'hero', 'db2_subtree_id': 12, 'points': 1},
                ],
            },
        ]

        result = _compute_talent_build_popularity(records, 'DeathKnight', 'Blood', top_n=10)

        self.assertEqual([item['diff_count'] for item in result['builds']], [0, 0])
        self.assertEqual(result['builds'][0]['hero_talent_summary'][0]['name'], '死亡使者')
        self.assertEqual(result['builds'][1]['hero_talent_summary'][0]['name'], '萨莱因')

    @patch('botend.wow.talents.metadata.TalentMetadataProvider.get_decoder_node_list')
    @patch('botend.wow.talents.metadata.TalentMetadataProvider.get_full_tree_nodes')
    @patch('botend.wow.talents.adapters.TalentMetadataProvider')
    @patch('botend.services.spec_stats_service.PlayerSpecTopPlayer.objects.filter')
    def test_get_player_detail_returns_render_model_and_keeps_legacy_fields(self, mock_filter, mock_provider_cls, mock_full_tree_nodes, mock_decoder_nodes):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_decoder_nodes.return_value = []
        mock_full_tree_nodes.return_value = [
            {
                'spell_id': 101,
                'talent_id': 101,
                'name': '职业节点',
                'tree_type': 'class',
                'row': 1,
                'column': 1,
                'points': 0,
                'selected': False,
            },
            {
                'spell_id': 201,
                'talent_id': 201,
                'name': '专精前置节点',
                'tree_type': 'spec',
                'row': 1,
                'column': 4,
                'points': 0,
                'selected': False,
            },
            {
                'spell_id': 202,
                'talent_id': 202,
                'name': '专精节点',
                'tree_type': 'spec',
                'row': 2,
                'column': 4,
                'points': 0,
                'selected': False,
                'parents': [201],
            },
        ]
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
            talent_build_code='BwQAAAAAAAAAAAAAAAAAAAAA',
            talents_json=[
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
        self.assertEqual(detail['talent_build_code'], 'BwQAAAAAAAAAAAAAAAAAAAAA')
        self.assertTrue(detail['has_talent_build_code'])
        self.assertEqual(detail['talent_parse_status'], 'success')
        self.assertEqual(detail['talent_render_model']['layout']['panels'][1]['tree_type'], 'spec')
        self.assertEqual(detail['talent_render_model']['trees'][1]['nodes'][0]['node_key'], 'spec:201')
        self.assertTrue(any(not node['selected'] for tree in detail['talent_render_model']['trees'] for node in tree['nodes']))

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
            'talent_build_code': render_model['build_code'],
            'has_talent_build_code': True,
            'talent_parse_status': 'success',
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
        self.assertIn('M 160 156 L 160 276', html)
        self.assertIn('https://www.wowhead.com/spell=2020', html)
        self.assertIn('BwQAAAAAAAAAAAAAAAAAAAAA', html)
        self.assertIn('talent-copy-btn', html)
        self.assertIn('已复制', html)

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    @patch('botend.services.spec_stats_service.PlayerSpecTopPlayer.objects.filter')
    @patch('botend.services.spec_stats_service.SpecDungeonRanking.objects.filter')
    def test_get_dungeon_detail_returns_popularity_talent_tree_render_model(self, mock_filter, mock_player_filter, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_player_filter.return_value.values.return_value = []
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
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
                'parents': [10],
            },
        ]
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
    def test_popularity_tree_merges_same_position_nodes_into_choice_options(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
            {
                'node_id': 90,
                'talent_id': 90,
                'spell_id': 900,
                'name': '共同父节点',
                'icon': 'parent_icon',
                'tree_type': 'spec',
                'row': 3,
                'column': 5,
                'db2_subtree_id': 0,
            },
            {
                'node_id': 100,
                'talent_id': 100,
                'spell_id': 1000,
                'name': '二选一A',
                'icon': 'a_icon',
                'tree_type': 'spec',
                'row': 4,
                'column': 5,
                'db2_subtree_id': 0,
                'parents': [90],
            },
            {
                'node_id': 101,
                'talent_id': 101,
                'spell_id': 1001,
                'name': '二选一B',
                'icon': 'b_icon',
                'tree_type': 'spec',
                'row': 4,
                'column': 5,
                'db2_subtree_id': 0,
                'parents': [90],
            },
            {
                'node_id': 200,
                'talent_id': 200,
                'spell_id': 2000,
                'name': '普通节点',
                'icon': 'normal_icon',
                'tree_type': 'spec',
                'row': 5,
                'column': 5,
                'db2_subtree_id': 0,
            },
        ]

        talent_tree = _compute_talent_popularity_tree(
            records=[{
                'talents_json': [
                    {'node_id': 101, 'talent_id': 101, 'spell_id': 1001, 'name': '二选一B', 'tree_type': 'spec', 'row': 4, 'column': 5, 'points': 1},
                    {'node_id': 200, 'talent_id': 200, 'spell_id': 2000, 'name': '普通节点', 'tree_type': 'spec', 'row': 5, 'column': 5, 'points': 1},
                ],
                'gear_json': [],
                'faction': 'Alliance',
            }],
            class_name='Monk',
            spec_name='Windwalker',
            top_n=10,
        )

        nodes = talent_tree['render_model']['nodes']
        positions = [(n.get('tree_type'), n.get('db2_subtree_id'), n.get('row'), n.get('column')) for n in nodes]
        self.assertEqual(len(positions), len(set(positions)))
        choice_node = next(n for n in nodes if n.get('row') == 4 and n.get('column') == 5)
        self.assertTrue(choice_node['is_choice_node'])
        self.assertEqual(len(choice_node['choice_options']), 2)
        self.assertTrue(all('description' in option for option in choice_node['choice_options']))
        self.assertTrue(all('description_zh' in option for option in choice_node['choice_options']))
        self.assertEqual(choice_node['name'], '二选一B')
        self.assertEqual(choice_node['usage_pct'], 100.0)

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_popularity_tree_does_not_mark_duplicate_source_rows_as_choice(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
            {
                'node_id': 90,
                'talent_id': 90,
                'spell_id': 900,
                'name': '父节点',
                'icon': 'parent_icon',
                'tree_type': 'spec',
                'row': 3600,
                'column': 11400,
                'db2_subtree_id': 0,
                'source': 'db2_backfill',
            },
            {
                'node_id': 96170,
                'talent_id': 96170,
                'spell_id': 96170,
                'display_spell_id': 108199,
                'name': '血魔之握-旧源',
                'icon': 'old_icon',
                'tree_type': 'spec',
                'row': 4200,
                'column': 11400,
                'db2_subtree_id': 0,
                'parents': [90],
                'source': 'dungeon_ranking',
            },
            {
                'node_id': 96170,
                'talent_id': 76042,
                'spell_id': 108199,
                'display_spell_id': 108199,
                'name': '血魔之握',
                'icon': 'db2_icon',
                'tree_type': 'spec',
                'row': 4200,
                'column': 11400,
                'db2_subtree_id': 0,
                'parents': [90],
                'source': 'db2_backfill',
            },
            {
                'node_id': 136213,
                'talent_id': 76042,
                'spell_id': 1263569,
                'display_spell_id': 1263569,
                'name': '憎恶附肢',
                'icon': 'choice_icon',
                'tree_type': 'spec',
                'row': 4200,
                'column': 11400,
                'db2_subtree_id': 0,
                'parents': [90],
                'source': 'db2_backfill',
            },
            {
                'node_id': 200,
                'talent_id': 200,
                'spell_id': 2000,
                'name': '普通节点',
                'icon': 'normal_icon',
                'tree_type': 'spec',
                'row': 4800,
                'column': 11400,
                'db2_subtree_id': 0,
            },
        ]

        talent_tree = _compute_talent_popularity_tree(
            records=[{
                'talents_json': [
                    {'node_id': 96170, 'spell_id': 108199, 'tree_type': 'spec', 'points': 1},
                    {'node_id': 200, 'spell_id': 2000, 'tree_type': 'spec', 'points': 1},
                ],
                'gear_json': [],
                'faction': 'Alliance',
            }],
            class_name='DeathKnight',
            spec_name='Blood',
            top_n=10,
        )

        nodes = talent_tree['render_model']['nodes']
        blood_grip = next(n for n in nodes if n.get('row') == 4200 and n.get('column') == 11400)
        self.assertTrue(blood_grip['is_choice_node'])
        self.assertEqual(len(blood_grip['choice_options']), 2)
        self.assertEqual(
            sorted(option['spell_id'] for option in blood_grip['choice_options']),
            [108199, 1263569],
        )
        self.assertNotIn(96170, [option['spell_id'] for option in blood_grip['choice_options']])

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_popularity_tree_filters_other_spec_components_before_position_merge(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
            {
                'node_id': 10,
                'talent_id': 100,
                'spell_id': 1000,
                'name': 'Frost Strike',
                'icon': 'frost_icon',
                'tree_type': 'spec',
                'row': 1200,
                'column': 12600,
                'db2_subtree_id': 0,
                'parents': [],
            },
            {
                'node_id': 20,
                'talent_id': 200,
                'spell_id': 2000,
                'name': 'Heart Strike',
                'icon': 'blood_icon',
                'tree_type': 'spec',
                'row': 1200,
                'column': 12600,
                'db2_subtree_id': 0,
                'parents': [],
            },
            {
                'node_id': 21,
                'talent_id': 201,
                'spell_id': 2001,
                'name': 'Marrowrend',
                'icon': 'blood_child_icon',
                'tree_type': 'spec',
                'row': 1800,
                'column': 12000,
                'db2_subtree_id': 0,
                'parents': [20],
            },
            {
                'node_id': 30,
                'talent_id': 300,
                'spell_id': 3000,
                'name': 'Outbreak',
                'icon': 'unholy_icon',
                'tree_type': 'spec',
                'row': 1200,
                'column': 12600,
                'db2_subtree_id': 0,
                'parents': [],
            },
        ]

        talent_tree = _compute_talent_popularity_tree(
            records=[{
                'talents_json': [
                    {'node_id': 20, 'talent_id': 200, 'spell_id': 2000, 'name': 'Heart Strike', 'tree_type': 'spec', 'row': 1200, 'column': 12600, 'points': 1},
                    {'node_id': 21, 'talent_id': 201, 'spell_id': 2001, 'name': 'Marrowrend', 'tree_type': 'spec', 'row': 1800, 'column': 12000, 'points': 1},
                ],
                'gear_json': [],
                'faction': 'Alliance',
            }],
            class_name='DeathKnight',
            spec_name='Blood',
            top_n=10,
        )

        nodes = talent_tree['render_model']['nodes']
        first_node = next(n for n in nodes if n.get('row') == 1200 and n.get('column') == 12600)
        self.assertEqual(first_node['name'], 'Heart Strike')
        self.assertFalse(first_node['is_choice_node'])
        self.assertEqual(first_node['choice_options'], [])
        self.assertEqual(
            sorted(n.get('name') for n in nodes if n.get('tree_type') == 'spec'),
            ['Heart Strike', 'Marrowrend'],
        )

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_popularity_tree_preserves_apex_points_from_metadata_bottom_node(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
            {
                'node_id': 137002,
                'talent_id': 110412,
                'spell_id': 1269310,
                'name': '怒火狂战',
                'icon': 'inv12_apextalent_warrior_rampagingberserker2',
                'tree_type': 'spec',
                'row': 10000,
                'column': 12000,
                'max_points': 4,
                'is_apex_talent': True,
                'apex_entries': [
                    {'node_id': 137004, 'talent_id': 110412, 'spell_id': 1269308, 'max_points': 1},
                    {'node_id': 137003, 'talent_id': 110412, 'spell_id': 1269309, 'max_points': 2},
                    {'node_id': 137002, 'talent_id': 110412, 'spell_id': 1269310, 'max_points': 1},
                ],
            },
        ]

        talent_tree = _compute_talent_popularity_tree(
            records=[{
                'talents_json': [
                    {
                        'node_id': 137002,
                        'talent_id': 110412,
                        'spell_id': 1269310,
                        'name': '怒火狂战',
                        'tree_type': 'spec',
                        'row': 10000,
                        'column': 12000,
                        'points': 1,
                        'max_points': 1,
                    },
                ],
                'gear_json': [],
                'faction': 'Alliance',
            }],
            class_name='Warrior',
            spec_name='Fury',
            top_n=10,
        )

        spec_tree = next(tree for tree in talent_tree['render_model']['trees'] if tree['tree_type'] == 'spec')
        apex_node = next(node for node in spec_tree['nodes'] if node.get('talent_id') == 110412)
        self.assertTrue(apex_node['is_apex_talent'])
        self.assertEqual(apex_node['points'], 4)
        self.assertEqual(apex_node['max_points'], 4)
        self.assertEqual(spec_tree['point_pools']['apex']['points'], 4)
        self.assertEqual(spec_tree['point_pools']['apex']['max_points'], 4)

    @patch('botend.services.spec_stats_service.WowTalentNodeMetadata')
    @patch('botend.services.spec_stats_service.connection')
    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_popularity_tree_renders_two_hero_subtrees_stacked(self, mock_provider_cls, mock_connection, mock_metadata):
        mock_metadata.objects.filter.return_value.exclude.return_value.values_list.return_value = []
        mock_cursor = mock_connection.cursor.return_value.__enter__.return_value
        mock_cursor.fetchall.side_effect = [
            [(None, 'Deathbringer')],
            [(None, 'Rider of the Apocalypse')],
        ]
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
            {'spell_id': 1001, 'talent_id': 1001, 'name': '职业', 'tree_type': 'class', 'row': 1000, 'column': 1000},
            {'spell_id': 2001, 'talent_id': 2001, 'name': '英雄一', 'tree_type': 'hero', 'db2_subtree_id': 11, 'row': 1000, 'column': 5000},
            {'spell_id': 2002, 'talent_id': 2002, 'name': '英雄二', 'tree_type': 'hero', 'db2_subtree_id': 22, 'row': 1000, 'column': 9000},
            {'spell_id': 3001, 'talent_id': 3001, 'name': '专精', 'tree_type': 'spec', 'row': 1000, 'column': 13000},
        ]

        talent_tree = _compute_talent_popularity_tree(
            records=[{
                'talents_json': [
                    {'spell_id': 1001, 'talent_id': 1001, 'name': '职业', 'tree_type': 'class', 'row': 1000, 'column': 1000},
                    {'spell_id': 2001, 'talent_id': 2001, 'name': '英雄一', 'tree_type': 'hero', 'db2_subtree_id': 11, 'row': 1000, 'column': 5000},
                    {'spell_id': 2002, 'talent_id': 2002, 'name': '英雄二', 'tree_type': 'hero', 'db2_subtree_id': 22, 'row': 1000, 'column': 9000},
                    {'spell_id': 3001, 'talent_id': 3001, 'name': '专精', 'tree_type': 'spec', 'row': 1000, 'column': 13000},
                ],
            }],
            class_name='DeathKnight',
            spec_name='Blood',
            top_n=10,
        )

        trees = talent_tree['render_model']['trees']
        self.assertEqual([tree['tree_type'] for tree in trees], ['class', 'hero', 'hero', 'spec'])
        self.assertEqual([tree['title'] for tree in trees], ['职业天赋', '死亡使者', '天启骑士', '专精天赋'])
        hero_panels = [tree['panel'] for tree in trees if tree['tree_type'] == 'hero']
        self.assertEqual(hero_panels[0]['x'], hero_panels[1]['x'])
        self.assertLess(hero_panels[0]['y'], hero_panels[1]['y'])

    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_dungeon_stats_template_renders_popularity_talent_tree(self, mock_provider_cls):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
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
                'parents': [10],
            },
        ]
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

    @patch('botend.services.spec_stats_service.WowTalentNodeMetadata')
    @patch('botend.services.spec_stats_service.TalentMetadataProvider')
    def test_raid_stats_template_renders_popularity_talent_tree(self, mock_provider_cls, mock_metadata_model):
        mock_provider_cls.return_value.merge_into_node.side_effect = (
            lambda node, class_name='', spec_name='': node
        )
        mock_provider_cls.return_value.get_full_tree_nodes.return_value = [
            {
                'spell_id': 3030,
                'talent_id': 30,
                'name': '团本前置',
                'tree_type': 'hero',
                'db2_subtree_id': 1,
                'row': 1,
                'column': 2,
            },
            {
                'spell_id': 4040,
                'talent_id': 40,
                'name': '团本热门节点',
                'tree_type': 'hero',
                'db2_subtree_id': 1,
                'row': 2,
                'column': 2,
                'parents': [30],
            },
        ]
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

class SpecStatsTalentBuildDiffTooltipTemplateTests(SimpleTestCase):
    def test_stats_templates_render_talent_build_diff_top_players(self):
        talent_build_popularity = {
            'total': 3,
            'template_code': 'CODE_A',
            'template_count': 2,
            'builds': [
                {
                    'rank': 1,
                    'code': 'CODE_B',
                    'count': 1,
                    'pct': 33.3,
                    'is_template': False,
                    'top_players': [
                        {'name': 'VariantOne', 'realm': 'RealmB', 'dps_fmt': '3,000'},
                        {'name': 'VariantTwo', 'realm': 'RealmC', 'dps_fmt': '2,500'},
                    ],
                    'hero_talent_summary': [],
                    'diff_count': 2,
                    'added_talents': [
                        {
                            'name': '变体天赋',
                            'icon': '',
                            'top_players': [{'name': 'VariantOne', 'realm': 'RealmB', 'dps_fmt': '3,000'}],
                        },
                    ],
                    'missing_talents': [
                        {
                            'name': '模板天赋',
                            'icon': '',
                            'top_players': [{'name': 'TemplateTwo', 'realm': 'RealmA', 'dps_fmt': '2,000'}],
                        },
                    ],
                },
            ],
        }
        nav = {
            'class_name': 'Monk',
            'spec_name': 'Windwalker',
            'class_cn': '武僧',
            'spec_cn': '踏风',
            'spec_icon': 'ability_monk_flyingdragonkick',
            'role': 'dps',
        }

        for template_name, detail_key, detail_name_key in [
            ('portal/spec_detail/dungeon_stats.html', 'dungeon_detail', 'dungeon_name'),
            ('portal/spec_detail/raid_stats.html', 'boss_detail', 'boss_name'),
        ]:
            html = render_to_string(
                template_name,
                {
                    'class_name': 'Monk',
                    'spec_name': 'Windwalker',
                    'nav': nav,
                    detail_key: {
                        detail_name_key: '测试目标',
                        'sample_size': 3,
                        'talent_build_popularity': talent_build_popularity,
                    },
                },
            )

            self.assertIn('talent-build-player-tooltip', html)
            self.assertIn('使用该字符串 Top5', html)
            self.assertNotIn('选取该天赋 Top5', html)
            self.assertNotIn('模板选取 Top5', html)
            self.assertIn('VariantOne-RealmB', html)
            self.assertIn('VariantTwo-RealmC', html)
            self.assertNotIn('TemplateTwo-RealmA', html)
            self.assertIn('3,000 DPS', html)
