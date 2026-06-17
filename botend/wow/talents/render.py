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
    panel_lookup = {panel.tree_type: panel for panel in layout.panels}
    render_nodes = []
    render_trees = []

    for tree in tree_set.trees:
        panel = panel_lookup.get(tree.tree_type)
        node_layout_lookup = {}
        path_payloads = []
        panel_payload = None
        if panel:
            node_layout_lookup = {node.node_key: node for node in panel.nodes}
            path_payloads = [path.to_dict() for path in panel.paths]
            panel_payload = panel.to_dict()

        tree_nodes = []
        for raw_node in tree.nodes:
            node = raw_node if isinstance(raw_node, TalentNodeModel) else TalentNodeModel.from_raw(raw_node)
            node_payload = node.to_dict()
            node_key = _build_node_key(node)
            node_payload['node_key'] = node_key
            layout_node = node_layout_lookup.get(node_key)
            if layout_node:
                node_payload.update(layout_node.to_dict())
            else:
                node_payload['selected'] = bool(node_key and node_key in selected_nodes) or node.selected or node.points > 0
            tree_nodes.append(node_payload)
            render_nodes.append(node_payload)

        # 英雄天赋树标题保持原样
        title = tree.title

        render_trees.append({
            'tree_type': tree.tree_type,
            'title': title,
            'nodes': tree_nodes,
            'grid_columns': tree.grid_columns,
            'grid_rows': tree.grid_rows,
            'synthetic_layout': tree.synthetic_layout,
            'paths': path_payloads,
            'panel': panel_payload,
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
