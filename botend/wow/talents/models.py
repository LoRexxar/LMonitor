# -*- coding: utf-8 -*-
"""
WoW 天赋通用 dataclass 模型。

这一层只负责定义稳定、可复用的数据结构，供后续 parser / engine / view model
逐步统一到同一套类型之上使用。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


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


def _to_optional_int(value):
    if value in (None, ''):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class TalentNodeModel:
    tree_type: str = 'spec'
    talent_code: str = ''
    node_id: int | None = None
    talent_id: int | None = None
    spell_id: int | None = None
    display_spell_id: int | None = None
    name: str = '未命名天赋'
    icon: str = ''
    points: int = 0
    max_points: int | None = None
    row: int | None = None
    column: int | None = None
    selected: bool = False
    is_choice_node: bool = False
    choice_options: list[dict] = field(default_factory=list)
    source: str = 'unknown'
    parents: list[int] = field(default_factory=list)
    layout_row: int | None = None
    layout_column: int | None = None
    db2_subtree_id: int = 0
    description: str = ''
    description_zh: str = ''
    top_players: list[dict] = field(default_factory=list)

    @property
    def key(self):
        return self.node_id or self.talent_id or self.spell_id

    @classmethod
    def from_raw(cls, raw: Any) -> 'TalentNodeModel':
        if isinstance(raw, cls):
            return raw

        if isinstance(raw, str):
            return cls(
                tree_type='build_code',
                talent_code=raw,
                name='天赋导入代码',
                selected=True,
                source='legacy_code',
            )

        if not isinstance(raw, dict):
            raise TypeError('TalentNodeModel.from_raw 仅支持 str / dict / TalentNodeModel')

        talent_id = _to_optional_int(raw.get('talent_id') or raw.get('talentID'))
        spell_id = _to_optional_int(raw.get('spell_id') or raw.get('spellID') or talent_id)
        node_id = _to_optional_int(raw.get('node_id') or raw.get('nodeID') or talent_id or spell_id)
        points = _to_optional_int(raw.get('points')) or 0
        tree_type = raw.get('tree_type') or raw.get('treeType') or 'spec'
        name = raw.get('name') or (f'技能ID {spell_id}' if spell_id else '未命名天赋')

        return cls(
            tree_type=tree_type,
            talent_code=raw.get('talent_code', ''),
            node_id=node_id,
            talent_id=talent_id,
            spell_id=spell_id,
            display_spell_id=_to_optional_int(raw.get('display_spell_id') or raw.get('displaySpellID')),
            name=name,
            icon=raw.get('icon', ''),
            points=points,
            max_points=_to_optional_int(raw.get('max_points') or raw.get('maxPoints')),
            row=_to_optional_int(raw.get('row') if raw.get('row') is not None else raw.get('tier')),
            column=_to_optional_int(raw.get('column')),
            selected=bool(raw.get('selected', points > 0)),
            is_choice_node=bool(raw.get('is_choice_node') or raw.get('isChoiceNode')),
            choice_options=[dict(option) for option in (raw.get('choice_options') or raw.get('choiceOptions') or []) if isinstance(option, dict)],
            source=raw.get('source', 'unknown'),
            parents=list(raw.get('parents') or raw.get('parents_json') or []),
            layout_row=_to_optional_int(raw.get('layout_row')),
            layout_column=_to_optional_int(raw.get('layout_column')),
            db2_subtree_id=int(raw.get('db2_subtree_id') or 0),
            description=raw.get('description') or '',
            description_zh=raw.get('description_zh') or '',
            top_players=[dict(player) for player in (raw.get('top_players') or raw.get('topPlayers') or []) if isinstance(player, dict)],
        )

    def to_dict(self):
        return asdict(self)


@dataclass
class TalentTreeModel:
    tree_type: str = 'spec'
    title: str = ''
    nodes: list[TalentNodeModel] = field(default_factory=list)
    grid_columns: int = 0
    grid_rows: int = 1
    synthetic_layout: bool = True

    def __post_init__(self):
        self.nodes = [TalentNodeModel.from_raw(node) for node in self.nodes]
        if not self.title:
            self.title = TREE_LABELS.get(self.tree_type, self.tree_type or '天赋')
        if not self.grid_columns:
            self.grid_columns = TREE_COLUMNS.get(self.tree_type, 8)
        if self.grid_rows < 1:
            self.grid_rows = 1

    def to_dict(self):
        return {
            'tree_type': self.tree_type,
            'title': self.title,
            'nodes': [node.to_dict() for node in self.nodes],
            'grid_columns': self.grid_columns,
            'grid_rows': self.grid_rows,
            'synthetic_layout': self.synthetic_layout,
        }


@dataclass
class TalentPayloadModel:
    class_name: str = ''
    spec_name: str = ''
    build_code: str = ''
    nodes: list[TalentNodeModel] = field(default_factory=list)

    def __post_init__(self):
        self.nodes = [TalentNodeModel.from_raw(node) for node in self.nodes]
        if not self.build_code:
            self.build_code = next((node.talent_code for node in self.nodes if node.talent_code), '')

    def to_dict(self):
        return {
            'class_name': self.class_name,
            'spec_name': self.spec_name,
            'build_code': self.build_code,
            'nodes': [node.to_dict() for node in self.nodes],
        }


@dataclass
class TalentTreeViewModel:
    build_code: str = ''
    nodes: list[TalentNodeModel] = field(default_factory=list)
    trees: list[TalentTreeModel] = field(default_factory=list)

    def __post_init__(self):
        self.nodes = [TalentNodeModel.from_raw(node) for node in self.nodes]
        self.trees = [
            tree if isinstance(tree, TalentTreeModel) else TalentTreeModel(**tree)
            for tree in self.trees
        ]
        if not self.build_code:
            self.build_code = next((node.talent_code for node in self.nodes if node.talent_code), '')

    def to_dict(self):
        return {
            'build_code': self.build_code,
            'nodes': [node.to_dict() for node in self.nodes],
            'trees': [tree.to_dict() for tree in self.trees],
        }


@dataclass
class TalentBuildStateModel:
    source_type: str = 'player'
    source_id: str = ''
    selected_nodes: set[str] = field(default_factory=set)
    node_ranks: dict[str, int] = field(default_factory=dict)
    choice_selection: dict[str, Any] = field(default_factory=dict)
    heat_values: dict[str, float] = field(default_factory=dict)
    highlight_groups: dict[str, Any] = field(default_factory=dict)
    build_code: str = ''
    confidence: float = 1.0

    def to_dict(self):
        return {
            'source_type': self.source_type,
            'source_id': self.source_id,
            'selected_nodes': sorted(self.selected_nodes),
            'node_ranks': dict(self.node_ranks),
            'choice_selection': dict(self.choice_selection),
            'heat_values': dict(self.heat_values),
            'highlight_groups': dict(self.highlight_groups),
            'build_code': self.build_code,
            'confidence': self.confidence,
        }


@dataclass
class TalentTreeSetModel:
    set_key: str = ''
    class_name: str = ''
    spec_name: str = ''
    trees: list[TalentTreeModel] = field(default_factory=list)
    layout_mode: str = 'three-column'
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.trees = [
            tree if isinstance(tree, TalentTreeModel) else TalentTreeModel(**tree)
            for tree in self.trees
        ]

    def to_dict(self):
        return {
            'set_key': self.set_key,
            'class_name': self.class_name,
            'spec_name': self.spec_name,
            'trees': [tree.to_dict() for tree in self.trees],
            'layout_mode': self.layout_mode,
            'meta': dict(self.meta),
        }
