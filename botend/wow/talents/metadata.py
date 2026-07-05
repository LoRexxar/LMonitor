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
from typing import Any

from botend.models import WowSpellSnapshot, WowTalentNodeMetadata
from botend.wow.spell_text import get_spell_text_resolver
from botend.wow.talents.versioning import TalentVersionResolver

STRUCTURAL_FIELDS = {'tree_type', 'row', 'column', 'max_points', 'parents', 'db2_subtree_id'}
AUTHORITATIVE_TALENT_SOURCES = {'db2_backfill', 'db2_repair', 'db2'}


def normalize_talent_option_spell_id(node):
    """返回用于判断二选一候选项的真实展示 spell id。"""
    value = node.get('display_spell_id') or node.get('spell_id')
    try:
        parsed = int(value)
    except Exception:
        return None
    return parsed if parsed > 0 else None


def is_authoritative_talent_source(node):
    return (node.get('source') or '') in AUTHORITATIVE_TALENT_SOURCES


def dedupe_talent_option_nodes(nodes):
    """去掉旧采集源和 DB2 回填源混在一起时的等价重复节点。"""
    grouped = {}
    for node in nodes:
        option_spell_id = normalize_talent_option_spell_id(node)
        if option_spell_id is None:
            key = ('node', node.get('node_id') or node.get('talent_id') or node.get('spell_id'))
        else:
            key = ('spell', option_spell_id)
        current = grouped.get(key)
        if current is None:
            grouped[key] = node
            continue
        if _talent_source_priority(node) > _talent_source_priority(current):
            grouped[key] = node
    return list(grouped.values())


def _talent_source_priority(node):
    score = 0
    if is_authoritative_talent_source(node):
        score += 10
    if node.get('icon'):
        score += 2
    if node.get('name'):
        score += 1
    if node.get('description') or node.get('description_zh'):
        score += 1
    return score


@dataclass
class TalentMetadataProvider:
    locale: str = 'zhCN'
    talent_version: object = None
    version_key: str = ''
    usage: str = TalentVersionResolver.USAGE_SIMULATOR
    _spec_cache: dict = field(default_factory=dict)
    _snapshot_cache: dict = field(default_factory=dict)
    _spell_text_resolver: Any = None

    @property
    def resolved_version(self):
        if self.talent_version is not None:
            return self.talent_version
        self.talent_version = TalentVersionResolver.resolve(
            version_key=self.version_key,
            usage=self.usage,
        )
        return self.talent_version

    @property
    def version_cache_key(self):
        version = self.resolved_version
        if not version:
            return 'missing'
        return getattr(version, 'key', None) or getattr(version, 'id', None) or 'missing'

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
        version = self.resolved_version
        if not version:
            return []
        cache_key = (self.version_cache_key, class_name or '', spec_name or '', 'full_nodes')
        if cache_key in self._spec_cache:
            return [dict(node) for node in self._spec_cache[cache_key]]

        rows = WowTalentNodeMetadata.objects.filter(
            class_name=class_name or '',
            spec_name=spec_name or '',
            source__in=AUTHORITATIVE_TALENT_SOURCES,
            talent_version=version,
        ).exclude(tree_type='hero_anchor').order_by('tree_type', 'row', 'column', 'node_id', 'spell_id', 'talent_id')

        grouped_by_node = {}
        seen_spell_ids = set()
        for row in rows.iterator():
            row_data = self._as_dict(row)
            # Deduplicate by spell_id first - some nodes have different talent_id
            # but the same spell_id (e.g., multiple Battle Stance entries)
            spell_id = row_data.get('spell_id')
            if spell_id and spell_id in seen_spell_ids:
                continue
            if spell_id:
                seen_spell_ids.add(spell_id)
            # Use talent_id (DB2 TraitNode ID) as grouping key.
            # Choice nodes have multiple entries with different node_id/spell_id
            # but the same talent_id. Using node_id would split them into separate groups.
            node_key = (
                row_data.get('tree_type') or 'spec',
                row_data.get('talent_id') or row_data.get('node_id') or row_data.get('spell_id'),
            )
            current = grouped_by_node.get(node_key)
            if not current:
                grouped_by_node[node_key] = row_data
                continue

            current_options = current.setdefault('choice_options', [])
            current['is_choice_node'] = True
            if row_data not in current_options:
                current_options.append(row_data)

        nodes = []
        for node in grouped_by_node.values():
            choice_nodes = dedupe_talent_option_nodes([node] + [
                option for option in node.get('choice_options', []) if option
            ])
            node = dict(choice_nodes[0]) if choice_nodes else node
            if len(choice_nodes) > 1:
                node['choice_options'] = [
                    self._build_choice_option(option) for option in choice_nodes
                ]
                node['is_choice_node'] = True
            else:
                node['choice_options'] = []
                node['is_choice_node'] = False
            nodes.append(node)

        self._spec_cache[cache_key] = [dict(node) for node in nodes]
        return [dict(node) for node in nodes]

    def get_decoder_node_list(self, class_name):
        """返回 build code 解码所需的完整节点列表。

        与 get_full_tree_nodes 不同，此方法：
        - 包含所有 spec 的节点（build code 编码整棵职业树）
        - 包含 hero_anchor 节点
        - 按 talent_id（DB2 TraitNode ID）分组，每个 talent_id 一条
        - 按 talent_id 排序（Blizzard build code 的 canonical ordering）
        - 不做 spell_id 去重（每个 TraitNode 独立占一个 decode 位）
        """
        version = self.resolved_version
        if not version:
            return []
        cache_key = (self.version_cache_key, class_name or '', '', 'decoder_nodes')
        if cache_key in self._spec_cache:
            return [dict(node) for node in self._spec_cache[cache_key]]

        rows = WowTalentNodeMetadata.objects.filter(
            class_name=class_name or '',
            source__in=AUTHORITATIVE_TALENT_SOURCES,
            talent_version=version,
        ).order_by('talent_id', 'node_id', 'spell_id')

        grouped = {}
        for row in rows.iterator():
            row_data = self._as_dict(row)
            tid = row_data.get('talent_id')
            if not tid:
                continue
            if tid not in grouped:
                grouped[tid] = row_data
            else:
                current = grouped[tid]
                if row_data.get('tree_type') == 'hero_anchor':
                    current['tree_type'] = 'hero_anchor'
                opts = current.setdefault('choice_options', [])
                current['is_choice_node'] = True
                if row_data not in opts:
                    opts.append(row_data)

        nodes = []
        for tid in sorted(grouped.keys()):
            node = grouped[tid]
            opts = node.get('choice_options', [])
            if opts:
                deduped = dedupe_talent_option_nodes([node] + opts)
                node = dict(deduped[0])
                if len(deduped) > 1:
                    node['choice_options'] = [
                        self._build_choice_option(o) for o in deduped
                    ]
                    node['is_choice_node'] = True
                else:
                    node['choice_options'] = []
                    node['is_choice_node'] = False
            else:
                node['choice_options'] = []
                node['is_choice_node'] = False
            nodes.append(node)

        self._spec_cache[cache_key] = nodes
        return [dict(n) for n in nodes]

    def _get_spec_indexes(self, class_name, spec_name):
        version = self.resolved_version
        if not version:
            return {}
        cache_key = (self.version_cache_key, class_name or '', spec_name or '')
        if cache_key in self._spec_cache:
            return self._spec_cache[cache_key]

        indexes = {}
        rows = WowTalentNodeMetadata.objects.filter(
            class_name=cache_key[1],
            spec_name=cache_key[2],
            talent_version=version,
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

    def _spell_resolver(self):
        if self._spell_text_resolver is None:
            self._spell_text_resolver = get_spell_text_resolver(self.locale)
        return self._spell_text_resolver

    def _resolve_text(self, resolver, text, spell_id):
        text = text or ''
        if '$' not in text:
            return ' '.join(text.split()).strip()
        return resolver.resolve(text, spell_id)

    def _as_dict(self, row):
        spell_id = row.display_spell_id or row.spell_id
        desc = getattr(row, 'description', '') or ''
        desc_zh = getattr(row, 'description_zh', '') or ''
        resolver = self._spell_resolver()
        return {
            'talent_version_id': getattr(row, 'talent_version_id', None),
            'talent_version_key': self.version_cache_key,
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
            'description': self._resolve_text(resolver, desc, spell_id),
            'description_zh': self._resolve_text(resolver, desc_zh, spell_id),
            'db2_subtree_id': getattr(row, 'db2_subtree_id', 0) or 0,
            'db2_tree_id': getattr(row, 'db2_tree_id', None),
            'db2_component_id': getattr(row, 'db2_component_id', 0) or 0,
            'source': getattr(row, 'source', '') or '',
            'selected': False,
            'points': 0,
        }

    def _build_choice_option(self, node):
        spell_id = node.get('display_spell_id') or node.get('spell_id')
        option_key = node.get('talent_id') or spell_id
        if not option_key:
            return {}
        resolver = get_spell_text_resolver(self.locale)
        return {
            'option_key': option_key,
            'node_id': node.get('node_id'),
            'talent_id': node.get('talent_id'),
            'spell_id': spell_id,
            'display_spell_id': node.get('display_spell_id'),
            'max_points': node.get('max_points') or 1,
            'name': node.get('name') or '',
            'icon': node.get('icon') or '',
            'description': resolver.resolve(node.get('description') or '', spell_id),
            'description_zh': resolver.resolve(node.get('description_zh') or '', spell_id),
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
