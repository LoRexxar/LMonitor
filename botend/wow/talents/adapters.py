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


TREE_ORDER = ('class', 'hero', 'spec')


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

    # 移除 hero_anchor 类型——这些是锚点节点，不应作为独立面板显示
    grouped_nodes.pop('hero_anchor', None)

    # DB 已有正确的 class/hero/spec 分类，不再需要 hero 左右过滤

    # Hero 子树过滤：按 db2_subtree_id 分组，单个玩家只保留当前选择的一棵英雄天赋树。
    # 某些来源的 talents_json / build code 解码可能会让两个 hero subtree 都带少量 points，
    # 但游戏内同一角色只能选择一个英雄天赋树；这里用总 points 最高的子树作为当前选择。
    if 'hero' in grouped_nodes:
        hero_nodes = grouped_nodes['hero']
        hero_subtrees = _group_hero_subtrees(hero_nodes)
        if len(hero_subtrees) > 1:
            selected_subtrees = {
                key: nodes for key, nodes in hero_subtrees.items()
                if any(n.points > 0 for n in nodes)
            }
            if selected_subtrees:
                keep_key = max(
                    selected_subtrees,
                    key=lambda k: (
                        sum((n.points or 0) for n in selected_subtrees[k]),
                        sum(1 for n in selected_subtrees[k] if (n.points or 0) > 0),
                        len(selected_subtrees[k]),
                    ),
                )
                grouped_nodes['hero'] = selected_subtrees[keep_key]
            else:
                # 无选中节点时保留节点数最多的子树作为默认展示，避免两个 hero tree 同时出现。
                largest_key = max(hero_subtrees, key=lambda k: len(hero_subtrees[k]))
                grouped_nodes['hero'] = hero_subtrees[largest_key]

    # 直接使用 DB2 原始坐标作为 layout 值（不做密集压缩）
    for tree_type in _iter_tree_types(grouped_nodes):
        for node in grouped_nodes[tree_type]:
            if node.column is not None:
                node.layout_column = node.column
            if node.row is not None:
                node.layout_row = node.row

    trees = []
    for tree_type in _iter_tree_types(grouped_nodes):
        nodes = sorted(
            grouped_nodes[tree_type],
            key=lambda item: (
                item.layout_row if item.layout_row is not None else 99999,
                item.layout_column if item.layout_column is not None else 99999,
                item.node_id or item.talent_id or item.spell_id or 0,
                item.name or '',
            ),
        )
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


def _group_hero_subtrees(hero_nodes):
    """将 hero 节点按 db2_subtree_id 分组为不同子树。

    优先使用 db2_subtree_id，如果都是 0 则回退到 column 分组。
    """
    if not hero_nodes:
        return {}

    # 优先按 db2_subtree_id 分组
    by_subtree_id = {}
    for node in hero_nodes:
        sid = getattr(node, 'db2_subtree_id', 0) or 0
        if sid > 0:
            by_subtree_id.setdefault(sid, []).append(node)

    if by_subtree_id:
        return by_subtree_id

    # 回退到 column 分组
    columns = sorted({(n.column or 0) for n in hero_nodes if (n.column or 0) > 0})
    if not columns:
        return {0: hero_nodes}

    groups = []
    current_group = [columns[0]]
    for i in range(1, len(columns)):
        if columns[i] - columns[i - 1] > 3000:
            groups.append(current_group)
            current_group = [columns[i]]
        else:
            current_group.append(columns[i])
    groups.append(current_group)

    if len(groups) <= 1:
        return {0: hero_nodes}

    column_to_group = {}
    for group_idx, group_cols in enumerate(groups):
        for col in group_cols:
            column_to_group[col] = group_idx

    result = {}
    for node in hero_nodes:
        col = node.column or 0
        group_key = column_to_group.get(col, 0)
        result.setdefault(group_key, []).append(node)

    return result


def _apply_global_dense_layout(nodes):
    """全局坐标归一化：所有 tree_type 共享同一坐标空间。

    将原始 DB2 的 column/row 值（如 1200, 2400, 7200, 10800, 14400）
    映射为紧凑的 layout_column/layout_row（1, 2, 3, ...），
    保证 class/hero/spec 三棵树的相对位置关系。
    """
    raw_rows = sorted({node.row for node in nodes if node.row is not None})
    raw_columns = sorted({node.column for node in nodes if node.column is not None})
    row_index = {value: index + 1 for index, value in enumerate(raw_rows)}
    column_index = {value: index + 1 for index, value in enumerate(raw_columns)}

    for node in nodes:
        if node.row is not None:
            node.layout_row = row_index.get(node.row, 1)
        if node.column is not None:
            node.layout_column = column_index.get(node.column, 1)
