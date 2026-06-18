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

SPEC_REGION_X = 9900


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
            if info['subtree_id'] == 0 and info['pos_x'] > SPEC_REGION_X:
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

            # Major spec components first. Type=1 isolated entry nodes are appended
            # after them and can still be assigned by real sample usage.
            comps.sort(key=lambda comp: (
                0 if len(comp) > 1 else 1,
                min(self.db2_nodes[n]['pos_x'] for n in comp),
                min(self.db2_nodes[n]['pos_y'] for n in comp),
                -len(comp),
            ))
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
            max_count = counter.most_common(1)[0][1]
            return {
                comp_id for comp_id, count in counter.items()
                if count >= max(1, max_count * 0.5)
            }

        specs = CLASS_SPEC_MAP.get(class_name, [])
        try:
            spec_index = specs.index(spec_name)
        except ValueError:
            return set()
        major_components = [
            idx for idx, comp in enumerate(self.tree_components.get(tree_id, []), start=1)
            if len(comp) > 1
        ]
        if spec_index < len(major_components):
            return {major_components[spec_index]}
        return set()

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
