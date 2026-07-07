# -*- coding: utf-8 -*-
"""
WoW 天赋解析模块

职责：
1. 将人物榜 / WCL / 其他来源的天赋输入统一成标准节点结构
2. 与元数据和展示层解耦，不在这里处理布局与样式
"""

from __future__ import annotations


def normalize_talent_payload(talents, class_name='', spec_name=''):
    """统一不同来源的天赋输入结构。"""
    nodes = []
    build_code = ''

    for raw in talents or []:
        if isinstance(raw, str):
            build_code = raw
            nodes.append({
                'tree_type': 'build_code',
                'talent_code': raw,
                'node_id': None,
                'talent_id': None,
                'spell_id': None,
                'name': '天赋导入代码',
                'icon': '',
                'points': 0,
                'row': None,
                'column': None,
                'selected': True,
                'source': 'legacy_code',
            })
            continue

        if not isinstance(raw, dict):
            continue

        talent_id = raw.get('talent_id') or raw.get('talentID')
        spell_id = raw.get('spell_id') or raw.get('spellID') or talent_id
        node_id = raw.get('node_id') or raw.get('nodeID') or talent_id or spell_id
        points = raw.get('points', 0) or 0
        tree_type = raw.get('tree_type') or raw.get('treeType') or 'spec'
        name = raw.get('name') or (f'技能ID {spell_id}' if spell_id else '未命名天赋')

        nodes.append({
            'tree_type': tree_type,
            'talent_code': raw.get('talent_code', ''),
            'node_id': node_id,
            'talent_id': talent_id,
            'spell_id': spell_id,
            'display_spell_id': raw.get('display_spell_id') or raw.get('displaySpellID'),
            'name': name,
            'icon': raw.get('icon', ''),
            'points': points,
            'max_points': raw.get('max_points') or raw.get('maxPoints'),
            'row': raw.get('row') if raw.get('row') is not None else raw.get('tier'),
            'column': raw.get('column'),
            'selected': bool(raw.get('selected', points > 0)),
            'is_choice_node': bool(raw.get('is_choice_node') or raw.get('isChoiceNode')),
            'choice_selection': raw.get('choice_selection') if raw.get('choice_selection') is not None else raw.get('choiceSelection'),
            'choice_options': [dict(option) for option in (raw.get('choice_options') or raw.get('choiceOptions') or []) if isinstance(option, dict)],
            'parents': list(raw.get('parents') or raw.get('parents_json') or []),
            'layout_row': raw.get('layout_row'),
            'layout_column': raw.get('layout_column'),
            'db2_subtree_id': raw.get('db2_subtree_id') or 0,
            'description': raw.get('description') or '',
            'description_zh': raw.get('description_zh') or '',
            'source': raw.get('source', 'unknown'),
            'flags': raw.get('flags', 0),
        })

    return {
        'class_name': class_name,
        'spec_name': spec_name,
        'build_code': build_code,
        'nodes': nodes,
    }
