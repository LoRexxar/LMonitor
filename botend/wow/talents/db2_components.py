# -*- coding: utf-8 -*-
"""DB2 talent component ownership helpers.

The metadata table stores one row per class/spec/tree node. A single class TraitTree
contains multiple spec-side connected components; only one of those components
belongs to each specialization. These helpers infer and expose that ownership so
backfill/repair commands can keep the metadata clean at the database layer.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import PlayerSpecTopPlayer, SpecDungeonRanking, SpecRaidRanking

SPEC_REGION_X = 7000  # spec 区域起始坐标（与 backfill 保持一致）


class TalentDb2ComponentResolver:
    def __init__(self, dump_dir='.cache/wago_db2_dumps/latest'):
        self.dump_dir = dump_dir
        self.db2_nodes = {}
        self.entry_to_node = {}
        self.node_to_entries = defaultdict(list)
        self.edges = defaultdict(set)
        self.tree_components = {}
        self.trait_to_component = {}
        self._spec_component_cache = {}
        self.load()

    def load(self):
        self._load_trait_nodes()
        self._load_node_entries()
        self._load_edges()
        self._build_components()

    def _csv_path(self, name):
        return os.path.join(self.dump_dir, name)

    def _load_trait_nodes(self):
        with open(self._csv_path('TraitNode.csv')) as f:
            for row in csv.DictReader(f):
                nid = int(row['ID'])
                self.db2_nodes[nid] = {
                    'tree_id': int(row['TraitTreeID']),
                    'subtree_id': int(row['TraitSubTreeID']) if row['TraitSubTreeID'] else 0,
                    'type': int(row['Type']) if row['Type'] else 0,
                    'pos_x': int(row['PosX']) if row['PosX'] else 0,
                    'pos_y': int(row['PosY']) if row['PosY'] else 0,
                    'flags': int(row['Flags']) if row['Flags'] else 0,
                }

    def _load_node_entries(self):
        with open(self._csv_path('TraitNodeXTraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                entry_id = int(row['TraitNodeEntryID'])
                trait_node_id = int(row['TraitNodeID'])
                self.entry_to_node[entry_id] = trait_node_id
                self.node_to_entries[trait_node_id].append(entry_id)

    def _load_edges(self):
        with open(self._csv_path('TraitEdge.csv')) as f:
            for row in csv.DictReader(f):
                left = int(row['LeftTraitNodeID'])
                right = int(row['RightTraitNodeID'])
                self.edges[left].add(right)
                self.edges[right].add(left)

    def _build_components(self):
        candidates_by_tree = defaultdict(set)
        for trait_node_id, info in self.db2_nodes.items():
            # spec 区域：SubTreeID=0 且 pos_x >= SPEC_REGION_X。
            # Type=3 是顶部入口/锚点，不属于任何专精 spec component，不能写进 spec 归属。
            if info['subtree_id'] == 0 and info['type'] != 3 and info['pos_x'] >= SPEC_REGION_X:
                candidates_by_tree[info['tree_id']].add(trait_node_id)

        for tree_id, candidates in candidates_by_tree.items():
            seen = set()
            comps = []
            for nid in sorted(candidates):
                if nid in seen:
                    continue
                stack = [nid]
                seen.add(nid)
                comp = []
                while stack:
                    current = stack.pop()
                    comp.append(current)
                    for nb in self.edges.get(current, set()):
                        if nb in candidates and nb not in seen:
                            seen.add(nb)
                            stack.append(nb)
                comps.append(comp)

            # 分离主连通分量和 Type=1 孤立节点
            major_components = [comp for comp in comps if len(comp) > 1]
            isolated_entry_nodes = [comp for comp in comps if len(comp) == 1 and self.db2_nodes[comp[0]]['type'] == 1]
            other_isolated = [comp for comp in comps if len(comp) == 1 and self.db2_nodes[comp[0]]['type'] != 1]

            # 主连通分量按坐标排序
            major_components.sort(key=lambda comp: (
                min(self.db2_nodes[n]['pos_x'] for n in comp),
                min(self.db2_nodes[n]['pos_y'] for n in comp),
            ))
            # Type=1 入口孤立节点按坐标排序
            isolated_entry_nodes.sort(key=lambda comp: (
                self.db2_nodes[comp[0]]['pos_x'],
                self.db2_nodes[comp[0]]['pos_y'],
            ))
            # 其它孤立节点按坐标排序
            other_isolated.sort(key=lambda comp: (
                self.db2_nodes[comp[0]]['pos_x'],
                self.db2_nodes[comp[0]]['pos_y'],
            ))

            # 合并：主连通分量 + Type=1 入口节点 + 其它孤立节点
            comps = major_components + isolated_entry_nodes + other_isolated

            self.tree_components[tree_id] = comps
            for component_id, comp in enumerate(comps, start=1):
                for trait_node_id in comp:
                    self.trait_to_component[trait_node_id] = component_id

    def component_id_for_trait_node(self, trait_node_id):
        return self.trait_to_component.get(trait_node_id, 0)

    def trait_node_for_entry(self, entry_id):
        return self.entry_to_node.get(entry_id)

    def tree_id_for_trait_node(self, trait_node_id):
        info = self.db2_nodes.get(trait_node_id) or {}
        return info.get('tree_id')

    def get_spec_component_id(self, class_name, spec_name, tree_id):
        ids = self.get_spec_component_ids(class_name, spec_name, tree_id)
        if not ids:
            return 0
        major_components = [
            idx for idx, comp in enumerate(self.tree_components.get(tree_id, []), start=1)
            if len(comp) > 1 and idx in ids
        ]
        return major_components[0] if major_components else sorted(ids)[0]

    def get_spec_component_ids(self, class_name, spec_name, tree_id):
        cache_key = (class_name or '', spec_name or '', int(tree_id or 0), 'set')
        if cache_key not in self._spec_component_cache:
            self._spec_component_cache[cache_key] = self._infer_spec_component_ids(class_name, spec_name, tree_id)
        return set(self._spec_component_cache[cache_key])

    def _infer_spec_component_ids(self, class_name, spec_name, tree_id):
        # 先尝试从玩家样本统计
        counter = Counter()
        querysets = [
            PlayerSpecTopPlayer.objects.filter(class_name=class_name, spec_name=spec_name).values_list('talents_json', flat=True)[:100],
            SpecDungeonRanking.objects.filter(class_name=class_name, spec_name=spec_name).values_list('talents_json', flat=True)[:100],
            SpecRaidRanking.objects.filter(class_name=class_name, spec_name=spec_name).values_list('talents_json', flat=True)[:100],
        ]
        for qs in querysets:
            for payload in qs:
                sample_components = set()
                for raw_id in self._extract_node_ids(payload):
                    trait_node_id = self.entry_to_node.get(raw_id) or raw_id
                    info = self.db2_nodes.get(trait_node_id)
                    if not info or info['tree_id'] != tree_id:
                        continue
                    comp_id = self.component_id_for_trait_node(trait_node_id)
                    if comp_id:
                        sample_components.add(comp_id)
                for comp_id in sample_components:
                    counter[comp_id] += 1

        if counter:
            # 返回高频 components：主 component + 相关 Type=1 入口 component
            max_count = counter.most_common(1)[0][1]
            threshold = max(1, max_count * 0.5)
            result = {comp_id for comp_id, count in counter.items() if count >= threshold}

            # 分离主连通分量和 Type=1 孤立节点
            major_comps = []
            entry_comps = []
            for comp_id in result:
                comp = self.tree_components.get(tree_id, [])[comp_id - 1] if comp_id <= len(self.tree_components.get(tree_id, [])) else []
                if len(comp) > 1:
                    major_comps.append(comp_id)
                elif len(comp) == 1 and self.db2_nodes[comp[0]]['type'] == 1:
                    entry_comps.append(comp_id)

            # 主 component + 同专精的入口 component
            if major_comps:
                return set(major_comps + entry_comps)
            return result

        # Fallback：无样本时按 CLASS_SPEC_MAP 顺序分配
        specs = CLASS_SPEC_MAP.get(class_name, [])
        try:
            spec_index = specs.index(spec_name)
        except ValueError:
            return set()

        all_components = self.tree_components.get(tree_id, [])
        major_components = [
            idx for idx, comp in enumerate(all_components, start=1)
            if len(comp) > 1
        ]
        isolated_entry_components = [
            idx for idx, comp in enumerate(all_components, start=1)
            if len(comp) == 1 and self.db2_nodes[comp[0]]['type'] == 1
        ]

        # 分配主 component
        result = set()
        if spec_index < len(major_components):
            result.add(major_components[spec_index])

        # 分配对应的 Type=1 入口 component（一对一对应）
        if spec_index < len(isolated_entry_components):
            result.add(isolated_entry_components[spec_index])

        return result

    @staticmethod
    def _extract_node_ids(payload):
        if not payload:
            return []
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                return []
        if not isinstance(payload, list):
            return []
        result = []
        for node in payload:
            if not isinstance(node, dict):
                continue
            for key in ('node_id', 'talent_id'):
                value = node.get(key)
                if not value:
                    continue
                try:
                    result.append(int(value))
                    break
                except (TypeError, ValueError):
                    continue
        return result
