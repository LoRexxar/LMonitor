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
            item.get('name') or '',
        ))
        trees.append({
            'tree_type': tree_type,
            'title': TREE_LABELS.get(tree_type, tree_type or '天赋'),
            'nodes': nodes,
        })

    for tree_type, nodes in groups.items():
        if tree_type in {'class', 'spec', 'hero', 'build_code'}:
            continue
        trees.append({
            'tree_type': tree_type,
            'title': TREE_LABELS.get(tree_type, tree_type or '天赋'),
            'nodes': sorted(nodes, key=lambda item: item.get('name') or ''),
        })

    build_code = next((node.get('talent_code') for node in enriched_nodes if node.get('talent_code')), payload.get('build_code', ''))
    return {
        'build_code': build_code,
        'nodes': enriched_nodes,
        'trees': trees,
    }
