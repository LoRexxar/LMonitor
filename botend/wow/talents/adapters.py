# -*- coding: utf-8 -*-
"""
WoW 天赋 adapter。

负责把当前 `talents_json` 输入转换成更稳定的树集合与 build 状态模型，
并尽量复用现有 `TalentNodeModel` / `TalentTreeModel` 命名。
"""

from __future__ import annotations

from collections import defaultdict

from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.models import (
    TREE_COLUMNS,
    TalentBuildStateModel,
    TalentNodeModel,
    TalentTreeModel,
    TalentTreeSetModel,
)
from botend.wow.talents.parser import normalize_talent_payload


TREE_ORDER = ('class', 'spec', 'hero')


def build_tree_set_from_talents(
    talents_json,
    class_name='',
    spec_name='',
    source_type='player',
    source_id='',
    metadata_provider=None,
):
    payload = normalize_talent_payload(
        talents_json,
        class_name=class_name,
        spec_name=spec_name,
    )
    grouped_nodes = defaultdict(list)
    selected_nodes = set()
    node_ranks = {}
    provider = metadata_provider

    for raw_node in payload.get('nodes', []):
        node_data = dict(raw_node)
        if node_data.get('tree_type') == 'build_code':
            continue
        if _should_merge_metadata(node_data, class_name=class_name, spec_name=spec_name):
            if provider is None:
                provider = TalentMetadataProvider()
            node_data = provider.merge_into_node(
                node_data,
                class_name=class_name,
                spec_name=spec_name,
            )

        node = TalentNodeModel.from_raw(node_data)
        tree_type = node.tree_type or 'spec'
        grouped_nodes[tree_type].append(node)

        node_key = _build_node_key(node)
        if node_key and node.points > 0:
            selected_nodes.add(node_key)
            node_ranks[node_key] = node.points

    trees = []
    for tree_type in _iter_tree_types(grouped_nodes):
        nodes = sorted(
            grouped_nodes[tree_type],
            key=lambda item: (
                item.row if item.row is not None else 999,
                item.column if item.column is not None else 999,
                item.node_id or item.talent_id or item.spell_id or 0,
                item.name or '',
            ),
        )
        _apply_dense_layout_coordinates(nodes)
        default_columns = TREE_COLUMNS.get(tree_type, 8)
        layout_rows = [node.layout_row for node in nodes if node.layout_row is not None]
        layout_columns = [node.layout_column for node in nodes if node.layout_column is not None]
        raw_rows = [node.row for node in nodes if node.row is not None]
        raw_columns = [node.column for node in nodes if node.column is not None]
        synthetic_layout = not any(raw_rows or raw_columns)
        grid_columns = max([default_columns, *layout_columns]) if layout_columns else default_columns
        if layout_rows:
            grid_rows = max(layout_rows)
        else:
            grid_rows = max(1, (len(nodes) + default_columns - 1) // default_columns)

        trees.append(
            TalentTreeModel(
                tree_type=tree_type,
                nodes=nodes,
                grid_columns=grid_columns,
                grid_rows=grid_rows,
                synthetic_layout=synthetic_layout,
            )
        )

    normalized_source_id = source_id or _build_set_key(class_name, spec_name) or source_type
    build_code = payload.get('build_code', '')
    tree_set = TalentTreeSetModel(
        set_key=_build_set_key(class_name, spec_name),
        class_name=class_name,
        spec_name=spec_name,
        trees=trees,
        layout_mode='three-column',
        meta={'build_code': build_code},
    )
    build_state = TalentBuildStateModel(
        source_type=source_type,
        source_id=normalized_source_id,
        selected_nodes=selected_nodes,
        node_ranks=node_ranks,
        build_code=build_code,
    )
    return tree_set, build_state


def _build_node_key(node: TalentNodeModel):
    node_identity = node.key
    if node_identity is None:
        return ''
    return f'{node.tree_type or "spec"}:{node_identity}'


def _build_set_key(class_name, spec_name):
    return ':'.join(part for part in [class_name, spec_name] if part)


def _iter_tree_types(grouped_nodes):
    yielded = set()
    for tree_type in TREE_ORDER:
        if tree_type in grouped_nodes:
            yielded.add(tree_type)
            yield tree_type
    for tree_type in sorted(grouped_nodes.keys()):
        if tree_type not in yielded:
            yield tree_type


def _should_merge_metadata(node, class_name='', spec_name=''):
    if not isinstance(node, dict):
        return False
    if not class_name or not spec_name:
        return _needs_metadata_enrichment(node)
    if node.get('node_id') or node.get('nodeID') or node.get('talent_id') or node.get('talentID') or node.get('spell_id') or node.get('spellID'):
        return True
    return _needs_metadata_enrichment(node)


def _needs_metadata_enrichment(node):
    if not isinstance(node, dict):
        return False
    if node.get('tree_type') in (None, '', 'unknown'):
        return True
    if node.get('row') is None and node.get('column') is None:
        return True

    name = node.get('name')
    if not name:
        return True
    if isinstance(name, str) and (name == '未命名天赋' or name.startswith('技能ID ')):
        return True
    return False


def _apply_dense_layout_coordinates(nodes):
    raw_rows = sorted({node.row for node in nodes if node.row is not None})
    raw_columns = sorted({node.column for node in nodes if node.column is not None})
    row_index = {value: index + 1 for index, value in enumerate(raw_rows)}
    column_index = {value: index + 1 for index, value in enumerate(raw_columns)}

    for fallback_index, node in enumerate(nodes):
        if node.row is not None and node.layout_row is None:
            node.layout_row = row_index.get(node.row, fallback_index + 1)
        if node.column is not None and node.layout_column is None:
            node.layout_column = column_index.get(node.column, ((fallback_index % TREE_COLUMNS.get(node.tree_type, 8)) + 1))
