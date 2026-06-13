# -*- coding: utf-8 -*-
"""
WoW 天赋元数据提供层

第一版先使用本地数据库缓存：
1. wow_talent_node_metadata
2. wow_spell_snapshot

未来可以在这里继续扩展到外部抓取源。
"""

from __future__ import annotations

from dataclasses import dataclass

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata


@dataclass
class TalentMetadataProvider:
    locale: str = 'zhCN'

    def get_node_metadata(self, class_name, spec_name, tree_type, spell_id=None, talent_id=None, node_id=None):
        """读取单个天赋节点元数据。"""
        query = WowTalentNodeMetadata.objects.filter(
            class_name=class_name,
            spec_name=spec_name,
            tree_type=tree_type,
        )

        if node_id:
            row = query.filter(node_id=node_id).first()
            if row:
                return self._as_dict(row)

        if spell_id:
            row = query.filter(spell_id=spell_id).first()
            if row:
                return self._as_dict(row)

        if talent_id:
            row = query.filter(talent_id=talent_id).first()
            if row:
                return self._as_dict(row)

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
            if merged.get(key) in (None, '', []):
                merged[key] = value
        return merged

    @staticmethod
    def _as_dict(row):
        return {
            'node_id': row.node_id,
            'spell_id': row.spell_id,
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

        snapshot = WowSpellSnapshot.objects.filter(spell_id=spell_id).order_by('-updated_at').first()
        if not snapshot:
            return {}
        return {
            'spell_id': spell_id,
            'name': snapshot.name_zh or snapshot.name,
            'icon': '',
            'parents': [],
        }
