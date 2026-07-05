# -*- coding: utf-8 -*-
"""
WoW 天赋渲染模型。

负责把原始 talents 输入统一组装为：
1. TalentTreeSetModel
2. TalentBuildStateModel
3. TalentTreeLayoutModel

供 view_model 或后续直接渲染层复用。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from botend.wow.talents.adapters import build_tree_set_from_talents
from botend.wow.talents.layout import TalentTreeLayoutModel, build_talent_tree_layout
from botend.wow.talents.models import (
    TREE_COLUMNS,
    TREE_LABELS,
    TalentBuildStateModel,
    TalentNodeModel,
    TalentTreeSetModel,
)


@dataclass
class TalentRenderModel:
    set_key: str = ''
    class_name: str = ''
    spec_name: str = ''
    layout_mode: str = 'three-column'
    build_code: str = ''
    tree_set: TalentTreeSetModel = field(default_factory=TalentTreeSetModel)
    build_state: TalentBuildStateModel = field(default_factory=TalentBuildStateModel)
    layout: TalentTreeLayoutModel = field(default_factory=TalentTreeLayoutModel)
    nodes: list[dict] = field(default_factory=list)
    trees: list[dict] = field(default_factory=list)

    def to_dict(self):
        return {
            'set_key': self.set_key,
            'class_name': self.class_name,
            'spec_name': self.spec_name,
            'layout_mode': self.layout_mode,
            'build_code': self.build_code,
            'tree_set': self.tree_set.to_dict(),
            'build_state': self.build_state.to_dict(),
            'layout': self.layout.to_dict(),
            'nodes': [dict(node) for node in self.nodes],
            'trees': [
                {
                    **tree,
                    'nodes': [dict(node) for node in tree.get('nodes', [])],
                    'paths': [dict(path) for path in tree.get('paths', [])],
                    'panel': dict(tree['panel']) if tree.get('panel') else None,
                }
                for tree in self.trees
            ],
        }


def build_talent_render_model(
    talents=None,
    class_name='',
    spec_name='',
    source_type='player',
    source_id='',
    tree_set=None,
    build_state=None,
    config=None,
    metadata_provider=None,
):
    if tree_set is None or build_state is None:
        tree_set, build_state = build_tree_set_from_talents(
            talents,
            class_name=class_name,
            spec_name=spec_name,
            source_type=source_type,
            source_id=source_id,
            metadata_provider=metadata_provider,
        )
    layout = build_talent_tree_layout(tree_set, build_state, config=config)
    build_code = build_state.build_code or tree_set.meta.get('build_code', '')
    nodes, trees = _build_render_collections(tree_set, layout, build_state, build_code)

    return TalentRenderModel(
        set_key=tree_set.set_key,
        class_name=tree_set.class_name,
        spec_name=tree_set.spec_name,
        layout_mode=tree_set.layout_mode,
        build_code=build_code,
        tree_set=tree_set,
        build_state=build_state,
        layout=layout,
        nodes=nodes,
        trees=trees,
    )


def _build_render_collections(tree_set, layout, build_state, build_code):
    selected_nodes = set(build_state.selected_nodes or set())
    render_nodes = []
    render_trees = []

    for index, tree in enumerate(tree_set.trees):
        panel = layout.panels[index] if index < len(layout.panels) else None
        node_layout_lookup = {}
        path_payloads = []
        panel_payload = None
        if panel:
            node_layout_lookup = {node.node_key: node for node in panel.nodes}
            path_payloads = [path.to_dict() for path in panel.paths]
            panel_payload = panel.to_dict()

        tree_nodes = []
        apex_keys = _detect_apex_node_keys(tree)
        for raw_node in tree.nodes:
            node = raw_node if isinstance(raw_node, TalentNodeModel) else TalentNodeModel.from_raw(raw_node)
            node_payload = node.to_dict()
            node_key = _build_node_key(node)
            node_payload['node_key'] = node_key
            is_apex = bool(node_key and node_key in apex_keys)
            node_payload['is_apex_talent'] = is_apex
            node_payload['point_pool'] = 'apex' if is_apex else (node.tree_type or 'spec')
            layout_node = node_layout_lookup.get(node_key)
            if layout_node:
                node_payload.update(layout_node.to_dict())
            else:
                node_payload['selected'] = bool(node_key and node_key in selected_nodes) or node.selected or node.points > 0
            tree_nodes.append(node_payload)
            render_nodes.append(node_payload)

        # 英雄天赋树标题保持原样
        title = tree.title
        point_pools = _build_point_pools(tree.tree_type, tree_nodes)

        render_trees.append({
            'tree_type': tree.tree_type,
            'title': title,
            'nodes': tree_nodes,
            'grid_columns': tree.grid_columns,
            'grid_rows': tree.grid_rows,
            'synthetic_layout': tree.synthetic_layout,
            'paths': path_payloads,
            'panel': panel_payload,
            'point_pools': point_pools,
        })

    if build_code:
        build_code_node = TalentNodeModel.from_raw(build_code).to_dict()
        build_code_node.update({
            'node_key': 'build_code',
            'layout_row': 1,
            'layout_column': 1,
        })
        render_nodes.append(build_code_node)
        render_trees.append({
            'tree_type': 'build_code',
            'title': TREE_LABELS.get('build_code', '导入代码'),
            'nodes': [build_code_node],
            'grid_columns': TREE_COLUMNS.get('build_code', 1),
            'grid_rows': 1,
            'synthetic_layout': True,
            'paths': [],
            'panel': None,
        })

    return render_nodes, render_trees


def _build_node_key(node):
    node_identity = node.key
    if node_identity is None:
        return ''
    return f'{node.tree_type or "spec"}:{node_identity}'


def _detect_apex_node_keys(tree):
    """识别 12.1 PTR 专精树底部的顶峰/独立 4 点节点。

    DB2 目前没有稳定的中文/英文字段叫“顶峰天赋”。PTR 数据里的共同结构是：
    spec 树最后一行或最后一组同坐标 entry 的 max_points 合计为 4。
    因此前端渲染层只做展示/点数池标记，不改 DB tree_type，避免影响 build code 顺序。
    """
    if getattr(tree, 'tree_type', '') != 'spec':
        return set()
    nodes = [
        node if isinstance(node, TalentNodeModel) else TalentNodeModel.from_raw(node)
        for node in (getattr(tree, 'nodes', None) or [])
    ]
    positioned = [node for node in nodes if node.layout_row is not None or node.row is not None]
    if not positioned:
        return set()
    max_row = max((node.layout_row if node.layout_row is not None else node.row) or 0 for node in positioned)
    bottom_nodes = [
        node for node in positioned
        if ((node.layout_row if node.layout_row is not None else node.row) or 0) == max_row
    ]
    max_points = sum(_node_max_points(node) for node in bottom_nodes)
    if max_points != 4:
        return set()
    return {_build_node_key(node) for node in bottom_nodes if _build_node_key(node)}


def _node_max_points(node):
    try:
        return int(node.max_points or 1)
    except (TypeError, ValueError):
        return 1


def _node_points(node):
    try:
        return int((node or {}).get('points') or 0)
    except (TypeError, ValueError, AttributeError):
        return 0


def _build_point_pools(tree_type, nodes):
    pools = {}
    if tree_type != 'spec':
        points = sum(_node_points(node) for node in nodes)
        pools[tree_type or 'spec'] = {'points': points, 'max_points': None}
        return pools

    spec_points = sum(_node_points(node) for node in nodes if not node.get('is_apex_talent'))
    apex_nodes = [node for node in nodes if node.get('is_apex_talent')]
    pools['spec'] = {'points': spec_points, 'max_points': None}
    if apex_nodes:
        pools['apex'] = {
            'points': sum(_node_points(node) for node in apex_nodes),
            'max_points': sum(int(node.get('max_points') or 1) for node in apex_nodes),
        }
    return pools
