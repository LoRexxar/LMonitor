# -*- coding: utf-8 -*-
"""
从 DB2 dump 补全所有缺失的天赋节点元数据。

sync_talent_metadata 只从玩家数据同步，如果某个天赋没有被玩家点过，
就不会创建元数据节点。此命令直接从 DB2 TraitNode 读取所有节点，
为每个职业的所有专精创建缺失的元数据。
"""
import csv
import os
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.utils import timezone

from botend.constants.wow import CLASS_SPEC_MAP
from botend.models import WowTalentNodeMetadata


class Command(BaseCommand):
    help = '从 DB2 dump 补全所有缺失的天赋节点元数据'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='.cache/wago_db2_dumps/latest',
                            help='DB2 dump 目录')
        parser.add_argument('--class-name', default='', help='仅处理指定职业')
        parser.add_argument('--dry-run', action='store_true', help='只输出统计，不写入')

    def handle(self, *args, **options):
        dump_dir = options['dump_dir']
        class_name = options['class_name']
        dry_run = options['dry_run']

        if not os.path.isdir(dump_dir):
            self.stderr.write(f'DB2 dump 目录不存在: {dump_dir}')
            return

        self.stdout.write(f'加载 DB2 数据: {dump_dir}')

        # 1. 加载 TraitNode
        db2_nodes = {}
        with open(os.path.join(dump_dir, 'TraitNode.csv')) as f:
            for row in csv.DictReader(f):
                nid = int(row['ID'])
                db2_nodes[nid] = {
                    'tree_id': int(row['TraitTreeID']),
                    'subtree_id': int(row['TraitSubTreeID']) if row['TraitSubTreeID'] else 0,
                    'type': int(row['Type']) if row['Type'] else 0,
                    'pos_x': int(row['PosX']) if row['PosX'] else 0,
                    'pos_y': int(row['PosY']) if row['PosY'] else 0,
                    'flags': int(row['Flags']) if row['Flags'] else 0,
                }
        self.stdout.write(f'  TraitNode: {len(db2_nodes)}')

        # 2. TraitNodeXTraitNodeEntry → entry_id → node_id, node_id → [entry_ids]
        entry_to_node = {}
        node_to_entries = defaultdict(list)
        with open(os.path.join(dump_dir, 'TraitNodeXTraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                eid = int(row['TraitNodeEntryID'])
                nid = int(row['TraitNodeID'])
                entry_to_node[eid] = nid
                node_to_entries[nid].append(eid)
        self.stdout.write(f'  TraitNodeXTraitNodeEntry: {len(entry_to_node)}')

        # 3. TraitNodeEntry
        entries_db2 = {}
        with open(os.path.join(dump_dir, 'TraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                entries_db2[int(row['ID'])] = {
                    'def_id': int(row['TraitDefinitionID']),
                    'max_ranks': int(row.get('MaxRanks', 1) or 1),
                }
        self.stdout.write(f'  TraitNodeEntry: {len(entries_db2)}')

        # 4. TraitDefinition
        defs = {}
        with open(os.path.join(dump_dir, 'TraitDefinition.csv')) as f:
            for row in csv.DictReader(f):
                defs[int(row['ID'])] = {
                    'spell_id': int(row.get('SpellID', 0) or 0),
                    'visible_spell_id': int(row.get('VisibleSpellID', 0) or 0),
                    'override_spell_id': int(row.get('OverridesSpellID', 0) or 0),
                    'name': row.get('OverrideName_lang', '') or '',
                    'icon': int(row.get('OverrideIcon', 0) or 0),
                }
        self.stdout.write(f'  TraitDefinition: {len(defs)}')

        # 5. SpellName (enUS + zhCN)
        spell_names = {}
        for locale in ['enUS', 'zhCN']:
            path = os.path.join(dump_dir, f'SpellName_{locale}.csv')
            if os.path.exists(path):
                with open(path) as f:
                    for row in csv.DictReader(f):
                        sid = int(row['ID'])
                        name = row.get('Name_lang', '') or ''
                        if sid not in spell_names:
                            spell_names[sid] = {}
                        spell_names[sid][locale] = name
        self.stdout.write(f'  SpellName: {len(spell_names)}')

        # 6. TraitEdge → parent 关系
        edges_by_right = defaultdict(list)
        with open(os.path.join(dump_dir, 'TraitEdge.csv')) as f:
            for row in csv.DictReader(f):
                left = int(row['LeftTraitNodeID'])
                right = int(row['RightTraitNodeID'])
                edges_by_right[right].append(left)
        self.stdout.write(f'  TraitEdge: {sum(len(v) for v in edges_by_right.values())} entries')

        # 7. TraitSubTree
        subtrees = {}
        subtree_path = os.path.join(dump_dir, 'TraitSubTree.csv')
        if os.path.exists(subtree_path):
            with open(subtree_path) as f:
                for row in csv.DictReader(f):
                    subtrees[int(row['ID'])] = {
                        'name': row.get('Name_lang', '') or '',
                        'tree_id': int(row.get('TraitTreeID', 0) or 0),
                    }
        self.stdout.write(f'  TraitSubTree: {len(subtrees)}')

        # 8. 构建 tree_id → class_name 映射
        # 优先从现有数据库数据推断，否则用硬编码
        tree_to_class = {
            750: 'DeathKnight',
            854: 'DemonHunter',
            793: 'Druid',
            872: 'Evoker',
            774: 'Hunter',
            658: 'Mage',
            1000: 'Monk',
            790: 'Paladin',
            795: 'Priest',
            852: 'Rogue',
            786: 'Shaman',
            720: 'Warlock',
            850: 'Warrior',
        }

        # 预取现有数据库节点 {(class_name, spec_name, tree_type, node_id, spell_id): id}
        existing = set()
        qs = WowTalentNodeMetadata.objects.values_list(
            'class_name', 'spec_name', 'tree_type', 'node_id', 'spell_id'
        )
        if class_name:
            qs = qs.filter(class_name=class_name)
        for row in qs.iterator():
            existing.add(row)

        self.stdout.write(f'\n现有数据库节点: {len(existing)}')

        # 处理每个职业
        targets = list(tree_to_class.items())
        if class_name:
            targets = [(tid, cn) for tid, cn in targets if cn == class_name]

        total_created = 0
        total_skipped = 0

        for tree_id, cls_name in targets:
            specs = CLASS_SPEC_MAP.get(cls_name, [])
            if not specs:
                continue

            # 该 tree 的所有 TraitNode
            tree_trait_nodes = {nid: n for nid, n in db2_nodes.items() if n['tree_id'] == tree_id}
            self.stdout.write(f'\n{cls_name} (TreeID={tree_id}): {len(tree_trait_nodes)} 个 TraitNode, {len(specs)} 个专精')

            created = 0
            skipped = 0

            for spec_name in specs:
                to_create = []

                for trait_node_id, node_info in tree_trait_nodes.items():
                    # 分类
                    subtree_id = node_info['subtree_id']
                    pos_x = node_info['pos_x']
                    if subtree_id > 0:
                        tree_type = 'hero'
                    elif pos_x < 7000:
                        tree_type = 'class'
                    else:
                        tree_type = 'spec'

                    # 获取所有 entry_ids for this trait_node
                    entry_ids = node_to_entries.get(trait_node_id, [])
                    if not entry_ids:
                        continue

                    # 解析 spell_id 和 name (先解析，再去重)
                    for entry_id in sorted(entry_ids):
                        spell_id = None
                        name = ''
                        name_zh = ''
                        icon = ''
                        max_points = 1
                        display_spell_id = None

                        if entry_id in entries_db2:
                            def_id = entries_db2[entry_id]['def_id']
                            max_points = entries_db2[entry_id]['max_ranks']
                            # def_id=0 或 type=1+flags=264 的节点是英雄天赋锚点
                            if def_id == 0 or (node_info['type'] == 1 and node_info['flags'] == 264):
                                tree_type = 'hero_anchor'
                            if def_id in defs:
                                d = defs[def_id]
                                display_spell_id = d['visible_spell_id'] or d['spell_id'] or d['override_spell_id'] or None
                                if display_spell_id and display_spell_id in spell_names:
                                    name_zh = spell_names[display_spell_id].get('zhCN', '')
                                    name = spell_names[display_spell_id].get('enUS', '')
                                if d['name'] and not name:
                                    name = d['name']
                                if d['icon']:
                                    icon = str(d['icon'])

                        resolved_spell_id = display_spell_id or entry_id

                        # 检查是否已存在 (用实际 spell_id)
                        check_key = (cls_name, spec_name, tree_type, entry_id, resolved_spell_id)
                        if check_key in existing:
                            skipped += 1
                            continue

                        # 解析 parents
                        parent_entry_ids = []
                        parent_node_ids = edges_by_right.get(trait_node_id, [])
                        for parent_nid in parent_node_ids:
                            parent_entries = node_to_entries.get(parent_nid, [])
                            if parent_entries:
                                parent_entry_ids.append(sorted(parent_entries)[0])

                        # hero subtree name
                        if subtree_id > 0 and subtree_id in subtrees:
                            subtree_name = subtrees[subtree_id]['name']
                            if subtree_name and not name:
                                name = subtree_name
                                name_zh = subtree_name

                        now = timezone.now()
                        obj = WowTalentNodeMetadata(
                            class_name=cls_name,
                            spec_name=spec_name,
                            tree_type=tree_type,
                            node_id=entry_id,
                            spell_id=resolved_spell_id,
                            talent_id=trait_node_id,
                            name=name[:255] if name else '',
                            name_zh=name_zh[:255] if name_zh else '',
                            icon=icon[:255] if icon else '',
                            row=node_info['pos_y'] if node_info['pos_y'] else None,
                            column=node_info['pos_x'] if node_info['pos_x'] else None,
                            max_points=max_points,
                            parents_json=sorted(parent_entry_ids),
                            source='db2_backfill',
                            db2_subtree_id=subtree_id,
                            display_spell_id=display_spell_id,
                            last_updated=now,
                        )
                        to_create.append(obj)
                        existing.add(check_key)

                if to_create and not dry_run:
                    WowTalentNodeMetadata.objects.bulk_create(
                        to_create, batch_size=500, ignore_conflicts=True
                    )
                created += len(to_create)

            total_created += created
            total_skipped += skipped

            self.stdout.write(
                f'  {cls_name}: 创建 {created} 个新节点, 跳过 {skipped} 个已存在'
            )

        self.stdout.write(self.style.SUCCESS(
            f'\n完成: 创建 {total_created} 个新节点, 跳过 {total_skipped} 个已存在'
        ))
