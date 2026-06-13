# -*- coding: utf-8 -*-
"""
WoW 天赋元数据提供层

第一版先使用本地数据库缓存：
1. wow_talent_node_metadata
2. wow_spell_snapshot

未来可以在这里继续扩展到外部抓取源。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata


@dataclass
class TalentMetadataProvider:
    locale: str = 'zhCN'
    _spec_cache: dict = field(default_factory=dict)
    _snapshot_cache: dict = field(default_factory=dict)

    def get_node_metadata(self, class_name, spec_name, tree_type, spell_id=None, talent_id=None, node_id=None):
        """读取单个天赋节点元数据。"""
        indexes = self._get_spec_indexes(class_name, spec_name)
        tree_key = tree_type or 'spec'
        tree_indexes = indexes.get(tree_key, {})

        if node_id:
            row = tree_indexes.get(('node_id', int(node_id)))
            if row:
                return row

        if spell_id:
            row = tree_indexes.get(('spell_id', int(spell_id)))
            if row:
                return row

        if talent_id:
            row = tree_indexes.get(('talent_id', int(talent_id)))
            if row:
                return row

        fallback = self._lookup_spell_snapshot(spell_id)
        if fallback:
            return fallback
        return {}

    def merge_into_node(self, node, class_name, spec_name):
        """将元数据补入标准化节点。"""
        metadata = self.get_node_metadata(
            class_name=class_name,
            spec_name=spec_name,
            tree_type=node.get('tree_type') or 'spec',
            spell_id=node.get('spell_id'),
            talent_id=node.get('talent_id'),
            node_id=node.get('node_id'),
        )
        if not metadata:
            return node

        merged = dict(node)
        for key in ['name', 'icon', 'row', 'column', 'max_points', 'parents']:
            value = metadata.get(key)
            if value in (None, '', []):
                continue
            if self._should_override(merged, key):
                merged[key] = value
        if metadata.get('display_spell_id'):
            merged['display_spell_id'] = metadata.get('display_spell_id')
            merged['spell_id'] = metadata.get('display_spell_id')
        return merged

    def _get_spec_indexes(self, class_name, spec_name):
        cache_key = (class_name or '', spec_name or '')
        if cache_key in self._spec_cache:
            return self._spec_cache[cache_key]

        indexes = {}
        rows = WowTalentNodeMetadata.objects.filter(
            class_name=cache_key[0],
            spec_name=cache_key[1],
        )
        for row in rows.iterator():
            row_data = self._as_dict(row)
            tree_key = row.tree_type or 'spec'
            tree_indexes = indexes.setdefault(tree_key, {})
            for field_name in ('node_id', 'spell_id', 'talent_id'):
                value = getattr(row, field_name)
                if not value:
                    continue
                try:
                    normalized = int(value)
                except Exception:
                    continue
                tree_indexes[(field_name, normalized)] = row_data

        self._spec_cache[cache_key] = indexes
        return indexes

    @staticmethod
    def _as_dict(row):
        return {
            'node_id': row.node_id,
            'spell_id': row.spell_id,
            'display_spell_id': row.display_spell_id,
            'talent_id': row.talent_id,
            'name': row.name_zh or row.name,
            'icon': row.icon,
            'tree_type': row.tree_type,
            'row': row.row,
            'column': row.column,
            'max_points': row.max_points,
            'parents': row.parents_json or [],
        }

    def _lookup_spell_snapshot(self, spell_id):
        if not spell_id:
            return {}
        spell_id = int(spell_id)
        if spell_id in self._snapshot_cache:
            return self._snapshot_cache[spell_id]

        snapshot = WowSpellSnapshot.objects.filter(spell_id=spell_id).order_by('-updated_at').first()
        if not snapshot:
            self._snapshot_cache[spell_id] = {}
            return {}
        self._snapshot_cache[spell_id] = {
            'spell_id': spell_id,
            'name': snapshot.name_zh or snapshot.name,
            'icon': '',
            'parents': [],
        }
        return self._snapshot_cache[spell_id]

    @staticmethod
    def _should_override(node, key):
        current = node.get(key)
        if current in (None, '', []):
            return True
        if key == 'name' and isinstance(current, str):
            return current == '未命名天赋' or current.startswith('技能ID ')
        return False
