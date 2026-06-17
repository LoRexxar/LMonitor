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

STRUCTURAL_FIELDS = {'tree_type', 'row', 'column', 'max_points', 'parents'}


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
        all_tree_indexes = list(indexes.values())

        if node_id:
            row = tree_indexes.get(('node_id', int(node_id)))
            if not row:
                row = self._find_across_trees(all_tree_indexes, 'node_id', node_id)
            if row:
                return row

        if spell_id:
            row = tree_indexes.get(('spell_id', int(spell_id)))
            if not row:
                row = self._find_across_trees(all_tree_indexes, 'spell_id', spell_id)
            if row:
                return row

        if talent_id:
            row = tree_indexes.get(('talent_id', int(talent_id)))
            if not row:
                row = self._find_across_trees(all_tree_indexes, 'talent_id', talent_id)
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
        metadata_tree_type = metadata.get('tree_type')
        if metadata_tree_type:
            merged['tree_type'] = metadata_tree_type
        for key in ['name', 'icon', 'row', 'column', 'max_points', 'parents', 'description', 'description_zh', 'db2_subtree_id']:
            value = metadata.get(key)
            if value in (None, '', []):
                continue
            if key in STRUCTURAL_FIELDS or self._should_override(merged, key):
                merged[key] = value
        if metadata.get('display_spell_id'):
            merged['display_spell_id'] = metadata.get('display_spell_id')
            merged['spell_id'] = metadata.get('display_spell_id')
        return merged

    def get_full_tree_nodes(self, class_name, spec_name):
        cache_key = (class_name or '', spec_name or '', 'full_nodes')
        if cache_key in self._spec_cache:
            return [dict(node) for node in self._spec_cache[cache_key]]

        rows = WowTalentNodeMetadata.objects.filter(
            class_name=class_name or '',
            spec_name=spec_name or '',
        ).order_by('tree_type', 'row', 'column', 'node_id', 'spell_id', 'talent_id')

        grouped_by_node = {}
        for row in rows.iterator():
            row_data = self._as_dict(row)
            node_key = (
                row_data.get('tree_type') or 'spec',
                row_data.get('node_id') or row_data.get('talent_id') or row_data.get('spell_id'),
            )
            current = grouped_by_node.get(node_key)
            if not current:
                grouped_by_node[node_key] = row_data
                continue

            current_options = current.setdefault('choice_options', [])
            current['is_choice_node'] = True
            option_payload = self._build_choice_option(row_data)
            if option_payload and option_payload not in current_options:
                current_options.append(option_payload)

        nodes = []
        for node in grouped_by_node.values():
            if node.get('choice_options'):
                base_option = self._build_choice_option(node)
                if base_option and base_option not in node['choice_options']:
                    node['choice_options'].insert(0, base_option)
            nodes.append(node)

        self._spec_cache[cache_key] = [dict(node) for node in nodes]
        return [dict(node) for node in nodes]

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
            'description': getattr(row, 'description', '') or '',
            'description_zh': getattr(row, 'description_zh', '') or '',
            'db2_subtree_id': getattr(row, 'db2_subtree_id', 0) or 0,
            'selected': False,
            'points': 0,
        }

    @staticmethod
    def _build_choice_option(node):
        spell_id = node.get('display_spell_id') or node.get('spell_id')
        option_key = node.get('talent_id') or spell_id
        if not option_key:
            return {}
        return {
            'option_key': option_key,
            'talent_id': node.get('talent_id'),
            'spell_id': spell_id,
            'display_spell_id': node.get('display_spell_id'),
            'name': node.get('name') or '',
            'icon': node.get('icon') or '',
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
    def _find_across_trees(tree_indexes_list, field_name, value):
        try:
            normalized = int(value)
        except Exception:
            return None
        key = (field_name, normalized)
        for tree_indexes in tree_indexes_list:
            row = tree_indexes.get(key)
            if row:
                return row
        return None

    @staticmethod
    def _should_override(node, key):
        current = node.get(key)
        if current in (None, '', []):
            return True
        if key == 'name' and isinstance(current, str):
            return current == '未命名天赋' or current.startswith('技能ID ')
        return False
