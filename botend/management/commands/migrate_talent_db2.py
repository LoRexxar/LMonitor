"""
天赋树 DB2 元数据迁移脚本

功能：
1. ALTER TABLE 添加新字段（db2_subtree_id, db2_node_type, db2_tree_id, description, description_zh）
2. 通过 DB2 数据重新分类 tree_type（class/hero/spec）
3. 更新 column/row 为 DB2 原始坐标
4. 创建 wow_talent_hero_subtree 表
5. 写入 hero 子树名称
"""
import csv
import json
import os
from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import connection


class Command(BaseCommand):
    help = '迁移天赋树 DB2 元数据：重新分类 + 新增字段 + hero 子树表'

    def add_arguments(self, parser):
        parser.add_argument('--dump-dir', default='.cache/wago_db2_dumps/latest',
                            help='DB2 dump 目录')
        parser.add_argument('--dry-run', action='store_true',
                            help='只输出统计，不实际写入')

    def handle(self, *args, **options):
        dump_dir = options['dump_dir']
        dry_run = options['dry_run']

        # ===== 1. 加载 DB2 数据 =====
        self.stdout.write('加载 DB2 数据...')

        # TraitNode
        db2_nodes = {}
        with open(os.path.join(dump_dir, 'TraitNode.csv')) as f:
            for row in csv.DictReader(f):
                nid = int(row['ID'])
                db2_nodes[nid] = {
                    'tree_id': int(row['TraitTreeID']),
                    'subtree_id': int(row['TraitSubTreeID']) if row['TraitSubTreeID'] else 0,
                    'type': int(row['Type']),
                    'pos_x': int(row['PosX']) if row['PosX'] else 0,
                    'pos_y': int(row['PosY']) if row['PosY'] else 0,
                    'flags': int(row['Flags']) if row['Flags'] else 0,
                }

        # TraitNodeXTraitNodeEntry → entry_id → node_id
        entry_to_node = {}
        with open(os.path.join(dump_dir, 'TraitNodeXTraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                eid = int(row['TraitNodeEntryID'])
                nid = int(row['TraitNodeID'])
                if nid > 0:
                    entry_to_node[eid] = nid

        # TraitDefinition
        defs = {}
        with open(os.path.join(dump_dir, 'TraitDefinition.csv')) as f:
            for row in csv.DictReader(f):
                did = int(row['ID'])
                defs[did] = {
                    'spell_id': int(row.get('SpellID', 0) or 0),
                    'name': row.get('OverrideName_lang', '') or '',
                    'desc': row.get('OverrideDescription_lang', '') or '',
                    'icon': int(row.get('OverrideIcon', 0) or 0),
                }

        # TraitNodeEntry
        entries_db2 = {}
        with open(os.path.join(dump_dir, 'TraitNodeEntry.csv')) as f:
            for row in csv.DictReader(f):
                entries_db2[int(row['ID'])] = {
                    'def_id': int(row['TraitDefinitionID']),
                    'max_ranks': int(row.get('MaxRanks', 1) or 1),
                }

        # Hero subtree names
        hero_names_path = os.path.join(dump_dir, 'hero_subtree_names.json')
        hero_names = {}
        if os.path.exists(hero_names_path):
            with open(hero_names_path) as f:
                hero_names = json.load(f)

        # TraitEdge
        edges = []
        with open(os.path.join(dump_dir, 'TraitEdge.csv')) as f:
            for row in csv.DictReader(f):
                edges.append({
                    'left': int(row['LeftTraitNodeID']),
                    'right': int(row['RightTraitNodeID']),
                })

        self.stdout.write(f'  TraitNode: {len(db2_nodes)}')
        self.stdout.write(f'  TraitNodeEntry: {len(entries_db2)}')
        self.stdout.write(f'  TraitDefinition: {len(defs)}')
        self.stdout.write(f'  TraitEdge: {len(edges)}')
        self.stdout.write(f'  Hero subtrees: {len(hero_names)}')

        # ===== 2. 分析数据库节点分类 =====
        self.stdout.write('\\n分析数据库节点分类...')

        cursor = connection.cursor()
        cursor.execute(
            'SELECT id, node_id, tree_type, class_name, spec_name, spell_id '
            'FROM wow_talent_node_metadata WHERE node_id IS NOT NULL'
        )
        db_rows = cursor.fetchall()

        updates = []  # (db_id, new_tree_type, pos_x, pos_y, subtree_id, node_type, tree_id, desc)
        stats = {'class': 0, 'hero': 0, 'spec': 0, 'unchanged': 0, 'no_mapping': 0}

        for db_id, node_id, old_type, class_name, spec_name, spell_id in db_rows:
            trait_node_id = entry_to_node.get(node_id)
            if not trait_node_id or trait_node_id not in db2_nodes:
                stats['no_mapping'] += 1
                continue

            d = db2_nodes[trait_node_id]

            if d['pos_y'] == 0:
                stats['no_mapping'] += 1
                continue

            # 分类
            if d['subtree_id'] > 0:
                new_type = 'hero'
            elif d['pos_x'] < 7000:
                new_type = 'class'
            else:
                new_type = 'spec'

            # 获取描述
            desc = ''
            for eid in [node_id]:  # node_id = entry_id
                if eid in entries_db2:
                    did = entries_db2[eid]['def_id']
                    if did in defs and defs[did]['desc']:
                        desc = defs[did]['desc']
                        break

            if new_type != old_type:
                stats[new_type] += 1
            else:
                stats['unchanged'] += 1

            updates.append({
                'db_id': db_id,
                'new_type': new_type,
                'pos_x': d['pos_x'],
                'pos_y': d['pos_y'],
                'subtree_id': d['subtree_id'],
                'node_type': d['type'],
                'tree_id': d['tree_id'],
                'flags': d['flags'],
                'desc': desc,
            })

        self.stdout.write(f'\\n分类结果:')
        for k, v in stats.items():
            self.stdout.write(f'  {k}: {v}')
        self.stdout.write(f'  总更新: {len(updates)}')

        if dry_run:
            self.stdout.write(self.style.WARNING('\\n--dry-run 模式，不写入数据库'))
            return

        # ===== 3. ALTER TABLE 添加新字段 =====
        self.stdout.write('\\n添加新字段...')

        new_columns = [
            ('db2_subtree_id', 'INT DEFAULT 0'),
            ('db2_node_type', 'INT NULL'),
            ('db2_tree_id', 'INT NULL'),
            ('db2_flags', 'INT DEFAULT 0'),
            ('description', 'TEXT NULL'),
            ('description_zh', 'TEXT NULL'),
        ]

        for col_name, col_def in new_columns:
            try:
                cursor.execute(f'ALTER TABLE wow_talent_node_metadata ADD COLUMN {col_name} {col_def}')
                self.stdout.write(f'  + {col_name}')
            except Exception as e:
                if 'Duplicate column' in str(e):
                    self.stdout.write(f'  = {col_name} (已存在)')
                else:
                    raise

        # ===== 4. 处理冲突 + 批量更新 =====
        self.stdout.write('\\n处理冲突行...')

        # 找出需要改 tree_type 且会与已有行冲突的记录
        # 先删掉这些 spec 行（hero 行已经存在，是冗余的）
        conflict_ids = []
        for u in updates:
            if u['new_type'] in ('hero', 'class'):
                # 检查是否存在同 (class_name, spec_name, node_id, spell_id) 但 tree_type 已经是 new_type 的行
                pass  # 下面用 SQL 统一处理

        cursor.execute('''
            SELECT a.id
            FROM wow_talent_node_metadata a
            JOIN wow_talent_node_metadata b
              ON a.class_name = b.class_name
              AND a.spec_name = b.spec_name
              AND a.node_id = b.node_id
              AND a.spell_id = b.spell_id
              AND a.id != b.id
            WHERE a.tree_type = 'spec' AND b.tree_type = 'hero'
        ''')
        conflict_ids = [row[0] for row in cursor.fetchall()]

        if conflict_ids:
            ids_str = ','.join(str(i) for i in conflict_ids)
            cursor.execute(f'DELETE FROM wow_talent_node_metadata WHERE id IN ({ids_str})')
            self.stdout.write(f'  删除 {len(conflict_ids)} 条冲突的 spec 行（已有对应 hero 行）')

        # 重新构建 updates 列表（排除已删除的行）
        conflict_set = set(conflict_ids)
        updates = [u for u in updates if u['db_id'] not in conflict_set]
        self.stdout.write(f'  剩余待更新: {len(updates)} 条')

        self.stdout.write('\\n批量更新节点数据...')

        batch_size = 500
        total = len(updates)
        updated = 0

        for i in range(0, total, batch_size):
            batch = updates[i:i + batch_size]

            ids_str = ','.join(str(u['db_id']) for u in batch)
            type_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN '{u['new_type']}'" for u in batch
            )
            sub_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN {u['subtree_id']}" for u in batch
            )
            ntype_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN {u['node_type']}" for u in batch
            )
            tree_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN {u['tree_id']}" for u in batch
            )
            flags_cases = ' '.join(
                f"WHEN id={u['db_id']} THEN {u['flags']}" for u in batch
            )
            cursor.execute(f'''
                UPDATE wow_talent_node_metadata
                SET tree_type = CASE {type_cases} END,
                    db2_subtree_id = CASE {sub_cases} END,
                    db2_node_type = CASE {ntype_cases} END,
                    db2_tree_id = CASE {tree_cases} END,
                    db2_flags = CASE {flags_cases} END
                WHERE id IN ({ids_str})
            ''')
            updated += len(batch)

            if (i // batch_size) % 10 == 0:
                self.stdout.write(f'  进度: {updated}/{total}')

        self.stdout.write(f'  已更新 {updated} 条记录')

        # ===== 5. 创建 hero_subtree 表 =====
        self.stdout.write('\\n创建 wow_talent_hero_subtree 表...')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wow_talent_hero_subtree (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                subtree_id INT NOT NULL,
                tree_id INT NOT NULL,
                class_name VARCHAR(30) NOT NULL DEFAULT '',
                spec_name VARCHAR(30) NOT NULL DEFAULT '',
                hero_name VARCHAR(100) NOT NULL DEFAULT '',
                hero_name_zh VARCHAR(100) NULL,
                description TEXT NULL,
                node_count INT DEFAULT 0,
                last_updated DATETIME(6) DEFAULT CURRENT_TIMESTAMP(6),
                UNIQUE KEY uk_subtree (tree_id, subtree_id)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
        ''')

        # 构建 tree_id → (class_name, spec_name) 映射
        tree_to_spec = {}
        cursor.execute(
            'SELECT DISTINCT db2_tree_id, class_name, spec_name '
            'FROM wow_talent_node_metadata WHERE db2_tree_id IS NOT NULL'
        )
        for row in cursor.fetchall():
            if row[0]:
                tree_to_spec[row[0]] = (row[1], row[2])

        # 统计每个 subtree 的节点数
        subtree_counts = defaultdict(int)
        for u in updates:
            if u['subtree_id'] > 0:
                subtree_counts[(u['tree_id'], u['subtree_id'])] += 1

        # 写入 hero_subtree 数据
        inserted = 0
        for sid_str, info in hero_names.items():
            sid = int(sid_str)
            tree_id = info['tree_id']
            name = info['name']
            desc = info['desc']
            class_name, spec_name = tree_to_spec.get(tree_id, ('', ''))
            node_count = subtree_counts.get((tree_id, sid), 0)

            try:
                cursor.execute('''
                    INSERT INTO wow_talent_hero_subtree
                        (subtree_id, tree_id, class_name, spec_name, hero_name, description, node_count)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        hero_name = VALUES(hero_name),
                        description = VALUES(description),
                        node_count = VALUES(node_count)
                ''', [sid, tree_id, class_name, spec_name, name, desc, node_count])
                inserted += 1
            except Exception as e:
                self.stdout.write(f'  跳过 SubTree {sid}: {e}')

        self.stdout.write(f'  写入 {inserted} 条 hero_subtree 记录')

        # ===== 6. 验证 =====
        self.stdout.write('\\n验证结果:')

        cursor.execute('''
            SELECT tree_type, COUNT(*) FROM wow_talent_node_metadata
            GROUP BY tree_type ORDER BY tree_type
        ''')
        for row in cursor.fetchall():
            self.stdout.write(f'  {row[0]}: {row[1]}')

        cursor.execute('SELECT COUNT(*) FROM wow_talent_hero_subtree')
        self.stdout.write(f'  hero_subtree 表: {cursor.fetchone()[0]} 条')

        self.stdout.write(self.style.SUCCESS('\\n完成！'))
