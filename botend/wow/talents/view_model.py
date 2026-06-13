# -*- coding: utf-8 -*-
"""
WoW 天赋视图模型

负责将“已解析的节点 + 元数据”转换成前端更容易直接渲染的结构。
"""

from __future__ import annotations

from collections import defaultdict

from botend.wow.talents.metadata import TalentMetadataProvider
from botend.wow.talents.parser import normalize_talent_payload


TREE_LABELS = {
    'class': '职业天赋',
    'spec': '专精天赋',
    'hero': '英雄天赋',
    'build_code': '导入代码',
}

TREE_COLUMNS = {
    'class': 8,
    'spec': 8,
    'hero': 4,
    'build_code': 1,
}


def build_talent_view_model(talents, class_name='', spec_name=''):
    payload = normalize_talent_payload(talents, class_name=class_name, spec_name=spec_name)
    provider = TalentMetadataProvider()

    enriched_nodes = [
        provider.merge_into_node(node, class_name=class_name, spec_name=spec_name)
        for node in payload['nodes']
    ]

    groups = defaultdict(list)
    for node in enriched_nodes:
        groups[node.get('tree_type') or 'spec'].append(node)

    trees = []
    for tree_type in ['class', 'spec', 'hero', 'build_code']:
        if not groups.get(tree_type):
            continue
        nodes = sorted(groups[tree_type], key=lambda item: (
            item.get('row') if item.get('row') is not None else 999,
            item.get('column') if item.get('column') is not None else 999,
            item.get('node_id') or item.get('talent_id') or item.get('spell_id') or 0,
            item.get('name') or '',
        ))
        layout = _apply_tree_layout(nodes, tree_type)
        trees.append({
            'tree_type': tree_type,
            'title': TREE_LABELS.get(tree_type, tree_type or '天赋'),
            'nodes': layout['nodes'],
            'grid_columns': layout['grid_columns'],
            'grid_rows': layout['grid_rows'],
            'synthetic_layout': layout['synthetic_layout'],
        })

    for tree_type, nodes in groups.items():
        if tree_type in {'class', 'spec', 'hero', 'build_code'}:
            continue
        trees.append({
            'tree_type': tree_type,
            'title': TREE_LABELS.get(tree_type, tree_type or '天赋'),
            'nodes': _apply_tree_layout(sorted(nodes, key=lambda item: item.get('name') or ''), tree_type)['nodes'],
            'grid_columns': TREE_COLUMNS.get(tree_type, 8),
            'grid_rows': max(1, (len(nodes) + TREE_COLUMNS.get(tree_type, 8) - 1) // TREE_COLUMNS.get(tree_type, 8)),
            'synthetic_layout': True,
        })

    build_code = next((node.get('talent_code') for node in enriched_nodes if node.get('talent_code')), payload.get('build_code', ''))
    return {
        'build_code': build_code,
        'nodes': enriched_nodes,
        'trees': trees,
    }


def _apply_tree_layout(nodes, tree_type):
    default_grid_columns = TREE_COLUMNS.get(tree_type, 8)
    prepared = [dict(node) for node in nodes]

    has_real_layout = any(node.get('row') is not None or node.get('column') is not None for node in prepared)
    if has_real_layout:
        row_values = _sorted_unique_ints(node.get('row') for node in prepared)
        column_values = _sorted_unique_ints(node.get('column') for node in prepared)
        row_map = {value: idx + 1 for idx, value in enumerate(row_values)}
        column_map = {value: idx + 1 for idx, value in enumerate(column_values)}
        max_row = 1
        max_col = 1
        for idx, node in enumerate(prepared):
            raw_row = _to_int(node.get('row'))
            raw_col = _to_int(node.get('column'))
            layout_row = row_map.get(raw_row) if raw_row is not None else None
            layout_col = column_map.get(raw_col) if raw_col is not None else None
            if layout_row is None:
                layout_row = (idx // max(1, len(column_values) or default_grid_columns)) + 1
            if layout_col is None:
                layout_col = (idx % max(1, len(column_values) or default_grid_columns)) + 1
            node['layout_row'] = max(1, layout_row)
            node['layout_column'] = max(1, layout_col)
            max_row = max(max_row, node['layout_row'])
            max_col = max(max_col, node['layout_column'])
        return {
            'nodes': prepared,
            'grid_columns': max(1, max_col),
            'grid_rows': max_row,
            'synthetic_layout': False,
        }

    for idx, node in enumerate(prepared):
        node['layout_row'] = (idx // default_grid_columns) + 1
        node['layout_column'] = (idx % default_grid_columns) + 1

    return {
        'nodes': prepared,
        'grid_columns': default_grid_columns,
        'grid_rows': max(1, (len(prepared) + default_grid_columns - 1) // default_grid_columns),
        'synthetic_layout': True,
    }


def _to_int(value):
    try:
        return int(str(value).strip())
    except Exception:
        return None


def _sorted_unique_ints(values):
    normalized = []
    for value in values:
        parsed = _to_int(value)
        if parsed is None:
            continue
        normalized.append(parsed)
    return sorted(set(normalized))
